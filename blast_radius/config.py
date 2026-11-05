"""Configuration loading with defaults. See config/config.example.yaml for the documented file."""
from __future__ import annotations

import copy
import os
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "collector": {
        "output": "/var/lib/blast-radius/facts/facts.json",
    },
    "engine": {
        "facts_files": ["/data/facts/facts.json"],
        "overrides_file": "/config/overrides.yaml",
        "data_dir": "/data/state",
        "check_interval_seconds": 60,
        "facts_max_age_hours": 26,
        "shrink_alert_ratio": 0.2,
        "shrink_type_ratio": 0.5,
        "shrink_type_min_nodes": 10,
        "auto_accept_after": 3,
        "keep_snapshots": 30,
        "keep_changes_days": 365,
    },
    "analysis": {
        "spof_threshold": 0.5,
    },
    "homeassistant": {
        "url": "http://homeassistant.local:8123",
        "token_env": "HA_TOKEN",
        "notify_service": "",
        "status_sensor": "sensor.blast_radius",
        "poll_seconds": 60,
        "timeout_seconds": 15,
        "startup_grace_seconds": 600,
        "mass_outage": {
            "min_entities": 5,
            "window_seconds": 300,
            "cooldown_minutes": 60,
        },
    },
    "web": {
        "port": 8098,
        "public_url": "",
    },
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None = None) -> dict[str, Any]:
    path = path or os.environ.get("BLAST_RADIUS_CONFIG", "/config/config.yaml")
    data: dict[str, Any] = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    cfg = _merge(DEFAULTS, data)
    cfg["_path"] = path
    return cfg


def load_overrides(path: str) -> dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
