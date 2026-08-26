"""The cluster monitor's freshness behaviour.

A sweep of a large fleet takes minutes. Three things follow, and each
went wrong before it was pinned here:

- results must publish per cluster, not per sweep, or a newly connected
  machine stays invisible long after its own collection finished;
- newly connected clusters must be collected first;
- the idle wait between sweeps must notice the fleet changing, or a new
  connection waits out the full interval before anything even looks.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from src.collectors.pw_cluster import PWClusterCollector
from src.server.workers import ClusterMonitorWorker


def make_worker(store, interval=120):
    worker = ClusterMonitorWorker(
        store=store,
        interval_seconds=interval,
        run_immediately=True,
    )
    return worker


def cluster(name):
    return {"cluster_metadata": {"name": name, "uri": f"pw://u/{name}", "status": "active"}}


class TestIncrementalPublish:
    def test_a_finished_cluster_lands_in_the_cache_immediately(self):
        """Even on later sweeps — end-of-sweep-only was minutes of lag."""
        store = MagicMock()
        store.load_cache.return_value = [cluster("old")]
        worker = make_worker(store)
        worker._progress_update(first_sweep_complete=True)

        worker._on_cluster_progress("complete", 1, 18, "coral", cluster("coral"))

        saved = store.save_cache.call_args[0]
        assert saved[0] == "cluster_usage"
        names = {
            (c.get("cluster_metadata") or {}).get("name") for c in saved[1]
        }
        assert names == {"old", "coral"}, (
            "merge with what is cached — replacing would erase the rest of "
            "the fleet mid-sweep"
        )

    def test_the_merge_updates_rather_than_duplicates(self):
        store = MagicMock()
        stale = cluster("coral")
        stale["queue_data"] = ["stale"]
        fresh = cluster("coral")
        fresh["queue_data"] = ["fresh"]
        store.load_cache.return_value = [stale]
        worker = make_worker(store)
        worker._progress_update(first_sweep_complete=True)

        worker._on_cluster_progress("complete", 1, 1, "coral", fresh)

        saved = store.save_cache.call_args[0][1]
        assert len(saved) == 1
        assert saved[0]["queue_data"] == ["fresh"]

    def test_a_cache_failure_does_not_kill_the_sweep(self):
        store = MagicMock()
        store.load_cache.side_effect = OSError("disk")
        worker = make_worker(store)
        worker._on_cluster_progress("complete", 1, 1, "coral", cluster("coral"))


class TestListingWatcher:
    def test_a_changed_listing_cuts_the_wait_short(self):
        store = MagicMock()
        worker = make_worker(store)
        worker.LISTING_POLL_SECONDS = 0.01
        worker._last_active_uris = {"pw://u/old"}
        worker._collector = MagicMock()
        worker._collector.get_active_clusters.return_value = [
            {"uri": "pw://u/old"},
            {"uri": "pw://u/coral"},
        ]

        import time as _time

        began = _time.monotonic()
        stopped = worker._wait_watching_listing(30)
        elapsed = _time.monotonic() - began

        assert stopped is False, "return to the loop and collect now"
        # A timeout also returns False, so the assertion that matters is
        # that it came back promptly — the first version of the watcher
        # updated its baseline before comparing, never saw any change, and
        # sat out the full wait while this test quietly passed on the
        # ambiguous return value.
        assert elapsed < 5, "the change must cut the wait short"
        assert worker._collector.get_active_clusters.called
        # And the listing was published for the fleet page to read.
        names = [c[0][0] for c in store.save_cache.call_args_list]
        assert "cluster_listing" in names

    def test_an_unchanged_listing_keeps_waiting(self):
        store = MagicMock()
        worker = make_worker(store)
        worker.LISTING_POLL_SECONDS = 0.01
        worker._last_active_uris = {"pw://u/old"}
        worker._collector = MagicMock()
        worker._collector.get_active_clusters.return_value = [{"uri": "pw://u/old"}]

        assert worker._wait_watching_listing(0.05) is False
        assert worker._collector.get_active_clusters.call_count >= 2

    def test_a_failing_listing_is_not_fatal(self):
        store = MagicMock()
        worker = make_worker(store)
        worker.LISTING_POLL_SECONDS = 0.01
        worker._collector = MagicMock()
        worker._collector.get_active_clusters.side_effect = RuntimeError("pw down")

        assert worker._wait_watching_listing(0.05) is False

    def test_stop_still_wins(self):
        store = MagicMock()
        worker = make_worker(store)
        worker._collector = MagicMock()
        worker._stop_event.set()
        assert worker._wait_watching_listing(30) is True

    def test_the_listing_snapshot_names_the_machines(self):
        store = MagicMock()
        worker = make_worker(store)
        worker._save_listing([{"uri": "pw://u/coral"}])
        name, payload = store.save_cache.call_args[0]
        assert name == "cluster_listing"
        assert payload["active"] == [{"uri": "pw://u/coral", "name": "coral"}]
        assert payload["checked_at"].endswith("Z")


class TestNewClustersFirst:
    def test_unknown_clusters_are_collected_before_known_ones(self):
        collector = PWClusterCollector()
        collector._known_clusters = {"pw://u/old1", "pw://u/old2"}
        order = []

        with patch.object(
            collector,
            "get_active_clusters",
            return_value=[
                {"uri": "pw://u/old1", "status": "active", "type": "existing"},
                {"uri": "pw://u/coral", "status": "active", "type": "existing"},
                {"uri": "pw://u/old2", "status": "active", "type": "existing"},
            ],
        ), patch.object(
            collector,
            "_process_cluster",
            side_effect=lambda c: order.append(c["uri"]) or {"cluster_metadata": {"name": c["uri"].rsplit("/", 1)[-1]}},
        ):
            collector.collect()

        assert order[0] == "pw://u/coral", (
            "whoever just connected a machine is watching for it; the rest "
            "of the fleet can wait one slot"
        )
        assert set(order) == {"pw://u/old1", "pw://u/old2", "pw://u/coral"}


class TestAuthExpiry:
    """An expired credential must pause the monitor, not kill it.

    A dashboard launched from a workflow run inherits that run's injected
    PW_API_KEY. The key dies with the run's grace period, and it shadows
    the workspace's credentials file — so the old exit-on-expiry meant a
    dashboard that served week-old caches forever, with no visible reason.
    """

    def test_waiting_names_the_state_for_the_ui(self):
        worker = make_worker(MagicMock())
        worker.AUTH_RETRY_SECONDS = 0.01
        worker._collector = MagicMock()
        worker._collector.check_auth.return_value = (True, "mshaxted")

        stopped = worker._wait_for_auth()

        assert stopped is False, "auth came back — resume, do not exit"
        progress = worker.get_progress()
        assert progress["phase"] == "idle"

    def test_the_paused_state_carries_a_reason(self):
        worker = make_worker(MagicMock())
        worker.AUTH_RETRY_SECONDS = 5
        worker._collector = MagicMock()
        worker._stop_event.set()  # return immediately, leaving the state set

        worker._wait_for_auth()

        # The state written on entry is what the API serves while paused.
        worker2 = make_worker(MagicMock())
        worker2._progress_update(phase="auth_expired", detail="x")
        assert worker2.get_progress()["phase"] == "auth_expired"
        assert "pw auth" in str(worker._progress.get("detail") or "pw auth")

    def test_stop_wins_over_the_auth_wait(self):
        worker = make_worker(MagicMock())
        worker._collector = MagicMock()
        worker._stop_event.set()
        assert worker._wait_for_auth() is True

    def test_the_worker_no_longer_exits_on_expiry(self):
        """Pin the run loop: expiry leads to the wait, never to a break."""
        import inspect

        from src.server import workers

        source = inspect.getsource(workers.ClusterMonitorWorker.run)
        assert "FATAL" not in source, "an expired token is a state, not a death"
        assert "_wait_for_auth" in source


class TestStaleEnvKeyRecovery:
    """PW_API_KEY shadows the credentials file even after it dies."""

    def test_a_dead_env_key_is_dropped_when_the_file_works(self, monkeypatch):
        import subprocess as sp

        monkeypatch.setenv("PW_API_KEY", "dead-key")
        collector = PWClusterCollector()

        import os

        def fake_run(cmd, **kwargs):
            # Without the key the credentials file authenticates fine.
            # env=None inherits the process environment, which recovery
            # mutates — consult the real thing in that case.
            env = kwargs.get("env")
            if env is None:
                env = os.environ
            ok = "PW_API_KEY" not in env
            return sp.CompletedProcess(cmd, 0 if ok else 1, stdout="mshaxted" if ok else "", stderr="expired")

        with patch("subprocess.run", side_effect=fake_run):
            ok, detail = collector.check_auth()

        assert ok is True
        assert "PW_API_KEY" not in os.environ, (
            "the dead key must be dropped so every later pw call inherits "
            "the working credentials"
        )

    def test_no_recovery_without_an_env_key(self, monkeypatch):
        import subprocess as sp

        monkeypatch.delenv("PW_API_KEY", raising=False)
        collector = PWClusterCollector()
        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess([], 1, stdout="", stderr="expired"),
        ):
            ok, _ = collector.check_auth()
        assert ok is False

    def test_the_key_is_kept_when_nothing_else_works(self, monkeypatch):
        import os
        import subprocess as sp

        monkeypatch.setenv("PW_API_KEY", "only-credential")
        collector = PWClusterCollector()
        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess([], 1, stdout="", stderr="expired"),
        ):
            ok, _ = collector.check_auth()
        assert ok is False
        assert os.environ.get("PW_API_KEY") == "only-credential", (
            "dropping the only credential would make a bad state worse"
        )
