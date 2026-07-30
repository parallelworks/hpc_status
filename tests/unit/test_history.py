"""Tests for the replayable history behind the topology timeline."""

from datetime import datetime, timedelta

import pytest

from src.data.history import build_history, frame_times, status_at_frames
from src.data.persistence import DataStore


NOW = datetime(2026, 7, 30, 12, 0, 0)


class TestFrameTimes:
    def test_covers_the_window_at_the_requested_step(self):
        times = frame_times(window_hours=6, step_minutes=30, now=NOW)
        assert times[0] == NOW - timedelta(hours=6)
        assert times[-1] == NOW
        assert len(times) == 13

    def test_step_is_widened_rather_than_returning_thousands_of_frames(self):
        times = frame_times(window_hours=24 * 7, step_minutes=1, now=NOW)
        assert len(times) <= 240
        assert times[-1] == NOW

    def test_absurd_windows_are_clamped(self):
        assert frame_times(window_hours=0, step_minutes=15, now=NOW)
        assert frame_times(window_hours=10_000, step_minutes=60, now=NOW)


class TestStatusAtFrames:
    def test_carries_the_last_known_status_forward(self):
        """A transition log says nothing about the gaps; a timeline needs them."""
        times = [NOW - timedelta(hours=h) for h in (4, 3, 2, 1, 0)]
        events = {
            "system:a": [
                (NOW - timedelta(hours=5), "UP"),
                (NOW - timedelta(hours=2, minutes=30), "DOWN"),
                (NOW - timedelta(minutes=30), "UP"),
            ]
        }
        assert status_at_frames(events, times)["system:a"] == [
            "UP",
            "UP",
            "DOWN",
            "DOWN",
            "UP",
        ]

    def test_frames_before_the_first_sighting_are_unknown(self):
        times = [NOW - timedelta(hours=2), NOW]
        events = {"system:a": [(NOW - timedelta(hours=1), "UP")]}
        assert status_at_frames(events, times)["system:a"] == [None, "UP"]


@pytest.fixture
def store(tmp_path):
    return DataStore(tmp_path)


def record_status(store, entity, status, when):
    with store._get_connection() as conn:
        conn.execute(
            "INSERT INTO system_history (system_name, status, timestamp, details) "
            "VALUES (?, ?, ?, ?)",
            (entity, status, when.isoformat(), None),
        )


def record_queue(store, cluster, when, *, running, pending=0, total=1000, queue="standard"):
    with store.queue_history._connect() as conn:
        conn.execute(
            "INSERT INTO queue_samples (cluster, queue, timestamp, jobs_running, "
            "jobs_pending, cores_running, cores_pending, cores_total) "
            "VALUES (?, ?, ?, 0, 0, ?, ?, ?)",
            (cluster, queue, when.isoformat(), running, pending, total),
        )


class TestBuildHistory:
    def test_empty_store_yields_frames_with_nothing_in_them(self, store):
        payload = build_history(store, window_hours=2, step_minutes=30, now=NOW)
        assert payload["frames"]
        assert all(frame["systems"] == {} for frame in payload["frames"])
        assert payload["systems"] == []

    def test_status_and_utilization_land_in_the_same_frame(self, store):
        record_status(store, "system:narwhal", "UP", NOW - timedelta(hours=3))
        record_queue(store, "narwhal", NOW - timedelta(minutes=20), running=800, total=1000)

        payload = build_history(store, window_hours=2, step_minutes=30, now=NOW)
        last = payload["frames"][-1]["systems"]["narwhal"]

        assert last["status"] == "UP"
        assert last["utilization_percent"] == 80.0
        assert last["cores_total"] == 1000

    def test_cluster_and_system_entities_describe_one_machine(self, store):
        """system:narwhal and cluster:narwhal are the same box."""
        record_status(store, "cluster:narwhal", "ACTIVE", NOW - timedelta(hours=1))
        payload = build_history(store, window_hours=2, step_minutes=30, now=NOW)
        assert payload["frames"][-1]["systems"]["narwhal"]["status"] == "ACTIVE"

    def test_a_gap_between_sweeps_holds_the_last_reading(self, store):
        """A missed sweep is a cluster we did not ask, not a cluster at zero."""
        record_queue(store, "narwhal", NOW - timedelta(hours=2), running=500, total=1000)
        payload = build_history(store, window_hours=3, step_minutes=30, now=NOW)
        readings = [
            frame["systems"].get("narwhal", {}).get("utilization_percent")
            for frame in payload["frames"]
        ]
        assert readings[-1] == 50.0
        # ...and nothing is invented for the time before the first sample.
        assert readings[0] is None

    def test_queues_of_one_cluster_are_summed_not_repeated(self, store):
        moment = NOW - timedelta(minutes=10)
        record_queue(store, "narwhal", moment, running=300, total=1000, queue="standard")
        record_queue(store, "narwhal", moment, running=200, total=1000, queue="debug")

        payload = build_history(store, window_hours=1, step_minutes=30, now=NOW)
        last = payload["frames"][-1]["systems"]["narwhal"]
        assert last["cores_running"] == 500
        assert last["cores_total"] == 1000  # not 2000
        assert last["utilization_percent"] == 50.0

    def test_utilization_is_capped_at_one_hundred(self, store):
        record_queue(store, "narwhal", NOW - timedelta(minutes=5), running=5000, total=1000)
        payload = build_history(store, window_hours=1, step_minutes=30, now=NOW)
        assert payload["frames"][-1]["systems"]["narwhal"]["utilization_percent"] == 100.0

    def test_samples_outside_the_window_are_left_out(self, store):
        record_queue(store, "narwhal", NOW - timedelta(days=3), running=800, total=1000)
        payload = build_history(store, window_hours=2, step_minutes=30, now=NOW)
        assert all("narwhal" not in frame["systems"] for frame in payload["frames"])

    def test_frames_are_ordered_oldest_first(self, store):
        record_status(store, "system:a", "UP", NOW - timedelta(hours=5))
        payload = build_history(store, window_hours=4, step_minutes=60, now=NOW)
        stamps = [frame["at"] for frame in payload["frames"]]
        assert stamps == sorted(stamps)
        assert payload["from"] == stamps[0]
        assert payload["to"] == stamps[-1]
