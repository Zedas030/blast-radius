"""End-to-end test against the running Docker deployment (started by run_e2e.sh)."""
import datetime as dt
import json
import os
import time

import httpx
import pytest

from tests import sample_homelab as s

pytestmark = pytest.mark.e2e
BR = os.environ.get("BR_URL", "http://127.0.0.1:18098")
HA = os.environ.get("FAKEHA_URL", "http://127.0.0.1:18123")
FACTS = os.environ.get("BR_FACTS", "")

ZIGBEE = ["binary_sensor.motion_kitchen", "sensor.motion_kitchen_lux", "binary_sensor.motion_hall",
          "sensor.motion_hall_lux", "binary_sensor.motion_bath", "sensor.motion_bath_lux", "button.coordinator_identify"]


def wait_for(fn, timeout=40, every=1.0):  # type: ignore[no-untyped-def]
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(every)
    raise AssertionError(f"timed out, last value: {last!r}")


def log() -> dict:
    return httpx.get(f"{HA}/_control/log", timeout=5).json()


def messages() -> list[str]:
    return [n.get("message", "") for n in log()["notifications"]]


def write_facts(facts: dict) -> None:
    tmp = FACTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(facts, fh)
    os.chmod(tmp, 0o644)
    os.replace(tmp, FACTS)


def test_01_up_and_graph() -> None:
    h = wait_for(lambda: httpx.get(f"{BR}/api/health", timeout=5).json()["ok"] and httpx.get(f"{BR}/api/health").json())
    assert h["version"]
    g = httpx.get(f"{BR}/api/graph?entities=true", timeout=10).json()
    assert len(g["nodes"]) > 50 and g["counts"]["function"] == 8
    page = httpx.get(BR, timeout=5)
    assert page.status_code == 200 and "Blast-Radius" in page.text


def test_02_questions() -> None:
    a = httpx.get(f"{BR}/api/ask", params={"q": "Was passiert, wenn der Server abstürzt?"}, timeout=10).json()
    assert a["matched"]["id"] in ("host:homeserver", "hw:server")
    assert "Heating" in a["speech"] and "Remote access" in a["speech"]
    a = httpx.get(f"{BR}/api/ask", params={"q": "What happens if the router fails?"}, timeout=10).json()
    assert a["matched"]["id"] == "hw:router"
    a = httpx.get(f"{BR}/api/ask", params={"q": "Zigbee-Coordinator fällt aus"}, timeout=10).json()
    assert [f["label"] for f in a["functions"]["failed"]] == ["Presence detection"]
    sp = httpx.get(f"{BR}/api/spof", timeout=10).json()
    assert sp["spofs"] and sp["spofs"][0]["id"] in ("hw:plug-server", "hw:router")


def test_03_status_sensor_and_token() -> None:
    lg = wait_for(lambda: log()["published"] and log())
    assert lg["published"][-1]["entity_id"] == "sensor.blast_radius"
    assert lg["published"][-1]["state"] in ("ok", "warning")
    assert all(a == "Bearer e2e-token" for a in lg["auth"])


def test_04_mass_outage_notification_with_cause() -> None:
    ok = [{"entity_id": e, "state": "on"} for e in ZIGBEE + ["weather.home", "climate.living"]]
    httpx.post(f"{HA}/_control/states", json=ok, timeout=5)
    time.sleep(4)  # baseline poll
    down = [{"entity_id": e, "state": "unavailable"} for e in ZIGBEE] + ok[-2:]
    httpx.post(f"{HA}/_control/states", json=down, timeout=5)
    msg = wait_for(lambda: next((m for m in messages() if "likely cause" in m), None))
    assert msg == "7 entities unavailable – likely cause: Zigbee coordinator"
    live = httpx.get(f"{BR}/api/live", timeout=5).json()
    assert live["cause"]["candidates"][0]["id"] == "hw:coordinator"
    m = httpx.get(f"{BR}/metrics", timeout=5).text
    assert "blast_radius_mass_outages_total 1.0" in m
    assert "blast_radius_ha_unavailable_entities 7.0" in m


def test_05_stale_facts_alert() -> None:
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=40)).isoformat(timespec="seconds")
    write_facts(s.facts(collected_at=old))
    msg = wait_for(lambda: next((m for m in messages() if "Nightly discovery did not run" in m), None))
    assert "homeserver" in msg
    write_facts(s.facts())
    wait_for(lambda: next((m for m in messages() if "fresh again" in m), None))


def test_06_source_error_and_shrink_alerts() -> None:
    f = s.facts()
    f["errors"] = [{"source": "homeassistant", "error": "FileNotFoundError: core.config_entries"}]
    f["sources"]["homeassistant"] = "error"
    del f["homeassistant"]
    write_facts(f)
    wait_for(lambda: next((m for m in messages() if "Discovery source 'homeassistant'" in m), None))
    msg = wait_for(lambda: next((m for m in messages() if "Large parts of the dependency graph are missing" in m), None))
    assert "entity nodes dropped" in msg
    st = httpx.get(f"{BR}/api/status", timeout=5).json()
    assert st["graph"]["counts"]["entity"] > 10  # previous graph still active
    write_facts(s.facts())
    wait_for(lambda: next((m for m in messages() if "complete again" in m), None))


def test_07_change_log() -> None:
    f = s.facts()
    f["docker"]["containers"].append({**f["docker"]["containers"][2], "id": "eeeeeeeeeeee", "name": "new-app"})
    write_facts(f)
    ch = wait_for(lambda: next((c for c in httpx.get(f"{BR}/api/changes", timeout=5).json()
                                if any(n["id"] == "ctr:homeserver:new-app" for n in c["added"])), None))
    assert ch["nodes_added"] >= 1
