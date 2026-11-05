"""Build the dependency graph from collector facts plus the hand-maintained overrides file."""
from __future__ import annotations

import fnmatch
import ipaddress
import re
from typing import Any

from .model import Graph

FRIENDLY_UNITS = {
    "grafana-server": "Grafana", "prometheus": "Prometheus", "pihole-FTL": "Pi-hole (DNS)",
    "mosquitto": "Mosquitto (MQTT broker)", "tailscaled": "Tailscale daemon", "nginx": "nginx (web server)",
    "docker": "Docker", "containerd": "containerd", "prometheus-node-exporter": "Node exporter",
    "ssh": "SSH server", "collectd": "collectd", "fail2ban": "fail2ban",
}
FRIENDLY_IMAGES = {
    "home-assistant": "Home Assistant", "uptime-kuma": "Uptime Kuma", "influxdb": "InfluxDB",
    "grafana": "Grafana", "prometheus": "Prometheus", "mosquitto": "Mosquitto (MQTT broker)",
}
FRIENDLY_DOMAINS = {
    "zha": "Zigbee (ZHA)", "tplink": "TP-Link/Tapo", "fritz": "FRITZ!Box Tools", "growatt_server": "Growatt",
    "mqtt": "MQTT", "mobile_app": "Mobile app", "alexa_media": "Alexa Media Player", "sonos": "Sonos",
    "spotify": "Spotify", "tuya": "Tuya", "vesync": "VeSync", "bambu_lab": "Bambu Lab", "met": "Met.no weather",
    "utility_meter": "Utility meter", "template": "Template", "integration": "Integral sensor",
    "google_translate": "Google Translate TTS", "radio_browser": "Radio Browser", "backup": "Backup",
    "sun": "Sun", "shopping_list": "Shopping list", "go2rtc": "go2rtc", "analytics": "Analytics",
}
BUILTIN_NETWORKS = {"internet", "lan", "dns", "dhcp", "tailnet"}
STATE_HELPER_DOMAINS = {"input_select", "input_boolean", "input_number", "input_text", "input_datetime",
                        "counter", "timer", "var"}
LOCAL_NAMES = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"}
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".fritz.box", ".internal", ".localdomain", ".ts.net")


def as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def slug(text: str) -> str:
    text = text.lower().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def registered_domain(host: str) -> str:
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_private or a.is_link_local


class Builder:
    def __init__(self, facts_list: list[dict], overrides: dict | None, config: dict | None = None) -> None:
        self.facts_list = facts_list
        self.ov = overrides or {}
        self.cfg = config or {}
        self.g = Graph()
        self.ip_index: dict[str, str] = {}
        self.port_index: dict[str, dict[int, str]] = {}  # host node id -> port -> node id
        self.host_names: dict[str, str] = {}  # lower name -> host node id
        self.containers: dict[str, dict[str, str]] = {}  # host node id -> container name -> node id
        self.cloud_hosts: dict[str, str] = {}
        self.ha_runtime: str | None = None
        self.ha_host: str | None = None
        self.main_host: str | None = None

    # ------------------------------------------------------------------ helpers
    def warn(self, msg: str) -> None:
        if msg not in self.g.warnings:
            self.g.warnings.append(msg)

    def node(self, *args: Any, **kw: Any) -> str:
        return self.g.add_node(*args, **kw).id

    def edge(self, src: str | None, dst: str | None, kind: str = "needs", **kw: Any) -> None:
        if src and dst:
            self.g.add_edge(src, dst, kind, **kw)

    def cloud(self, host: str) -> str:
        host = host.lower()
        if host in self.cloud_hosts:
            return self.cloud_hosts[host]
        dom = registered_domain(host)
        if dom in self.cloud_hosts:
            return self.cloud_hosts[dom]
        nid = f"cloud:{dom}"
        if not self.g.has(nid):
            self.node(nid, "cloud", f"{dom} (cloud)", attrs={"hosts": [host]})
            self.edge(nid, "net:internet")
            self.edge(nid, "net:dns")
        else:
            hosts = self.g.nodes[nid].attrs.setdefault("hosts", [])
            if host not in hosts:
                hosts.append(host)
        self.cloud_hosts[host] = nid
        return nid

    def resolve_endpoint(self, ctx_host: str | None, host: str, port: int | None) -> str | None:
        """Map a host[:port] reference seen on ``ctx_host`` to a graph node."""
        host = (host or "").lower().strip("[]")
        if not host:
            return None
        target_host: str | None = None
        if host in LOCAL_NAMES:
            if host == "0.0.0.0":
                return None
            target_host = ctx_host
        elif host in self.host_names:
            target_host = self.host_names[host]
        elif ctx_host and host in self.containers.get(ctx_host, {}):
            return self.containers[ctx_host][host]
        elif host in self.ip_index:
            nid = self.ip_index[host]
            if self.g.nodes[nid].type == "host":
                target_host = nid
            else:
                return nid
        if target_host:
            if port and port in self.port_index.get(target_host, {}):
                return self.port_index[target_host][port]
            return target_host
        try:
            ipaddress.ip_address(host)
            return "net:lan" if is_private(host) else "net:internet"
        except ValueError:
            pass
        if "." in host and not host.endswith(LOCAL_SUFFIXES):
            return self.cloud(host)
        short = host.split(".")[0]
        if short in self.host_names:
            return self.host_names[short]
        return None

    # ------------------------------------------------------------------ overrides: base nodes
    def _override_nodes(self) -> None:
        ov = self.ov
        for key, spec in (ov.get("hardware") or {}).items():
            spec = spec or {}
            nid = f"hw:{key}"
            attrs = {k: v for k, v in spec.items() if k in ("note", "model", "location", "planned", "spof")}
            label = spec.get("label", key) + (" (planned)" if spec.get("planned") else "")
            self.node(nid, "hardware", label, attrs=attrs, aliases=spec.get("aliases", []), source="override")
            for ip in as_list(spec.get("ips")):
                self.ip_index[str(ip).lower()] = nid
        for key, spec in (ov.get("cloud") or {}).items():
            spec = spec or {}
            nid = f"cloud:{key}"
            self.node(nid, "cloud", spec.get("label", key), attrs={"note": spec.get("note", "")},
                      aliases=spec.get("aliases", []), source="override")
            for h in spec.get("hosts", []) or []:
                self.cloud_hosts[str(h).lower()] = nid
        for key, spec in (ov.get("networks") or {}).items():
            spec = spec or {}
            if key in BUILTIN_NETWORKS and "label" not in spec:
                continue  # only adds dependencies to a built-in network node
            self.node(f"net:{key}", "network", spec.get("label", key), attrs={"note": spec.get("note", "")},
                      aliases=spec.get("aliases", []), source="override")
        for key, spec in (ov.get("hosts") or {}).items():
            spec = spec or {}
            nid = f"host:{key}"
            label = spec.get("label", key) + (" (planned)" if spec.get("planned") else "")
            self.node(nid, "host", label, attrs={k: v for k, v in spec.items() if k in ("note", "planned", "spof")},
                      aliases=spec.get("aliases", []), source="override")
            self.host_names[key.lower()] = nid
            for ip in spec.get("ips", []) or []:
                self.ip_index[str(ip).lower()] = nid
        for key, spec in (ov.get("services") or {}).items():
            spec = spec or {}
            self.node(f"svc:{key}", "service", spec.get("label", key), attrs={"note": spec.get("note", "")},
                      aliases=spec.get("aliases", []), source="override")

    def _base_networks(self) -> None:
        g = self.g
        if not g.has("net:internet"):
            self.node("net:internet", "network", "Internet", aliases=["internet", "online"])
        if not g.has("net:lan"):
            self.node("net:lan", "network", "Home network (LAN / Wi-Fi)", aliases=["lan", "wlan", "wifi", "heimnetz", "netzwerk"])
        if not g.has("net:dns"):
            self.node("net:dns", "network", "DNS (name resolution)", aliases=["dns", "name resolution"])
        if not g.has("net:dhcp"):
            self.node("net:dhcp", "network", "DHCP (address assignment)", aliases=["dhcp"])
        self.edge("net:lan", "net:dhcp")
        internet_providers = set()
        for ref in as_list(((self.ov.get("networks") or {}).get("internet") or {}).get("needs")):
            internet_providers.add(str(ref))
        for n in g.of_type("cloud"):
            if n.id not in internet_providers and not n.attrs.get("offline"):
                self.edge(n.id, "net:internet")
                self.edge(n.id, "net:dns")

    # ------------------------------------------------------------------ host facts
    def _host(self, facts: dict) -> str:
        hf = facts.get("host") or {}
        name = str(facts.get("host_name") or hf.get("hostname") or "host")
        key = name.lower()
        hosts_ov = self.ov.get("hosts") or {}
        ov_key = next((k for k, v in hosts_ov.items() if k.lower() == key or key in [str(a).lower() for a in (v or {}).get("aliases", [])]), None)
        nid = f"host:{ov_key or key}"
        if not self.g.has(nid):
            self.node(nid, "host", name, aliases=[name])
        self.host_names[key] = nid
        self.host_names[name.lower()] = nid
        for a in hf.get("addresses", []):
            if a.get("ip"):
                self.ip_index[a["ip"]] = nid
        self.port_index.setdefault(nid, {})
        self.containers.setdefault(nid, {})
        return nid

    def _host_network(self, host: str, facts: dict) -> None:
        hf = facts.get("host") or {}
        if not hf:
            return
        gw_ip = next((r.get("gateway") for r in hf.get("default_routes", []) if r.get("gateway")), None)
        if gw_ip:
            gw = self.ip_index.get(gw_ip)
            if gw is None:
                gw = self.node("hw:gateway", "hardware", f"Router / gateway {gw_ip}", aliases=["router", "gateway"])
                self.ip_index[gw_ip] = gw
            self.g.nodes[gw].attrs["gateway_for"] = sorted(set(self.g.nodes[gw].attrs.get("gateway_for", []) + [host]))
            self.edge("net:lan", gw, via=f"default gateway of {host}")
            self.edge("net:internet", gw, via=f"default gateway of {host}")
            dhcp_on_host = bool((facts.get("pihole") or {}).get("dhcp_active"))
            if any(r.get("dhcp") for r in hf.get("default_routes", [])) and not dhcp_on_host:
                self.edge("net:dhcp", gw, via=f"{host} gets its address via DHCP from {gw_ip}")
        # DNS resolvers of this host: alternatives
        resolvers = []
        for ip in hf.get("resolvers", []):
            n = self.resolve_endpoint(host, ip, 53)
            if n and n not in ("net:lan", "net:internet") and n not in resolvers:
                resolvers.append(n)
            elif n is None or n in ("net:lan",):
                self.warn(f"DNS resolver {ip} of {host} is not mapped to a node (add it to 'ips' of a hardware entry in overrides)")
        for n in resolvers:
            self.edge("net:dns", n, group=f"resolvers:{host}", via=f"resolv.conf of {host}")
        self.g.nodes["net:dns"].attrs["resolvers"] = hf.get("resolvers", [])
        # the host itself sits in the LAN; mounts
        for m in hf.get("network_mounts", []):
            target = self.resolve_endpoint(host, m.get("server", ""), None)
            if target:
                self.g.nodes[host].attrs.setdefault("network_mounts", []).append(m["target"])
                self._mounts.append((host, m["target"], target))

    def _units(self, host: str, facts: dict) -> None:
        hf = facts.get("host") or {}
        by_unit: dict[str, str] = {}
        for u in hf.get("units", []):
            unit = u["unit"]
            short = unit.removesuffix(".service")
            label = FRIENDLY_UNITS.get(short) or (u.get("description") if 0 < len(u.get("description", "")) <= 40 else short)
            nid = f"svc:{host.split(':', 1)[1]}:{short}"
            self.node(nid, "service", label, attrs={"unit": unit, "host": host}, aliases=[short])
            self.edge(nid, host, "runs_on")
            by_unit[unit] = nid
        for u in hf.get("units", []):
            for req in u.get("requires", []):
                self.edge(by_unit.get(u["unit"]), by_unit.get(req), via="systemd Requires")
        self._unit_nodes[host] = by_unit
        for ls in hf.get("listeners", []):
            unit = ls.get("unit")
            if unit and unit in by_unit and unit != "docker.service":
                self.port_index[host].setdefault(ls["port"], by_unit[unit])

    def _docker(self, host: str, facts: dict) -> None:
        dk = facts.get("docker")
        if not dk:
            return
        hname = host.split(":", 1)[1]
        units = self._unit_nodes.get(host, {})
        docker = units.get("docker.service")
        if docker is None:
            docker = self.node(f"svc:{hname}:docker", "service", "Docker", attrs={"host": host})
            self.edge(docker, host, "runs_on")
        by_id: dict[str, str] = {}
        by_service: dict[tuple[str, str], str] = {}
        skipped = []
        for c in dk.get("containers", []):
            if c.get("state") not in ("running", "restarting"):
                skipped.append(c["name"])
                continue
            img = c.get("image", "")
            friendly = next((v for k, v in FRIENDLY_IMAGES.items() if k in img), None)
            label = f"{friendly} (container {c['name']})" if friendly and slug(friendly) != slug(c["name"]) else f"{c['name']} (container)"
            nid = f"ctr:{hname}:{c['name']}"
            self.node(nid, "service", label, attrs={"image": img, "container": c["name"], "host": host,
                                                    "network_mode": c.get("network_mode", ""),
                                                    "restart": c.get("restart", "")},
                      aliases=[c["name"]] + ([friendly] if friendly else []))
            self.edge(nid, docker, "runs_on")
            by_id[c["id"][:12]] = nid
            self.containers[host][c["name"].lower()] = nid
            if c.get("compose_service"):
                by_service[(c.get("compose_project", ""), c["compose_service"])] = nid
                self.containers[host].setdefault(c["compose_service"].lower(), nid)
            for p in c.get("host_ports", []):
                self.port_index[host][p] = nid
            for net in c.get("networks", []):
                if net in ("bridge", "host", "none") or c.get("network_mode") == "host":
                    continue
                net_id = f"net:{hname}:docker:{net}"
                self.node(net_id, "network", f"Docker network {net}", attrs={"host": host})
                self.edge(net_id, docker, "runs_on")
                self.edge(nid, net_id)
            if friendly == "Home Assistant" and self.ha_runtime is None:
                self.ha_runtime, self.ha_host = nid, host
        for ls in (facts.get("host") or {}).get("listeners", []):
            cid = ls.get("container_id")
            if cid and cid in by_id:
                self.port_index[host].setdefault(ls["port"], by_id[cid])
        self._container_facts.append((host, dk.get("containers", []), by_service))
        if skipped:
            self.g.meta.setdefault("stopped_containers", {})[hname] = skipped

    def _container_refs(self) -> None:
        for host, containers, by_service in self._container_facts:
            hname = host.split(":", 1)[1]
            for c in containers:
                nid = f"ctr:{hname}:{c['name']}"
                if not self.g.has(nid):
                    continue
                for dep in c.get("depends_on", []):
                    self.edge(nid, by_service.get((c.get("compose_project", ""), dep)), via="compose depends_on")
                own_net = c.get("network_mode") != "host"
                for r in c.get("env_refs", []):
                    if (own_net and r["host"] in LOCAL_NAMES) or "LISTEN" in r["var"].upper():
                        continue  # loopback inside a bridged container is the container itself
                    target = self.resolve_endpoint(host, r["host"], r.get("port"))
                    if target and target != nid:
                        self.edge(nid, target, via=f"env {r['var']}")
                for r in c.get("file_refs", []):
                    if own_net and r["host"] in LOCAL_NAMES:
                        continue
                    target = self.resolve_endpoint(host, r["host"], r.get("port"))
                    if target and target != nid:
                        self.edge(nid, target, via=f"config file {r.get('file', '')}")

    def _nginx(self, host: str, facts: dict) -> None:
        ng = facts.get("nginx")
        if not ng:
            return
        hname = host.split(":", 1)[1]
        nginx = self._unit_nodes.get(host, {}).get("nginx.service")
        for site in ng.get("sites", []):
            port = (site.get("listen") or [None])[0]
            nid = f"site:{hname}:{site['name']}"
            label = f"Website {site['name']}" + (f" :{port}" if port else "")
            self.node(nid, "service", label, attrs={"listen": site.get("listen", []), "roots": site.get("roots", []),
                                                    "host": host}, aliases=[site["name"]])
            self.edge(nid, nginx, "runs_on")
            for p in site.get("listen", []):
                self.port_index[host][p] = nid
            self._sites.append((host, nid, site))

    def _site_refs(self) -> None:
        for host, nid, site in self._sites:
            for up in site.get("upstreams", []):
                target = self.resolve_endpoint(host, up["host"], up.get("port"))
                if target and target != nid:
                    self.edge(nid, target, soft=True, via=f"proxy_pass {up['host']}:{up.get('port')}")
            for root in site.get("roots", []):
                for mhost, mtarget, server in self._mounts:
                    if mhost == host and root.startswith(mtarget):
                        self.edge(nid, server, via=f"web root {root} is on network mount {mtarget}")

    def _prometheus_grafana(self, host: str, facts: dict) -> None:
        units = self._unit_nodes.get(host, {})
        hname = host.split(":", 1)[1]
        prom = units.get("prometheus.service") or self.containers.get(host, {}).get("prometheus")
        if prom and facts.get("prometheus"):
            for job in facts["prometheus"].get("jobs", []):
                for t in job.get("targets", []):
                    h, _, p = t.rpartition(":")
                    target = self.resolve_endpoint(host, h or t, int(p) if p.isdigit() else None)
                    if target and target != prom:
                        self.edge(prom, target, soft=True, via=f"scrape job {job.get('job')}")
        graf = units.get("grafana-server.service") or self.containers.get(host, {}).get("grafana")
        if graf and facts.get("grafana"):
            for ds in facts["grafana"].get("datasources", []):
                for r in ds.get("refs", []):
                    target = self.resolve_endpoint(host, r["host"], r.get("port"))
                    if target and target != graf:
                        self.edge(graf, target, soft=True, via=f"datasource {ds.get('name')}")
        del hname

    def _tailscale(self, host: str, facts: dict) -> None:
        ts = facts.get("tailscale")
        if not ts:
            return
        hname = host.split(":", 1)[1]
        tsd = self._unit_nodes.get(host, {}).get("tailscaled.service")
        if not self.g.has("net:tailnet"):
            self.node("net:tailnet", "network", "Tailscale remote access",
                      aliases=["tailscale", "tailnet", "fernzugriff", "remote access", "vpn"])
        coord = self.cloud_hosts.get("controlplane.tailscale.com") or self.cloud("controlplane.tailscale.com")
        self.edge("net:tailnet", coord, via="Tailscale coordination server")
        self.edge("net:tailnet", tsd or host, via=f"tailscaled on {hname}")
        self.edge("net:tailnet", "net:internet")
        attrs = self.g.nodes["net:tailnet"].attrs
        attrs["peers"] = sorted({p.get("name", "") for p in ts.get("peers", [])} | set(attrs.get("peers", [])))
        attrs["backend_state"] = ts.get("backend_state", "")

    # ------------------------------------------------------------------ Home Assistant
    def _homeassistant(self, facts: dict, host: str) -> None:
        ha = facts.get("homeassistant")
        if not ha:
            return
        rt = self.ha_runtime
        if rt is None:
            rt = self.node("svc:homeassistant", "service", "Home Assistant", aliases=["home assistant", "ha"])
            self.edge(rt, host, "runs_on")
            self.ha_runtime, self.ha_host = rt, host
        self.g.nodes[rt].aliases.extend(a for a in ["home assistant", "ha", "hass"] if a not in self.g.nodes[rt].aliases)
        ctx = self.ha_host
        dom_cfg = self.ov.get("integrations") or {}
        entries = {}
        mobile_by_slug = {}
        for e in ha.get("config_entries", []):
            if e.get("disabled"):
                continue
            dom = e["domain"]
            nid = f"int:{e['entry_id']}"
            title = e.get("title") or ""
            dlabel = FRIENDLY_DOMAINS.get(dom, dom)
            label = dlabel if not title or slug(title) == slug(dlabel) or len(title) > 40 else f"{dlabel}: {title}"
            self.node(nid, "integration", label, attrs={"domain": dom, "title": title},
                      aliases=[dom, dlabel] + ([title] if title and len(title) <= 40 else []))
            self.edge(nid, rt, "runs_on")
            entries[e["entry_id"]] = nid
            for r in e.get("hosts", []):
                target = self.resolve_endpoint(ctx, r["host"], r.get("port"))
                if target and target != nid:
                    self.edge(nid, target, via=f"{r.get('key', 'config')} = {r['host']}" + (f":{r['port']}" if r.get("port") else ""))
            for extra in (dom_cfg.get(dom) or []):
                for target in self.resolve_ref(extra, f"integrations.{dom}"):
                    self.edge(nid, target, via="override (integrations)")
            if dom == "mobile_app":
                mobile_by_slug[slug(title)] = nid
            self._entry_entity_refs.append((nid, e.get("entity_refs", [])))
        devices = {}
        for d in ha.get("devices", []):
            if d.get("disabled"):
                continue
            nid = f"dev:{d['id']}"
            attrs = {k: d.get(k) for k in ("manufacturer", "model", "area") if d.get(k)}
            ents = [entries[x] for x in d.get("entries", []) if x in entries]
            attrs["domains"] = sorted({self.g.nodes[x].attrs["domain"] for x in ents})
            self.node(nid, "device", d["name"], attrs=attrs, aliases=[d["name"]])
            devices[d["id"]] = nid
            if len(ents) == 1:
                self.edge(nid, ents[0])
            else:
                for x in ents:
                    self.edge(nid, x, soft=True, via="one of several integrations")
            if not ents:
                self.edge(nid, rt, "runs_on")
        for d in ha.get("devices", []):
            if d.get("via") and d["id"] in devices and d["via"] in devices:
                child, parent = self.g.nodes[devices[d["id"]]], self.g.nodes[devices[d["via"]]]
                if set(child.attrs.get("domains", [])) & set(parent.attrs.get("domains", [])):
                    self.edge(child.id, parent.id, "controlled_by", via="HA via_device")
                else:
                    # e.g. a Wi-Fi plug "connected via" the router: topology only, not control
                    child.attrs["connected_via"] = parent.label
        for e in ha.get("entities", []):
            if e.get("disabled"):
                continue
            eid = e["entity_id"]
            nid = f"entity:{eid}"
            self.node(nid, "entity", e.get("name") or eid,
                      attrs={"entity_id": eid, "platform": e.get("platform", "")}, aliases=[eid])
            dev = devices.get(e.get("device_id") or "")
            ent = entries.get(e.get("entry_id") or "")
            if dev:
                self.edge(nid, dev)
            if ent:
                self.edge(nid, ent)
            if not dev and not ent:
                self.edge(nid, rt, "runs_on")
        # helpers (template, utility_meter, ...) use other entities
        for int_id, refs in self._entry_entity_refs:
            for r in refs:
                self.edge(int_id, f"entity:{r}", soft=True, via="source entity")

        def link_refs(nid: str, item: dict) -> None:
            for r in item.get("entities", []):
                if r in item.get("trigger_entities", []):
                    continue
                self.edge(nid, f"entity:{r}", soft=True, via="uses entity")
            for r in item.get("devices", []):
                if r in item.get("trigger_devices", []):
                    continue
                self.edge(nid, devices.get(r), soft=True, via="uses device")
            for r in item.get("trigger_entities", []):
                self.edge(nid, f"entity:{r}", group="trigger", via="trigger")
            for r in item.get("trigger_devices", []):
                self.edge(nid, devices.get(r), group="trigger", via="trigger")
            for r in item.get("action_entities", []):
                if r.split(".")[0] in STATE_HELPER_DOMAINS:
                    # a helper whose value is written by this automation/script depends on it
                    self.edge(f"entity:{r}", nid, weak=True, via="value is set by this")
            for svc in item.get("services", []):
                dom, _, obj = svc.partition(".")
                if dom == "notify" and obj.startswith("mobile_app_"):
                    self.edge(nid, mobile_by_slug.get(obj.removeprefix("mobile_app_")), soft=True, via=svc)
                elif dom == "script":
                    self.edge(nid, f"script:{obj}", soft=True, via=svc)

        for s in ha.get("scripts", []):
            self.node(f"script:{s['id']}", "script", s.get("alias") or s["id"], aliases=[s["id"]])
        for a in ha.get("automations", []):
            nid = f"auto:{a['id']}"
            self.node(nid, "automation", a.get("alias") or a["id"], aliases=[a["id"]])
            self.edge(nid, rt, "runs_on")
            link_refs(nid, a)
        for s in ha.get("scripts", []):
            nid = f"script:{s['id']}"
            self.edge(nid, rt, "runs_on")
            link_refs(nid, s)
        for d in ha.get("dashboards", []):
            nid = f"dash:{d['id']}"
            self.node(nid, "dashboard", f"Dashboard {d.get('title') or d['id']}", aliases=[d.get("title") or d["id"]])
            self.edge(nid, rt, "runs_on")
            link_refs(nid, d)

    # ------------------------------------------------------------------ reference resolution
    def resolve_ref(self, ref: Any, where: str = "") -> list[str]:
        """Resolve a reference written in the overrides file to node ids (see overrides.example.yaml)."""
        ref = str(ref).strip()
        g = self.g
        out: list[str] = []
        if g.has(ref):
            return [ref]
        kind, _, val = ref.partition(":")
        low = val.lower()
        if val:
            if kind == "container":
                out = [nid for h in self.containers.values() for n, nid in h.items() if n == low]
            elif kind == "unit":
                u = low.removesuffix(".service")
                out = [n.id for n in g.nodes.values() if n.id.startswith("svc:") and n.attrs.get("unit", "").lower() == f"{u}.service"]
            elif kind == "site":
                out = [n.id for n in g.nodes.values() if n.id.startswith("site:") and n.id.split(":")[-1].lower() == low]
            elif kind == "integration":
                out = [n.id for n in g.of_type("integration") if n.attrs.get("domain") == low or n.label.lower() == low]
            elif kind == "device":
                name, _, dom = low.rpartition("@") if "@" in low else (low, "", "")
                out = [n.id for n in g.of_type("device") if n.label.lower().strip() == name.strip()
                       and (not dom or dom in n.attrs.get("domains", []))]
            elif kind == "automation":
                out = [n.id for n in g.of_type("automation") if n.label.lower() == low or n.id == f"auto:{val}"]
            elif kind == "script":
                out = [n.id for n in g.of_type("script") if n.label.lower() == low or n.id == f"script:{val}"]
            elif kind == "dashboard":
                out = [n.id for n in g.of_type("dashboard") if n.label.lower() in (low, f"dashboard {low}")]
            elif kind == "port":
                host = self.main_host
                p = int(val) if val.isdigit() else -1
                out = [self.port_index.get(host or "", {}).get(p)] if p in self.port_index.get(host or "", {}) else []
            elif kind in ("hw", "cloud", "net", "host", "svc", "fn", "entity", "int", "dev", "auto", "ctr", "site"):
                out = [ref] if g.has(ref) else []
        if not out and re.match(r"^[a-z_]+\.[a-z0-9_*?]+$", ref):
            if any(ch in ref for ch in "*?"):
                out = sorted(n.id for n in g.of_type("entity") if fnmatch.fnmatch(n.attrs.get("entity_id", ""), ref))
            elif g.has(f"entity:{ref}"):
                out = [f"entity:{ref}"]
        if not out and not val:
            for prefix in ("hw", "net", "cloud", "host", "svc", "fn"):
                if g.has(f"{prefix}:{ref}"):
                    out = [f"{prefix}:{ref}"]
                    break
        if not out:
            out = [n.id for n in g.nodes.values() if n.label.lower() == ref.lower() and n.type != "entity"]
        if not out:
            self.warn(f"override reference '{ref}' ({where}) matches nothing")
        return out

    def _override_edges(self) -> None:
        ov = self.ov

        def deps(nid: str, spec: dict, where: str) -> None:
            if not self.g.has(nid):
                return
            node = self.g.nodes[nid]
            if spec.get("say"):
                node.attrs["say"] = str(spec["say"])  # spoken name for voice assistants
            for a in as_list(spec.get("aliases")):
                if a not in node.aliases:
                    node.aliases.append(str(a))
            for key, kind in (("runs_on", "runs_on"), ("needs", "needs"), ("powered_by", "powered_by"),
                              ("controlled_by", "controlled_by")):
                vals = spec.get(key)
                if vals is None:
                    continue
                for ref in (vals if isinstance(vals, list) else [vals]):
                    for t in self.resolve_ref(ref, f"{where}.{key}"):
                        self.edge(nid, t, kind, via="override")
            for ref in as_list(spec.get("uses")):
                for t in self.resolve_ref(ref, f"{where}.uses"):
                    self.edge(nid, t, soft=True, via="override")
            any_of = as_list(spec.get("any_of"))
            if any_of and not any(isinstance(x, list) for x in any_of):
                any_of = [any_of]  # a flat list is ONE group of alternatives
            for i, group in enumerate(any_of):
                for ref in (group if isinstance(group, list) else [group]):
                    for t in self.resolve_ref(ref, f"{where}.any_of"):
                        self.edge(nid, t, group=f"any_of_{i}", via="override (alternatives)")
            if spec.get("ha_device"):
                for t in self.resolve_ref(f"device:{spec['ha_device']}", f"{where}.ha_device"):
                    self.edge(t, nid, via="override: HA device is this hardware")
                    self.g.nodes[t].aliases.extend(a for a in self.g.nodes[nid].aliases if a not in self.g.nodes[t].aliases)

        for section, prefix in (("hardware", "hw"), ("cloud", "cloud"), ("networks", "net"), ("hosts", "host"),
                                ("services", "svc")):
            for key, spec in (ov.get(section) or {}).items():
                deps(f"{prefix}:{key}", spec or {}, f"{section}.{key}")
        for e in ov.get("edges", []) or []:
            srcs = self.resolve_ref(e.get("from"), "edges.from")
            dsts = self.resolve_ref(e.get("to"), "edges.to")
            for s in srcs:
                for d in dsts:
                    self.edge(s, d, e.get("kind", "needs"), soft=bool(e.get("soft")), group=e.get("group"),
                              weak=bool(e.get("weak")), via=e.get("note", "override"))
        for e in ov.get("remove_edges", []) or []:
            for s in self.resolve_ref(e.get("from"), "remove_edges.from"):
                for d in self.resolve_ref(e.get("to"), "remove_edges.to"):
                    self.g.remove_edge(s, d, e.get("kind"))
        for ref, names in (ov.get("aliases") or {}).items():
            for t in self.resolve_ref(ref, "aliases"):
                self.g.nodes[t].aliases.extend(str(n) for n in names)
        for key, spec in (ov.get("functions") or {}).items():
            spec = spec or {}
            nid = f"fn:{key}"
            self.node(nid, "function", spec.get("label", key), attrs={"note": spec.get("note", "")},
                      aliases=spec.get("aliases", []), source="override")
            deps(nid, spec, f"functions.{key}")
            if not self.g.deps.get(nid):
                self.warn(f"function '{key}' has no resolvable dependencies")
        for ref in ov.get("ignore", []) or []:
            for t in self.resolve_ref(ref, "ignore"):
                self.g.remove_node(t)

    # ------------------------------------------------------------------ main
    def build(self) -> Graph:
        self._mounts: list[tuple[str, str, str]] = []
        self._unit_nodes: dict[str, dict[str, str]] = {}
        self._container_facts: list = []
        self._sites: list = []
        self._entry_entity_refs: list = []
        self._override_nodes()
        self._base_networks()
        hosts = []
        for facts in self.facts_list:
            h = self._host(facts)
            hosts.append((h, facts))
            if self.main_host is None:
                self.main_host = h
        for h, facts in hosts:
            self._units(h, facts)
            self._docker(h, facts)
            self._nginx(h, facts)
        for h, facts in hosts:
            self._host_network(h, facts)
        self._container_refs()
        self._site_refs()
        for h, facts in hosts:
            self._prometheus_grafana(h, facts)
            self._tailscale(h, facts)
        for h, facts in hosts:
            self._homeassistant(facts, h)
        self._override_edges()
        self.g.meta.update({
            "hosts": [h for h, _ in hosts],
            "collected_at": {h: f.get("collected_at") for h, f in hosts},
            "source_errors": [dict(e, host=h) for h, f in hosts for e in f.get("errors", [])],
        })
        return self.g


def build_graph(facts_list: list[dict], overrides: dict | None, config: dict | None = None) -> Graph:
    return Builder(facts_list, overrides, config).build()
