"""Background workers for data refresh and monitoring.

Handles periodic data collection and state management.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..data.persistence import DataStore
from ..data.topology import slugify


def _log(msg: str) -> None:
    """Print with flush for reliable output in daemon threads."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


class DashboardState:
    """Manages dashboard state with caching and persistence.

    Loads cached data on startup for instant availability,
    then refreshes from live sources in the background.
    """

    def __init__(self, store: DataStore, generate_fn, source_name: str = "fleet_status"):
        self.store = store
        self.generate_fn = generate_fn
        self.source_name = source_name
        self._payload: Optional[Dict] = None
        self._last_error: Optional[str] = None
        self._last_refresh_ts: Optional[float] = None
        self._payload_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._is_loading = False
        self._load_initial_data()

    def _load_initial_data(self) -> None:
        """Load cached data immediately on startup."""
        cached = self.store.load_cache(self.source_name, max_age=timedelta(hours=24))
        if cached:
            with self._payload_lock:
                self._payload = cached
                # Mark as from cache
                if isinstance(self._payload, dict):
                    self._payload.setdefault("meta", {})
                    self._payload["meta"]["from_cache"] = True
        else:
            self._is_loading = True

    def refresh(self, *, blocking: bool = True) -> Tuple[bool, str]:
        """Refresh data from source.

        Args:
            blocking: If False, returns immediately if refresh already in progress

        Returns:
            Tuple of (success, message)
        """
        if not self._refresh_lock.acquire(blocking=blocking):
            return False, "Refresh already in progress."
        try:
            payload = self.generate_fn()

            # Guard: do not overwrite good data with empty results
            systems = payload.get("systems", []) if isinstance(payload, dict) else []
            if not systems and self._payload and self._payload.get("systems"):
                msg = "Collection returned 0 systems; keeping stale data"
                _log(f"[{self.source_name}] {msg}")
                with self._payload_lock:
                    self._last_error = msg
                    self._last_refresh_ts = time.time()
                    if isinstance(self._payload, dict):
                        self._payload.setdefault("meta", {})["stale"] = True
                # Save snapshot for history but do NOT overwrite cache or payload
                self.store.save_snapshot(self.source_name, payload)
                return False, msg

            # Save to cache
            self.store.save_cache(self.source_name, payload)
            # Save snapshot to DB for history
            self.store.save_snapshot(self.source_name, payload)
            self._record_transitions(systems)
            with self._payload_lock:
                self._payload = payload
                self._last_error = None
                self._last_refresh_ts = time.time()
                self._is_loading = False
            return True, "Refreshed."
        except Exception as exc:
            with self._payload_lock:
                self._last_error = str(exc)
            return False, f"Refresh failed: {exc}"
        finally:
            self._refresh_lock.release()

    def _record_transitions(self, systems: list) -> None:
        """Log status changes so the topology view can show uptime history.

        Entities are namespaced (``system:<slug>``) because PW cluster
        reachability is tracked separately under ``cluster:<slug>`` — the
        two are different signals for what may be the same machine.
        """
        try:
            entries = []
            for row in systems or []:
                name = (row.get("system") or "").strip()
                if not name:
                    continue
                entries.append(
                    (
                        f"system:{slugify(name)}",
                        row.get("status") or "UNKNOWN",
                        {
                            "name": name,
                            "dsrc": row.get("dsrc"),
                            "scheduler": row.get("scheduler"),
                            "login": row.get("login"),
                        },
                    )
                )
            recorded = self.store.record_system_statuses(entries)
            if recorded:
                _log(f"[{self.source_name}] Recorded {recorded} status transition(s)")
        except Exception as exc:
            _log(f"[{self.source_name}] Unable to record status history: {exc}")

    def snapshot(self) -> Tuple[Optional[Dict], Optional[str], Optional[float]]:
        """Get current state snapshot.

        Returns:
            Tuple of (payload, last_error, last_refresh_timestamp)
        """
        with self._payload_lock:
            return self._payload, self._last_error, self._last_refresh_ts

    def get_status(self) -> Dict[str, Any]:
        """Get status information for API responses."""
        if self._is_loading and not self._payload:
            return {
                "meta": {
                    "status": "loading",
                    "message": "Collecting data from HPC systems...",
                    "first_poll_pending": True,
                },
                "systems": [],
                "summary": None,
            }
        return self._payload or {}

    def is_ready(self) -> bool:
        """Check if data is available."""
        return self._payload is not None


class RefreshWorker(threading.Thread):
    """Background worker for periodic data refresh."""

    daemon = True

    def __init__(self, state: DashboardState, interval_seconds: int):
        super().__init__(name="dashboard-refresh-worker")
        self.state = state
        self.interval = max(60, interval_seconds)
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            self.state.refresh(blocking=True)

    def stop(self) -> None:
        self._stop_event.set()


class ClusterMonitorWorker(threading.Thread):
    """Background worker for cluster monitoring via PW CLI."""

    daemon = True

    def __init__(
        self,
        *,
        store: DataStore,
        interval_seconds: int,
        python_executable: Optional[str] = None,
        run_immediately: bool = True,
        failure_threshold: int = 3,
        pause_duration: int = 300,
        pw_context: Optional[str] = None,
    ):
        super().__init__(name="cluster-monitor-worker")
        self.store = store
        self.interval = max(60, interval_seconds)
        self.python_executable = python_executable or sys.executable
        self._stop_event = threading.Event()
        self._run_immediately = run_immediately
        self._collector = None
        self.pw_context = pw_context
        # Circuit breaker state
        self._consecutive_failures = 0
        self._failure_threshold = failure_threshold
        self._pause_duration = pause_duration
        self._auth_expired = False
        # Periodic cleanup counter
        self._collection_count = 0
        self._cleanup_every = 100
        # Progress tracking — surfaced to the UI so the queue/quota/storage
        # pages can show "collecting 3/12 clusters" instead of HTTP 503.
        self._progress_lock = threading.Lock()
        self._progress: Dict[str, Any] = {
            "phase": "idle",  # idle | warming_up | refreshing | ready | error
            "total": 0,
            "collected": 0,
            "current_cluster": None,
            "started_at": None,
            "last_completed_at": None,
            "first_sweep_complete": False,
        }
        # Cumulative results for the *current* in-flight sweep. During the
        # first sweep we persist this list incrementally so partial data
        # shows up in the UI as clusters finish.
        self._cycle_results: list = []

    def run(self) -> None:
        # Initialize collector lazily
        from ..collectors.pw_cluster import PWClusterCollector

        _log(f"[cluster-monitor] Starting (interval={self.interval}s, "
             f"failure_threshold={self._failure_threshold}, "
             f"pause_duration={self._pause_duration}s)")

        self._collector = PWClusterCollector(pw_context=self.pw_context)

        # If we already have a usable cluster_usage cache on disk, treat the
        # first sweep as already done so the UI doesn't slip back into
        # "warming up" after a service restart.
        try:
            cached = self.store.load_cache("cluster_usage")
            if cached:
                count = len(cached if isinstance(cached, list) else cached.get("clusters") or [])
                if count:
                    self._progress_update(
                        phase="ready",
                        first_sweep_complete=True,
                        collected=count,
                        total=count,
                    )
        except Exception as exc:
            _log(f"[cluster-monitor] Cache probe at startup failed: {exc}")

        if not self._collector.is_available():
            _log("[cluster-monitor] WARNING: pw CLI not available, will retry each cycle")

        # Verify authentication before starting collection loop
        auth_ok, auth_detail = self._collector.check_auth()
        if not auth_ok:
            _log(f"[cluster-monitor] FATAL: Not authenticated at startup: {auth_detail}")
            _log("[cluster-monitor] Exiting — please re-authenticate and restart the service")
            return

        if not self._run_immediately:
            _log(f"[cluster-monitor] Waiting {self.interval}s before first collection")
            if self._stop_event.wait(self.interval):
                return

        _log("[cluster-monitor] Running first collection now")
        while not self._stop_event.is_set():
            self._collect_data()
            if self._auth_expired:
                _log("[cluster-monitor] FATAL: Authentication token expired — exiting")
                _log("[cluster-monitor] Please re-authenticate (pw auth) and restart the service")
                break
            _log(f"[cluster-monitor] Next collection in {self.interval}s")
            if self._stop_event.wait(self.interval):
                break

        _log("[cluster-monitor] Stopped")

    def stop(self) -> None:
        self._stop_event.set()

    def get_progress(self) -> Dict[str, Any]:
        """Snapshot the current collection progress for the API to surface."""
        with self._progress_lock:
            return dict(self._progress)

    def _progress_update(self, **kwargs) -> None:
        with self._progress_lock:
            self._progress.update(kwargs)

    def _on_cluster_progress(self, phase, collected, total, cluster_name, cluster_data):
        """Per-cluster callback from the collector.

        On the *first* sweep (no cache yet), we persist the cumulative list
        after each cluster finishes so the UI can render partial data as it
        arrives. On subsequent sweeps we only update progress fields and let
        the worker save the full result at the end of the cycle — that keeps
        a good cache from being overwritten by a partial one mid-refresh.
        """
        if phase == "start":
            self._progress_update(
                current_cluster=cluster_name,
                total=total,
            )
            return
        # phase == "complete"
        self._progress_update(
            collected=collected,
            total=total,
            last_completed_at=datetime.utcnow().isoformat() + "Z",
        )
        if cluster_data is None:
            return
        self._cycle_results.append(cluster_data)
        with self._progress_lock:
            first_sweep_done = self._progress.get("first_sweep_complete", False)
        if not first_sweep_done:
            try:
                self.store.save_cache("cluster_usage", list(self._cycle_results))
            except Exception as exc:
                _log(f"[cluster-monitor] Incremental cache save failed: {exc}")

    def _record_cluster_transitions(self, clusters: list) -> None:
        """Log reachability changes for PW clusters (``cluster:<slug>``).

        This is the signal behind "connected for 3h 12m" on the topology
        page: a cluster is recorded as connected whenever a sweep produced
        telemetry for it, and DOWN when the sweep reached it but the
        control plane reports it off.
        """
        try:
            entries = []
            for cluster in clusters or []:
                meta = cluster.get("cluster_metadata") or {}
                name = meta.get("name") or str(meta.get("uri") or "").rsplit("/", 1)[-1]
                if not name:
                    continue
                entries.append(
                    (
                        f"cluster:{slugify(name)}",
                        meta.get("status") or "UNKNOWN",
                        {"name": name, "uri": meta.get("uri")},
                    )
                )
            recorded = self.store.record_system_statuses(entries)
            if recorded:
                _log(f"[cluster-monitor] Recorded {recorded} connection transition(s)")
        except Exception as exc:
            _log(f"[cluster-monitor] Unable to record connection history: {exc}")

    def _check_auth_or_expire(self) -> bool:
        """Check authentication; set _auth_expired if token is gone.

        Returns:
            True if authenticated, False if expired.
        """
        auth_ok, detail = self._collector.check_auth()
        if not auth_ok:
            _log(f"[cluster-monitor] Authentication lost: {detail}")
            self._auth_expired = True
            return False
        return True

    def _collect_data(self) -> None:
        """Collect data from PW clusters."""
        if not self._collector:
            return

        # Circuit breaker: pause longer after repeated failures
        if self._consecutive_failures >= self._failure_threshold:
            _log(
                f"[cluster-monitor] Circuit breaker open: "
                f"{self._consecutive_failures} consecutive failures, "
                f"pausing {self._pause_duration}s"
            )
            # Before pausing, check if auth is the problem
            if not self._check_auth_or_expire():
                return
            if self._stop_event.wait(self._pause_duration):
                return
            self._consecutive_failures = 0

        # Mark the start of this sweep so the UI can render progress.
        with self._progress_lock:
            first_sweep_done = self._progress.get("first_sweep_complete", False)
        self._cycle_results = []
        self._progress_update(
            phase="refreshing" if first_sweep_done else "warming_up",
            started_at=datetime.utcnow().isoformat() + "Z",
            collected=0,
            total=0,
            current_cluster=None,
        )

        try:
            _log("[cluster-monitor] Collecting cluster data...")
            data = self._collector.collect(progress_cb=self._on_cluster_progress)
            clusters = data.get("clusters", [])

            if not clusters:
                self._consecutive_failures += 1
                _log(
                    f"[cluster-monitor] Empty result "
                    f"(failure {self._consecutive_failures}/{self._failure_threshold}); "
                    f"keeping existing cache"
                )
                # On repeated empty results, verify auth is still valid
                if self._consecutive_failures >= self._failure_threshold:
                    if not self._check_auth_or_expire():
                        return
                data.setdefault("meta", {})["empty_result"] = True
                self.store.save_snapshot("pw_cluster", data)
                self._progress_update(
                    phase="ready" if first_sweep_done else "error",
                    current_cluster=None,
                )
                return

            # Success: reset failure counter and save the canonical cache.
            self._consecutive_failures = 0
            self.store.save_cache("cluster_usage", clusters)
            self.store.save_snapshot("pw_cluster", data)
            self._record_cluster_transitions(clusters)
            self._progress_update(
                phase="ready",
                first_sweep_complete=True,
                current_cluster=None,
                collected=len(clusters),
                total=len(clusters),
            )
            _log(f"[cluster-monitor] Collected data for {data['meta']['cluster_count']} clusters")

            # Periodic database cleanup
            self._collection_count += 1
            if self._collection_count % self._cleanup_every == 0:
                try:
                    deleted = self.store.cleanup_old_data(days=30)
                    if deleted > 0:
                        _log(f"[cluster-monitor] Cleaned up {deleted} old records")
                except Exception as cleanup_exc:
                    _log(f"[cluster-monitor] Cleanup failed: {cleanup_exc}")
        except Exception as exc:
            self._consecutive_failures += 1
            _log(
                f"[cluster-monitor] Collection failed "
                f"(failure {self._consecutive_failures}/{self._failure_threshold}): {exc}"
            )
            self._progress_update(
                phase="ready" if first_sweep_done else "error",
                current_cluster=None,
            )
            # On repeated exceptions, check if it's an auth problem
            if self._consecutive_failures >= self._failure_threshold:
                self._check_auth_or_expire()


class CollectorManager:
    """Manages multiple data collectors and their workers."""

    def __init__(self, store: DataStore):
        self.store = store
        self._workers: Dict[str, threading.Thread] = {}
        self._states: Dict[str, DashboardState] = {}

    def register_collector(
        self,
        name: str,
        generate_fn,
        interval: int = 120,
        run_immediately: bool = True,
    ) -> DashboardState:
        """Register a data collector.

        Args:
            name: Unique collector name
            generate_fn: Function that generates data
            interval: Refresh interval in seconds
            run_immediately: Whether to run immediately on start

        Returns:
            DashboardState for the collector
        """
        state = DashboardState(self.store, generate_fn, source_name=name)
        worker = RefreshWorker(state, interval_seconds=interval)
        self._states[name] = state
        self._workers[name] = worker
        return state

    def start_all(self) -> None:
        """Start all registered workers."""
        for name, worker in self._workers.items():
            if not worker.is_alive():
                _log(f"[collector-manager] Starting {name} worker")
                worker.start()

    def stop_all(self, timeout: float = 5.0) -> None:
        """Stop all workers."""
        for name, worker in self._workers.items():
            if hasattr(worker, "stop"):
                worker.stop()
            worker.join(timeout=timeout)

    def get_state(self, name: str) -> Optional[DashboardState]:
        """Get state for a collector."""
        return self._states.get(name)

    def get_all_status(self) -> Dict[str, Any]:
        """Get status for all collectors."""
        return {
            name: {
                "ready": state.is_ready(),
                "last_refresh": state._last_refresh_ts,
                "last_error": state._last_error,
            }
            for name, state in self._states.items()
        }
