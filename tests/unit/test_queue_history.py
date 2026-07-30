"""Tests for queue depth history and wait estimates."""

from datetime import datetime, timedelta

import pytest

from src.data.queue_history import QueueHistoryStore, format_duration


def cluster(name="narwhal", *, queues, cores_total=1000):
    return {
        "cluster_metadata": {"name": name},
        "queue_data": {
            "cluster_totals": {"cores_total": cores_total},
            "queues": queues,
        },
    }


def queue(name="standard", *, running=0, pending=0, jobs_running=0, jobs_pending=0):
    return {
        "queue_name": name,
        "cores_running": running,
        "cores_pending": pending,
        "jobs_running": jobs_running,
        "jobs_pending": jobs_pending,
    }


@pytest.fixture
def store(tmp_path):
    return QueueHistoryStore(tmp_path / "history.db")


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "under 2 minutes"),
            (60, "under 2 minutes"),
            (600, "~10 minutes"),
            (5400, "~1.5 hours"),
            (172800, "~2.0 days"),
        ],
    )
    def test_units(self, seconds, expected):
        assert format_duration(seconds) == expected

    def test_none_passes_through(self):
        assert format_duration(None) is None


class TestRecording:
    def test_records_one_row_per_queue(self, store):
        recorded = store.record_clusters(
            [cluster(queues=[queue("standard"), queue("debug")])]
        )
        assert recorded == 2

    def test_skips_clusters_without_queues(self, store):
        assert store.record_clusters([cluster(queues=[])]) == 0
        assert store.record_clusters([]) == 0

    def test_prune_drops_old_samples(self, store):
        old = datetime.utcnow() - timedelta(days=30)
        store.record_clusters([cluster(queues=[queue()])], now=old)
        store.record_clusters([cluster(queues=[queue()])])
        assert store.prune(days=7) == 1


class TestEstimates:
    def _series(self, store, running_values, *, pending, now, step_hours=1):
        for index, running in enumerate(running_values):
            stamp = now - timedelta(hours=step_hours * (len(running_values) - 1 - index))
            store.record_clusters(
                [cluster(queues=[queue(running=running, pending=pending)])], now=stamp
            )

    def test_estimate_from_observed_turnover(self, store):
        now = datetime(2026, 7, 30, 12, 0, 0)
        # 100 cores freed, then 150 — 250 cores over 3h is ~83 cores/h.
        self._series(store, [800, 700, 750, 600], pending=300, now=now)

        estimate = store.estimate_waits(window_hours=6, now=now)[("narwhal", "standard")]

        assert estimate["drain_rate_cores_per_hour"] == 83
        assert estimate["pending_cores"] == 300
        # 300 cores waiting / 83 per hour ~= 3.6h
        assert 3.5 * 3600 < estimate["wait_seconds"] < 3.7 * 3600
        assert "observed turnover" in estimate["basis"]

    def test_no_backlog_means_no_wait(self, store):
        now = datetime(2026, 7, 30, 12, 0, 0)
        self._series(store, [800, 700], pending=0, now=now)

        estimate = store.estimate_waits(window_hours=6, now=now)[("narwhal", "standard")]
        assert estimate["wait_seconds"] == 0
        assert estimate["wait_display"] == "no backlog"

    def test_no_turnover_yields_no_number(self, store):
        """A saturated queue must not produce an invented estimate."""
        now = datetime(2026, 7, 30, 12, 0, 0)
        self._series(store, [800, 800, 800], pending=500, now=now)

        estimate = store.estimate_waits(window_hours=6, now=now)[("narwhal", "standard")]
        assert estimate["wait_seconds"] is None
        assert estimate["confidence"] == "none"
        assert "no core turnover" in estimate["basis"]

    def test_single_sample_is_not_enough(self, store):
        now = datetime(2026, 7, 30, 12, 0, 0)
        store.record_clusters(
            [cluster(queues=[queue(running=800, pending=100)])], now=now
        )
        assert store.estimate_waits(window_hours=6, now=now) == {}

    def test_samples_outside_the_window_are_ignored(self, store):
        now = datetime(2026, 7, 30, 12, 0, 0)
        self._series(store, [900, 800], pending=100, now=now - timedelta(hours=20))
        assert store.estimate_waits(window_hours=6, now=now) == {}

    def test_confidence_grows_with_samples(self, store):
        now = datetime(2026, 7, 30, 12, 0, 0)
        self._series(store, [800, 700], pending=100, now=now)
        low = store.estimate_waits(window_hours=6, now=now)[("narwhal", "standard")]
        assert low["confidence"] == "low"

        store.record_clusters(
            [cluster(queues=[queue(running=600, pending=100)])],
            now=now - timedelta(minutes=30),
        )
        store.record_clusters(
            [cluster(queues=[queue(running=500, pending=100)])], now=now
        )
        better = store.estimate_waits(window_hours=6, now=now)[("narwhal", "standard")]
        assert better["confidence"] == "medium"

    def test_queues_are_estimated_independently(self, store):
        now = datetime(2026, 7, 30, 12, 0, 0)
        for index, running in enumerate([800, 600]):
            stamp = now - timedelta(hours=1 - index)
            store.record_clusters(
                [
                    cluster(
                        queues=[
                            queue("standard", running=running, pending=200),
                            queue("debug", running=10, pending=0),
                        ]
                    )
                ],
                now=stamp,
            )
        estimates = store.estimate_waits(window_hours=6, now=now)
        assert estimates[("narwhal", "standard")]["wait_seconds"] > 0
        assert estimates[("narwhal", "debug")]["wait_display"] == "no backlog"
