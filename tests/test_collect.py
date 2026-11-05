"""Collector helpers: host references are extracted, secrets never leak."""
import json
import os

from blast_radius import collect


def test_refs_in_value() -> None:
    assert collect.refs_in_value("tcp://192.0.2.10:1883") == [{"host": "192.0.2.10", "port": 1883}]
    assert collect.refs_in_value("socket://192.0.2.186:6638") == [{"host": "192.0.2.186", "port": 6638}]
    assert collect.refs_in_value("mqtt:1883") == [{"host": "mqtt", "port": 1883}]
    assert collect.refs_in_value("version 1.2.3.4.5") == []
    assert collect.refs_in_value("hello") == []


def test_credentials_are_stripped_from_urls() -> None:
    refs = collect.refs_in_value("https://user:s3cret@api.example.com/path?token=abc")
    assert refs == [{"host": "api.example.com", "port": None}]
    assert "s3cret" not in json.dumps(refs)


def test_walk_hosts_skips_secret_keys_and_attaches_port() -> None:
    data = {"broker": "192.0.2.10", "port": 1883, "password": "192.0.2.99", "api_key": "https://x.example",
            "nested": {"url": "http://h.example.com"}, "token": {"host": "192.0.2.98"}}
    out = collect.walk_hosts(data)
    assert {"key": "broker", "host": "192.0.2.10", "port": 1883} in out
    assert {"key": "nested.url", "host": "h.example.com", "port": None} in out
    assert "192.0.2.99" not in json.dumps(out) and "192.0.2.98" not in json.dumps(out)
    assert "x.example" not in json.dumps(out)


def test_refs_in_text_file_skips_credential_lines(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "app.conf"
    p.write_text('url = "tcp://192.0.2.10:1883"\npassword = "192.0.2.77"\n', encoding="utf-8")
    assert collect.refs_in_text_file(str(p)) == [{"host": "192.0.2.10", "port": 1883}]
    big = tmp_path / "big.conf"
    big.write_text("x" * 70000, encoding="utf-8")
    assert collect.refs_in_text_file(str(big)) == []


def test_refs_extracts_entities_and_services() -> None:
    obj = {"trigger": [{"entity_id": "binary_sensor.door"}], "action": [{"action": "notify.mobile_app_phone"},
           {"target": {"entity_id": ["light.hall", "light.unknown"]}}]}
    r = collect._refs(obj, {"binary_sensor.door", "light.hall"}, set())
    assert r["entities"] == ["binary_sensor.door", "light.hall"]
    assert r["services"] == ["notify.mobile_app_phone"]


def test_homeassistant_collector_sanitises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    st = tmp_path / ".storage"
    st.mkdir()
    (st / "core.config_entries").write_text(json.dumps({"data": {"entries": [
        {"entry_id": "1", "domain": "zha", "title": "", "data": {"device": {"path": "socket://192.0.2.50:6638"}}, "options": {}},
        {"entry_id": "2", "domain": "x", "title": "X", "data": {"host": "192.0.2.5", "password": "hunter2",
                                                                "token": "abc"}, "options": {}, "disabled_by": "user"},
    ]}}))
    (st / "core.device_registry").write_text(json.dumps({"data": {"devices": [
        {"id": "d1", "name": "Coord", "config_entry_id": "1", "via_device_id": None},
    ]}}))
    (st / "core.entity_registry").write_text(json.dumps({"data": {"entities": [
        {"entity_id": "sensor.a", "device_id": "d1", "config_entry_id": "1", "platform": "zha"},
    ]}}))
    (tmp_path / "automations.yaml").write_text(
        "- id: '1'\n  alias: Test\n  triggers:\n  - trigger: state\n    entity_id: sensor.a\n"
        "  actions:\n  - action: notify.mobile_app_phone\n    data:\n      message: !secret msg\n", encoding="utf-8")
    ha = collect.collect_homeassistant({"ha_config_dir": str(tmp_path)})
    text = json.dumps(ha)
    assert "hunter2" not in text and '"abc"' not in text
    assert ha["config_entries"][0]["hosts"] == [{"key": "data.device.path", "host": "192.0.2.50", "port": 6638}]
    assert ha["config_entries"][1]["disabled"] is True
    assert ha["devices"][0]["entries"] == ["1"]  # HA 2026 single config_entry_id format
    assert ha["automations"][0]["trigger_entities"] == ["sensor.a"]
    assert ha["automations"][0]["services"] == ["notify.mobile_app_phone"]


def test_collect_records_failing_sources(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def boom(cfg):  # type: ignore[no-untyped-def]
        raise RuntimeError("nope")
    monkeypatch.setitem(collect.SOURCES, "tailscale", boom)
    facts = collect.collect({"sources": ["tailscale"]})
    assert facts["sources"] == {"tailscale": "error"}
    assert facts["errors"][0]["source"] == "tailscale"


def test_main_writes_atomically(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    cfg = tmp_path / "config.yaml"
    out = tmp_path / "facts" / "facts.json"
    cfg.write_text(f"collector:\n  sources: [grafana]\n  grafana_datasources: {tmp_path}\n  output: {out.as_posix()}\n")
    (tmp_path / "ds.yaml").write_text("datasources:\n- name: P\n  type: prometheus\n  url: http://localhost:9090\n")
    assert collect.main(["--config", str(cfg)]) == 0
    data = json.loads(out.read_text())
    assert data["grafana"]["datasources"][0]["refs"] == [{"host": "localhost", "port": 9090}]
    assert not os.path.exists(str(out) + ".tmp")
