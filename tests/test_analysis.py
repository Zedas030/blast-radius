"""Graph logic on small hand-made graphs."""
from blast_radius.analysis import CauseFinder, impact_report, path_to_root, resolve_question, search, simulate, spof
from blast_radius.model import Graph


def chain() -> Graph:
    g = Graph()
    for nid, t in [("hw:plug", "hardware"), ("hw:pi", "hardware"), ("host:pi", "host"), ("svc:docker", "service"),
                   ("svc:ha", "service"), ("int:zha", "integration"), ("hw:coord", "hardware"),
                   ("dev:s1", "device"), ("dev:s2", "device"), ("entity:a", "entity"), ("entity:b", "entity"),
                   ("entity:c", "entity"), ("fn:presence", "function"), ("auto:x", "automation"),
                   ("fn:auto", "function")]:
        g.add_node(nid, t, nid.split(":")[1])
    g.add_edge("hw:pi", "hw:plug", "powered_by")
    g.add_edge("host:pi", "hw:pi", "runs_on")
    g.add_edge("svc:docker", "host:pi", "runs_on")
    g.add_edge("svc:ha", "svc:docker", "runs_on")
    g.add_edge("int:zha", "svc:ha", "runs_on")
    g.add_edge("int:zha", "hw:coord")
    for d, e in [("dev:s1", "entity:a"), ("dev:s2", "entity:b")]:
        g.add_edge(d, "int:zha")
        g.add_edge(e, d)
    g.add_edge("entity:c", "svc:ha", "runs_on")
    g.add_edge("fn:presence", "entity:a", soft=True)
    g.add_edge("fn:presence", "entity:b", soft=True)
    g.add_edge("auto:x", "svc:ha", "runs_on")
    g.add_edge("auto:x", "entity:a", group="trigger")
    g.add_edge("auto:x", "entity:c", soft=True)
    g.add_edge("fn:auto", "auto:x")
    return g


def test_propagation_along_hard_edges() -> None:
    g = chain()
    sim = simulate(g, ["hw:plug"])
    assert {"hw:pi", "host:pi", "svc:docker", "svc:ha", "int:zha", "entity:a", "entity:b", "entity:c",
            "fn:presence", "auto:x", "fn:auto"} <= sim.failed
    assert "hw:coord" not in sim.failed  # dependencies of failed nodes stay up


def test_soft_dependencies_degrade_until_all_fail() -> None:
    g = chain()
    g.add_node("dev:s3", "device", "s3")
    g.add_node("entity:d", "entity", "d")
    g.add_edge("entity:d", "dev:s3")
    g.add_edge("fn:presence", "entity:d", soft=True)
    sim = simulate(g, ["hw:coord"])
    assert "fn:presence" in sim.degraded  # entity:d still there
    sim = simulate(g, ["hw:coord", "dev:s3"])
    assert "fn:presence" in sim.failed


def test_trigger_group_fails_when_all_triggers_gone_and_degradation_propagates() -> None:
    g = chain()
    sim = simulate(g, ["hw:coord"])
    assert "auto:x" in sim.failed  # its only trigger entity:a is gone
    assert "fn:auto" in sim.failed
    g2 = chain()
    g2.add_node("entity:t2", "entity", "t2")
    g2.add_edge("entity:t2", "svc:ha", "runs_on")
    g2.add_edge("auto:x", "entity:t2", group="trigger")
    sim = simulate(g2, ["hw:coord"])
    # one of two triggers left: automation still works but has lost an alternative
    assert "auto:x" in sim.at_risk and "auto:x" not in sim.failed
    assert "fn:auto" not in sim.degraded  # lost redundancy does not propagate


def test_degraded_propagates_to_hard_dependents() -> None:
    g = chain()
    g.add_node("entity:e", "entity", "e")
    g.add_edge("entity:e", "svc:ha", "runs_on")
    g.add_edge("auto:x", "entity:e", soft=True)
    sim = simulate(g, ["entity:c"])
    assert "auto:x" in sim.degraded
    assert "fn:auto" in sim.degraded


def test_any_of_group() -> None:
    g = Graph()
    for nid in ("svc:dns1", "hw:router", "net:dns", "fn:web"):
        g.add_node(nid, "function" if nid.startswith("fn") else "service", nid)
    g.add_edge("net:dns", "svc:dns1", group="r")
    g.add_edge("net:dns", "hw:router", group="r")
    g.add_edge("fn:web", "net:dns")
    assert simulate(g, ["svc:dns1"]).state("net:dns") == "at_risk"
    assert simulate(g, ["svc:dns1"]).state("fn:web") == "ok"
    sim = simulate(g, ["svc:dns1", "hw:router"])
    assert sim.state("net:dns") == "failed" and sim.state("fn:web") == "failed"


def test_cycles_terminate() -> None:
    g = Graph()
    for nid in ("a", "b", "c"):
        g.add_node(nid, "service", nid)
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    g.add_edge("c", "a")
    assert simulate(g, ["a"]).failed == {"a", "b", "c"}


def test_path_and_report() -> None:
    g = chain()
    sim = simulate(g, ["hw:plug"])
    assert path_to_root(sim, "fn:auto")[0] == "hw:plug"
    assert path_to_root(sim, "fn:auto")[-1] == "fn:auto"
    r = impact_report(g, ["hw:plug"])
    assert r["functions"]["total"] == 2
    assert {f["id"] for f in r["functions"]["failed"]} == {"fn:presence", "fn:auto"}
    steps = r["functions"]["failed"][0]["path"]
    assert steps[0]["id"] == "hw:plug" and steps[1]["edge"] == "powered by it"
    assert "2 of 2 functions" in r["summary"]
    assert "hw:plug" not in [i["id"] for grp in r["groups"] for i in grp["failed"]]


def test_spof_merges_identical_outcomes_and_respects_threshold() -> None:
    g = chain()
    r = spof(g, threshold=0.5)
    ids = [x["id"] for x in r["spofs"]]
    assert ids[0] == "hw:plug"  # most upstream of plug → pi → host → docker → ha
    same = {m["id"] for m in r["spofs"][0]["same_impact"]}
    assert {"hw:pi", "host:pi", "svc:docker", "svc:ha"} <= same
    g.nodes["hw:plug"].attrs["spof"] = False
    assert spof(g, 0.5)["spofs"][0]["id"] == "hw:pi"


def test_cause_finder_prefers_specific_explanation() -> None:
    g = chain()
    cf = CauseFinder(g)
    res = cf.find(["a", "b"], ["a", "b"])
    assert res["candidates"][0]["id"] == "hw:coord"  # hardware wins over integration with the same impact
    assert {m["id"] for m in res["candidates"][0]["same_impact"]} == {"int:zha"}
    assert res["message"].startswith("2 entities unavailable – likely cause: coord")
    res = cf.find(["a"], ["a"])
    assert res["candidates"][0]["id"] == "dev:s1"
    res = cf.find(["a", "b", "c"], ["a", "b", "c"])
    assert res["candidates"][0]["id"] in ("svc:ha", "hw:plug", "hw:pi", "host:pi", "svc:docker")
    assert cf.find(["unknown"], [])["count"] == 0


def test_search_and_questions() -> None:
    g = chain()
    g.nodes["hw:pi"].aliases.append("raspberry")
    g.add_node("hw:fritzbox", "hardware", "FRITZ!Box 6660", aliases=["fritzbox", "router"])
    assert search(g, "fritzbox")[0]["id"] == "hw:fritzbox"
    assert resolve_question(g, "Was passiert, wenn die Fritzbox ausfällt?")[0]["id"] == "hw:fritzbox"
    assert resolve_question(g, "What happens if the raspberry crashes")[0]["id"] == "hw:pi"
    assert resolve_question(g, "was passiert wenn") == []


def test_graph_roundtrip() -> None:
    g = chain()
    g2 = Graph.from_dict(g.to_dict())
    assert g2.counts() == g.counts() and len(g2.edges) == len(g.edges)
    assert simulate(g2, ["hw:coord"]).failed == simulate(g, ["hw:coord"]).failed
