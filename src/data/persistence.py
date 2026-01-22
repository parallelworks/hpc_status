"""Data persistence layer for HPC status data.

Provides both JSON cache for fast reads and SQLite for historical data.
All data is stored in ~/.hpc_status/ to survive restarts.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional


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
        self._init_db()

    # --- JSON Cache (fast reads) ---

    def save_cache(self, name: str, data: Dict[str, Any]) -> None:
        """Save data to JSON cache file.

        Args:
            name: Cache name (e.g., 'fleet_status', 'cluster_usage')
            data: Data to cache
        """
        cache_file = self.cache_dir / f"{name}.json"
        cache_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

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

    def _init_db(self) -> None:
        """Create database tables if they don't exist."""
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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

    def cleanup_old_data(self, days: int = 30) -> int:
        """Remove data older than specified days.

        Returns number of rows deleted.
        """
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        deleted = 0
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM snapshots WHERE timestamp < ?", (cutoff,))
            deleted += cursor.rowcount
            cursor = conn.execute("DELETE FROM system_history WHERE timestamp < ?", (cutoff,))
            deleted += cursor.rowcount
        return deleted
