"""Replayable history for the topology view.

The dashboard records two independent time series already: status
transitions (``system_history``) and queue depth (``queue_samples``). This
turns them into evenly spaced frames so the topology page can be scrubbed
and played back — "what did the fleet look like at 03:00 when my job
died?" — instead of only ever showing the current instant.

Frames are built server-side because the two series are shaped differently:
transitions are sparse and must be carried forward, while queue samples are
dense and must be bucketed. Doing that once here beats doing it in every
client.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Frames are capped so a wide window with a fine step cannot generate a
# response measured in megabytes.
MAX_FRAMES = 240


def _slug(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _parse(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"


def frame_times(
    *, window_hours: int, step_minutes: int, now: Optional[datetime] = None
) -> List[datetime]:
    """Evenly spaced instants covering the window, oldest first."""
    end = (now or datetime.utcnow()).replace(microsecond=0)
    window_hours = max(1, min(24 * 14, window_hours))
    step = max(1, step_minutes)
    count = int((window_hours * 60) / step) + 1
    if count > MAX_FRAMES:
        step = int((window_hours * 60) / (MAX_FRAMES - 1))
        count = MAX_FRAMES
    start = end - timedelta(minutes=step * (count - 1))
    return [start + timedelta(minutes=step * i) for i in range(count)]


def status_at_frames(
    events_by_entity: Dict[str, List[Tuple[datetime, str]]], times: List[datetime]
) -> Dict[str, List[Optional[str]]]:
    """Carry each system's last known status forward across the frames.

    A transition log says nothing at all about the instants between
    transitions, which is precisely the information a timeline needs.
    """
    result: Dict[str, List[Optional[str]]] = {}
    for entity, events in events_by_entity.items():
        ordered = sorted(events, key=lambda pair: pair[0])
        statuses: List[Optional[str]] = []
        index = 0
        current: Optional[str] = None
        for moment in times:
            while index < len(ordered) and ordered[index][0] <= moment:
                current = ordered[index][1]
                index += 1
            statuses.append(current)
        result[entity] = statuses
    return result


def build_history(
    store,
    *,
    window_hours: int = 24,
    step_minutes: int = 15,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build playback frames from the recorded status and queue history.

    Args:
        store: a ``DataStore`` (its queue_history is used too).
        window_hours: how far back to replay.
        step_minutes: spacing between frames.
        now: injectable clock for tests.

    Returns:
        ``{window_hours, step_minutes, frames: [{at, systems: {...}}], systems: [...]}``
        where each system entry carries the status and, where telemetry
        exists, utilization and pending cores at that instant.
    """
    times = frame_times(window_hours=window_hours, step_minutes=step_minutes, now=now)
    if not times:
        return {"window_hours": window_hours, "step_minutes": step_minutes, "frames": []}

    statuses = status_at_frames(_status_events(store, times[0]), times)
    utilization = _queue_series(store, times)

    known = sorted({*(_entity_slug(e) for e in statuses), *utilization.keys()} - {""})

    frames = []
    for index, moment in enumerate(times):
        systems: Dict[str, Dict[str, Any]] = {}
        for entity, series in statuses.items():
            slug = _entity_slug(entity)
            if not slug or series[index] is None:
                continue
            systems.setdefault(slug, {})["status"] = series[index]
        for slug, series in utilization.items():
            point = series[index]
            if point is None:
                continue
            systems.setdefault(slug, {}).update(point)
        frames.append({"at": _iso(moment), "systems": systems})

    return {
        "window_hours": window_hours,
        "step_minutes": int(
            (times[1] - times[0]).total_seconds() / 60 if len(times) > 1 else step_minutes
        ),
        "from": _iso(times[0]),
        "to": _iso(times[-1]),
        "systems": known,
        "frames": frames,
    }


def _entity_slug(entity: str) -> str:
    """``system:narwhal`` and ``cluster:narwhal`` are the same machine here."""
    return _slug(str(entity).split(":", 1)[-1])


def _status_events(store, since: datetime) -> Dict[str, List[Tuple[datetime, str]]]:
    """Transitions per entity, including the one in force before the window."""
    with store._get_connection() as conn:
        rows = conn.execute(
            "SELECT system_name, status, timestamp FROM system_history "
            "ORDER BY system_name ASC, timestamp ASC"
        ).fetchall()

    events: Dict[str, List[Tuple[datetime, str]]] = {}
    for name, status, stamp in rows:
        parsed = _parse(stamp)
        if parsed is None:
            continue
        events.setdefault(name, []).append((parsed, str(status or "UNKNOWN").upper()))
    return events


def _queue_series(store, times: List[datetime]) -> Dict[str, List[Optional[Dict[str, Any]]]]:
    """Bucket queue samples onto the frame times, per cluster."""
    try:
        history = store.queue_history
    except Exception:
        return {}

    with history._connect() as conn:
        rows = conn.execute(
            "SELECT cluster, timestamp, cores_running, cores_pending, cores_total "
            "FROM queue_samples WHERE timestamp >= ? ORDER BY timestamp ASC",
            (times[0].isoformat(),),
        ).fetchall()

    # cluster -> frame index -> accumulated totals
    buckets: Dict[str, Dict[int, Dict[str, float]]] = {}
    step = (times[1] - times[0]).total_seconds() if len(times) > 1 else 900
    for cluster, stamp, running, pending, total in rows:
        parsed = _parse(stamp)
        if parsed is None:
            continue
        index = int((parsed - times[0]).total_seconds() // step)
        if index < 0 or index >= len(times):
            continue
        slot = buckets.setdefault(_slug(cluster), {}).setdefault(
            index, {"running": 0.0, "pending": 0.0, "total": 0.0, "samples": 0}
        )
        # Queues of the same cluster share a cores_total; sum the per-queue
        # running/pending and keep the largest total seen.
        slot["running"] += float(running or 0)
        slot["pending"] += float(pending or 0)
        slot["total"] = max(slot["total"], float(total or 0))
        slot["samples"] += 1

    series: Dict[str, List[Optional[Dict[str, Any]]]] = {}
    for cluster, frames in buckets.items():
        points: List[Optional[Dict[str, Any]]] = []
        last: Optional[Dict[str, Any]] = None
        for index in range(len(times)):
            slot = frames.get(index)
            if slot:
                total = slot["total"]
                last = {
                    "cores_running": int(slot["running"]),
                    "cores_pending": int(slot["pending"]),
                    "cores_total": int(total),
                    "utilization_percent": (
                        round(min(100.0, slot["running"] / total * 100), 1)
                        if total
                        else None
                    ),
                }
            # Carry the last reading forward: a gap between sweeps is not a
            # cluster with zero cores, it is a cluster we did not ask.
            points.append(dict(last) if last else None)
        series[cluster] = points
    return series
