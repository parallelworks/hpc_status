"""Fleet-wide insight generation.

This is the logic behind ``/api/insights``, lifted out of the request
handler so the topology graph can attach the same insights to the nodes
they belong to. One implementation, two consumers — a warning badge on a
node and the row on the Insights page always agree.

Insights are plain dictionaries (not ``SystemInsight``) because that is the
shape the existing API and frontend already speak.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Status values that mean "operational" across the collectors we support.
OPERATIONAL_STATUSES = frozenset({"ON", "UP", "RUNNING", "ONLINE", "ACTIVE"})


def _safe_number(value: Any, default: float = 0) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except Exception:
        return default


def generate_fleet_insights(
    fleet_payload: Optional[Dict[str, Any]],
    cluster_payload: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Generate insights from fleet status and cluster telemetry.

    Args:
        fleet_payload: ``/api/status`` shaped payload.
        cluster_payload: list of PW cluster telemetry entries.

    Returns:
        Insight dicts sorted by descending priority.
    """
    insights: List[Dict[str, Any]] = []
    insights.extend(_fleet_status_insights(fleet_payload))
    for cluster in cluster_payload or []:
        insights.extend(_cluster_insights(cluster))
    insights.sort(key=lambda item: item.get("priority", 0), reverse=True)
    return insights


def _fleet_status_insights(fleet_payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Insights derived from the fleet status collector."""
    if not fleet_payload or not fleet_payload.get("systems"):
        return []

    from .recommendations import RecommendationEngine

    generated = []
    engine = RecommendationEngine(fleet_payload.get("systems", []))
    for insight in engine.generate_insights():
        generated.append(
            {
                "type": insight.type,
                "message": insight.message,
                "priority": insight.priority,
                "metric": insight.related_metric,
                "cluster": insight.cluster,
                "queue": insight.queue,
                "system": insight.system,
                "action_description": insight.action_description,
            }
        )
    return generated


def _cluster_insights(cluster: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Insights for a single cluster's telemetry payload."""
    insights: List[Dict[str, Any]] = []
    metadata = cluster.get("cluster_metadata", {}) or {}
    name = metadata.get("name", "Unknown")
    status = (metadata.get("status") or "").upper()

    if status and status not in OPERATIONAL_STATUSES:
        insights.append(
            {
                "type": "warning",
                "message": f"{name}: Cluster status is {status}",
                "priority": 4,
                "metric": "status",
                "cluster": name,
                "queue": None,
                "action_description": "Use an alternative system",
            }
        )

    insights.extend(_allocation_insights(cluster, name))
    insights.extend(_queue_depth_insights(cluster, name))
    insights.extend(_gpu_insights(cluster, name))
    return insights


def _allocation_insights(cluster: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    insights = []
    usage_data = cluster.get("usage_data", {}) or {}
    for system in usage_data.get("systems", []):
        allocated = _safe_number(system.get("hours_allocated", 0))
        remaining = _safe_number(system.get("hours_remaining", 0))
        if allocated <= 0:
            continue
        percent = (remaining / allocated) * 100
        if percent < 10:
            insights.append(
                {
                    "type": "warning",
                    "message": (
                        f"{name}: Allocation critically low ({percent:.0f}% remaining)"
                    ),
                    "priority": 5,
                    "metric": "allocation",
                    "cluster": name,
                }
            )
        elif percent < 25:
            insights.append(
                {
                    "type": "warning",
                    "message": f"{name}: Allocation running low ({percent:.0f}% remaining)",
                    "priority": 3,
                    "metric": "allocation",
                    "cluster": name,
                }
            )
    return insights


def _queue_depth_insights(cluster: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    """Backlog warnings, sized against the cluster's own capacity.

    "100 pending" means very different things on a 60k-core machine and a
    50-core one, so pending *cores* relative to capacity is the primary
    signal; absolute job counts are the fallback for clusters with no node
    inventory reported.
    """
    insights = []
    queue_data = cluster.get("queue_data", {}) or {}
    cluster_total_cores = sum(
        _safe_number(node.get("cores_available", 0))
        for node in queue_data.get("nodes", [])
    )

    for queue in queue_data.get("queues", []):
        queue_name = queue.get("queue_name", "Unknown")
        pending_jobs = _safe_number(queue.get("jobs_pending", 0))
        pending_cores = _safe_number(queue.get("cores_pending", 0))
        running_cores = _safe_number(queue.get("cores_running", 0))

        severity = None
        detail = ""
        if cluster_total_cores > 0 and pending_cores > 0:
            share = pending_cores / cluster_total_cores
            if share >= 0.25:
                severity = "warning"
                detail = (
                    f"queue is holding {int(pending_cores):,} cores "
                    f"({share * 100:.0f}% of cluster capacity) of pending demand"
                )
            elif running_cores > 0 and pending_cores / running_cores >= 2:
                severity = "info"
                detail = (
                    f"pending demand is {pending_cores / running_cores:.1f}× "
                    f"the running load"
                )
        elif pending_jobs > 100:
            severity = "warning"
            detail = f"{int(pending_jobs)} pending jobs"
        elif pending_jobs > 50:
            severity = "info"
            detail = f"{int(pending_jobs)} pending jobs"

        if severity:
            insights.append(
                {
                    "type": severity,
                    "message": f"{name}/{queue_name}: {detail}",
                    "priority": 4 if severity == "warning" else 2,
                    "metric": "queue_depth",
                    "cluster": name,
                    "queue": queue_name,
                    "action_description": (
                        "Consider an alternative queue or cluster for new jobs"
                        if severity == "warning"
                        else None
                    ),
                }
            )
    return insights


def _gpu_insights(cluster: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    insights = []
    summary = (cluster.get("gpu_data", {}) or {}).get("summary", {}) or {}
    if not summary.get("gpu_count", 0):
        return insights

    utilization = summary.get("avg_utilization_percent", 0)
    if utilization > 90:
        insights.append(
            {
                "type": "info",
                "message": f"{name}: High GPU utilization ({utilization}%)",
                "priority": 2,
                "metric": "gpu_utilization",
                "cluster": name,
            }
        )
    elif utilization < 10:
        insights.append(
            {
                "type": "info",
                "message": f"{name}: GPUs are mostly idle ({utilization}% utilization)",
                "priority": 1,
                "metric": "gpu_utilization",
                "cluster": name,
            }
        )
    return insights
