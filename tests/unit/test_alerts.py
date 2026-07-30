"""Tests for state-change alerting."""

from datetime import datetime, timedelta

import pytest

from src.server.alerts import AlertDispatcher, classify_transition


class TestClassifyTransition:
    def test_first_sighting_is_not_an_alert(self):
        # Otherwise a fresh install pages you once per system on startup.
        assert classify_transition(None, "UP") is None
        assert classify_transition(None, "DOWN") is None

    def test_no_change_is_not_an_alert(self):
        assert classify_transition("UP", "UP") is None

    def test_going_down_is_critical(self):
        assert classify_transition("UP", "DOWN") == {
            "kind": "degraded",
            "severity": "critical",
        }

    def test_degrading_is_a_warning(self):
        assert classify_transition("UP", "DEGRADED")["severity"] == "warning"
        assert classify_transition("ACTIVE", "MAINTENANCE")["severity"] == "warning"

    def test_recovery_is_informational(self):
        assert classify_transition("DOWN", "UP") == {
            "kind": "recovered",
            "severity": "info",
        }

    def test_movement_between_unhealthy_states(self):
        assert classify_transition("DOWN", "MAINTENANCE") == {
            "kind": "changed",
            "severity": "warning",
        }


@pytest.fixture
def dispatcher():
    sent = []

    d = AlertDispatcher(
        enabled=True,
        webhook_url="https://example.invalid/hook",
        min_severity="warning",
        cooldown_seconds=900,
        deployment_name="Test Monitor",
        log=lambda msg: None,
    )
    # Capture instead of delivering; delivery itself is urllib and not
    # worth a live socket in unit tests.
    d._send_async = lambda event: sent.append(event)
    d.sent = sent
    return d


class TestDispatcher:
    def test_sends_on_outage(self, dispatcher):
        queued = dispatcher.record_transitions(
            [("system:narwhal", "UP", "DOWN", {"name": "Narwhal"})]
        )
        assert len(queued) == 1
        assert dispatcher.sent[0]["name"] == "Narwhal"
        assert dispatcher.sent[0]["severity"] == "critical"

    def test_respects_min_severity(self, dispatcher):
        # Recovery is "info", below the configured "warning" floor.
        assert dispatcher.record_transitions([("system:a", "DOWN", "UP", None)]) == []
        # ...but it is still recorded for the events feed.
        assert dispatcher.recent_events()[0]["kind"] == "recovered"

    def test_cooldown_suppresses_flapping(self, dispatcher):
        now = datetime(2026, 7, 30, 12, 0, 0)
        first = dispatcher.record_transitions(
            [("system:a", "UP", "DOWN", None)], now=now
        )
        second = dispatcher.record_transitions(
            [("system:a", "DOWN", "UP", None)], now=now + timedelta(minutes=1)
        )
        third = dispatcher.record_transitions(
            [("system:a", "UP", "DOWN", None)], now=now + timedelta(minutes=2)
        )
        assert len(first) == 1
        assert second == []  # below min_severity anyway
        assert third == []  # inside the cooldown

        later = dispatcher.record_transitions(
            [("system:a", "UP", "DOWN", None)], now=now + timedelta(minutes=20)
        )
        assert len(later) == 1

    def test_cooldown_is_per_system(self, dispatcher):
        now = datetime(2026, 7, 30, 12, 0, 0)
        dispatcher.record_transitions([("system:a", "UP", "DOWN", None)], now=now)
        other = dispatcher.record_transitions(
            [("system:b", "UP", "DOWN", None)], now=now + timedelta(seconds=5)
        )
        assert len(other) == 1

    def test_disabled_dispatcher_still_records_events(self):
        d = AlertDispatcher(enabled=False, log=lambda msg: None)
        assert d.record_transitions([("system:a", "UP", "DOWN", None)]) == []
        assert d.recent_events()[0]["status"] == "DOWN"
        assert d.enabled is False

    def test_enabled_requires_a_webhook(self):
        assert AlertDispatcher(enabled=True, webhook_url=None).enabled is False

    def test_payload_is_chat_and_machine_readable(self, dispatcher):
        dispatcher.dashboard_url = "https://status.example.mil/"
        dispatcher.record_transitions(
            [("system:narwhal", "UP", "DOWN", {"name": "Narwhal"})]
        )
        payload = dispatcher.build_payload(dispatcher.sent[0])
        assert "Narwhal" in payload["text"]
        assert "DOWN" in payload["text"]
        assert "was UP" in payload["text"]
        assert "https://status.example.mil/" in payload["text"]
        assert payload["event"]["entity"] == "system:narwhal"

    def test_recent_events_are_newest_first_and_bounded(self, dispatcher):
        for i in range(5):
            dispatcher.record_transitions([(f"system:{i}", "UP", "DOWN", None)])
        events = dispatcher.recent_events(limit=3)
        assert len(events) == 3
        assert events[0]["entity"] == "system:4"
