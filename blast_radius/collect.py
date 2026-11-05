"""Host-side collector.

Runs on the host (as root, inside a read-only systemd sandbox), reads configuration and runtime state
and writes ONE sanitized JSON file. It never writes anywhere else and never changes anything.

Design rules:
- Only standard library + PyYAML (available as python3-yaml on Debian), so it runs with the host Python.
- Secrets never leave this process: config entry data, container environments and URLs are reduced to
  host names / IP addresses. Keys that look like credentials are skipped entirely.
- Every source is independent; a failing source is recorded in ``errors`` and the rest still runs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import traceback
from typing import Any, Callable
from urllib.parse import urlsplit

import yaml

FACTS_VERSION = 1
SECRET_KEY_RE = re.compile(r"pass|token|secret|key|cred|auth|cookie|session|pin|otp|cert", re.I)
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s'\"]+", re.I)
HOSTPORT_RE = re.compile(r"^([a-z0-9][a-z0-9.-]*[a-z0-9]):(\d{2,5})$", re.I)
ENTITY_RE = re.compile(r"\b([a-z_][a-z0-9_]*)\.([a-z0-9_]+)\b")
HEX32_RE = re.compile(r"\b[0-9a-f]{32}\b")


# --------------------------------------------------------------------------- helpers

class _TolerantLoader(yaml.SafeLoader):
    """SafeLoader that accepts HA tags such as !secret / !include without resolving them."""


def _unknown_tag(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> Any:  # noqa: ARG001
    if isinstance(node, yaml.ScalarNode):
        return f"<{tag_suffix} {loader.construct_scalar(node)}>"
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TolerantLoader.add_multi_constructor("!", _unknown_tag)


def load_yaml(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.load(fh, Loader=_TolerantLoader)  # noqa: S506 - SafeLoader subclass


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def run(cmd: list[str], timeout: int = 30) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} exited {res.returncode}: {res.stderr.strip()[:300]}")
    return res.stdout


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def refs_in_value(value: str) -> list[dict[str, Any]]:
    """Return host references (host + optional port) in a string, without credentials, paths or queries."""
    found: list[tuple[str, int | None]] = []
    for url in URL_RE.findall(value):
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port
        except ValueError:
            host, port = None, None
        if host:
            found.append((host, port))
    stripped = URL_RE.sub(" ", value)
    for m in re.finditer(r"(?<![\d.])((?:\d{1,3}\.){3}\d{1,3})(?::(\d{2,5}))?(?![\d.])", stripped):
        if is_ip(m.group(1)):
            found.append((m.group(1), int(m.group(2)) if m.group(2) else None))
    m = HOSTPORT_RE.match(value.strip())
    if m and not found:
        found.append((m.group(1), int(m.group(2))))
    out: list[dict[str, Any]] = []
    for h, p in found:
        ref = {"host": h.lower(), "port": p}
        if ref not in out:
            out.append(ref)
    return out


def hosts_in_value(value: str) -> list[str]:
    out: list[str] = []
    for r in refs_in_value(value):
        if r["host"] not in out:
            out.append(r["host"])
    return out


def walk_hosts(obj: Any, path: str = "") -> list[dict[str, Any]]:
    """Walk a config structure and collect host references from non-secret keys.

    A numeric sibling key named ``port`` is attached to host references found in the same mapping.
    """
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        local: list[dict[str, Any]] = []
        for k, v in obj.items():
            if SECRET_KEY_RE.search(str(k)):
                continue
            sub = walk_hosts(v, f"{path}.{k}" if path else str(k))
            (local if isinstance(v, str) else out).extend(sub)
        port = obj.get("port")
        if isinstance(port, int) or (isinstance(port, str) and port.isdigit()):
            for r in local:
                r["port"] = r.get("port") or int(port)
        out = local + out
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += walk_hosts(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        for r in refs_in_value(obj):
            out.append({"key": path, **r})
    return out


def refs_in_text_file(path: str, max_bytes: int = 65536) -> list[dict[str, Any]]:
    """Host references in a small text config file; lines that mention credentials are skipped."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > max_bytes:
            return []
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        if SECRET_KEY_RE.search(line.split("=")[0].split(":")[0]):
            continue
        for r in refs_in_value(line):
            if r not in out:
                out.append(r)
    return out


# --------------------------------------------------------------------------- sources

def collect_host(cfg: dict) -> dict:
    facts: dict[str, Any] = {"hostname": socket.gethostname()}
    resolvers = []
    try:
        with open(cfg.get("resolv_conf", "/etc/resolv.conf"), encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "nameserver":
                    resolvers.append(parts[1])
    except OSError:
        pass
    facts["resolvers"] = resolvers
    routes = run(["ip", "-j", "route", "show", "default"])
    facts["default_routes"] = [
        {"gateway": r.get("gateway"), "dev": r.get("dev"), "dhcp": r.get("protocol") == "dhcp"}
        for r in json.loads(routes or "[]")
    ]
    addrs = json.loads(run(["ip", "-j", "addr"]) or "[]")
    facts["addresses"] = [
        {"ifname": a.get("ifname"), "ip": i.get("local")}
        for a in addrs for i in a.get("addr_info", [])
        if i.get("family") == "inet" and not str(i.get("local", "")).startswith("127.")
    ]
    mounts = json.loads(run(["findmnt", "-J", "-t", "nfs,nfs4,cifs,smb3", "-o", "TARGET,SOURCE,FSTYPE"]) or "{}")
    facts["network_mounts"] = [
        {"target": m["target"], "source": m["source"], "fstype": m["fstype"],
         "server": (hosts_in_value(m["source"].replace("//", "smb://", 1)) or [m["source"].split(":")[0]])[0]}
        for m in mounts.get("filesystems", [])
    ]
    facts["listeners"] = collect_listeners()
    facts["units"] = collect_units(cfg)
    return facts


def _owner_of_pid(pid: int) -> dict[str, str]:
    try:
        with open(f"/proc/{pid}/cgroup", encoding="utf-8") as fh:
            cg = fh.read()
    except OSError:
        return {}
    m = re.search(r"docker[-/]([0-9a-f]{12,64})", cg)
    if m:
        return {"container_id": m.group(1)[:12]}
    m = re.search(r"/([^/]+\.service)", cg)
    if m:
        return {"unit": m.group(1)}
    return {}


def collect_listeners() -> list[dict]:
    out = []
    seen = set()
    for line in run(["ss", "-ltnpH"]).splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        addr, _, port = local.rpartition(":")
        addr = addr.split("%")[0].strip("[]")
        procs = re.findall(r'\("([^"]+)",pid=(\d+)', line)
        owner: dict[str, str] = {}
        name = procs[0][0] if procs else ""
        for _pname, pid in procs:
            owner = _owner_of_pid(int(pid))
            if owner:
                break
        key = (port, owner.get("unit"), owner.get("container_id"), name)
        if key in seen:
            continue
        seen.add(key)
        out.append({"port": int(port), "addr": addr, "process": name, **owner})
    return out


def collect_units(cfg: dict) -> list[dict]:
    ignore = [re.compile(p) for p in cfg.get("ignore_units", [])]
    names = []
    for line in run(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend", "--plain"]).splitlines():
        unit = line.split()[0] if line.split() else ""
        if unit and not any(p.search(unit) for p in ignore):
            names.append(unit)
    units = []
    for unit in names:
        props = run(["systemctl", "show", unit, "-p", "Requires,BindsTo,Description"])
        p = dict(line.split("=", 1) for line in props.splitlines() if "=" in line)
        req = [u for u in (p.get("Requires", "") + " " + p.get("BindsTo", "")).split() if u.endswith(".service")]
        units.append({"unit": unit, "description": p.get("Description", ""), "requires": sorted(set(req))})
    return units


def collect_docker(cfg: dict) -> dict:
    ignore_env = cfg.get("ignore_env_vars", [])
    ids = run(["docker", "ps", "-aq", "--no-trunc"]).split()
    containers = []
    if ids:
        for c in json.loads(run(["docker", "inspect", *ids], timeout=60)):
            labels = c.get("Config", {}).get("Labels") or {}
            env_refs = []
            env = {}
            for item in c.get("Config", {}).get("Env") or []:
                var, _, val = item.partition("=")
                if SECRET_KEY_RE.search(var) or any(re.search(p, var) for p in ignore_env):
                    continue
                env[var] = val
            for var, val in env.items():
                for r in refs_in_value(val):
                    if r["port"] is None:
                        # MQTT_HOST + MQTT_PORT style pairs
                        stem = re.sub(r"_(HOST|HOSTNAME|ADDRESS|ADDR|SERVER|URL)$", "", var)
                        pv = env.get(stem + "_PORT", "")
                        r["port"] = int(pv) if pv.isdigit() else None
                    env_refs.append({"var": var, **r})
            file_refs = []
            for m in c.get("Mounts", []):
                if m.get("Type") == "bind":
                    for r in refs_in_text_file(m.get("Source", "")):
                        file_refs.append({"file": m.get("Destination", ""), **r})
            hc = c.get("HostConfig", {})
            dep = labels.get("com.docker.compose.depends_on", "")
            ports = []
            for spec, binds in (hc.get("PortBindings") or {}).items():
                for b in binds or []:
                    if b.get("HostPort"):
                        ports.append(int(b["HostPort"]))
            containers.append({
                "id": c["Id"][:12],
                "name": c["Name"].lstrip("/"),
                "image": c.get("Config", {}).get("Image", ""),
                "state": c.get("State", {}).get("Status", ""),
                "restart": (hc.get("RestartPolicy") or {}).get("Name", ""),
                "network_mode": hc.get("NetworkMode", ""),
                "networks": sorted((c.get("NetworkSettings", {}).get("Networks") or {}).keys()),
                "compose_project": labels.get("com.docker.compose.project", ""),
                "compose_service": labels.get("com.docker.compose.service", ""),
                "depends_on": sorted({d.split(":")[0] for d in dep.split(",") if d}),
                "env_refs": env_refs,
                "file_refs": file_refs,
                "host_ports": sorted(set(ports)),
                "mounts": [m.get("Source", "") for m in c.get("Mounts", []) if m.get("Type") == "bind"],
            })
    nets = []
    for n in json.loads(run(["docker", "network", "inspect", *run(["docker", "network", "ls", "-q"]).split()]) or "[]"):
        nets.append({"name": n["Name"], "driver": n.get("Driver", ""), "internal": n.get("Internal", False)})
    return {"containers": containers, "networks": nets}


def collect_tailscale(cfg: dict) -> dict:  # noqa: ARG001
    st = json.loads(run(["tailscale", "status", "--json"]))
    self_ = st.get("Self") or {}
    peers = [
        {"name": p.get("HostName", ""), "os": p.get("OS", ""), "online": bool(p.get("Online"))}
        for p in (st.get("Peer") or {}).values()
    ]
    return {
        "backend_state": st.get("BackendState", ""),
        "self": {"name": self_.get("HostName", ""), "online": bool(self_.get("Online"))},
        "magic_dns": bool((st.get("CurrentTailnet") or {}).get("MagicDNSEnabled")),
        "peers": peers,
    }


def collect_pihole(cfg: dict) -> dict:
    path = cfg.get("pihole_toml", "/etc/pihole/pihole.toml")
    section = ""
    dhcp_active = False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if s.startswith("["):
                section = s.strip("[]")
            elif section == "dhcp" and re.match(r"active\s*=", s):
                dhcp_active = s.split("=", 1)[1].strip().startswith("true")
    return {"dns": True, "dhcp_active": dhcp_active}


def collect_nginx(cfg: dict) -> dict:
    sites = []
    for path in sorted(glob.glob(os.path.join(cfg.get("nginx_sites", "/etc/nginx/sites-enabled"), "*"))):
        try:
            text = open(path, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        text = re.sub(r"#.*", "", text)
        listen = sorted({int(p) for p in re.findall(r"listen\s+(?:\[::\]:)?(?:[\d.]+:)?(\d+)", text)})
        roots = sorted(set(re.findall(r"\broot\s+([^;]+);", text)))
        ups = []
        for target in re.findall(r"proxy_pass\s+([^;]+);", text):
            m = re.match(r"https?://([^/:]+)(?::(\d+))?", target.strip())
            if m:
                ups.append({"host": m.group(1), "port": int(m.group(2) or 80)})
        uniq = {json.dumps(u, sort_keys=True) for u in ups}
        sites.append({"name": os.path.basename(path), "listen": listen, "roots": roots,
                      "upstreams": [json.loads(u) for u in sorted(uniq)]})
    return {"sites": sites}


def collect_prometheus(cfg: dict) -> dict:
    conf = load_yaml(cfg.get("prometheus_config", "/etc/prometheus/prometheus.yml")) or {}
    jobs = []
    for job in conf.get("scrape_configs") or []:
        targets = []
        for sc in job.get("static_configs") or []:
            targets += [str(t) for t in sc.get("targets") or []]
        jobs.append({"job": job.get("job_name", ""), "targets": targets})
    alert_targets = []
    for am in ((conf.get("alerting") or {}).get("alertmanagers") or []):
        for sc in am.get("static_configs") or []:
            alert_targets += [str(t) for t in sc.get("targets") or []]
    return {"jobs": jobs, "alertmanagers": alert_targets}


def collect_grafana(cfg: dict) -> dict:
    out = []
    for path in sorted(glob.glob(os.path.join(cfg.get("grafana_datasources", "/etc/grafana/provisioning/datasources"), "*.y*ml"))):
        conf = load_yaml(path) or {}
        for ds in conf.get("datasources") or []:
            url = str(ds.get("url", ""))
            out.append({"name": ds.get("name", ""), "type": ds.get("type", ""), "refs": refs_in_value(url)})
    return {"datasources": out}


def _refs(obj: Any, entity_ids: set[str], device_ids: set[str]) -> dict[str, list[str]]:
    text = json.dumps(obj, ensure_ascii=False)
    ents, services = set(), set()
    for dom, obj_id in ENTITY_RE.findall(text):
        full = f"{dom}.{obj_id}"
        if full in entity_ids:
            ents.add(full)
        elif dom in ("notify", "script", "tts", "media_player", "scene"):
            services.add(full)
    devs = {d for d in HEX32_RE.findall(text) if d in device_ids}
    return {"entities": sorted(ents), "services": sorted(services), "devices": sorted(devs)}


def collect_homeassistant(cfg: dict) -> dict:
    base = cfg.get("ha_config_dir", "/home/pi/homeassistant")
    st = os.path.join(base, ".storage")
    entries_raw = load_json(os.path.join(st, "core.config_entries"))["data"]["entries"]
    devices_raw = load_json(os.path.join(st, "core.device_registry"))["data"]["devices"]
    entities_raw = load_json(os.path.join(st, "core.entity_registry"))["data"]["entities"]
    areas = {}
    try:
        areas = {a["id"]: a["name"] for a in load_json(os.path.join(st, "core.area_registry"))["data"]["areas"]}
    except (OSError, KeyError):
        pass

    entries = []
    for e in entries_raw:
        hosts = walk_hosts(e.get("data") or {}, "data") + walk_hosts(e.get("options") or {}, "options")
        entries.append({
            "entry_id": e["entry_id"], "domain": e["domain"], "title": e.get("title", ""),
            "disabled": bool(e.get("disabled_by")), "hosts": hosts,
        })
    devices = []
    for d in devices_raw:
        devices.append({
            "id": d["id"], "name": d.get("name_by_user") or d.get("name") or d["id"],
            "manufacturer": d.get("manufacturer") or "", "model": d.get("model") or "",
            "via": d.get("via_device_id"),
            "entries": d.get("config_entries") or ([d["config_entry_id"]] if d.get("config_entry_id") else []),
            "composite": d.get("composite_device_id"),
            "primary_entry": d.get("primary_config_entry"),
            "area": areas.get(d.get("area_id") or "", ""), "disabled": bool(d.get("disabled_by")),
            "service": d.get("entry_type") == "service",
        })
    entities = []
    for e in entities_raw:
        entities.append({
            "entity_id": e["entity_id"], "device_id": e.get("device_id"), "entry_id": e.get("config_entry_id"),
            "platform": e.get("platform", ""), "name": e.get("name") or e.get("original_name") or "",
            "disabled": bool(e.get("disabled_by")), "hidden": bool(e.get("hidden_by")),
        })
    entity_ids = {e["entity_id"] for e in entities}
    device_ids = {d["id"] for d in devices}
    raw_by_id = {e["entry_id"]: e for e in entries_raw}
    for entry in entries:
        raw = raw_by_id[entry["entry_id"]]
        refs = _refs({"data": raw.get("data") or {}, "options": raw.get("options") or {}}, entity_ids, set())
        entry["entity_refs"] = refs["entities"]

    automations = []
    path = os.path.join(base, "automations.yaml")
    if os.path.exists(path):
        for a in load_yaml(path) or []:
            if not isinstance(a, dict):
                continue
            trig = a.get("triggers", a.get("trigger")) or []
            trig_refs = _refs(trig, entity_ids, device_ids)
            act_refs = _refs(a.get("actions", a.get("action")) or [], entity_ids, device_ids)
            automations.append({"id": str(a.get("id", a.get("alias", ""))), "alias": a.get("alias", ""),
                                "trigger_entities": trig_refs["entities"], "trigger_devices": trig_refs["devices"],
                                "action_entities": act_refs["entities"],
                                **_refs({k: v for k, v in a.items() if k not in ("alias", "description", "id")},
                                        entity_ids, device_ids)})
    scripts = []
    path = os.path.join(base, "scripts.yaml")
    if os.path.exists(path):
        for sid, s in (load_yaml(path) or {}).items():
            if isinstance(s, dict):
                refs = _refs(s.get("sequence", s), entity_ids, device_ids)
                scripts.append({"id": sid, "alias": s.get("alias", sid), "action_entities": refs["entities"], **refs})

    dashboards = []
    titles = {"lovelace": "Overview"}
    try:
        for item in load_json(os.path.join(st, "lovelace_dashboards"))["data"]["items"]:
            titles[f"lovelace.{item['id']}"] = item.get("title") or item.get("url_path")
    except (OSError, KeyError):
        pass
    for path in sorted(glob.glob(os.path.join(st, "lovelace*"))):
        name = os.path.basename(path)
        if name in ("lovelace_dashboards", "lovelace_resources") or ".bak" in name:
            continue
        try:
            conf = load_json(path)["data"].get("config")
        except (OSError, KeyError, ValueError):
            continue
        key = "lovelace" if name == "lovelace.lovelace" else name
        dashboards.append({"id": name, "title": titles.get(key, titles.get(name, name.split(".", 1)[-1])),
                           **_refs(conf, entity_ids, device_ids)})
    return {"config_entries": entries, "devices": devices, "entities": entities,
            "automations": automations, "scripts": scripts, "dashboards": dashboards}


SOURCES: dict[str, Callable[[dict], dict]] = {
    "host": collect_host,
    "docker": collect_docker,
    "tailscale": collect_tailscale,
    "pihole": collect_pihole,
    "nginx": collect_nginx,
    "prometheus": collect_prometheus,
    "grafana": collect_grafana,
    "homeassistant": collect_homeassistant,
}


def collect(cfg: dict) -> dict:
    enabled = cfg.get("sources") or list(SOURCES)
    facts: dict[str, Any] = {
        "version": FACTS_VERSION,
        "host_name": cfg.get("host_name") or socket.gethostname(),
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sources": {},
        "errors": [],
    }
    for name in enabled:
        fn = SOURCES.get(name)
        if fn is None:
            facts["errors"].append({"source": name, "error": "unknown source"})
            continue
        try:
            facts[name] = fn(cfg)
            facts["sources"][name] = "ok"
        except Exception as exc:  # noqa: BLE001 - keep collecting other sources
            facts["sources"][name] = "error"
            facts["errors"].append({"source": name, "error": f"{type(exc).__name__}: {exc}"[:500]})
            traceback.print_exc(file=sys.stderr)
    return facts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Blast-Radius host collector (read-only)")
    ap.add_argument("--config", default="/opt/blast-radius/config/config.yaml")
    ap.add_argument("--out", default=None, help="output file (default from config collector.output)")
    args = ap.parse_args(argv)
    conf = load_yaml(args.config) or {}
    cfg = conf.get("collector") or {}
    out = args.out or cfg.get("output", "/var/lib/blast-radius/facts/facts.json")
    facts = collect(cfg)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(facts, fh, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, out)
    ok = sum(1 for v in facts["sources"].values() if v == "ok")
    print(f"blast-radius-collect: {ok}/{len(facts['sources'])} sources ok -> {out}")
    for e in facts["errors"]:
        print(f"  error {e['source']}: {e['error']}", file=sys.stderr)
    return 0 if not facts["errors"] else 2


if __name__ == "__main__":
    sys.exit(main())
