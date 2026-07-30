"""Outbound alerts for system state changes.

The monitor already knows the moment a system flips UP → DOWN; this turns
that into a notification instead of something you only see if you happen to
have the dashboard open.

Delivery is a plain webhook POST, which covers Slack, Teams, and most
incident tools without this project growing an SMTP client or per-vendor
integrations. The payload carries both a human ``text`` field (what chat
clients render) and the structured event (what automation wants).

Two rules keep it from becoming noise or a liability:

- Sending happens on a background thread. A hung webhook must never stall
  a collection sweep.
- Every system has a cooldown, so a machine flapping every poll produces
  one alert, not sixty.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Statuses that mean "this system is usable".
HEALTHY = frozenset({"UP", "ACTIVE", "ON", "RUNNING", "ONLINE"})

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


def classify_transition(previous: Optional[str], current: str) -> Optional[Dict[str, str]]:
    """Describe a status change, or None when it is not worth alerting on.

    The first sighting of a system is not an alert: on a fresh install every
    system would page you simply for existing.
    """
    current = (current or "UNKNOWN").upper()
    if previous is None:
        return None
    previous = previous.upper()
    if previous == current:
        return None

    was_healthy = previous in HEALTHY
    is_healthy = current in HEALTHY

    if was_healthy and not is_healthy:
        severity = "critical" if current == "DOWN" else "warning"
        return {"kind": "degraded", "severity": severity}
    if not was_healthy and is_healthy:
        return {"kind": "recovered", "severity": "info"}
    # Movement between two unhealthy states (DOWN → MAINTENANCE, say).
    return {"kind": "changed", "severity": "warning"}


class AlertDispatcher:
    """Turns recorded status transitions into webhook deliveries."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        webhook_url: Optional[str] = None,
        min_severity: str = "warning",
        cooldown_seconds: int = 900,
        timeout: int = 10,
        deployment_name: str = "HPC Status Monitor",
        dashboard_url: Optional[str] = None,
        log=print,
    ):
        self.enabled = bool(enabled and webhook_url)
        self.webhook_url = webhook_url
        self.min_severity = min_severity if min_severity in SEVERITY_ORDER else "warning"
        self.cooldown = timedelta(seconds=max(0, cooldown_seconds))
        self.timeout = timeout
        self.deployment_name = deployment_name
        self.dashboard_url = dashboard_url
        self._log = log
        self._last_sent: Dict[str, datetime] = {}
        self._lock = threading.Lock()
        # Recent events are kept in memory for /api/events, whether or not a
        # webhook is configured — the audit trail is useful on its own.
        self._recent: List[Dict[str, Any]] = []

    # --- event intake -----------------------------------------------------

    def record_transitions(
        self,
        transitions: Iterable[Tuple[str, Optional[str], str, Optional[Dict]]],
        *,
        now: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Register (entity, previous, current, details) transitions.

        Returns the events that were queued for delivery.
        """
        stamp = now or datetime.utcnow()
        queued: List[Dict[str, Any]] = []

        for entity, previous, current, details in transitions:
            classification = classify_transition(previous, current)
            if not classification:
                continue
            event = {
                "entity": entity,
                "name": (details or {}).get("name") or entity.split(":", 1)[-1],
                "previous": (previous or "").upper() or None,
                "status": (current or "UNKNOWN").upper(),
                "kind": classification["kind"],
                "severity": classification["severity"],
                "at": stamp.replace(microsecond=0).isoformat() + "Z",
                "details": details or {},
            }
            with self._lock:
                self._recent.insert(0, event)
                del self._recent[200:]
            if self._should_send(event, stamp):
                queued.append(event)

        for event in queued:
            self._send_async(event)
        return queued

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._recent[: max(1, limit)])

    # --- delivery ---------------------------------------------------------

    def _should_send(self, event: Dict[str, Any], now: datetime) -> bool:
        if not self.enabled:
            return False
        if SEVERITY_ORDER[event["severity"]] < SEVERITY_ORDER[self.min_severity]:
            return False
        with self._lock:
            last = self._last_sent.get(event["entity"])
            if last and now - last < self.cooldown:
                return False
            self._last_sent[event["entity"]] = now
        return True

    def _send_async(self, event: Dict[str, Any]) -> None:
        thread = threading.Thread(
            target=self._send,
            args=(event,),
            name="alert-dispatch",
            daemon=True,
        )
        thread.start()

    def _send(self, event: Dict[str, Any]) -> None:
        payload = self.build_payload(event)
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if response.status >= 300:
                    self._log(f"[alerts] Webhook returned HTTP {response.status}")
        except urllib.error.URLError as exc:
            self._log(f"[alerts] Webhook delivery failed: {exc}")
        except Exception as exc:
            self._log(f"[alerts] Webhook delivery error: {exc}")

    def build_payload(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Build the webhook body: chat-friendly text plus structured data."""
        icon = {"critical": "🔴", "warning": "🟠", "info": "🟢"}[event["severity"]]
        verb = {
            "degraded": "is",
            "recovered": "recovered — now",
            "changed": "changed to",
        }[event["kind"]]
        text = (
            f"{icon} {self.deployment_name}: {event['name']} {verb} {event['status']}"
        )
        if event["previous"]:
            text += f" (was {event['previous']})"
        if self.dashboard_url:
            text += f"\n{self.dashboard_url}"
        return {"text": text, "event": event, "source": self.deployment_name}
