"""Background workers for data refresh and monitoring.

Handles periodic data collection and state management.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..data.persistence import DataStore


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
            # Save to cache
            self.store.save_cache(self.source_name, payload)
            # Save snapshot to DB for history
            self.store.save_snapshot(self.source_name, payload)
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
    ):
        super().__init__(name="cluster-monitor-worker")
        self.store = store
        self.interval = max(60, interval_seconds)
        self.python_executable = python_executable or sys.executable
        self._stop_event = threading.Event()
        self._run_immediately = run_immediately
        self._collector = None

    def run(self) -> None:
        # Initialize collector lazily
        from ..collectors.pw_cluster import PWClusterCollector
        self._collector = PWClusterCollector()

        if not self._run_immediately:
            if self._stop_event.wait(self.interval):
                return

        while not self._stop_event.is_set():
            self._collect_data()
            if self._stop_event.wait(self.interval):
                break

    def stop(self) -> None:
        self._stop_event.set()

    def _collect_data(self) -> None:
        """Collect data from PW clusters."""
        if not self._collector:
            return

        try:
            print("[cluster-monitor] Collecting cluster data...")
            data = self._collector.collect()

            # Save to cache
            self.store.save_cache("cluster_usage", data.get("clusters", []))
            self.store.save_snapshot("pw_cluster", data)

            print(f"[cluster-monitor] Collected data for {data['meta']['cluster_count']} clusters")
        except Exception as exc:
            print(f"[cluster-monitor] Collection failed: {exc}")


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
                print(f"[collector-manager] Starting {name} worker")
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
