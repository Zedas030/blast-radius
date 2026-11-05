"""Engine (rebuild, change log, self-monitoring, mass outage) and HTTP API."""
import copy
import datetime as dt
import json
import os

import pytest
from fastapi.testclient import TestClient

from blast_radius.app import create_app
from blast_radius.config import DEFAULTS, _merge
from blast_radius.engine import Engine
from blast_radius.homeassistant import UnavailabilityTracker
from tests import sample_homelab as s


class FakeHA:
    """Stands in for blast_radius.homeassistant.HomeAssistant."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.status: list[tuple[str, dict]] = []
        self.states_list: list[dict] = []
        self.notify_service = "notify.test"
        self.configured = True
        self.last_error = ""

    def notify(self, title: str, message: str, url: str = "") -> bool:
        self.sent.append(message)
        return True

    def publish_status(self, state: str, attributes: dict) -> bool:
        self.status.append((state, attributes))
        return True

    def states(self) -> list[dict]:
        return self.states_list

    def core_state(self) -> str:
        return self.core

    core = "RUNNING"


def write_facts(path: str, facts: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(facts, fh)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # make the change visible


@pytest.fixture()
def env(tmp_path):  # type: ignore[no-untyped-def]
    facts_path = str(tmp_path / "facts.json")
    ov_path = str(tmp_path / "overrides.yaml")
    write_facts(facts_path, s.facts())
    with open(ov_path, "w", encoding="utf-8") as fh:
        fh.write(s.OVERRIDES_YAML)
    cfg = _merge(DEFAULTS, {
        "engine": {"facts_files": [facts_path], "overrides_file": ov_path, "data_dir": str(tmp_path / "state")},
        "homeassistant": {"url": "http://ha.invalid", "startup_grace_seconds": 0, "mass_outage": {"min_entities": 5, "window_seconds": 300,
                                                                     "cooldown_minutes": 60}},
    })
    eng = Engine(cfg)
    eng.ha = FakeHA()
    return eng, facts_path, ov_path, cfg


def test_initial_build_and_change_log(env) -> None:  # type: ignore[no-untyped-def]
    eng, facts_path, _, _ = env
    r = eng.check()
    assert r["rebuilt"] and r["accepted"]
    assert eng.changes()[0]["initial"] is True
    assert eng.check()["rebuilt"] is False  # nothing changed
    f = s.facts()
    f["homeassistant"]["entities"].append({"entity_id": "sensor.new_one", "device_id": "D_HUB", "entry_id": "E_HUB",
                                           "platform": "tplink", "name": "New", "disabled": False})
    write_facts(facts_path, f)
    r = eng.check()
    assert r["rebuilt"]
    ch = eng.changes()[0]
    assert ch["nodes_added"] == 1 and ch["added"][0]["id"] == "entity:sensor.new_one"
    assert os.path.exists(os.path.join(eng.data_dir, "graph.json"))


def test_shrink_is_rejected_alerted_and_accepted_later(env) -> None:  # type: ignore[no-untyped-def]
    eng, facts_path, _, _ = env
    eng.check()
    before = len(eng.graph.nodes)
    f = s.facts()
    f["homeassistant"]["entities"] = f["homeassistant"]["entities"][:3]
    f["homeassistant"]["devices"] = f["homeassistant"]["devices"][:2]
    write_facts(facts_path, f)
    r = eng.check()
    assert r["accepted"] is False and len(eng.graph.nodes) == before  # previous graph stays active
    assert "shrink" in eng.state["alerts"]
    assert any("Large parts of the dependency graph are missing" in m for m in eng.ha.sent)
    eng.accept_pending()
    r = eng.check()
    assert r["accepted"] is True and len(eng.graph.nodes) < before
    assert "shrink" not in eng.state["alerts"]


def test_source_errors_and_stale_facts_alert_once_and_resolve(env) -> None:  # type: ignore[no-untyped-def]
    eng, facts_path, _, _ = env
    f = s.facts()
    f["errors"] = [{"source": "docker", "error": "RuntimeError: docker ps exited 1"}]
    f["sources"]["docker"] = "error"
    write_facts(facts_path, f)
    eng.check()
    eng.check(force=True)
    assert len([m for m in eng.ha.sent if "docker" in m]) == 1  # no repeated messages
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat()
    write_facts(facts_path, s.facts(collected_at=old))
    eng.check()
    assert any("Nightly discovery did not run" in m for m in eng.ha.sent)
    assert "source:homeserver:docker" not in eng.state["alerts"]  # resolved with the new facts
    write_facts(facts_path, s.facts())
    eng.check()
    assert not eng.state["alerts"]
    assert any("fresh again" in m for m in eng.ha.sent)


def test_missing_facts_file_alerts(env) -> None:  # type: ignore[no-untyped-def]
    eng, facts_path, _, _ = env
    os.remove(facts_path)
    eng.check()
    assert any("cannot be read" in m for m in eng.ha.sent)


def test_tracker_baseline_and_window() -> None:
    t = UnavailabilityTracker()
    t.update([{"entity_id": "a", "state": "unavailable"}, {"entity_id": "b", "state": "on"}], now=0)
    assert t.new_within(300, now=10) == set()  # already unavailable at start = baseline
    t.update([{"entity_id": "a", "state": "unavailable"}, {"entity_id": "b", "state": "unavailable"}], now=100)
    assert t.new_within(300, now=110) == {"b"}
    assert t.new_within(300, now=500) == set()
    t.update([{"entity_id": "b", "state": "on"}], now=600)
    assert t.unavailable() == set()


def test_mass_outage_detection_notifies_with_cause_and_cooldown(env) -> None:  # type: ignore[no-untyped-def]
    eng, *_ = env
    eng.check()
    ha = eng.ha
    zig = ["binary_sensor.motion_kitchen", "sensor.motion_kitchen_lux", "binary_sensor.motion_hall",
           "sensor.motion_hall_lux", "binary_sensor.motion_bath", "sensor.motion_bath_lux", "button.coordinator_identify"]
    ha.states_list = [{"entity_id": e, "state": "on"} for e in zig + ["weather.home"]]
    eng.poll_ha()
    assert not [m for m in ha.sent if "likely cause" in m]
    ha.states_list = [{"entity_id": e, "state": "unavailable"} for e in zig] + [{"entity_id": "weather.home", "state": "sunny"}]
    live = eng.poll_ha()
    msgs = [m for m in ha.sent if "likely cause" in m]
    assert msgs == ["7 entities unavailable – likely cause: Zigbee coordinator"]
    assert live["cause"]["candidates"][0]["id"] == "hw:coordinator"
    eng.poll_ha()
    assert len([m for m in ha.sent if "likely cause" in m]) == 1  # cooldown
    assert ha.status and ha.status[-1][1]["last_cause"].startswith("7 entities")


def test_no_alarm_while_home_assistant_starts(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    eng, *_ = env
    eng.cfg["homeassistant"]["startup_grace_seconds"] = 600
    eng.check()
    zig = ["binary_sensor.motion_kitchen", "sensor.motion_kitchen_lux", "binary_sensor.motion_hall",
           "sensor.motion_hall_lux", "binary_sensor.motion_bath", "sensor.motion_bath_lux", "button.coordinator_identify"]
    clock = [1_900_000_000.0]
    monkeypatch.setattr("blast_radius.engine.time.time", lambda: clock[0])
    ha = eng.ha
    ha.core, ha.states_list = "NOT_RUNNING", [{"entity_id": e, "state": "on"} for e in zig]
    assert eng.poll_ha()["warming_up"] is True
    # HA is RUNNING, integrations still loading: everything unavailable for a moment
    ha.core, ha.states_list = "RUNNING", [{"entity_id": e, "state": "unavailable"} for e in zig]
    clock[0] += 60
    live = eng.poll_ha()
    assert live["warming_up"] is True and live["new"] == 0 and not ha.sent
    clock[0] += 600  # grace over; still unavailable = baseline, no alarm
    live = eng.poll_ha()
    assert live["warming_up"] is False and live["new"] == 0 and not ha.sent
    # a real outage later is reported
    ha.states_list = [{"entity_id": e, "state": "on"} for e in zig]
    clock[0] += 60
    eng.poll_ha()
    ha.states_list = [{"entity_id": e, "state": "unavailable"} for e in zig]
    clock[0] += 60
    eng.poll_ha()
    assert [m for m in ha.sent if "likely cause: Zigbee coordinator" in m]
    # HA unreachable (restart) restarts the grace period
    def down():  # type: ignore[no-untyped-def]
        raise OSError("connection refused")
    ha.states = down
    eng.poll_ha()
    assert eng.ha_running_since is None


def test_small_outage_is_not_reported(env) -> None:  # type: ignore[no-untyped-def]
    eng, *_ = env
    eng.check()
    eng.ha.states_list = [{"entity_id": "climate.living", "state": "heat"}]
    eng.poll_ha()
    eng.ha.states_list = [{"entity_id": "climate.living", "state": "unavailable"}]
    live = eng.poll_ha()
    assert live["cause"] is None and not eng.ha.sent


@pytest.fixture()
def client(env):  # type: ignore[no-untyped-def]
    eng, _, _, cfg = env
    app = create_app(copy.deepcopy(cfg), start_loops=False)
    app.state.engine.ha = FakeHA()
    app.state.engine.check(force=True)
    return TestClient(app)


def test_api_endpoints(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/health").json()["ok"] is True
    g = client.get("/api/graph").json()
    assert not any(n["type"] == "entity" for n in g["nodes"])
    assert any(n["type"] == "entity" for n in client.get("/api/graph?entities=true").json()["nodes"])
    assert client.get("/api/search?q=zigbee").json()[0]["id"] == "hw:coordinator"
    r = client.get("/api/impact", params={"node": "hw:coordinator"}).json()
    assert [f["id"] for f in r["functions"]["failed"]] == ["fn:presence"]
    r = client.post("/api/impact", json={"nodes": ["hw:router", "unit:x"]}).json()
    assert r["roots"][0]["id"] == "hw:router"
    a = client.get("/api/ask", params={"q": "Was passiert, wenn der Zigbee-Coordinator ausfällt?"}).json()
    assert a["matched"]["id"] == "hw:coordinator" and "Presence detection" in a["speech"]
    assert a["matched"]["say"] == "der Zigbee-Koordinator"
    r = client.get("/api/ask", params={"q": "Was passiert, wenn der Hub ausfällt?"}).json()
    assert {"id": "fn:heating", "label": "Heating", "say": "die Heizung"} in r["functions"]["failed"]
    assert client.get("/api/ask", params={"q": "was passiert"}).json()["matched"] is None
    assert client.get("/api/spof").json()["spofs"]
    sim = client.post("/api/cause/simulate", json={"entities": ["binary_sensor.motion_kitchen", "binary_sensor.motion_hall",
                                                                "binary_sensor.motion_bath"]}).json()
    assert sim["candidates"][0]["id"] == "hw:coordinator"
    node = client.get("/api/node/hw:coordinator").json()
    assert any(d["id"] == "int:E_ZHA" for d in node["dependents"])
    assert client.get("/api/node/nope").status_code == 404
    assert client.get("/api/changes").json()[0]["initial"] is True
    m = client.get("/metrics").text
    assert 'blast_radius_graph_nodes{type="entity"}' in m and "blast_radius_build_ok 1.0" in m
    assert client.get("/").status_code == 200
    assert client.get("/static/vendor/cytoscape.min.js").status_code == 200
    assert client.post("/api/rebuild").json()["rebuilt"] is True


def test_api_without_graph(tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = _merge(DEFAULTS, {"engine": {"facts_files": [str(tmp_path / "none.json")],
                                       "overrides_file": str(tmp_path / "none.yaml"), "data_dir": str(tmp_path / "st")}})
    app = create_app(cfg, start_loops=False)
    c = TestClient(app)
    assert c.get("/api/health").json()["ok"] is False
    assert c.get("/api/graph").status_code == 503
