"""Queue depth history and time-to-start estimates.

The dashboard has always shown how deep a queue is. Depth alone does not
answer the question people actually have — *when will my job start?* — so
this records queue depth over time and turns it into an estimate.

Method, stated plainly because an invented number is worse than none:

    Every sweep records running/pending cores per queue. Between two
    consecutive samples, any drop in running cores is capacity that came
    free. Summed over the window that gives an observed turnover rate in
    cores per hour, and the backlog ahead of a new job divided by that rate
    is the estimate.

Consequences worth knowing:

- A queue with no observed turnover yields no estimate, not "infinity".
- A saturated queue whose freed cores are instantly refilled shows less
  turnover than really occurred, so estimates skew pessimistic.
- Estimates are labelled with the sample count they came from, so the UI
  can be honest about confidence.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Samples older than this are pruned: a week is plenty for a trailing rate,
# and one row per queue per sweep adds up quickly.
RETENTION_DAYS = 7

# Below this many samples an estimate is too thin to show with confidence.
MIN_SAMPLES = 3


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default


def _slug(text: Any) -> str:
    return "".join(c for c in str(text or "").lower() if c.isalnum())


def format_duration(seconds: Optional[float]) -> Optional[str]:
    """Render a wait in the units a person would use."""
    if seconds is None:
        return None
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "under 2 minutes"
    minutes = seconds / 60
    if minutes < 60:
        return f"~{int(round(minutes / 5) * 5)} minutes"
    hours = minutes / 60
    if hours < 24:
        return f"~{hours:.1f} hours" if hours < 10 else f"~{int(round(hours))} hours"
    return f"~{hours / 24:.1f} days"


class QueueHistoryStore:
    """Time series of queue depth, and the estimates derived from it."""

    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_samples (
                    id INTEGER PRIMARY KEY,
                    cluster TEXT NOT NULL,
                    queue TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    jobs_running INTEGER DEFAULT 0,
                    jobs_pending INTEGER DEFAULT 0,
                    cores_running INTEGER DEFAULT 0,
                    cores_pending INTEGER DEFAULT 0,
                    cores_total INTEGER DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_queue_samples "
                "ON queue_samples(cluster, queue, timestamp)"
            )

    # --- recording --------------------------------------------------------

    def record_clusters(
        self, clusters: Iterable[Dict[str, Any]], *, now: Optional[datetime] = None
    ) -> int:
        """Record one sample per queue from a collection sweep."""
        stamp = (now or datetime.utcnow()).isoformat()
        rows: List[Tuple] = []

        for cluster in clusters or []:
            meta = cluster.get("cluster_metadata") or {}
            name = meta.get("name") or str(meta.get("uri") or "").rsplit("/", 1)[-1]
            cluster_slug = _slug(name)
            if not cluster_slug:
                continue
            queue_data = cluster.get("queue_data") or {}
            totals = queue_data.get("cluster_totals") or {}
            cores_total = int(_num(totals.get("cores_total")))
            if not cores_total:
                cores_total = int(
                    sum(_num(n.get("cores_available")) for n in queue_data.get("nodes") or [])
                )
            for queue in queue_data.get("queues") or []:
                queue_name = str(queue.get("queue_name") or "").strip()
                if not queue_name:
                    continue
                rows.append(
                    (
                        cluster_slug,
                        queue_name,
                        stamp,
                        int(_num(queue.get("jobs_running"))),
                        int(_num(queue.get("jobs_pending"))),
                        int(_num(queue.get("cores_running"))),
                        int(_num(queue.get("cores_pending"))),
                        cores_total,
                    )
                )

        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO queue_samples (cluster, queue, timestamp, jobs_running, "
                "jobs_pending, cores_running, cores_pending, cores_total) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def prune(self, days: int = RETENTION_DAYS) -> int:
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM queue_samples WHERE timestamp < ?", (cutoff,)
            )
            return cursor.rowcount

    # --- estimation -------------------------------------------------------

    def estimate_waits(
        self, *, window_hours: int = 6, now: Optional[datetime] = None
    ) -> Dict[Tuple[str, str], Dict[str, Any]]:
        """Estimate time-to-start for every queue with enough history.

        Returns a map of ``(cluster_slug, queue_name)`` to an estimate dict.
        One query and one pass, because this runs on every queue-page load.
        """
        reference = now or datetime.utcnow()
        window_start = reference - timedelta(hours=max(1, window_hours))

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT cluster, queue, timestamp, cores_running, cores_pending, "
                "cores_total FROM queue_samples WHERE timestamp >= ? "
                "ORDER BY cluster, queue, timestamp ASC",
                (window_start.isoformat(),),
            ).fetchall()

        series: Dict[Tuple[str, str], List[Tuple[datetime, float, float, float]]] = {}
        for cluster, queue, stamp, running, pending, total in rows:
            parsed = _parse(stamp)
            if parsed is None:
                continue
            series.setdefault((cluster, queue), []).append(
                (parsed, _num(running), _num(pending), _num(total))
            )

        estimates = {}
        for key, samples in series.items():
            estimate = self._estimate_from_series(samples, window_hours=window_hours)
            if estimate:
                estimates[key] = estimate
        return estimates

    @staticmethod
    def _estimate_from_series(
        samples: List[Tuple[datetime, float, float, float]], *, window_hours: int
    ) -> Optional[Dict[str, Any]]:
        if len(samples) < 2:
            return None

        released = 0.0
        for (_, prev_running, _, _), (_, running, _, _) in zip(samples, samples[1:]):
            if running < prev_running:
                released += prev_running - running

        span_hours = (samples[-1][0] - samples[0][0]).total_seconds() / 3600
        if span_hours <= 0:
            return None

        rate = released / span_hours  # cores freed per hour
        _, _, pending_cores, cores_total = samples[-1]
        confidence = (
            "low"
            if len(samples) < MIN_SAMPLES or span_hours < 0.5
            else "medium"
            if len(samples) < 20
            else "high"
        )

        if pending_cores <= 0:
            return {
                "wait_seconds": 0,
                "wait_display": "no backlog",
                "pending_cores": 0,
                "drain_rate_cores_per_hour": round(rate),
                "samples": len(samples),
                "window_hours": window_hours,
                "confidence": confidence,
                "basis": "nothing is waiting in this queue",
            }

        if rate <= 0:
            return {
                "wait_seconds": None,
                "wait_display": None,
                "pending_cores": int(pending_cores),
                "drain_rate_cores_per_hour": 0,
                "samples": len(samples),
                "window_hours": window_hours,
                "confidence": "none",
                "basis": (
                    f"no core turnover observed in the last {window_hours}h, "
                    f"so there is nothing to extrapolate from"
                ),
            }

        wait_seconds = (pending_cores / rate) * 3600
        return {
            "wait_seconds": int(wait_seconds),
            "wait_display": format_duration(wait_seconds),
            "pending_cores": int(pending_cores),
            "cores_total": int(cores_total),
            "drain_rate_cores_per_hour": round(rate),
            "samples": len(samples),
            "window_hours": window_hours,
            "confidence": confidence,
            "basis": (
                f"{int(pending_cores):,} cores waiting ÷ {round(rate):,} cores/h "
                f"observed turnover over {span_hours:.1f}h"
            ),
        }


def _parse(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None
