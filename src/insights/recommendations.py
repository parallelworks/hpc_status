"""Recommendation engine for queue selection and load balancing.

Provides intelligent suggestions for:
- Best queue/system for a given job
- Load balancing across multiple systems
- Allocation and storage warnings
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..data.models import SystemInsight


@dataclass
class JobRequirements:
    """Requirements for a job to be submitted."""

    cores: int
    walltime_hours: float
    memory_gb: Optional[float] = None
    gpus: int = 0
    storage_gb: Optional[float] = None
    priority: str = "normal"  # 'low', 'normal', 'high'


@dataclass
class QueueRecommendation:
    """A queue recommendation with scoring and explanation."""

    system: str
    queue: str
    score: float
    estimated_wait_minutes: Optional[int]
    reason: str
    allocation_remaining: Optional[float] = None
    storage_available_gb: Optional[float] = None


class RecommendationEngine:
    """Engine for generating queue and system recommendations.

    Uses current system data and user context to provide intelligent
    suggestions for job placement.
    """

    def __init__(self, systems_data: List[Dict], user_context: Optional[Dict] = None):
        """Initialize the recommendation engine.

        Args:
            systems_data: List of system/cluster data dictionaries
            user_context: Optional user-specific context (groups, quotas, etc.)
        """
        self.systems = systems_data
        self.user = user_context or {}

    def recommend_queue(
        self, requirements: JobRequirements, max_results: int = 5
    ) -> List[QueueRecommendation]:
        """Get ranked queue recommendations for given job requirements.

        Args:
            requirements: Job requirements
            max_results: Maximum number of recommendations to return

        Returns:
            List of QueueRecommendation sorted by score (highest first)
        """
        candidates: List[QueueRecommendation] = []

        for system in self.systems:
            # Skip offline systems
            if system.get("status", "").upper() not in ("UP", "ON"):
                continue

            queues = system.get("queues", [])
            for queue in queues:
                score = self._score_queue(queue, requirements, system)
                if score <= 0:
                    continue

                wait = self._estimate_wait(queue, requirements)
                reason = self._explain_recommendation(queue, requirements, system)
                allocation = self._get_allocation_remaining(system)

                candidates.append(
                    QueueRecommendation(
                        system=system.get("cluster") or system.get("name", "Unknown"),
                        queue=queue.get("name") or queue.get("queue_name", "Unknown"),
                        score=score,
                        estimated_wait_minutes=wait,
                        reason=reason,
                        allocation_remaining=allocation,
                    )
                )

        # Sort by score descending
        candidates.sort(key=lambda x: x.score, reverse=True)
        return candidates[:max_results]

    def _score_queue(
        self, queue: Dict, requirements: JobRequirements, system: Dict
    ) -> float:
        """Calculate a score for how well a queue matches requirements.

        Returns a score between 0 and 1, where higher is better.
        Returns 0 if the queue cannot handle the job.
        """
        score = 1.0

        # Check walltime limits
        max_walltime = self._parse_walltime(queue.get("max_walltime", ""))
        if max_walltime and requirements.walltime_hours > max_walltime:
            return 0  # Queue can't handle this job duration

        # Penalize for pending jobs (queue depth)
        pending_jobs = self._safe_int(queue.get("jobs", {}).get("pending", 0))
        if pending_jobs > 0:
            # More pending = lower score
            score *= max(0.2, 1 - (pending_jobs * 0.05))

        # Penalize for high utilization
        utilization = queue.get("utilization_percent")
        if utilization is not None:
            if utilization > 90:
                score *= 0.5
            elif utilization > 70:
                score *= 0.8

        # Boost for matching queue type
        queue_type = (queue.get("type") or queue.get("queue_type") or "").lower()
        if requirements.gpus > 0 and "gpu" in queue_type:
            score *= 1.2  # GPU jobs should prefer GPU queues
        elif requirements.gpus == 0 and "gpu" in queue_type:
            score *= 0.5  # Non-GPU jobs should avoid GPU queues

        # Consider allocation remaining
        allocation = self._get_allocation_remaining(system)
        if allocation is not None:
            if allocation < 10:
                score *= 0.3  # Very low allocation
            elif allocation < 25:
                score *= 0.7  # Low allocation
            elif allocation > 75:
                score *= 1.1  # Healthy allocation

        return min(1.0, score)

    def _estimate_wait(self, queue: Dict, requirements: JobRequirements) -> Optional[int]:
        """Estimate wait time in minutes for a job in this queue.

        This is a rough estimate based on queue depth.
        """
        pending_jobs = self._safe_int(queue.get("jobs", {}).get("pending", 0))

        if pending_jobs == 0:
            return 5  # Likely to start quickly

        # Very rough estimate: assume average job is 1 hour
        # More sophisticated would use historical data
        estimated_minutes = pending_jobs * 30

        return min(estimated_minutes, 480)  # Cap at 8 hours

    def _explain_recommendation(
        self, queue: Dict, requirements: JobRequirements, system: Dict
    ) -> str:
        """Generate a human-readable explanation for the recommendation."""
        reasons = []

        pending = self._safe_int(queue.get("jobs", {}).get("pending", 0))
        if pending == 0:
            reasons.append("No pending jobs")
        elif pending < 5:
            reasons.append("Low queue depth")

        allocation = self._get_allocation_remaining(system)
        if allocation is not None and allocation > 50:
            reasons.append(f"{allocation:.0f}% allocation remaining")

        utilization = queue.get("utilization_percent")
        if utilization is not None and utilization < 50:
            reasons.append("Good capacity available")

        return "; ".join(reasons) if reasons else "Available"

    def _get_allocation_remaining(self, system: Dict) -> Optional[float]:
        """Get allocation percentage remaining for a system."""
        usage = system.get("usage", {})
        percent = usage.get("percent_remaining")
        if percent is not None:
            return float(percent)

        allocated = usage.get("total_allocated_hours", 0)
        remaining = usage.get("total_remaining_hours", 0)
        if allocated > 0:
            return (remaining / allocated) * 100
        return None

    def _parse_walltime(self, walltime: str) -> Optional[float]:
        """Parse walltime string (HH:MM:SS) to hours."""
        if not walltime or walltime == "-":
            return None
        try:
            parts = walltime.split(":")
            if len(parts) == 3:
                hours = int(parts[0])
                minutes = int(parts[1])
                return hours + minutes / 60
            return None
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        try:
            return int(str(value).strip().replace(",", ""))
        except (ValueError, TypeError):
            return default

    def suggest_load_balance(
        self, total_jobs: int, requirements: JobRequirements
    ) -> Dict[str, Any]:
        """Suggest distribution across systems for parallel job submission.

        Args:
            total_jobs: Total number of jobs to distribute
            requirements: Requirements for each job

        Returns:
            Dictionary with 'distribution' and metadata
        """
        available_systems = [
            s for s in self.systems
            if s.get("status", "").upper() in ("UP", "ON")
        ]

        if not available_systems:
            return {
                "distribution": {},
                "systems_excluded": {"all": "No systems available"},
                "confidence": 0,
            }

        # Calculate capacity for each system
        system_capacities = {}
        for system in available_systems:
            name = system.get("cluster") or system.get("name", "Unknown")
            capacity = self._calculate_capacity(system, requirements)
            if capacity > 0:
                system_capacities[name] = {
                    "capacity": capacity,
                    "system": system,
                }

        if not system_capacities:
            return {
                "distribution": {},
                "systems_excluded": {"all": "No systems with available capacity"},
                "confidence": 0,
            }

        # Distribute jobs proportionally to capacity
        total_capacity = sum(v["capacity"] for v in system_capacities.values())
        distribution = {}
        jobs_distributed = 0

        for name, info in system_capacities.items():
            ratio = info["capacity"] / total_capacity
            jobs = int(total_jobs * ratio)

            # Get best queue for this system
            queues = info["system"].get("queues", [])
            best_queue = self._find_best_queue(queues, requirements)

            distribution[name] = {
                "jobs": jobs,
                "queue": best_queue,
                "reason": f"{ratio * 100:.0f}% of available capacity",
            }
            jobs_distributed += jobs

        # Distribute any remaining jobs to highest capacity system
        if jobs_distributed < total_jobs:
            best_system = max(system_capacities.keys(), key=lambda k: system_capacities[k]["capacity"])
            distribution[best_system]["jobs"] += total_jobs - jobs_distributed

        return {
            "distribution": distribution,
            "systems_excluded": {},
            "confidence": 0.85,
        }

    def _calculate_capacity(self, system: Dict, requirements: JobRequirements) -> float:
        """Calculate effective capacity of a system for given requirements."""
        # Base capacity on allocation and queue availability
        allocation = self._get_allocation_remaining(system)
        if allocation is not None and allocation < 5:
            return 0  # Allocation nearly depleted

        queues = system.get("queues", [])
        if not queues:
            return 0

        # Sum available cores across queues
        total_free_cores = 0
        for queue in queues:
            cores = queue.get("cores", {})
            running = self._safe_int(cores.get("running", 0))
            pending = self._safe_int(cores.get("pending", 0))
            # Rough estimate: prefer queues with low pending
            free_estimate = max(0, 1000 - running - pending * 2)
            total_free_cores += free_estimate

        return float(total_free_cores)

    def _find_best_queue(self, queues: List[Dict], requirements: JobRequirements) -> str:
        """Find the best queue from a list for given requirements."""
        if not queues:
            return "standard"

        # Score each queue
        scored = []
        for queue in queues:
            name = queue.get("name") or queue.get("queue_name", "unknown")
            pending = self._safe_int(queue.get("jobs", {}).get("pending", 0))
            scored.append((name, pending))

        # Return queue with lowest pending jobs
        scored.sort(key=lambda x: x[1])
        return scored[0][0]

    def generate_insights(self) -> List[SystemInsight]:
        """Generate all current insights and warnings.

        Returns a list of insights sorted by priority.
        """
        insights: List[SystemInsight] = []

        # Allocation warnings
        for system in self.systems:
            name = system.get("cluster") or system.get("name", "Unknown")
            allocation = self._get_allocation_remaining(system)

            if allocation is not None:
                if allocation < 10:
                    insights.append(
                        SystemInsight(
                            type="warning",
                            message=f"{name}: Allocation critically low ({allocation:.0f}% remaining). Request additional hours soon.",
                            priority=5,
                            related_metric="allocation",
                            cluster=name,
                        )
                    )
                elif allocation < 25:
                    insights.append(
                        SystemInsight(
                            type="warning",
                            message=f"{name}: Allocation running low ({allocation:.0f}% remaining).",
                            priority=3,
                            related_metric="allocation",
                            cluster=name,
                        )
                    )

            # Queue depth warnings
            for queue in system.get("queues", []):
                pending = self._safe_int(queue.get("jobs", {}).get("pending", 0))
                if pending > 50:
                    queue_name = queue.get("name") or queue.get("queue_name")
                    insights.append(
                        SystemInsight(
                            type="info",
                            message=f"{name}/{queue_name}: High queue depth ({pending} pending jobs). Consider alternative systems.",
                            priority=2,
                            related_metric="queue_depth",
                            cluster=name,
                        )
                    )

        # System status warnings
        for system in self.systems:
            status = system.get("status", "").upper()
            name = system.get("cluster") or system.get("name", "Unknown")

            if status == "DEGRADED":
                insights.append(
                    SystemInsight(
                        type="warning",
                        message=f"{name}: System is degraded. Jobs may experience delays.",
                        priority=4,
                        related_metric="status",
                        cluster=name,
                    )
                )
            elif status == "MAINTENANCE":
                insights.append(
                    SystemInsight(
                        type="warning",
                        message=f"{name}: System under maintenance. Consider alternative systems.",
                        priority=4,
                        related_metric="status",
                        cluster=name,
                    )
                )

        # Sort by priority (highest first)
        insights.sort(key=lambda x: x.priority, reverse=True)
        return insights
