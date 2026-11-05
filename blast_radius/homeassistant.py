"""Home Assistant access: read states, send notifications, publish the status sensor.

The only write calls are ``notify.*`` and ``POST /api/states/<status sensor>`` (a state shown in HA,
no configuration change).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("blast_radius.ha")


class HomeAssistant:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.url = cfg.get("url", "").rstrip("/")
        self.token = os.environ.get(cfg.get("token_env", "HA_TOKEN"), "")
        self.notify_service = cfg.get("notify_service", "")
        self.status_sensor = cfg.get("status_sensor", "")
        self.timeout = float(cfg.get("timeout_seconds", 15))
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def states(self) -> list[dict[str, Any]]:
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(f"{self.url}/api/states", headers=self._headers())
            r.raise_for_status()
            return r.json()

    def notify(self, title: str, message: str, url: str = "") -> bool:
        if not self.configured or not self.notify_service:
            log.warning("notification not sent (HA or notify_service not configured): %s", message)
            return False
        domain, _, service = self.notify_service.partition(".")
        payload: dict[str, Any] = {"title": title, "message": message}
        if url:
            payload["data"] = {"url": url, "clickAction": url}
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.post(f"{self.url}/api/services/{domain}/{service}", headers=self._headers(), json=payload)
                r.raise_for_status()
            log.info("notification sent: %s", message)
            return True
        except httpx.HTTPError as exc:
            self.last_error = f"notify failed: {exc}"
            log.error("notification failed: %s (%s)", exc, message)
            return False

    def publish_status(self, state: str, attributes: dict[str, Any]) -> bool:
        if not self.configured or not self.status_sensor:
            return False
        try:
            with httpx.Client(timeout=self.timeout) as c:
                r = c.post(f"{self.url}/api/states/{self.status_sensor}", headers=self._headers(),
                           json={"state": state, "attributes": {"friendly_name": "Blast-Radius", "icon": "mdi:radar",
                                                               "heartbeat": int(time.time()), **attributes}})
                r.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            self.last_error = f"status sensor failed: {exc}"
            log.warning("status sensor update failed: %s", exc)
            return False


class UnavailabilityTracker:
    """Remember since when each entity is unavailable, to detect *new* mass outages."""

    def __init__(self) -> None:
        self.since: dict[str, float | None] = {}  # None = was already unavailable at start (baseline)
        self.initialised = False

    def update(self, states: list[dict[str, Any]], now: float | None = None) -> None:
        now = time.time() if now is None else now
        current = {s["entity_id"] for s in states if s.get("state") == "unavailable"}
        for eid in list(self.since):
            if eid not in current:
                del self.since[eid]
        for eid in current:
            if eid not in self.since:
                self.since[eid] = now if self.initialised else None
        self.initialised = True

    def unavailable(self) -> set[str]:
        return set(self.since)

    def new_within(self, window_s: float, now: float | None = None) -> set[str]:
        now = time.time() if now is None else now
        return {e for e, t in self.since.items() if t is not None and now - t <= window_s}
