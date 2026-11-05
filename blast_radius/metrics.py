"""Prometheus metrics (served at /metrics)."""
from __future__ import annotations

import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Info

REGISTRY = CollectorRegistry()

INFO = Info("blast_radius", "Blast-Radius build information", registry=REGISTRY)
UP = Gauge("blast_radius_up", "1 while the Blast-Radius engine is running", registry=REGISTRY)
NODES = Gauge("blast_radius_graph_nodes", "Nodes in the active dependency graph", ["type"], registry=REGISTRY)
EDGES = Gauge("blast_radius_graph_edges", "Edges in the active dependency graph", registry=REGISTRY)
WARNINGS = Gauge("blast_radius_graph_warnings", "Unresolved override references and similar warnings", registry=REGISTRY)
SPOFS = Gauge("blast_radius_spof_count", "Single points of failure above the threshold", registry=REGISTRY)
ACTIVATED = Gauge("blast_radius_graph_activated_timestamp_seconds", "When the active graph was activated", registry=REGISTRY)
BUILD_OK = Gauge("blast_radius_build_ok", "1 if the last graph build succeeded", registry=REGISTRY)
SHRINK = Gauge("blast_radius_graph_shrink_pending", "1 if a suspiciously smaller graph was rejected", registry=REGISTRY)
FACTS_AGE = Gauge("blast_radius_facts_age_seconds", "Age of the collector facts", ["host"], registry=REGISTRY)
SOURCE_OK = Gauge("blast_radius_source_ok", "1 if a discovery source worked in the last collection",
                  ["host", "source"], registry=REGISTRY)
HA_OK = Gauge("blast_radius_ha_poll_ok", "1 if the last Home Assistant poll worked", registry=REGISTRY)
UNAVAILABLE = Gauge("blast_radius_ha_unavailable_entities", "Entities currently unavailable in Home Assistant",
                    registry=REGISTRY)
ALERTS = Gauge("blast_radius_alerts_active", "Active self-monitoring alerts", registry=REGISTRY)
MASS = Counter("blast_radius_mass_outages_total", "Mass outages detected (notifications sent)", registry=REGISTRY)
NOTIFY = Counter("blast_radius_notifications_total", "Notifications attempted", ["result"], registry=REGISTRY)


def init(version: str) -> None:
    INFO.info({"version": version})
    UP.set(1)


def graph_updated(g: Any, spof_result: dict[str, Any] | None) -> None:
    for t, c in g.counts().items():
        NODES.labels(type=t).set(c)
    EDGES.set(len(g.edges))
    WARNINGS.set(len(g.warnings))
    SPOFS.set(len((spof_result or {}).get("spofs", [])))
    ACTIVATED.set(time.time())


def build_ok(ok: bool, pending_shrink: bool = False) -> None:
    BUILD_OK.set(1 if ok else 0)
    SHRINK.set(1 if pending_shrink else 0)


def facts_age(host: str, age: float | None) -> None:
    FACTS_AGE.labels(host=host).set(age if age is not None else -1)


def source_ok(host: str, source: str, ok: bool) -> None:
    SOURCE_OK.labels(host=host, source=source).set(1 if ok else 0)


def ha_poll(ok: bool, unavailable: int) -> None:
    HA_OK.set(1 if ok else 0)
    UNAVAILABLE.set(unavailable)


def alerts_active(n: int) -> None:
    ALERTS.set(n)


def mass_outage() -> None:
    MASS.inc()


def notification(sent: bool) -> None:
    NOTIFY.labels(result="sent" if sent else "failed").inc()
