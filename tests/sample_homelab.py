"""An anonymised sample homelab used by the unit tests, the end-to-end test and the README examples.

`python tests/sample_homelab.py <dir>` writes facts.json + overrides.yaml into <dir>.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import yaml

SERVER_IP = "192.0.2.10"


def _ent(eid: str, device: str | None, entry: str | None, name: str = "") -> dict:
    return {"entity_id": eid, "device_id": device, "entry_id": entry, "platform": eid.split(".")[0],
            "name": name or eid, "disabled": False, "hidden": False}


def facts(collected_at: str | None = None) -> dict:
    collected_at = collected_at or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    units = [
        {"unit": u, "description": "", "requires": r} for u, r in [
            ("docker.service", []), ("mosquitto.service", []), ("pihole-FTL.service", []), ("nginx.service", []),
            ("prometheus.service", []), ("grafana-server.service", []), ("tailscaled.service", []),
        ]
    ]
    listeners = [
        {"port": 1883, "addr": "0.0.0.0", "process": "mosquitto", "unit": "mosquitto.service"},
        {"port": 53, "addr": "0.0.0.0", "process": "pihole-FTL", "unit": "pihole-FTL.service"},
        {"port": 9090, "addr": "*", "process": "prometheus", "unit": "prometheus.service"},
        {"port": 3000, "addr": "*", "process": "grafana", "unit": "grafana-server.service"},
        {"port": 8123, "addr": "0.0.0.0", "process": "python3", "container_id": "aaaaaaaaaaaa"},
        {"port": 8080, "addr": "0.0.0.0", "process": "nginx", "unit": "nginx.service"},
    ]
    containers = [
        {"id": "aaaaaaaaaaaa", "name": "homeassistant", "image": "ghcr.io/home-assistant/home-assistant:stable",
         "state": "running", "restart": "unless-stopped", "network_mode": "host", "networks": ["host"],
         "compose_project": "", "compose_service": "", "depends_on": [], "env_refs": [], "file_refs": [],
         "host_ports": [], "mounts": []},
        {"id": "bbbbbbbbbbbb", "name": "battery-bridge", "image": "example/battery-bridge:1",
         "state": "running", "restart": "unless-stopped", "network_mode": "energy_default", "networks": ["energy_default"],
         "compose_project": "energy", "compose_service": "battery-bridge", "depends_on": [],
         "env_refs": [{"var": "MQTT_HOST", "host": SERVER_IP, "port": 1883},
                      {"var": "CLOUD_URL", "host": "api.battery-cloud.example", "port": None}],
         "file_refs": [], "host_ports": [], "mounts": []},
        {"id": "cccccccccccc", "name": "influxdb", "image": "influxdb:1.8", "state": "running", "restart": "always",
         "network_mode": "bridge", "networks": ["bridge"], "compose_project": "", "compose_service": "",
         "depends_on": [], "env_refs": [], "file_refs": [{"file": "/etc/influxdb.conf", "host": "127.0.0.1", "port": 8088}],
         "host_ports": [8086], "mounts": []},
        {"id": "dddddddddddd", "name": "old-thing", "image": "example/old", "state": "exited", "restart": "no",
         "network_mode": "bridge", "networks": [], "compose_project": "", "compose_service": "", "depends_on": [],
         "env_refs": [], "file_refs": [], "host_ports": [], "mounts": []},
    ]
    entries = [
        {"entry_id": "E_ZHA", "domain": "zha", "title": "", "disabled": False,
         "hosts": [{"key": "data.device.path", "host": "192.0.2.50", "port": 6638}], "entity_refs": []},
        {"entry_id": "E_HUB", "domain": "tplink", "title": "Smart Hub", "disabled": False,
         "hosts": [{"key": "data.host", "host": "192.0.2.60", "port": None}], "entity_refs": []},
        {"entry_id": "E_PLUG", "domain": "tplink", "title": "Server plug", "disabled": False,
         "hosts": [{"key": "data.host", "host": "192.0.2.61", "port": None}], "entity_refs": []},
        {"entry_id": "E_MQTT", "domain": "mqtt", "title": SERVER_IP, "disabled": False,
         "hosts": [{"key": "data.broker", "host": SERVER_IP, "port": 1883}], "entity_refs": []},
        {"entry_id": "E_PHONE", "domain": "mobile_app", "title": "Phone", "disabled": False,
         "hosts": [{"key": "data.app_data.push_url", "host": "mobile-apps.home-assistant.io", "port": None}],
         "entity_refs": []},
        {"entry_id": "E_WEATHER", "domain": "met", "title": "Home", "disabled": False, "hosts": [], "entity_refs": []},
        {"entry_id": "E_TPL", "domain": "template", "title": "Total power", "disabled": False, "hosts": [],
         "entity_refs": ["sensor.server_plug_power"]},
        {"entry_id": "E_OFF", "domain": "spotify", "title": "x", "disabled": True, "hosts": [], "entity_refs": []},
    ]
    devices = [
        {"id": "D_COORD", "name": "Zigbee Coordinator", "manufacturer": "Example", "model": "ZB-ETH", "via": None,
         "entries": ["E_ZHA"], "area": "", "disabled": False},
        *[{"id": f"D_PIR{i}", "name": f"Motion {room}", "manufacturer": "Example", "model": "PIR", "via": "D_COORD",
           "entries": ["E_ZHA"], "area": room, "disabled": False} for i, room in enumerate(["Kitchen", "Hall", "Bath"])],
        {"id": "D_HUB", "name": "Smart Hub", "manufacturer": "TP-Link", "model": "KH100", "via": "D_ROUTER",
         "entries": ["E_HUB"], "area": "Hall", "disabled": False},
        {"id": "D_TRV1", "name": "Thermostat Living", "manufacturer": "TP-Link", "model": "KE100", "via": "D_HUB",
         "entries": ["E_HUB"], "area": "Living", "disabled": False},
        {"id": "D_TRV2", "name": "Thermostat Bedroom", "manufacturer": "TP-Link", "model": "KE100", "via": "D_HUB",
         "entries": ["E_HUB"], "area": "Bedroom", "disabled": False},
        {"id": "D_PLUG", "name": "Server plug", "manufacturer": "TP-Link", "model": "P110", "via": "D_ROUTER",
         "entries": ["E_PLUG"], "area": "", "disabled": False},
        {"id": "D_ROUTER", "name": "Router", "manufacturer": "Example", "model": "R1", "via": None,
         "entries": [], "area": "", "disabled": False},
        {"id": "D_BATT", "name": "Balcony battery", "manufacturer": "Example", "model": "B2000", "via": None,
         "entries": ["E_MQTT"], "area": "", "disabled": False},
        {"id": "D_PHONE", "name": "Phone", "manufacturer": "Apple", "model": "iPhone", "via": None,
         "entries": ["E_PHONE"], "area": "", "disabled": False},
    ]
    entities = [
        _ent("binary_sensor.motion_kitchen", "D_PIR0", "E_ZHA", "Motion kitchen"),
        _ent("sensor.motion_kitchen_lux", "D_PIR0", "E_ZHA"),
        _ent("binary_sensor.motion_hall", "D_PIR1", "E_ZHA", "Motion hall"),
        _ent("sensor.motion_hall_lux", "D_PIR1", "E_ZHA"),
        _ent("binary_sensor.motion_bath", "D_PIR2", "E_ZHA", "Motion bath"),
        _ent("sensor.motion_bath_lux", "D_PIR2", "E_ZHA"),
        _ent("button.coordinator_identify", "D_COORD", "E_ZHA"),
        _ent("climate.living", "D_TRV1", "E_HUB", "Living room heating"),
        _ent("climate.bedroom", "D_TRV2", "E_HUB", "Bedroom heating"),
        _ent("sensor.hub_signal", "D_HUB", "E_HUB"),
        _ent("switch.server_plug", "D_PLUG", "E_PLUG"),
        _ent("sensor.server_plug_power", "D_PLUG", "E_PLUG"),
        _ent("sensor.battery_soc", "D_BATT", "E_MQTT", "Battery level"),
        _ent("sensor.battery_power", "D_BATT", "E_MQTT"),
        _ent("device_tracker.phone", "D_PHONE", "E_PHONE"),
        _ent("weather.home", None, "E_WEATHER"),
        _ent("sensor.total_power", None, "E_TPL"),
        _ent("input_select.room", None, None, "Current room"),
        {**_ent("sensor.disabled_one", "D_HUB", "E_HUB"), "disabled": True},
    ]
    automations = [
        {"id": "room_tracking", "alias": "Room tracking",
         "trigger_entities": ["binary_sensor.motion_kitchen", "binary_sensor.motion_hall", "binary_sensor.motion_bath"],
         "trigger_devices": [], "action_entities": ["input_select.room"],
         "entities": ["binary_sensor.motion_kitchen", "binary_sensor.motion_hall", "binary_sensor.motion_bath",
                      "input_select.room"], "services": [], "devices": []},
        {"id": "heating_follows", "alias": "Heating follows me", "trigger_entities": ["input_select.room"],
         "trigger_devices": [], "action_entities": ["climate.living", "climate.bedroom"],
         "entities": ["input_select.room", "climate.living", "climate.bedroom"], "services": [], "devices": []},
        {"id": "battery_low", "alias": "Battery low warning", "trigger_entities": ["sensor.battery_soc"],
         "trigger_devices": [], "action_entities": [], "entities": ["sensor.battery_soc"],
         "services": ["notify.mobile_app_phone"], "devices": []},
    ]
    return {
        "version": 1, "host_name": "homeserver", "collected_at": collected_at,
        "sources": {s: "ok" for s in ("host", "docker", "tailscale", "pihole", "nginx", "prometheus", "grafana", "homeassistant")},
        "errors": [],
        "host": {
            "hostname": "homeserver", "resolvers": [SERVER_IP, "192.0.2.1"],
            "default_routes": [{"gateway": "192.0.2.1", "dev": "eth0", "dhcp": True}],
            "addresses": [{"ifname": "eth0", "ip": SERVER_IP}],
            "network_mounts": [{"target": "/mnt/nas", "source": "//192.0.2.20/share", "fstype": "cifs", "server": "192.0.2.20"}],
            "listeners": listeners, "units": units,
        },
        "docker": {"containers": containers, "networks": [{"name": "energy_default", "driver": "bridge", "internal": False}]},
        "tailscale": {"backend_state": "Running", "self": {"name": "homeserver", "online": True}, "magic_dns": True,
                      "peers": [{"name": "phone", "os": "iOS", "online": True}]},
        "pihole": {"dns": True, "dhcp_active": False},
        "nginx": {"sites": [
            {"name": "status", "listen": [8080], "roots": ["/var/www/status"], "upstreams": [{"host": "127.0.0.1", "port": 9090}]},
            {"name": "docs", "listen": [8081], "roots": ["/mnt/nas/docs"], "upstreams": []},
        ]},
        "prometheus": {"jobs": [{"job": "node", "targets": ["localhost:9100"]}, {"job": "ha", "targets": ["localhost:8123"]}],
                       "alertmanagers": []},
        "grafana": {"datasources": [{"name": "Prometheus", "type": "prometheus", "refs": [{"host": "localhost", "port": 9090}]},
                                    {"name": "Influx", "type": "influxdb", "refs": [{"host": "localhost", "port": 8086}]}]},
        "homeassistant": {"config_entries": entries, "devices": devices, "entities": entities,
                          "automations": automations, "scripts": [],
                          "dashboards": [{"id": "lovelace.lovelace", "title": "Overview",
                                          "entities": ["climate.living", "climate.bedroom", "sensor.battery_soc", "weather.home"],
                                          "services": [], "devices": []}]},
    }


OVERRIDES_YAML = """
hardware:
  mains: {label: Mains power, spof: false}
  plug-server:
    label: Plug powering the server
    ha_device: "Server plug@tplink"
    powered_by: mains
  router:
    label: Router (Wi-Fi, DHCP)
    aliases: [router, gateway]
    ips: [192.0.2.1]
    powered_by: mains
  server:
    label: Home server (hardware)
    powered_by: plug-server
  coordinator:
    label: Zigbee coordinator
    say: der Zigbee-Koordinator
    aliases: [zigbee coordinator, coordinator, zigbee]
    ips: [192.0.2.50]
    ha_device: "Zigbee Coordinator@zha"
    needs: net:lan
    powered_by: mains
  hub:
    label: Thermostat hub
    ha_device: "Smart Hub@tplink"
    needs: net:lan
  nas:
    label: NAS
    ips: [192.0.2.20]
    needs: net:lan
hosts:
  homeserver:
    label: Home server
    aliases: [server, homeserver]
    runs_on: hw:server
cloud:
  battery:
    label: Battery vendor cloud
    hosts: [api.battery-cloud.example]
  push:
    label: Push relay
    hosts: [mobile-apps.home-assistant.io]
integrations:
  met: [net:internet]
edges:
  - {from: "device:Balcony battery@mqtt", to: container:battery-bridge}
functions:
  heating:
    label: Heating
    say: die Heizung
    aliases: [heizung]
    needs: [hw:hub]
    uses: [climate.*]
  heating-follows:
    label: Heating follows me
    needs: ["automation:Heating follows me"]
  presence:
    label: Presence detection
    uses: [binary_sensor.motion_*]
  battery:
    label: Battery monitoring
    needs: [sensor.battery_soc]
  remote:
    label: Remote access
    needs: [net:tailnet]
  docs:
    label: Documentation website
    needs: [site:docs]
  dns-any:
    label: Name resolution
    any_of: [unit:pihole-FTL, hw:router]
  broken:
    label: Uses something that does not exist
    needs: [hw:does-not-exist]
"""


def overrides() -> dict:
    return yaml.safe_load(OVERRIDES_YAML)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "facts.json"), "w", encoding="utf-8") as fh:
        json.dump(facts(), fh, indent=1)
    with open(os.path.join(out, "overrides.yaml"), "w", encoding="utf-8") as fh:
        fh.write(OVERRIDES_YAML.lstrip())
    print(f"sample homelab written to {out}")
