"""Data persistence layer for HPC status data.

Provides both JSON cache for fast reads and SQLite for historical data.
All data is stored in ~/.hpc_status/ to survive restarts.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Status values that mean "the monitor could reach this system". Collectors
# emit different vocabularies (HPCMP scrape says UP/DOWN, PW says ACTIVE/ON),
# so both sets are normalized here rather than at every call site.
UP_STATUSES = frozenset({"UP", "ACTIVE", "ON", "RUNNING", "ONLINE"})
REACHABLE_STATUSES = UP_STATUSES | {"DEGRADED", "MAINTENANCE"}


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse a stored ISO timestamp, tolerating a trailing Z and offsets."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Everything in this store is UTC; drop offsets so comparisons are naive.
    return parsed.replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    """Render a naive UTC datetime as an ISO-8601 Z string."""
    if value is None:
        return None
    return value.replace(microsecond=0).isoformat() + "Z"


def get_data_dir() -> Path:
    """Get user-persistent data directory.

    Returns ~/.hpc_status/ by default, or HPC_STATUS_DATA_DIR env var.
    Creates subdirectories if they don't exist.
    """
    data_dir = Path(os.environ.get("HPC_STATUS_DATA_DIR", Path.home() / ".hpc_status"))

    # Create all subdirectories
    for subdir in ["cache", "user_data", "markdown", "logs"]:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)

    return data_dir


class DataStore:
    """Persistent storage for status data.

    Provides two storage mechanisms:
    - JSON cache files for fast dashboard startup
    - SQLite database for historical data and queries
    """

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or get_data_dir()
        self.db_path = self.data_dir / "status.db"
        self.cache_dir = self.data_dir / "cache"
        self.user_data_dir = self.data_dir / "user_data"
        self.markdown_dir = self.data_dir / "markdown"
        self.logs_dir = self.data_dir / "logs"
        # get_data_dir() creates these, but an explicit data_dir (config
        # ``data_dir:`` or a test fixture) has not been through it.
        for directory in (
            self.cache_dir,
            self.user_data_dir,
            self.markdown_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._queue_history = None
        self._init_db()

    @property
    def queue_history(self):
        """Queue depth time series (lazily created, shares the same DB)."""
        if self._queue_history is None:
            from .queue_history import QueueHistoryStore

            self._queue_history = QueueHistoryStore(self.db_path)
        return self._queue_history

    # --- JSON Cache (fast reads) ---

    def save_cache(self, name: str, data: Dict[str, Any]) -> None:
        """Save data to JSON cache file atomically.

        Uses atomic write (write to temp file then rename) to prevent
        file corruption if interrupted during write.

        Args:
            name: Cache name (e.g., 'fleet_status', 'cluster_usage')
            data: Data to cache
        """
        cache_file = self.cache_dir / f"{name}.json"
        content = json.dumps(data, indent=2, default=str)

        # Write to temp file then rename for atomic operation
        fd, tmp_path = tempfile.mkstemp(dir=self.cache_dir, suffix=".tmp")
        try:
            try:
                f = os.fdopen(fd, "w", encoding="utf-8")
            except Exception:
                os.close(fd)
                raise
            with f:
                f.write(content)
            # Atomic rename
            os.replace(tmp_path, cache_file)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def cache_updated_at(self, name: str) -> Optional[str]:
        """When a cache file was last written, as UTC ISO — or None.

        The payloads themselves stamp ``generated_at`` per request, which
        says when you asked, not when anything was collected. The file
        mtime is the honest freshness signal.
        """
        cache_file = self.cache_dir / f"{name}.json"
        if not cache_file.exists():
            return None
        return (
            datetime.utcfromtimestamp(cache_file.stat().st_mtime)
            .replace(microsecond=0)
            .isoformat()
            + "Z"
        )

    def load_cache(self, name: str, max_age: Optional[timedelta] = None) -> Optional[Dict[str, Any]]:
        """Load data from JSON cache file.

        Args:
            name: Cache name
            max_age: Maximum age of cache to accept (None = any age)

        Returns:
            Cached data or None if not found/expired
        """
        cache_file = self.cache_dir / f"{name}.json"
        if not cache_file.exists():
            return None

        # Check age if max_age specified
        if max_age:
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if datetime.now() - mtime > max_age:
                return None

        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            # Add cache metadata
            if isinstance(data, dict):
                data.setdefault("_cache_meta", {})
                data["_cache_meta"]["from_cache"] = True
                data["_cache_meta"]["cache_file"] = str(cache_file)
                data["_cache_meta"]["cache_age_seconds"] = (
                    datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
                ).total_seconds()
            return data
        except json.JSONDecodeError:
            return None

    def get_cache_age(self, name: str) -> Optional[float]:
        """Get age of cache in seconds.

        Returns None if cache doesn't exist.
        """
        cache_file = self.cache_dir / f"{name}.json"
        if not cache_file.exists():
            return None
        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
        return (datetime.now() - mtime).total_seconds()

    def clear_cache(self, name: Optional[str] = None) -> None:
        """Clear cache file(s).

        Args:
            name: Specific cache to clear, or None for all
        """
        if name:
            cache_file = self.cache_dir / f"{name}.json"
            if cache_file.exists():
                cache_file.unlink()
        else:
            for cache_file in self.cache_dir.glob("*.json"):
                cache_file.unlink()

    # --- User Data Storage ---

    def save_user_data(self, name: str, data: Dict[str, Any]) -> None:
        """Save user-specific data (groups, jobs, quotas)."""
        user_file = self.user_data_dir / f"{name}.json"
        user_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def load_user_data(self, name: str) -> Optional[Dict[str, Any]]:
        """Load user-specific data."""
        user_file = self.user_data_dir / f"{name}.json"
        if not user_file.exists():
            return None
        try:
            return json.loads(user_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    # --- Markdown Storage ---

    def save_markdown(self, slug: str, content: str) -> None:
        """Save system markdown briefing."""
        md_file = self.markdown_dir / f"{slug}.md"
        md_file.write_text(content, encoding="utf-8")

    def load_markdown(self, slug: str) -> Optional[str]:
        """Load system markdown briefing."""
        md_file = self.markdown_dir / f"{slug}.md"
        if not md_file.exists():
            return None
        return md_file.read_text(encoding="utf-8")

    def list_markdown_files(self) -> list:
        """List available markdown files."""
        return [f.stem for f in self.markdown_dir.glob("*.md")]

    # --- SQLite (historical data, queries) ---

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection with proper configuration.

        Uses WAL mode for better concurrent read/write performance
        and sets appropriate timeouts to avoid blocking.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        # Enable WAL mode for better concurrent access
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        """Create database tables if they don't exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY,
                    collector TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    data JSON NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_collector_timestamp
                ON snapshots(collector, timestamp DESC)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_history (
                    id INTEGER PRIMARY KEY,
                    system_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    details JSON
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_system_timestamp
                ON system_history(system_name, timestamp DESC)
            """)

    def save_snapshot(self, collector: str, data: Dict[str, Any]) -> None:
        """Save a data snapshot to the database.

        Args:
            collector: Collector name (e.g., 'hpcmp', 'pw_cluster')
            data: Snapshot data
        """
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO snapshots (collector, timestamp, data) VALUES (?, ?, ?)",
                (collector, datetime.utcnow().isoformat(), json.dumps(data)),
            )

    def get_latest_snapshot(
        self, collector: str, max_age: Optional[timedelta] = None
    ) -> Optional[Dict[str, Any]]:
        """Get most recent snapshot, optionally filtered by age.

        Args:
            collector: Collector name
            max_age: Maximum age to accept

        Returns:
            Snapshot data or None
        """
        with self._get_connection() as conn:
            query = """
                SELECT data, timestamp FROM snapshots
                WHERE collector = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """
            row = conn.execute(query, (collector,)).fetchone()

            if row:
                data, ts = row
                if max_age:
                    snapshot_time = datetime.fromisoformat(ts)
                    if datetime.utcnow() - snapshot_time > max_age:
                        return None
                return json.loads(data)
        return None

    def save_system_status(self, system_name: str, status: str, details: Optional[Dict] = None) -> None:
        """Record a system status change for historical tracking."""
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO system_history (system_name, status, timestamp, details) VALUES (?, ?, ?, ?)",
                (system_name, status, datetime.utcnow().isoformat(), json.dumps(details) if details else None),
            )

    def get_system_history(
        self, system_name: str, limit: int = 100, since: Optional[datetime] = None
    ) -> list:
        """Get status history for a system.

        Returns list of (timestamp, status, details) tuples.
        """
        with self._get_connection() as conn:
            if since:
                query = """
                    SELECT timestamp, status, details FROM system_history
                    WHERE system_name = ? AND timestamp > ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                rows = conn.execute(query, (system_name, since.isoformat(), limit)).fetchall()
            else:
                query = """
                    SELECT timestamp, status, details FROM system_history
                    WHERE system_name = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """
                rows = conn.execute(query, (system_name, limit)).fetchall()

            return [
                {
                    "timestamp": ts,
                    "status": status,
                    "details": json.loads(details) if details else None,
                }
                for ts, status, details in rows
            ]

    def record_system_statuses(
        self, entries: Iterable[Tuple[str, str, Optional[Dict]]]
    ) -> List[Tuple[str, Optional[str], str, Optional[Dict]]]:
        """Record status transitions for a batch of systems.

        A row is written only when a system's status differs from the last
        one stored for it, so ``system_history`` stays a transition log
        rather than one row per poll. That keeps "connected since" and
        uptime math cheap: a system that has been UP for a week is a single
        row, not thousands.

        Args:
            entries: (system_name, status, details) tuples.

        Returns:
            The transitions recorded, as (name, previous_status, status,
            details). ``previous_status`` is None the first time a system is
            seen, which callers use to avoid alerting on discovery.
        """
        rows = [
            (str(name).strip(), (status or "UNKNOWN").upper(), details)
            for name, status, details in entries
            if str(name or "").strip()
        ]
        if not rows:
            return []

        last = self.get_last_statuses()
        now = datetime.utcnow().isoformat()
        transitions = [
            (name, last.get(name, {}).get("status"), status, details)
            for name, status, details in rows
            if last.get(name, {}).get("status") != status
        ]
        if not transitions:
            return []
        with self._get_connection() as conn:
            conn.executemany(
                "INSERT INTO system_history (system_name, status, timestamp, details) "
                "VALUES (?, ?, ?, ?)",
                [
                    (name, status, now, json.dumps(details) if details else None)
                    for name, _, status, details in transitions
                ],
            )
        return transitions

    def get_last_statuses(self) -> Dict[str, Dict[str, Any]]:
        """Return the most recent recorded status for every known system."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT h.system_name, h.status, h.timestamp
                FROM system_history h
                JOIN (
                    SELECT system_name, MAX(timestamp) AS ts
                    FROM system_history GROUP BY system_name
                ) latest
                  ON h.system_name = latest.system_name
                 AND h.timestamp = latest.ts
                """
            ).fetchall()
        return {
            name: {"status": status, "timestamp": ts} for name, status, ts in rows
        }

    def get_connection_stats(self, window_hours: int = 24) -> Dict[str, Dict[str, Any]]:
        """Summarize connection history for every tracked system.

        Returns a map of ``system_name`` to:

        - ``first_seen``: first time the monitor ever recorded this system
        - ``status``: last recorded status
        - ``connected_since``: start of the current unbroken reachable run
          (None when the system is currently unreachable)
        - ``last_change``: when the current status began
        - ``uptime_ratio``: time-weighted fraction of the window spent UP
        - ``transitions``: status changes inside the window

        Uptime is time-weighted rather than sample-counted because rows are
        only written on change — counting rows would say a system that
        flapped once is 50% up.
        """
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT system_name, status, timestamp FROM system_history "
                "ORDER BY system_name ASC, timestamp ASC"
            ).fetchall()

        now = datetime.utcnow()
        window_start = now - timedelta(hours=max(1, window_hours))

        by_system: Dict[str, List[Tuple[datetime, str]]] = {}
        for name, status, ts in rows:
            parsed = _parse_timestamp(ts)
            if parsed is None:
                continue
            by_system.setdefault(name, []).append((parsed, (status or "UNKNOWN").upper()))

        stats: Dict[str, Dict[str, Any]] = {}
        for name, events in by_system.items():
            first_seen, _ = events[0]
            last_change, current_status = events[-1]

            # Walk backwards to find the start of the current reachable run.
            connected_since = None
            if current_status in REACHABLE_STATUSES:
                connected_since = last_change
                for ts_val, status in reversed(events[:-1]):
                    if status not in REACHABLE_STATUSES:
                        break
                    connected_since = ts_val

            up_seconds = 0.0
            transitions = 0
            # Spans inside the window, so the UI can draw a status timeline
            # without a second round trip. The first span is clipped to the
            # window start and carries whatever state was already in effect.
            spans: List[Dict[str, Any]] = []
            for idx, (ts_val, status) in enumerate(events):
                end = events[idx + 1][0] if idx + 1 < len(events) else now
                if end <= window_start:
                    continue
                # idx 0 is the first sighting of the system, not a change.
                if idx > 0 and ts_val > window_start:
                    transitions += 1
                span_start = max(ts_val, window_start)
                if status in UP_STATUSES and end > span_start:
                    up_seconds += (end - span_start).total_seconds()
                spans.append(
                    {
                        "status": status,
                        "from": _iso(span_start),
                        "to": _iso(end),
                        "seconds": max(0, int((end - span_start).total_seconds())),
                    }
                )

            observed_seconds = (now - max(first_seen, window_start)).total_seconds()
            uptime_ratio = (
                round(min(up_seconds / observed_seconds, 1.0), 4)
                if observed_seconds > 0
                else None
            )

            stats[name] = {
                "first_seen": _iso(first_seen),
                "status": current_status,
                "last_change": _iso(last_change),
                "connected_since": _iso(connected_since),
                "uptime_ratio": uptime_ratio,
                "uptime_window_hours": window_hours,
                "transitions": transitions,
                "window_start": _iso(window_start),
                "window_end": _iso(now),
                # Cap the timeline: a system that flaps hundreds of times in
                # a day should not bloat every topology response.
                "spans": spans[-60:],
            }
        return stats

    def cleanup_old_data(self, days: int = 30) -> int:
        """Remove data older than specified days.

        Returns number of rows deleted.
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        deleted = 0
        with self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM snapshots WHERE timestamp < ?", (cutoff,))
            deleted += cursor.rowcount
            # Keep each system's most recent transition regardless of age —
            # it is the anchor for "connected since" on long-lived systems.
            cursor = conn.execute(
                """
                DELETE FROM system_history
                WHERE timestamp < ?
                  AND id NOT IN (
                      SELECT h.id FROM system_history h
                      JOIN (
                          SELECT system_name, MAX(timestamp) AS ts
                          FROM system_history GROUP BY system_name
                      ) latest
                        ON h.system_name = latest.system_name
                       AND h.timestamp = latest.ts
                  )
                """,
                (cutoff,),
            )
            deleted += cursor.rowcount
        # Queue samples are the highest-volume table (one row per queue per
        # sweep), so they get their own shorter retention.
        try:
            deleted += self.queue_history.prune()
        except Exception:
            pass
        return deleted
