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
from pathlib import Path
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


class TestHalfExpiredKey:
    """A key can half-die: the listing works, every SSH probe fails.

    The sweep then "succeeds" — 19 connected clusters, zero capabilities,
    no cores anywhere — and the dashboard called that ready. It must keep
    the data (connectedness is still true) but say what is wrong.
    """

    def test_an_all_auth_failed_sweep_carries_a_warning(self):
        store = MagicMock()
        store.load_cache.return_value = None
        worker = make_worker(store)
        worker._collector = MagicMock()
        worker._collector.collect.return_value = {
            "meta": {"cluster_count": 2, "auth_errors": 2},
            "clusters": [cluster("raider"), cluster("ruth")],
        }

        worker._collect_data()

        progress = worker.get_progress()
        assert progress["phase"] == "ready", "the data is still worth serving"
        assert progress["detail"] and "pw auth" in progress["detail"], (
            "no cores online must come with the reason attached"
        )

    def test_a_healthy_sweep_clears_the_warning(self):
        store = MagicMock()
        store.load_cache.return_value = None
        worker = make_worker(store)
        worker._progress_update(detail="stale warning from last sweep")
        worker._collector = MagicMock()
        worker._collector.collect.return_value = {
            "meta": {"cluster_count": 1, "auth_errors": 0},
            "clusters": [cluster("raider")],
        }

        worker._collect_data()

        assert worker.get_progress()["detail"] is None

    def test_a_partial_auth_failure_is_not_a_fleet_warning(self):
        """One flaky cluster is that cluster's problem, not a banner."""
        store = MagicMock()
        store.load_cache.return_value = None
        worker = make_worker(store)
        worker._collector = MagicMock()
        worker._collector.collect.return_value = {
            "meta": {"cluster_count": 3, "auth_errors": 1},
            "clusters": [cluster("a"), cluster("b"), cluster("c")],
        }

        worker._collect_data()

        assert worker.get_progress()["detail"] is None


class TestProbeFailureCaching:
    def test_a_failed_probe_is_not_cached(self):
        """All-False caps cached for the TTL outlives the fix by 10 minutes."""
        import subprocess as sp

        collector = PWClusterCollector()
        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess([], 1, stdout="", stderr="boom"),
        ):
            caps = collector._get_capabilities("pw://u/raider")
        assert not any(caps.values())
        assert "pw://u/raider" not in collector._capability_cache

    def test_an_auth_failed_probe_is_counted(self):
        import subprocess as sp

        collector = PWClusterCollector()
        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess(
                [], 1, stdout="",
                stderr="[ERROR] Authentication has expired. Please authenticate again using 'pw auth'.",
            ),
        ):
            collector._get_capabilities("pw://u/raider")
            collector._get_capabilities("pw://u/ruth")
        assert collector._auth_error_count == 2

    def test_a_generic_failure_is_not_an_auth_error(self):
        import subprocess as sp

        collector = PWClusterCollector()
        with patch(
            "subprocess.run",
            return_value=sp.CompletedProcess([], 1, stdout="", stderr="connection refused"),
        ):
            collector._get_capabilities("pw://u/raider")
        assert collector._auth_error_count == 0


class TestWorkspaceKeyRecovery:
    """The workspace key outlives the run; the run's key does not.

    ACTIVATE writes the workspace's own key into
    /etc/profile.d/parallelworks-env.sh. Measured on activate.hpc.mil:
    a detached process that adopted it kept authenticating cluster SSH
    minutes after its run completed, where the run's injected key is
    revoked in about fifteen seconds.
    """

    def test_the_workspace_key_is_preferred_over_saved_credentials(self, monkeypatch):
        import os
        import subprocess as sp

        monkeypatch.setenv("PW_API_KEY", "dead-run-key")
        collector = PWClusterCollector()

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["bash", "-c"]:
                return sp.CompletedProcess(cmd, 0, stdout="workspace-key", stderr="")
            env = kwargs.get("env")
            if env is None:
                env = os.environ
            ok = env.get("PW_API_KEY") == "workspace-key"
            return sp.CompletedProcess(cmd, 0 if ok else 1, stdout="mshaxted" if ok else "", stderr="expired")

        with patch("subprocess.run", side_effect=fake_run):
            assert collector._recover_from_stale_env_key() is True

        assert os.environ["PW_API_KEY"] == "workspace-key"

    def test_it_falls_back_to_saved_credentials(self, monkeypatch):
        import os
        import subprocess as sp

        monkeypatch.setenv("PW_API_KEY", "dead-run-key")
        collector = PWClusterCollector()

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["bash", "-c"]:
                return sp.CompletedProcess(cmd, 0, stdout="", stderr="")  # no such file
            env = kwargs.get("env")
            if env is None:
                env = os.environ
            ok = "PW_API_KEY" not in env
            return sp.CompletedProcess(cmd, 0 if ok else 1, stdout="mshaxted" if ok else "", stderr="expired")

        with patch("subprocess.run", side_effect=fake_run):
            assert collector._recover_from_stale_env_key() is True

        assert "PW_API_KEY" not in os.environ

    def test_a_workspace_key_that_does_not_work_is_not_adopted(self, monkeypatch):
        import os
        import subprocess as sp

        monkeypatch.setenv("PW_API_KEY", "dead-run-key")
        collector = PWClusterCollector()

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["bash", "-c"]:
                return sp.CompletedProcess(cmd, 0, stdout="also-dead", stderr="")
            return sp.CompletedProcess(cmd, 1, stdout="", stderr="expired")

        with patch("subprocess.run", side_effect=fake_run):
            assert collector._recover_from_stale_env_key() is False

        assert os.environ["PW_API_KEY"] == "dead-run-key", (
            "keep what we have rather than swapping in something worse"
        )

    def test_only_the_key_is_taken_from_the_profile_script(self):
        """That file also sets Kubernetes and telemetry variables."""
        import inspect

        source = inspect.getsource(PWClusterCollector._workspace_key)
        assert "printf" in source and "PW_API_KEY" in source
        assert "subshell" in source or "Sourced in a subshell" in source


class TestLauncherPrefersDurableAuth:
    SCRIPT = (
        Path(__file__).resolve().parents[2] / "scripts" / "serve-endpoint.sh"
    ).read_text()

    def test_it_adopts_the_workspace_key(self):
        assert "/etc/profile.d/parallelworks-env.sh" in self.SCRIPT
        assert "outlives this run" in self.SCRIPT

    def test_it_only_adopts_a_key_that_works(self):
        assert 'PW_API_KEY="${workspace_key}" pw auth whoami' in self.SCRIPT

    def test_it_takes_only_the_key_from_that_file(self):
        """Sourced in a subshell: the file also sets unrelated variables."""
        block = self.SCRIPT[self.SCRIPT.index("PW_ENV_FILE=") :]
        block = block[: block.index("Publishing as")]
        assert "$(" in block and "printf '%s' \"${PW_API_KEY:-}\"" in block


class TestParallelSweep:
    """A sweep is dominated by SSH round trips, so run several at once.

    Measured on a cloud host: one `pw ssh` takes ~3.5s, each cluster costs
    several, and nineteen clusters took six minutes end to end. The bound
    is rate_limiting.max_concurrent_ssh, which the config had advertised
    since before anything read it.
    """

    @staticmethod
    def clusters(count):
        return [
            {"uri": f"pw://u/c{i}", "status": "active", "type": "existing"}
            for i in range(count)
        ]

    def test_clusters_are_swept_concurrently(self):
        import threading
        import time

        collector = PWClusterCollector(max_concurrent=4)
        active = []
        peak = 0
        lock = threading.Lock()

        def slow(cluster):
            nonlocal peak
            with lock:
                active.append(cluster["uri"])
                peak = max(peak, len(active))
            time.sleep(0.05)
            with lock:
                active.remove(cluster["uri"])
            return {"cluster_metadata": {"name": cluster["uri"].rsplit("/", 1)[-1]}}

        with patch.object(collector, "get_active_clusters", return_value=self.clusters(8)), \
             patch.object(collector, "_process_cluster", side_effect=slow):
            result = collector.collect()

        assert len(result["clusters"]) == 8
        assert peak > 1, "a sequential sweep is what made this slow"
        assert peak <= 4, "and the bound must be respected"

    def test_the_bound_is_honoured_exactly(self):
        import threading
        import time

        collector = PWClusterCollector(max_concurrent=2)
        peak = 0
        active = 0
        lock = threading.Lock()

        def slow(cluster):
            nonlocal peak, active
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return {"cluster_metadata": {"name": "x"}}

        with patch.object(collector, "get_active_clusters", return_value=self.clusters(6)), \
             patch.object(collector, "_process_cluster", side_effect=slow):
            collector.collect()
        assert peak <= 2

    def test_one_worker_still_works(self):
        """max_concurrent_ssh: 1 must behave exactly as the old loop did."""
        collector = PWClusterCollector(max_concurrent=1)
        seen = []
        with patch.object(collector, "get_active_clusters", return_value=self.clusters(3)), \
             patch.object(
                 collector,
                 "_process_cluster",
                 side_effect=lambda c: seen.append(c["uri"]) or {"cluster_metadata": {"name": c["uri"][-2:]}},
             ):
            collector.collect()
        assert seen == ["pw://u/c0", "pw://u/c1", "pw://u/c2"]

    def test_one_failing_cluster_does_not_end_the_sweep(self):
        collector = PWClusterCollector(max_concurrent=3)

        def flaky(cluster):
            if cluster["uri"].endswith("c1"):
                raise RuntimeError("ssh exploded")
            return {"cluster_metadata": {"name": cluster["uri"].rsplit("/", 1)[-1]}}

        with patch.object(collector, "get_active_clusters", return_value=self.clusters(4)), \
             patch.object(collector, "_process_cluster", side_effect=flaky):
            result = collector.collect()
        assert len(result["clusters"]) == 3, "the other three still count"

    def test_progress_is_reported_once_per_cluster(self):
        collector = PWClusterCollector(max_concurrent=3)
        completes = []
        with patch.object(collector, "get_active_clusters", return_value=self.clusters(5)), \
             patch.object(
                 collector,
                 "_process_cluster",
                 side_effect=lambda c: {"cluster_metadata": {"name": c["uri"][-2:]}},
             ):
            collector.collect(
                progress_cb=lambda phase, i, total, name, data: (
                    completes.append(i) if phase == "complete" else None
                )
            )
        assert sorted(completes) == [1, 2, 3, 4, 5], (
            "the counter must not double-count or skip under concurrency"
        )

    def test_newly_connected_clusters_are_submitted_first(self):
        collector = PWClusterCollector(max_concurrent=1)
        collector._known_clusters = {"pw://u/c0", "pw://u/c1"}
        order = []
        with patch.object(collector, "get_active_clusters", return_value=self.clusters(3)), \
             patch.object(
                 collector,
                 "_process_cluster",
                 side_effect=lambda c: order.append(c["uri"]) or {"cluster_metadata": {"name": "x"}},
             ):
            collector.collect()
        assert order[0] == "pw://u/c2"


class TestConcurrentCacheMerge:
    def test_two_clusters_finishing_at_once_do_not_lose_each_other(self):
        """Read-modify-write on one file, now from several threads."""
        import threading

        store = MagicMock()
        state = {"cache": []}
        store.load_cache.side_effect = lambda name: list(state["cache"])

        def save(name, value):
            # Widen the window a real filesystem would give this race.
            import time

            time.sleep(0.01)
            state["cache"] = list(value)

        store.save_cache.side_effect = save
        worker = make_worker(store)
        worker._progress_update(first_sweep_complete=True)

        threads = [
            threading.Thread(
                target=worker._on_cluster_progress,
                args=("complete", i, 4, f"c{i}", cluster(f"c{i}")),
            )
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        names = {(c.get("cluster_metadata") or {}).get("name") for c in state["cache"]}
        assert names == {"c0", "c1", "c2", "c3"}, (
            f"a cluster was lost to the race: {sorted(names)}"
        )
