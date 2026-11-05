# Blast-Radius

**What fails if X fails?** Blast-Radius builds a dependency map of a homelab and smart home automatically and
answers that question – grouped by what *you* notice ("heating, solar display, voice assistant, remote access"),
with the technical path behind every answer.

- **Automatic discovery (read-only):** hosts, systemd services, Docker containers, networks, `depends_on`,
  host names/IPs in container environments and config files, nginx upstreams and web roots, Prometheus scrape
  targets, Grafana data sources, DNS resolvers, gateway, DHCP, Tailscale – and Home Assistant: integrations →
  devices → entities, hubs/coordinators (`via_device`), and which automations, scripts and dashboards use which entities.
- **Overrides** for what cannot be discovered: power supply ("the server hangs on plug X"), cabling, cloud
  dependencies, software on other machines, and *functions* as people experience them.
- **Impact simulation** in a web UI (Cytoscape.js): click a node → everything affected is highlighted; search;
  combined failures with shift-click.
- **Single points of failure:** nodes that alone take down more than 50 % of functions or entities.
- **Cause hint:** when many Home Assistant entities become `unavailable` at once, Blast-Radius finds the most
  likely common cause in the graph ("14 entities unavailable – likely cause: Zigbee coordinator") and notifies you.
- **JSON API** for voice assistants: `GET /api/ask?q=What happens if the Pi crashes?`
- **Nightly rediscovery** with a change log, **self-monitoring** (failed discovery, stale data, graph suddenly
  much smaller), notifications via a Home Assistant `notify` service, **Prometheus metrics**.

---

## How it works

```
 host (systemd timer 03:30, read-only sandbox)            Docker container (always on)
 ┌──────────────────────────────────────┐                 ┌───────────────────────────────────────────┐
 │ blast-radius-collect                 │  facts.json     │ engine: build graph (facts + overrides)   │
 │  ip/ss/systemctl/docker inspect/     │ ──────────────▶ │  · change log, shrink check, alerts       │
 │  tailscale status/nginx/prometheus/  │  (sanitised,    │  · polls HA /api/states every minute      │
 │  grafana/HA .storage + YAML          │   no secrets)   │ web UI + JSON API + /metrics  :8098       │
 └──────────────────────────────────────┘                 └───────────────────────────────────────────┘
                                                      overrides.yaml ─┘        │ notify.* + sensor.blast_radius
                                                                               ▼
                                                                        Home Assistant
```

- The **collector** runs on the host as root inside a systemd sandbox (`ProtectSystem=strict`) whose only
  writable path is `/var/lib/blast-radius/facts`. It reduces everything to host names, IPs, ports and entity ids;
  keys and lines that look like credentials (`pass`, `token`, `secret`, `key`, …) are skipped, user/password parts
  of URLs are dropped. It changes nothing.
- The **engine** container sees only that facts file and its config; it has no Docker socket, no host mounts
  besides those, runs as an unprivileged user with a read-only root file system and all capabilities dropped.
  Its only write calls to Home Assistant are the notification and the status sensor.

### The graph

Node types: `hardware`, `network`, `cloud`, `host`, `service`, `integration`, `device`, `entity`, `automation`,
`script`, `dashboard`, `function`. Edges always point from the dependent to its dependency:

| kind | meaning | example |
|---|---|---|
| `runs_on` | software runs on a host/runtime | container → Docker → host |
| `needs` | functional dependency | integration → coordinator; site → upstream |
| `powered_by` | power supply | server → smart plug → mains |
| `controlled_by` | controlled through a hub | thermostat → hub; sensor → Zigbee coordinator |

Edge flavours decide how failures propagate:

- **hard** (default): the dependent fails.
- **soft** (`uses`): the dependent is only *degraded*; it fails when **all** its soft dependencies failed
  (e.g. a dashboard, the action targets of an automation).
- **group / any_of**: alternatives – fails only if every alternative failed; otherwise the node keeps working but
  is marked *lost redundancy* (e.g. two DNS resolvers). Automation triggers form such a group.
- **weak**: can only degrade (a helper value written by an automation keeps its last value).

## Installation

Requirements: Linux host with Docker + Compose v2, `python3` with `python3-yaml` (for the collector), systemd.

```bash
git clone https://github.com/Zedas030/blast-radius.git
cd blast-radius
sudo ./scripts/install.sh
```

The script installs to `/opt/blast-radius`, creates `config/config.yaml`, `config/overrides.yaml` and `.env` from
the examples (existing files are kept), enables the nightly timer, runs the collector once and starts the container
(`restart: unless-stopped`, so it comes back after reboots).

Then:

1. Enter the Home Assistant token in `/opt/blast-radius/.env` (see [Secrets](#secrets)).
2. Adapt `/opt/blast-radius/config/config.yaml` (HA URL, notify service, paths of your configs).
3. Describe your home in `/opt/blast-radius/config/overrides.yaml` (see below).
4. `sudo docker compose -f /opt/blast-radius/docker-compose.yml restart` and open `http://<host>:8098`.

Update: pull the repository and run `sudo ./scripts/install.sh` again.

### Optional integrations

- **Prometheus:** add a scrape job
  ```yaml
  - job_name: blast_radius
    static_configs:
      - targets: ['localhost:8098']
  ```
- **Dead man's switch in Home Assistant:** Blast-Radius updates `sensor.blast_radius` every poll (attribute
  `heartbeat`). An automation that notifies when the sensor has not been updated for 30 minutes also covers the case
  that the container itself is dead (Blast-Radius cannot report its own death):
  ```yaml
  - alias: "Blast-Radius: dead man's switch"
    triggers:
      - trigger: time_pattern
        minutes: "/15"
    conditions:
      - condition: template
        value_template: >
          {{ states.sensor.blast_radius is none or
             (now() - states.sensor.blast_radius.last_updated).total_seconds() > 1800 }}
    actions:
      - action: notify.mobile_app_phone
        data:
          title: Blast-Radius
          message: No sign of life from Blast-Radius for 30 minutes.
    mode: single
  ```

## Configuration

`config/config.yaml` is fully commented ([example](config/config.example.yaml)). The important bits:

| key | default | meaning |
|---|---|---|
| `collector.ha_config_dir` | `/home/pi/homeassistant` | HA config dir (`.storage`, `automations.yaml`, `scripts.yaml`) |
| `engine.facts_files` | `/data/facts/*.json` | one facts file per host |
| `engine.facts_max_age_hours` | 26 | older data → "nightly discovery did not run" |
| `engine.shrink_alert_ratio` | 0.2 | new graph with 20 % fewer nodes → alert, previous graph stays active |
| `analysis.spof_threshold` | 0.5 | SPOF = takes down > 50 % of functions or entities |
| `homeassistant.notify_service` | – | e.g. `notify.mobile_app_phone` |
| `homeassistant.mass_outage.min_entities` | 5 | newly unavailable entities within `window_seconds` that count as mass outage |
| `homeassistant.startup_grace_seconds` | 600 | after Home Assistant (re)starts, only learn what is unavailable – integrations report `unavailable` while they load |

### Overrides

`config/overrides.yaml` ([example](config/overrides.example.yaml)) describes what discovery cannot see. It is
re-read within a minute after saving. References that match nothing are listed under **Health → Override warnings**.

```yaml
hardware:
  plug-server:
    label: Plug powering the server
    ha_device: "Server plug@tplink"     # this HA device *is* this hardware
    powered_by: mains
  coordinator:
    label: Zigbee coordinator
    aliases: [zigbee coordinator, coordinator]
    ips: [192.0.2.50]                    # discovered references to this IP resolve to this node
    ha_device: "Zigbee Coordinator@zha"
    needs: net:lan
hosts:
  homeserver: {label: Home server, runs_on: hw:server}
cloud:
  battery: {label: Battery vendor cloud, hosts: [api.battery-cloud.example]}
integrations:
  spotify: [cloud:spotify]               # extra dependencies of an HA integration domain
functions:
  heating:
    label: Heating
    needs: [hw:hub]
    uses: [climate.*]                    # degraded if some, failed if all thermostats are gone
  presence:
    label: Presence detection
    uses: [binary_sensor.motion_*]
  dns:
    label: Name resolution
    any_of: [unit:pihole-FTL, hw:router]
```

Reference syntax: `hw:/cloud:/net:/host:/svc:/fn:<key>`, `container:<name>`, `unit:<systemd unit>`,
`site:<nginx site>`, `port:<n>`, `integration:<domain or label>`, `device:<name>[@<domain>]`,
`automation:<alias>`, `script:<id>`, `dashboard:<title>`, entity ids with `*` wildcards. Built-in networks:
`net:lan`, `net:internet`, `net:dns`, `net:dhcp`, `net:tailnet`. `spof: false` keeps a node (e.g. mains power)
out of the SPOF list; `planned: true` marks hardware that does not exist yet.

## Using it

**Web UI** (`http://<host>:8098`):

- *Map* – the graph without entities (toggle "entities" for all of them). Click a node to simulate its failure,
  shift-click to add more. The side panel lists the functions that stop working, those that are degraded,
  things that lost redundancy, and per type every affected node with its path (expand an item).
  The search box takes names and plain questions ("What happens if the router fails?").
- *Single points of failure* – merged rows for nodes with identical outcome (plug → board → OS).
- *Live & cause* – current unavailable entities, last cause analysis, history, and a form to simulate a mass
  outage with any list of entity ids (no notification).
- *Changes* – what each rebuild added or removed. *Health* – alerts, discovery times, override warnings, rebuild.

**API** (OpenAPI docs at `/docs`):

| endpoint | purpose |
|---|---|
| `GET /api/ask?q=…` | natural-language question (German or English) → `speech`, `summary`, `functions`, `matched` |
| `GET /api/impact?node=<id>&node=<id>` / `?q=<name>` | full impact report with paths |
| `GET /api/search?q=…` | find nodes |
| `GET /api/spof` | single points of failure |
| `POST /api/cause/simulate` `{"entities": [...]}` | likely cause for a set of unavailable entities |
| `GET /api/live`, `/api/changes`, `/api/status`, `/api/graph` | live state, change log, health, graph |
| `POST /api/rebuild`, `POST /api/graph/accept` | rebuild now / accept a rejected smaller graph |
| `GET /metrics` | Prometheus |

Example for a voice assistant:

```bash
curl -s "http://192.0.2.10:8098/api/ask?q=Was%20passiert%20wenn%20der%20Router%20ausf%C3%A4llt" | jq -r .speech
```

**CLI** (inside the container): `docker exec blast-radius blast-radius ask "what if the router fails"`,
`… impact zigbee`, `… spof`, `… cause sensor.a sensor.b`, `… build`.

## Self-monitoring

| situation | reaction |
|---|---|
| a discovery source fails (e.g. Docker not answering) | notification naming host + source; resolved message when it works again |
| facts older than 26 h (timer did not run) | notification |
| new graph much smaller than the active one | notification, **previous graph stays active**; accept in *Health* or automatically after 3 builds in a row |
| ≥ 5 entities newly unavailable within 5 min | cause analysis + notification (same cause at most hourly) |
| engine dead | HA dead man's switch on `sensor.blast_radius` (see above) and the `blast_radius_up` metric |

Metrics include `blast_radius_graph_nodes{type}`, `blast_radius_build_ok`, `blast_radius_facts_age_seconds`,
`blast_radius_source_ok{source}`, `blast_radius_graph_shrink_pending`, `blast_radius_ha_unavailable_entities`,
`blast_radius_mass_outages_total`, `blast_radius_notifications_total`, `blast_radius_alerts_active`.

## Secrets

| secret | where | how to change / rotate |
|---|---|---|
| `HA_TOKEN` – Home Assistant long-lived access token | `/opt/blast-radius/.env` (root, `chmod 600`), template: [.env.example](.env.example) | HA → profile → *Security* → *Long-lived access tokens* → create a new one, put it into `.env`, `sudo docker compose -f /opt/blast-radius/docker-compose.yml up -d`, then delete the old token in HA |

Nothing else needs credentials: the collector reads local files and commands as root. `config.yaml` and
`overrides.yaml` contain no secrets. The facts file never contains passwords or tokens (see the tests in
`tests/test_collect.py`).

## Operation

| what | where |
|---|---|
| code | `/opt/blast-radius/app` |
| config / overrides / secrets | `/opt/blast-radius/config/`, `/opt/blast-radius/.env` |
| collector facts | `/var/lib/blast-radius/facts/facts.json` |
| active graph, snapshots, change log, state | `/var/lib/blast-radius/state/` |
| logs | `docker logs blast-radius` (rotated, 3 × 10 MB), `journalctl -u blast-radius-collect` |
| rediscover now | `sudo systemctl start blast-radius-collect` |
| limits | container 384 MB RAM, 0.5 CPU, 128 PIDs; collector 256 MB |

**More hosts:** run the collector on the other host as well (copy `blast_radius/collect.py`, `config.yaml` with
`collector.host_name`), and bring its facts file into `/var/lib/blast-radius/facts/<host>.json` (e.g. with a
timer and `scp`). Every `*.json` there becomes part of the graph.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest                         # unit tests (graph logic, discovery, engine, API)
python tests/sample_homelab.py /tmp/demo # anonymised sample facts + overrides
BLAST_RADIUS_CONFIG=... python -m blast_radius.app
./tests/e2e/run_e2e.sh                   # end-to-end: builds the image and runs it against a fake Home Assistant
```

## License

MIT, see [LICENSE](LICENSE). Bundles Cytoscape.js (MIT).
