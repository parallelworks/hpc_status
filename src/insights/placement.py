"""Where should this job run?

The dashboard already knows every queue's capacity, backlog, walltime limit,
and the allocation behind it. This turns that into a ranked answer for a
specific job shape instead of leaving the user to cross-reference four pages.

Two design rules:

- **Blockers are separate from score.** A queue whose walltime is shorter
  than the job cannot run it at any score, and saying so is more useful than
  ranking it last.
- **Every number is explained.** Each candidate carries the reasons behind
  its score, because a recommendation nobody can audit is a recommendation
  nobody will trust.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# Score weights, out of 100.
WEIGHT_CAPACITY = 40  # can it start now?
WEIGHT_WAIT = 30  # measured time-to-start
WEIGHT_BACKLOG = 20  # how contended the queue is
WEIGHT_ALLOCATION = 10  # will the allocation cover it?

# A wait at or beyond this is scored as "as bad as it gets".
WAIT_CEILING_HOURS = 12.0

OPERATIONAL = frozenset({"ON", "UP", "RUNNING", "ONLINE", "ACTIVE"})


@dataclass
class PlacementRequest:
    """The job we are trying to place."""

    cores: int = 1
    hours: float = 1.0
    gpus: int = 0
    queue_type: Optional[str] = None

    @property
    def core_hours(self) -> float:
        return max(0.0, self.cores * self.hours)

    @classmethod
    def from_params(cls, params: Dict[str, Any]) -> "PlacementRequest":
        """Build from query-string style parameters, clamped to sane values."""

        def first(key: str, default: str) -> str:
            value = params.get(key, default)
            if isinstance(value, (list, tuple)):
                value = value[0] if value else default
            return str(value)

        def number(key: str, default: float, low: float, high: float) -> float:
            try:
                return max(low, min(high, float(first(key, str(default)))))
            except (TypeError, ValueError):
                return default

        queue_type = first("queue_type", "").strip() or None
        return cls(
            cores=int(number("cores", 1, 1, 10_000_000)),
            hours=number("hours", 1.0, 0.1, 24 * 30),
            gpus=int(number("gpus", 0, 0, 100_000)),
            queue_type=queue_type,
        )


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default


def _slug(text: Any) -> str:
    return "".join(c for c in str(text or "").lower() if c.isalnum())


def parse_walltime_hours(value: Any) -> Optional[float]:
    """Parse a scheduler walltime limit into hours.

    Handles HH:MM:SS, DD:HH:MM:SS, "24:00:00", "7-00:00:00" (Slurm), and
    plain hour counts. Returns None for "unlimited" or unparseable input,
    which callers treat as "no constraint known" rather than zero.
    """
    text = str(value or "").strip().lower()
    if not text or text in {"-", "none", "unlimited", "infinite", "n/a"}:
        return None

    days = 0.0
    if "-" in text:  # Slurm's DD-HH:MM:SS
        head, _, tail = text.partition("-")
        try:
            days = float(head)
            text = tail
        except ValueError:
            pass

    parts = text.split(":")
    try:
        if len(parts) == 4:
            d, h, m, s = (float(p) for p in parts)
            return d * 24 + h + m / 60 + s / 3600
        if len(parts) == 3:
            h, m, s = (float(p) for p in parts)
            return days * 24 + h + m / 60 + s / 3600
        if len(parts) == 2:
            h, m = (float(p) for p in parts)
            return days * 24 + h + m / 60
        if len(parts) == 1 and re.match(r"^\d+(\.\d+)?$", parts[0]):
            return days * 24 + float(parts[0])
    except ValueError:
        return None
    return None


def _cluster_capacity(cluster: Dict[str, Any]) -> Tuple[int, int, int]:
    """(cores_total, cores_running, gpus_total) for a cluster."""
    queue_data = cluster.get("queue_data") or {}
    totals = queue_data.get("cluster_totals") or {}
    if totals:
        return (
            int(_num(totals.get("cores_total"))),
            int(_num(totals.get("cores_running"))),
            int(_num(totals.get("gpus_total"))),
        )
    nodes = queue_data.get("nodes") or []
    return (
        int(sum(_num(n.get("cores_available")) for n in nodes)),
        int(sum(_num(n.get("cores_running")) for n in nodes)),
        int(sum(_num(n.get("gpus_available")) for n in nodes)),
    )


def _allocation_hours(cluster: Dict[str, Any]) -> Optional[float]:
    systems = (cluster.get("usage_data") or {}).get("systems") or []
    if not systems:
        return None
    return sum(_num(s.get("hours_remaining")) for s in systems)


def rank_placements(
    clusters: List[Dict[str, Any]],
    request: PlacementRequest,
    *,
    wait_estimates: Optional[Dict[Tuple[str, str], Dict[str, Any]]] = None,
    limit: int = 5,
) -> Dict[str, Any]:
    """Rank (cluster, queue) pairs for a job.

    Returns ``{request, candidates, blocked, considered}``. ``candidates``
    is sorted best-first; ``blocked`` lists what was excluded and why, so a
    user who sees no results learns something.
    """
    wait_estimates = wait_estimates or {}
    candidates: List[Dict[str, Any]] = []
    blocked: List[Dict[str, Any]] = []
    considered = 0

    for cluster in clusters or []:
        meta = cluster.get("cluster_metadata") or {}
        name = meta.get("name") or str(meta.get("uri") or "").rsplit("/", 1)[-1]
        cluster_slug = _slug(name)
        status = str(meta.get("status") or "").upper()
        cores_total, cores_running, gpus_total = _cluster_capacity(cluster)
        cores_free = max(cores_total - cores_running, 0)
        allocation_left = _allocation_hours(cluster)

        cluster_blockers = []
        if status and status not in OPERATIONAL:
            cluster_blockers.append(f"cluster status is {status}")
        if cores_total and request.cores > cores_total:
            cluster_blockers.append(
                f"job needs {request.cores:,} cores, cluster has {cores_total:,}"
            )
        if request.gpus and not gpus_total:
            cluster_blockers.append("job needs GPUs, cluster reports none")
        if allocation_left is not None and allocation_left <= 0:
            cluster_blockers.append("no allocation hours remaining")

        for queue in (cluster.get("queue_data") or {}).get("queues") or []:
            queue_name = str(queue.get("queue_name") or "").strip()
            if not queue_name:
                continue
            considered += 1

            blockers = list(cluster_blockers)
            walltime_hours = parse_walltime_hours(queue.get("max_walltime"))
            if walltime_hours is not None and request.hours > walltime_hours:
                blockers.append(
                    f"walltime limit is {walltime_hours:g}h, job needs {request.hours:g}h"
                )
            # Per-queue size caps matter: a data-transfer queue on a big
            # cluster has the whole machine's idle cores behind it but will
            # not take a 1,024-core job. "-" parses to 0, meaning unknown.
            max_cores = _num(queue.get("max_cores"))
            if max_cores and request.cores > max_cores:
                blockers.append(
                    f"queue caps jobs at {max_cores:,.0f} cores, "
                    f"job needs {request.cores:,}"
                )
            if request.queue_type:
                queue_type = str(queue.get("queue_type") or "").lower()
                if request.queue_type.lower() not in (queue_type, queue_name.lower()):
                    continue
            if (
                allocation_left is not None
                and 0 < allocation_left < request.core_hours
            ):
                blockers.append(
                    f"job needs {request.core_hours:,.0f} core-hours, "
                    f"{allocation_left:,.0f} remain"
                )

            entry = _score_candidate(
                cluster_name=name,
                cluster_slug=cluster_slug,
                queue=queue,
                request=request,
                cores_free=cores_free,
                cores_total=cores_total,
                allocation_left=allocation_left,
                walltime_hours=walltime_hours,
                estimate=wait_estimates.get((cluster_slug, queue_name)),
            )
            if blockers:
                entry["blockers"] = blockers
                blocked.append(entry)
            else:
                candidates.append(entry)

    # Ties are common (several queues on one idle cluster score alike), so
    # break them on the most idle capacity, then deterministically by name.
    candidates.sort(
        key=lambda c: (-c["score"], -c["cores_free"], c["cluster"], c["queue"] or "")
    )
    blocked.sort(
        key=lambda c: (-c["score"], -c["cores_free"], c["cluster"], c["queue"] or "")
    )
    return {
        "request": {
            "cores": request.cores,
            "hours": request.hours,
            "gpus": request.gpus,
            "core_hours": request.core_hours,
            "queue_type": request.queue_type,
        },
        "candidates": candidates[:limit],
        "blocked": blocked[:limit],
        "considered": considered,
    }


def _score_candidate(
    *,
    cluster_name: str,
    cluster_slug: str,
    queue: Dict[str, Any],
    request: PlacementRequest,
    cores_free: int,
    cores_total: int,
    allocation_left: Optional[float],
    walltime_hours: Optional[float],
    estimate: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    reasons: List[str] = []
    pending_cores = _num(queue.get("cores_pending"))
    running_cores = _num(queue.get("cores_running"))

    # Capacity: can the job start on what is idle right now?
    if request.cores <= 0:
        capacity_ratio = 1.0
    else:
        capacity_ratio = min(1.0, cores_free / request.cores)
    capacity_score = capacity_ratio * WEIGHT_CAPACITY
    if capacity_ratio >= 1:
        reasons.append(f"{cores_free:,} cores idle now — enough to start immediately")
    elif cores_free:
        reasons.append(
            f"only {cores_free:,} of the {request.cores:,} cores requested are idle"
        )

    # Wait: measured, when we have it.
    wait_hours = None
    if estimate and estimate.get("wait_seconds") is not None:
        wait_hours = estimate["wait_seconds"] / 3600
        wait_score = max(0.0, 1 - min(1.0, wait_hours / WAIT_CEILING_HOURS)) * WEIGHT_WAIT
        display = estimate.get("wait_display")
        if display == "no backlog":
            reasons.append("nothing queued ahead of you")
        elif display:
            reasons.append(f"estimated start {display}")
    else:
        # Unknown is scored mid-band: neither rewarded nor punished.
        wait_score = WEIGHT_WAIT * 0.5

    # Backlog relative to the cluster's size.
    if cores_total > 0:
        backlog_share = min(1.0, pending_cores / cores_total)
        backlog_score = (1 - backlog_share) * WEIGHT_BACKLOG
        if backlog_share >= 0.25:
            reasons.append(
                f"{pending_cores:,.0f} cores already queued "
                f"({backlog_share * 100:.0f}% of the cluster)"
            )
    else:
        backlog_score = WEIGHT_BACKLOG * 0.5
        backlog_share = None

    # Allocation headroom.
    if allocation_left is None:
        allocation_score = WEIGHT_ALLOCATION * 0.5
        allocation_ratio = None
    elif request.core_hours <= 0:
        allocation_score = WEIGHT_ALLOCATION
        allocation_ratio = None
    else:
        allocation_ratio = allocation_left / request.core_hours
        allocation_score = min(1.0, allocation_ratio) * WEIGHT_ALLOCATION
        if allocation_ratio < 3:
            reasons.append(
                f"this job would use {1 / allocation_ratio * 100:.0f}% of the "
                f"remaining allocation"
                if allocation_ratio
                else "no allocation left"
            )

    score = capacity_score + wait_score + backlog_score + allocation_score

    return {
        "cluster": cluster_name,
        "cluster_slug": cluster_slug,
        "queue": queue.get("queue_name"),
        "queue_type": queue.get("queue_type"),
        "score": round(score, 1),
        "components": {
            "capacity": round(capacity_score, 1),
            "wait": round(wait_score, 1),
            "backlog": round(backlog_score, 1),
            "allocation": round(allocation_score, 1),
        },
        "cores_free": cores_free,
        "cores_total": cores_total,
        "cores_pending": int(pending_cores),
        "cores_running": int(running_cores),
        "max_walltime": queue.get("max_walltime"),
        "max_walltime_hours": walltime_hours,
        "allocation_hours_remaining": (
            round(allocation_left) if allocation_left is not None else None
        ),
        "wait_estimate": estimate,
        "wait_hours": round(wait_hours, 2) if wait_hours is not None else None,
        "reasons": reasons,
        "links": {
            "queues": f"queues.html?cluster={cluster_slug}",
            "topology": f"topology.html?node=sys:{cluster_slug}",
        },
    }
