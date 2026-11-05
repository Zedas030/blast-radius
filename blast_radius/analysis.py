"""Graph analysis: outage propagation, impact report, single points of failure, cause finding, search."""
from __future__ import annotations

import re
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import EDGE_LABELS, Edge, Graph

# Types that describe "things that can break" (candidates for SPOF and cause finding).
INFRA_TYPES = ("hardware", "network", "cloud", "host", "service", "integration", "device")
# Order used to pick the most meaningful node among nodes with an identical impact.
TYPE_PRIORITY = {t: i for i, t in enumerate(
    ("hardware", "network", "cloud", "host", "service", "integration", "device",
     "automation", "script", "dashboard", "entity", "function"))}
REPORT_ORDER = ("function", "automation", "script", "dashboard", "hardware", "network", "cloud", "host",
                "service", "integration", "device", "entity")
TYPE_PLURAL = {
    "function": "functions", "automation": "automations", "script": "scripts", "dashboard": "dashboards",
    "hardware": "hardware", "network": "networks", "cloud": "cloud services", "host": "hosts",
    "service": "services", "integration": "integrations", "device": "devices", "entity": "entities",
}


@dataclass
class Simulation:
    roots: list[str]
    failed: set[str] = field(default_factory=set)
    degraded: set[str] = field(default_factory=set)
    at_risk: set[str] = field(default_factory=set)  # still works, but an alternative is gone
    reason: dict[str, Edge] = field(default_factory=dict)

    def state(self, node_id: str) -> str:
        if node_id in self.failed:
            return "failed"
        if node_id in self.degraded:
            return "degraded"
        if node_id in self.at_risk:
            return "at_risk"
        return "ok"


def _evaluate(graph: Graph, node_id: str, failed: set[str], degraded: set[str]) -> tuple[str, Edge | None]:
    deps = graph.deps.get(node_id, [])
    if not deps:
        return "ok", None
    soft = [e for e in deps if e.soft]
    groups: dict[str, list[Edge]] = {}
    weak_hit: Edge | None = None
    for e in deps:
        if e.weak:
            if e.dst in failed and weak_hit is None:
                weak_hit = e
            continue
        if e.soft:
            continue
        if e.group:
            groups.setdefault(e.group, []).append(e)
        elif e.dst in failed:
            return "failed", e
    partial: Edge | None = None
    for edges in groups.values():
        down = [e for e in edges if e.dst in failed]
        if len(down) == len(edges):
            return "failed", down[-1]
        if down:
            partial = partial or down[0]
    if soft:
        down = [e for e in soft if e.dst in failed]
        if len(down) == len(soft):
            return "failed", down[0]
        if down:
            return "degraded", down[0]
    if weak_hit:
        return "degraded", weak_hit
    for e in deps:
        if e.dst in degraded:
            return "degraded", e
    if partial:
        return "at_risk", partial
    return "ok", None


def simulate(graph: Graph, roots: Iterable[str]) -> Simulation:
    """Fail ``roots`` and propagate along the dependency edges until nothing changes."""
    roots = [r for r in dict.fromkeys(roots) if r in graph.nodes]
    sim = Simulation(roots=roots, failed=set(roots))
    queue: deque[str] = deque()
    for r in roots:
        queue.extend(e.src for e in graph.rdeps.get(r, []))
    while queue:
        n = queue.popleft()
        if n in sim.failed:
            continue
        state, edge = _evaluate(graph, n, sim.failed, sim.degraded)
        if state == "failed":
            sim.failed.add(n)
            sim.degraded.discard(n)
        elif state == "degraded" and n not in sim.degraded:
            sim.degraded.add(n)
            sim.at_risk.discard(n)
        elif state == "at_risk" and n not in sim.at_risk and n not in sim.degraded:
            sim.at_risk.add(n)
            if edge is not None:
                sim.reason[n] = edge
            continue  # lost redundancy does not propagate
        else:
            continue
        if edge is not None:
            sim.reason[n] = edge
        queue.extend(e.src for e in graph.rdeps.get(n, []))
    return sim


def path_to_root(sim: Simulation, node_id: str, limit: int = 40) -> list[str]:
    """Node ids from the failed root down to ``node_id`` (root first)."""
    path = [node_id]
    seen = {node_id}
    cur = node_id
    while cur in sim.reason and len(path) < limit:
        nxt = sim.reason[cur].dst
        if nxt in seen:
            break
        path.append(nxt)
        seen.add(nxt)
        cur = nxt
    return list(reversed(path))


def describe_path(graph: Graph, sim: Simulation, node_id: str) -> list[dict[str, str]]:
    """Path with labels and the edge kind leading to each step, e.g. Pi → (runs on) Docker → …"""
    ids = path_to_root(sim, node_id)
    out = []
    for i, nid in enumerate(ids):
        n = graph.nodes[nid]
        step = {"id": nid, "label": n.label, "type": n.type, "state": sim.state(nid)}
        if i > 0:
            e = sim.reason.get(nid)
            if e is not None:
                step["edge"] = EDGE_LABELS[e.kind] + (", partly" if e.soft or e.weak else "")
        out.append(step)
    return out


def _join(labels: list[str], limit: int) -> str:
    if not labels:
        return ""
    if len(labels) <= limit:
        return ", ".join(labels)
    return ", ".join(labels[:limit]) + f" and {len(labels) - limit} more"


def impact_report(graph: Graph, roots: Iterable[str], *, with_paths: bool = True) -> dict[str, Any]:
    sim = simulate(graph, roots)
    groups = []
    counts: dict[str, dict[str, int]] = {}
    for t in REPORT_ORDER:
        failed = sorted((graph.nodes[n] for n in sim.failed if graph.nodes[n].type == t and n not in sim.roots),
                        key=lambda n: n.label.lower())
        degraded = sorted((graph.nodes[n] for n in sim.degraded if graph.nodes[n].type == t),
                          key=lambda n: n.label.lower())
        counts[t] = {"failed": len(failed), "degraded": len(degraded)}
        if not failed and not degraded:
            continue

        def item(n: Any) -> dict[str, Any]:
            d: dict[str, Any] = {"id": n.id, "label": n.label}
            if n.attrs.get("say"):
                d["say"] = n.attrs["say"]
            if with_paths:
                d["path"] = describe_path(graph, sim, n.id)
            return d

        groups.append({"type": t, "title": TYPE_PLURAL[t], "failed": [item(n) for n in failed],
                       "degraded": [item(n) for n in degraded]})
    total_fn = len(graph.of_type("function"))
    root_labels = [graph.nodes[r].label for r in sim.roots]
    fn_failed = [graph.nodes[i["id"]].label for g in groups if g["type"] == "function" for i in g["failed"]]
    fn_degraded = [graph.nodes[i["id"]].label for g in groups if g["type"] == "function" for i in g["degraded"]]
    tech = [f"{counts[t]['failed']} {TYPE_PLURAL[t]}" for t in REPORT_ORDER
            if t != "function" and counts[t]["failed"]]
    who = " and ".join(root_labels) if root_labels else "nothing"
    if fn_failed:
        summary = f"If {who} fails, {len(fn_failed)} of {total_fn} functions stop working: {_join(fn_failed, 8)}."
    else:
        summary = f"If {who} fails, none of your {total_fn} functions stops completely."
    if fn_degraded:
        summary += f" Degraded: {_join(fn_degraded, 6)}."
    if tech:
        summary += " Technically affected: " + ", ".join(tech) + "."
    speech = (f"If {who} fails, these stop working: {_join(fn_failed, 6)}." if fn_failed
              else f"If {who} fails, nothing important stops completely.")
    if fn_degraded:
        speech += f" Limited: {_join(fn_degraded, 4)}."
    return {
        "roots": [{"id": r, "label": graph.nodes[r].label, "type": graph.nodes[r].type,
                   **({"say": graph.nodes[r].attrs["say"]} if graph.nodes[r].attrs.get("say") else {})}
                  for r in sim.roots],
        "summary": summary,
        "speech": speech,
        "functions": {
            "failed": next((g["failed"] for g in groups if g["type"] == "function"), []),
            "degraded": next((g["degraded"] for g in groups if g["type"] == "function"), []),
            "total": total_fn,
        },
        "groups": groups,
        "counts": counts,
        "at_risk": [{"id": n, "label": graph.nodes[n].label, "type": graph.nodes[n].type,
                     **({"path": describe_path(graph, sim, n)} if with_paths else {})}
                    for n in sorted(sim.at_risk)],
        "failed_ids": sorted(sim.failed),
        "degraded_ids": sorted(sim.degraded),
        "at_risk_ids": sorted(sim.at_risk),
    }


# --------------------------------------------------------------------------- SPOF

def _share(sim: Simulation, graph: Graph, totals: dict[str, int]) -> tuple[float, float]:
    fn = sum(1 for n in sim.failed if graph.nodes[n].type == "function")
    ent = sum(1 for n in sim.failed if graph.nodes[n].type == "entity")
    return (fn / totals["function"] if totals["function"] else 0.0,
            ent / totals["entity"] if totals["entity"] else 0.0)


def spof(graph: Graph, threshold: float = 0.5, limit: int = 25) -> dict[str, Any]:
    """Nodes whose failure alone takes down more than ``threshold`` of functions or entities.

    Nodes with an identical outcome (e.g. a power plug, the board it powers and the OS on it) are merged
    into one row; the most upstream node is shown first.
    """
    totals = {"function": len(graph.of_type("function")), "entity": len(graph.of_type("entity"))}
    rows: dict[frozenset[str], dict[str, Any]] = {}
    for node in graph.of_type(*INFRA_TYPES):
        if node.attrs.get("spof") is False or node.attrs.get("planned"):
            continue
        sim = simulate(graph, [node.id])
        fn_share, ent_share = _share(sim, graph, totals)
        score = max(fn_share, ent_share)
        if score <= 0:
            continue
        sig = frozenset(n for n in sim.failed if graph.nodes[n].type in ("function", "entity"))
        size = len(sim.failed)
        row = rows.get(sig)
        entry = {"id": node.id, "label": node.label, "type": node.type, "size": size}
        if row is None:
            rows[sig] = {"score": score, "function_share": fn_share, "entity_share": ent_share,
                         "members": [entry],
                         "functions": sorted(graph.nodes[n].label for n in sim.failed
                                             if graph.nodes[n].type == "function")}
        else:
            row["members"].append(entry)
    out = []
    for row in rows.values():
        row["members"].sort(key=lambda m: (-m["size"], TYPE_PRIORITY.get(m["type"], 99), m["label"]))
        main = row["members"][0]
        out.append({"id": main["id"], "label": main["label"], "type": main["type"],
                    "score": round(row["score"], 4), "function_share": round(row["function_share"], 4),
                    "entity_share": round(row["entity_share"], 4), "functions": row["functions"],
                "impact_size": main["size"],
                    "same_impact": [{"id": m["id"], "label": m["label"], "type": m["type"]}
                                    for m in row["members"][1:]]})
    out.sort(key=lambda r: (-r["score"], -r["impact_size"], r["label"].lower()))
    spofs = [r for r in out if r["score"] > threshold]
    return {"threshold": threshold, "totals": totals, "spofs": spofs, "top": out[:limit]}


# --------------------------------------------------------------------------- cause finding

class CauseFinder:
    """Find the node that best explains a set of unavailable entities. Impact sets are cached per graph."""

    def __init__(self, graph: Graph) -> None:
        self.graph = graph
        self._impact: dict[str, frozenset[str]] = {}
        ent_ids = {n.id for n in graph.of_type("entity")}
        for node in graph.of_type(*INFRA_TYPES):
            sim = simulate(graph, [node.id])
            ents = frozenset(n for n in sim.failed if n in ent_ids)
            if ents:
                self._impact[node.id] = ents

    def find(self, new_unavailable: Iterable[str], all_unavailable: Iterable[str] = (),
             top: int = 3) -> dict[str, Any]:
        def norm(ids: Iterable[str]) -> set[str]:
            out = set()
            for i in ids:
                nid = i if i.startswith("entity:") else f"entity:{i}"
                if nid in self.graph.nodes:
                    out.add(nid)
            return out

        new = norm(new_unavailable)
        unavailable = norm(all_unavailable) | new
        if not new:
            return {"count": 0, "candidates": [], "message": "No known entities unavailable."}
        by_sig: dict[frozenset[str], dict[str, Any]] = {}
        for nid, ents in self._impact.items():
            hit = len(ents & new)
            if not hit:
                continue
            recall = hit / len(new)
            precision = len(ents & unavailable) / len(ents)
            score = recall * precision
            node = self.graph.nodes[nid]
            row = by_sig.setdefault(ents, {"score": score, "recall": recall, "precision": precision,
                                           "explains": hit, "size": len(ents), "members": []})
            row["members"].append(node)
        ranked = sorted(by_sig.values(), key=lambda r: (-round(r["score"], 6), r["size"],
                                                        min(TYPE_PRIORITY[m.type] for m in r["members"])))
        candidates = []
        for row in ranked[:top]:
            members = sorted(row["members"], key=lambda m: (TYPE_PRIORITY[m.type], m.label.lower()))
            main = members[0]
            candidates.append({
                "id": main.id, "label": main.label, "type": main.type,
                "score": round(row["score"], 4), "recall": round(row["recall"], 4),
                "precision": round(row["precision"], 4), "explains": row["explains"],
                "entities_behind_node": row["size"],
                "same_impact": [{"id": m.id, "label": m.label, "type": m.type} for m in members[1:]],
            })
        unexplained: list[str] = []
        if candidates:
            best = self._impact[candidates[0]["id"]]
            unexplained = sorted(n.removeprefix("entity:") for n in new - best)
        msg = f"{len(new)} entities unavailable"
        if candidates:
            c = candidates[0]
            msg += f" – likely cause: {c['label']}"
            if c["explains"] < len(new):
                msg += f" (explains {c['explains']} of {len(new)})"
        return {"count": len(new), "candidates": candidates, "unexplained": unexplained, "message": msg}


# --------------------------------------------------------------------------- search

STOPWORDS = set("""
was passiert wenn wenn's der die das den dem des ein eine einer eines mein meine meinen meiner unser
ausfällt ausfaellt ausfallen ausfall fällt faellt fallt aus abstürzt absturzt abstuerzt stürzt sturzt
kaputt geht weg ist hat dann alles mit ohne bei von im in am an auf zu und oder nicht mehr
what happens if when the a an my our fails fail failing down goes go crashes crash dies die is
breaks broken stops offline outage of to and or
""".split())


def normalize(text: str) -> str:
    text = text.lower().replace("ß", "ss")
    text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9._:]+", " ", text).strip()


def _names(node: Any) -> list[str]:
    names = [node.label, node.id, node.id.split(":", 1)[-1]] + list(node.aliases)
    return [normalize(n) for n in names if n]


def search(graph: Graph, query: str, limit: int = 20, include_entities: bool = True) -> list[dict[str, Any]]:
    q = normalize(query)
    if not q:
        return []
    q_tokens = set(q.split())
    scored = []
    for node in graph.nodes.values():
        if node.type == "entity" and not include_entities:
            continue
        best = 0.0
        for name in _names(node):
            if not name:
                continue
            if name == q:
                s = 100.0
            elif name.startswith(q):
                s = 80.0
            elif re.search(rf"\b{re.escape(q)}\b", name):
                s = 70.0
            elif q in name:
                s = 55.0
            else:
                tokens = set(name.replace(".", " ").replace("_", " ").replace(":", " ").split())
                common = q_tokens & tokens
                s = 40.0 * len(common) / len(q_tokens) if common else 0.0
            best = max(best, s)
        if best <= 0:
            continue
        if node.type == "entity":
            best -= 15
        elif node.source == "override":
            best += 5  # hand-maintained nodes carry the names people use
        best -= TYPE_PRIORITY.get(node.type, 0) * 0.1
        scored.append((best, node))
    scored.sort(key=lambda x: (-x[0], x[1].label.lower()))
    return [{"id": n.id, "label": n.label, "type": n.type, "score": round(s, 1)} for s, n in scored[:limit]]


def resolve_question(graph: Graph, question: str) -> list[dict[str, Any]]:
    """Find the node(s) a natural-language question is about (German or English)."""
    tokens = [t for t in normalize(question).split() if t not in STOPWORDS]
    if not tokens:
        return []
    # try the longest phrases first: "zigbee coordinator" beats "zigbee"
    for size in range(len(tokens), 0, -1):
        best: list[dict[str, Any]] = []
        for i in range(len(tokens) - size + 1):
            phrase = " ".join(tokens[i:i + size])
            hits = [h for h in search(graph, phrase, limit=5) if h["score"] >= 55]
            if hits and (not best or hits[0]["score"] > best[0]["score"]):
                best = hits
        if best:
            return best
    return []
