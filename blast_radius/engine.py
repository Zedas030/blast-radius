"""Engine: keeps the active graph, rebuilds it when facts change, watches itself and Home Assistant."""
from __future__ import annotations

import datetime as dt
import glob
import hashlib
import json
import logging
import os
import threading
import time
from typing import Any

from . import metrics
from .analysis import CauseFinder, impact_report, spof
from .build import build_graph
from .config import load_overrides
from .diff import diff_graphs, shrink_check
from .homeassistant import HomeAssistant, UnavailabilityTracker
from .model import Graph

log = logging.getLogger("blast_radius.engine")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _parse_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class Engine:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.cfg = cfg
        e = cfg["engine"]
        self.data_dir = e["data_dir"]
        os.makedirs(os.path.join(self.data_dir, "snapshots"), exist_ok=True)
        self.lock = threading.RLock()
        self.graph: Graph | None = None
        self.finder: CauseFinder | None = None
        self.spof_cache: dict[str, Any] | None = None
        self.ha = HomeAssistant(cfg["homeassistant"])
        self.tracker = UnavailabilityTracker()
        self.state = self._load_state()
        self.last_signature = ""
        self.last_build: dict[str, Any] = {}
        self.live: dict[str, Any] = {"ok": None, "unavailable": 0, "new": 0, "last_poll": None, "cause": None}
        self._load_active()

    # ------------------------------------------------------------------ persistence
    def _path(self, name: str) -> str:
        return os.path.join(self.data_dir, name)

    def _load_state(self) -> dict[str, Any]:
        try:
            with open(self._path("state.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {"alerts": {}, "pending_shrink": 0, "accept_next": False, "causes": [], "last_cause_sent": {}}

    def _save_state(self) -> None:
        tmp = self._path("state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self._path("state.json"))

    def _load_active(self) -> None:
        try:
            with open(self._path("graph.json"), encoding="utf-8") as fh:
                self._activate(Graph.from_dict(json.load(fh)))
            log.info("loaded active graph (%d nodes)", len(self.graph.nodes))
        except (OSError, ValueError):
            log.info("no active graph yet")

    def _activate(self, g: Graph) -> None:
        with self.lock:
            self.graph = g
            self.finder = CauseFinder(g)
            self.spof_cache = spof(g, self.cfg["analysis"]["spof_threshold"])
        metrics.graph_updated(g, self.spof_cache)

    def _write_graph(self, g: Graph) -> None:
        data = json.dumps(g.to_dict(), ensure_ascii=False)
        tmp = self._path("graph.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, self._path("graph.json"))
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(self._path(f"snapshots/graph-{stamp}.json"), "w", encoding="utf-8") as fh:
            fh.write(data)
        snaps = sorted(glob.glob(self._path("snapshots/graph-*.json")))
        for old in snaps[: max(0, len(snaps) - int(self.cfg["engine"]["keep_snapshots"]))]:
            os.remove(old)

    def _append_change(self, entry: dict[str, Any]) -> None:
        with open(self._path("changes.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def changes(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with open(self._path("changes.jsonl"), encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            return []
        out = []
        for line in reversed(lines[-limit:]):
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    # ------------------------------------------------------------------ alerts
    def raise_alert(self, key: str, message: str) -> None:
        alerts = self.state.setdefault("alerts", {})
        if key in alerts:
            return
        alerts[key] = {"message": message, "since": _now_iso()}
        self._save_state()
        log.warning("ALERT %s: %s", key, message)
        sent = self.ha.notify("Blast-Radius", message, self.cfg["web"].get("public_url", ""))
        metrics.notification(sent)
        metrics.alerts_active(len(alerts))

    def clear_alert(self, key: str, message: str | None = None) -> None:
        alerts = self.state.setdefault("alerts", {})
        if key not in alerts:
            return
        del alerts[key]
        self._save_state()
        log.info("resolved %s", key)
        if message:
            metrics.notification(self.ha.notify("Blast-Radius", message, self.cfg["web"].get("public_url", "")))
        metrics.alerts_active(len(alerts))

    def clear_alerts(self, prefix: str, keep: set[str], message: str) -> None:
        for key in [k for k in self.state.get("alerts", {}) if k.startswith(prefix) and k not in keep]:
            self.clear_alert(key, message.format(key=key.split(":", 1)[-1]))

    # ------------------------------------------------------------------ build cycle
    def _facts_files(self) -> list[str]:
        out = []
        for pattern in self.cfg["engine"]["facts_files"]:
            out += sorted(glob.glob(pattern)) or [pattern]
        return out

    def _signature(self) -> str:
        h = hashlib.sha1()
        for p in self._facts_files() + [self.cfg["engine"]["overrides_file"]]:
            try:
                st = os.stat(p)
                h.update(f"{p}:{st.st_mtime_ns}:{st.st_size};".encode())
            except OSError:
                h.update(f"{p}:missing;".encode())
        return h.hexdigest()

    def check(self, force: bool = False) -> dict[str, Any]:
        """Periodic check: facts age, rebuild if inputs changed."""
        facts_list = []
        stale = set()
        max_age = float(self.cfg["engine"]["facts_max_age_hours"]) * 3600
        for p in self._facts_files():
            try:
                with open(p, encoding="utf-8") as fh:
                    facts = json.load(fh)
            except (OSError, ValueError) as exc:
                self.raise_alert(f"facts:{p}", f"Discovery data {p} cannot be read ({type(exc).__name__}). "
                                               "Is the blast-radius-collect timer running?")
                stale.add(f"facts:{p}")
                continue
            facts["_file"] = p
            ts = _parse_ts(facts.get("collected_at"))
            age = time.time() - ts if ts else None
            metrics.facts_age(facts.get("host_name", p), age)
            if age is None or age > max_age:
                hours = f"{age / 3600:.0f} h" if age else "unknown time"
                self.raise_alert(f"facts:{p}", f"Nightly discovery did not run: data of {facts.get('host_name', p)} "
                                               f"is {hours} old (limit {max_age / 3600:.0f} h).")
                stale.add(f"facts:{p}")
            facts_list.append(facts)
        self.clear_alerts("facts:", stale, "Discovery data is fresh again ({key}).")
        sig = self._signature()
        if not force and sig == self.last_signature and self.graph is not None:
            return {"rebuilt": False}
        self.last_signature = sig
        return self.rebuild(facts_list)

    def rebuild(self, facts_list: list[dict[str, Any]]) -> dict[str, Any]:
        started = time.time()
        # source errors from the collector
        errs = set()
        for f in facts_list:
            for e in f.get("errors", []):
                key = f"source:{f.get('host_name')}:{e['source']}"
                errs.add(key)
                self.raise_alert(key, f"Discovery source '{e['source']}' on {f.get('host_name')} failed: {e['error'][:200]}")
            for src, st in (f.get("sources") or {}).items():
                metrics.source_ok(f.get("host_name", "?"), src, st == "ok")
        self.clear_alerts("source:", errs, "Discovery source {key} works again.")
        if not facts_list:
            metrics.build_ok(False)
            return {"rebuilt": False, "error": "no facts"}
        try:
            overrides = load_overrides(self.cfg["engine"]["overrides_file"])
            new = build_graph(facts_list, overrides, self.cfg)
        except Exception as exc:  # noqa: BLE001
            log.exception("graph build failed")
            self.raise_alert("build", f"Building the dependency graph failed: {type(exc).__name__}: {exc}"[:300])
            metrics.build_ok(False)
            return {"rebuilt": False, "error": str(exc)}
        self.clear_alert("build", "Building the dependency graph works again.")
        new.meta["built_at"] = _now_iso()
        e = self.cfg["engine"]
        reasons = shrink_check(self.graph, new, e["shrink_alert_ratio"], e["shrink_type_ratio"], e["shrink_type_min_nodes"])
        accepted = True
        if reasons:
            self.state["pending_shrink"] = int(self.state.get("pending_shrink", 0)) + 1
            if self.state.get("accept_next") or self.state["pending_shrink"] >= int(e["auto_accept_after"]):
                log.warning("accepting shrunken graph (%s)", "; ".join(reasons))
                self.clear_alert("shrink", "Smaller dependency graph accepted: " + "; ".join(reasons))
            else:
                accepted = False
                self.raise_alert("shrink", "Large parts of the dependency graph are missing: " + "; ".join(reasons)
                                 + ". The previous graph stays active. Accept it in the web UI if this is intended.")
        if accepted:
            self.state["pending_shrink"] = 0
            self.state["accept_next"] = False
            if not reasons:
                self.clear_alert("shrink", "Dependency graph is complete again.")
            change = diff_graphs(self.graph, new)
            if change["initial"] or change["nodes_added"] or change["nodes_removed"] or change["edges_added"] or change["edges_removed"]:
                self._append_change({"at": _now_iso(), **change})
                self._write_graph(new)
            elif not os.path.exists(self._path("graph.json")):
                self._write_graph(new)
            else:
                with open(self._path("graph.json"), "w", encoding="utf-8") as fh:
                    json.dump(new.to_dict(), fh, ensure_ascii=False)
            self._activate(new)
        self._save_state()
        self.last_build = {"at": _now_iso(), "accepted": accepted, "shrink": reasons,
                           "nodes": len(new.nodes), "edges": len(new.edges),
                           "seconds": round(time.time() - started, 2), "warnings": len(new.warnings)}
        metrics.build_ok(True, pending_shrink=not accepted)
        log.info("build: %s", self.last_build)
        return {"rebuilt": True, **self.last_build}

    def accept_pending(self) -> None:
        self.state["accept_next"] = True
        self._save_state()
        self.last_signature = ""

    # ------------------------------------------------------------------ live monitoring
    def poll_ha(self) -> dict[str, Any]:
        if not self.ha.configured:
            self.live.update(ok=False, error="Home Assistant URL/token not configured")
            metrics.ha_poll(False, 0)
            return self.live
        try:
            states = self.ha.states()
        except Exception as exc:  # noqa: BLE001
            self.live.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:200], last_poll=_now_iso())
            metrics.ha_poll(False, len(self.tracker.unavailable()))
            return self.live
        self.tracker.update(states)
        return self.evaluate_unavailable(self.tracker.new_within(self.cfg["homeassistant"]["mass_outage"]["window_seconds"]),
                                         self.tracker.unavailable(), notify=True)

    def evaluate_unavailable(self, new: set[str], unavailable: set[str], notify: bool) -> dict[str, Any]:
        mo = self.cfg["homeassistant"]["mass_outage"]
        cause = None
        with self.lock:
            finder = self.finder
        if finder and len(new) >= int(mo["min_entities"]):
            cause = finder.find(new, unavailable)
            cause["at"] = _now_iso()
            if notify:
                self._notify_cause(cause)
        self.live.update(ok=True, error="", last_poll=_now_iso(), unavailable=len(unavailable), new=len(new),
                         cause=cause or (self.live.get("cause") if self.live.get("cause") and new else None))
        metrics.ha_poll(True, len(unavailable))
        self.publish_status()
        return self.live

    def _notify_cause(self, cause: dict[str, Any]) -> None:
        cooldown = float(self.cfg["homeassistant"]["mass_outage"]["cooldown_minutes"]) * 60
        key = cause["candidates"][0]["id"] if cause["candidates"] else "unknown"
        sent = self.state.setdefault("last_cause_sent", {})
        if time.time() - float(sent.get(key, 0)) < cooldown:
            return
        sent[key] = time.time()
        self.state.setdefault("causes", []).insert(0, {k: cause[k] for k in ("at", "count", "message", "candidates")})
        self.state["causes"] = self.state["causes"][:50]
        self._save_state()
        metrics.mass_outage()
        metrics.notification(self.ha.notify("Blast-Radius", cause["message"], self.cfg["web"].get("public_url", "")))

    def publish_status(self) -> None:
        alerts = self.state.get("alerts", {})
        g = self.graph
        state = "error" if any(k.startswith(("facts:", "build")) for k in alerts) else ("warning" if alerts else "ok")
        attrs = {
            "nodes": len(g.nodes) if g else 0,
            "edges": len(g.edges) if g else 0,
            "graph_built_at": (g.meta.get("built_at") if g else None),
            "collected_at": (g.meta.get("collected_at") if g else None),
            "alerts": [a["message"] for a in alerts.values()],
            "spof_count": len((self.spof_cache or {}).get("spofs", [])),
            "unavailable_entities": self.live.get("unavailable", 0),
            "last_cause": (self.live.get("cause") or {}).get("message"),
            "url": self.cfg["web"].get("public_url", ""),
        }
        self.ha.publish_status(state, attrs)

    # ------------------------------------------------------------------ queries
    def require_graph(self) -> Graph:
        if self.graph is None:
            raise LookupError("no graph built yet")
        return self.graph

    def impact(self, roots: list[str], with_paths: bool = True) -> dict[str, Any]:
        return impact_report(self.require_graph(), roots, with_paths=with_paths)

    def status(self) -> dict[str, Any]:
        g = self.graph
        return {
            "graph": {"nodes": len(g.nodes), "edges": len(g.edges), "counts": g.counts(), "meta": g.meta,
                      "warnings": g.warnings} if g else None,
            "last_build": self.last_build,
            "alerts": self.state.get("alerts", {}),
            "pending_shrink": self.state.get("pending_shrink", 0),
            "live": self.live,
            "ha_configured": self.ha.configured,
            "notify_service": self.ha.notify_service,
        }
