"""Graph building from collector facts + overrides (sample homelab)."""
import pytest

from blast_radius.analysis import CauseFinder, impact_report, simulate, spof
from blast_radius.build import build_graph, registered_domain
from tests import sample_homelab as s


@pytest.fixture()
def g():  # type: ignore[no-untyped-def]
    return build_graph([s.facts()], s.overrides())


def deps(g, nid):  # type: ignore[no-untyped-def]
    return {(e.dst, e.kind) for e in g.deps[nid]}


def test_hosts_units_containers(g) -> None:  # type: ignore[no-untyped-def]
    assert g.nodes["host:homeserver"].label == "Home server"
    assert ("hw:server", "runs_on") in deps(g, "host:homeserver")
    assert ("svc:homeserver:docker", "runs_on") in deps(g, "ctr:homeserver:homeassistant")
    assert not g.has("ctr:homeserver:old-thing")  # stopped containers are left out
    assert g.meta["stopped_containers"]["homeserver"] == ["old-thing"]


def test_env_and_file_refs(g) -> None:  # type: ignore[no-untyped-def]
    bridge = deps(g, "ctr:homeserver:battery-bridge")
    assert ("svc:homeserver:mosquitto", "needs") in bridge  # MQTT_HOST + MQTT_PORT → port index
    assert ("cloud:battery", "needs") in bridge
    assert ("net:homeserver:docker:energy_default", "needs") in bridge
    # loopback inside a bridged container is the container itself
    assert all(not d.startswith("host:") for d, _ in deps(g, "ctr:homeserver:influxdb"))


def test_network_basics(g) -> None:  # type: ignore[no-untyped-def]
    assert ("hw:router", "needs") in deps(g, "net:lan")
    assert ("hw:router", "needs") in deps(g, "net:internet")
    assert ("hw:router", "needs") in deps(g, "net:dhcp")
    groups = {e.dst: e.group for e in g.deps["net:dns"]}
    assert groups == {"svc:homeserver:pihole-FTL": "resolvers:host:homeserver", "hw:router": "resolvers:host:homeserver"}
    assert ("svc:homeserver:tailscaled", "needs") in deps(g, "net:tailnet")


def test_nginx_prometheus_grafana(g) -> None:  # type: ignore[no-untyped-def]
    assert ("svc:homeserver:prometheus", "needs") in deps(g, "site:homeserver:status")
    assert ("hw:nas", "needs") in deps(g, "site:homeserver:docs")  # web root on a network mount
    assert ("ctr:homeserver:homeassistant", "needs") in deps(g, "svc:homeserver:prometheus")
    assert ("ctr:homeserver:influxdb", "needs") in deps(g, "svc:homeserver:grafana-server")
    assert all(e.soft for e in g.deps["svc:homeserver:grafana-server"] if e.kind == "needs")


def test_homeassistant_structure(g) -> None:  # type: ignore[no-untyped-def]
    assert ("hw:coordinator", "needs") in deps(g, "int:E_ZHA")
    assert ("dev:D_COORD", "controlled_by") in deps(g, "dev:D_PIR0")
    assert ("dev:D_HUB", "controlled_by") in deps(g, "dev:D_TRV1")
    # via_device of another integration (router) is topology only
    assert ("dev:D_ROUTER", "controlled_by") not in deps(g, "dev:D_PLUG")
    assert ("hw:plug-server", "needs") in deps(g, "dev:D_PLUG")
    assert ("net:lan", "needs") in deps(g, "int:E_HUB")
    assert ("svc:homeserver:mosquitto", "needs") in deps(g, "int:E_MQTT")
    assert not g.has("entity:sensor.disabled_one") and not g.has("int:E_OFF")
    assert ("entity:sensor.server_plug_power", "needs") in deps(g, "int:E_TPL")
    trig = [e for e in g.deps["auto:room_tracking"] if e.group == "trigger"]
    assert len(trig) == 3
    # helper written by an automation depends on it
    assert [e.weak for e in g.deps["entity:input_select.room"] if e.dst == "auto:room_tracking"] == [True]
    assert ("int:E_PHONE", "needs") in deps(g, "auto:battery_low")
    assert ("cloud:push", "needs") in deps(g, "int:E_PHONE")


def test_override_warnings_and_functions(g) -> None:  # type: ignore[no-untyped-def]
    assert any("hw:does-not-exist" in w for w in g.warnings)
    assert {e.dst for e in g.deps["fn:heating"] if e.soft} == {"entity:climate.living", "entity:climate.bedroom"}
    assert {e.group for e in g.deps["fn:dns-any"]} == {"any_of_0"}
    assert ("ctr:homeserver:battery-bridge", "needs") in deps(g, "dev:D_BATT")


def test_scenarios(g) -> None:  # type: ignore[no-untyped-def]
    r = impact_report(g, ["hw:coordinator"])
    failed = {f["id"] for f in r["functions"]["failed"]}
    degraded = {f["id"] for f in r["functions"]["degraded"]}
    assert failed == {"fn:presence"}
    assert "fn:heating-follows" in degraded  # its trigger helper lost its writer
    r = impact_report(g, ["hw:router"])
    failed = {f["id"] for f in r["functions"]["failed"]}
    assert {"fn:heating", "fn:presence", "fn:remote", "fn:docs", "fn:battery"} <= failed
    assert "fn:dns-any" not in failed  # Pi-hole still answers
    r = impact_report(g, ["hw:server"])
    assert {f["id"] for f in r["functions"]["failed"]} >= {"fn:heating", "fn:battery", "fn:remote"}
    assert simulate(g, ["hw:server"]).state("fn:dns-any") == "at_risk"  # router is the remaining alternative


def test_spof_and_cause(g) -> None:  # type: ignore[no-untyped-def]
    r = spof(g, 0.5)
    ids = [x["id"] for x in r["spofs"]]
    assert "hw:mains" not in ids
    assert ids[0] in ("hw:plug-server", "hw:router")
    cf = CauseFinder(g)
    ents = ["binary_sensor.motion_kitchen", "sensor.motion_kitchen_lux", "binary_sensor.motion_hall",
            "sensor.motion_hall_lux", "binary_sensor.motion_bath", "sensor.motion_bath_lux", "button.coordinator_identify"]
    res = cf.find(ents, ents)
    assert res["candidates"][0]["id"] == "hw:coordinator"
    assert "likely cause: Zigbee coordinator" in res["message"]
    # noise: one unrelated entity is unavailable too
    res = cf.find(ents[:-1] + ["weather.home"], ents[:-1] + ["weather.home"])
    assert res["candidates"][0]["id"] == "hw:coordinator"
    assert res["unexplained"] == ["weather.home"]


def test_registered_domain() -> None:
    assert registered_domain("openapi.growatt.com") == "growatt.com"
    assert registered_domain("a.b.example.co.uk") == "example.co.uk"
