"""Data models for HPC status monitoring.

This module defines the core data structures for representing HPC resources,
following these semantic principles:

1. CAPACITY vs AVAILABILITY
   - Capacity: Total/maximum resources (static, changes with hardware)
   - Availability: Currently usable resources (dynamic, changes with load)

2. EXPLICIT UNITS
   - All numeric fields have clear units in their names or documentation
   - Time: seconds (integers) or hours (floats for allocations)
   - Storage: gigabytes (floats)
   - Counts: cores, nodes, jobs (integers)

3. NORMALIZED STATUS VALUES
   - System: UP, DOWN, DEGRADED, MAINTENANCE, UNKNOWN
   - Queue: ACTIVE, INACTIVE, DRAINING, OFFLINE
   - Storage: HEALTHY, WARNING, CRITICAL
   - Insight Severity: CRITICAL, WARNING, INFO, SUGGESTION

See schemas/ directory for JSON Schema definitions.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional, Dict, Any


# =============================================================================
# Status Enumerations (Normalized across schedulers)
# =============================================================================


class SystemOperationalStatus(str, Enum):
    """Operational status of an HPC system."""

    UP = "UP"  # System is fully operational and accepting jobs
    DOWN = "DOWN"  # System is offline and not accessible
    DEGRADED = "DEGRADED"  # Operational but with reduced capacity
    MAINTENANCE = "MAINTENANCE"  # Undergoing planned maintenance
    UNKNOWN = "UNKNOWN"  # Status could not be determined


class QueueState(str, Enum):
    """State of a scheduler queue or partition."""

    ACTIVE = "ACTIVE"  # Accepting and running jobs
    INACTIVE = "INACTIVE"  # Not accepting new jobs
    DRAINING = "DRAINING"  # Running existing jobs, not accepting new ones
    OFFLINE = "OFFLINE"  # Completely offline


class StorageHealthStatus(str, Enum):
    """Health status of a storage filesystem."""

    HEALTHY = "HEALTHY"  # Usage below warning threshold (<80%)
    WARNING = "WARNING"  # Usage elevated (80-95%)
    CRITICAL = "CRITICAL"  # Nearly full (>95%)


class InsightSeverity(str, Enum):
    """Severity level of a generated insight."""

    CRITICAL = "CRITICAL"  # Requires immediate attention
    WARNING = "WARNING"  # Should be addressed soon
    INFO = "INFO"  # Informational, no action required
    SUGGESTION = "SUGGESTION"  # Optional improvement


class InsightType(str, Enum):
    """Category of insight."""

    RECOMMENDATION = "RECOMMENDATION"  # Suggestion for where/how to run jobs
    WARNING = "WARNING"  # Potential issue
    ALERT = "ALERT"  # Active issue requiring attention
    INFO = "INFO"  # General information


# =============================================================================
# Resource Pool Model (Capacity vs Availability)
# =============================================================================


@dataclass
class ResourceCapacity:
    """Static capacity - total/maximum resources.

    Capacity changes only when hardware is added/removed or policies change.
    """

    total: int  # Total resources that exist
    allocatable: Optional[int] = None  # Resources that could be allocated (minus reserved)

    def __post_init__(self):
        if self.allocatable is None:
            self.allocatable = self.total


@dataclass
class ResourceAvailability:
    """Dynamic availability - current resource state.

    Availability changes constantly as jobs start and complete.
    """

    idle: int  # Resources currently idle and allocatable
    allocated: int  # Resources allocated to running jobs
    pending: int = 0  # Resources requested by queued jobs
    reserved: int = 0  # Resources reserved for future use
    offline: int = 0  # Resources unavailable (failed, draining)


@dataclass
class ResourcePool:
    """A pool of compute resources with explicit capacity vs availability.

    This is the core abstraction for understanding HPC resource state.
    Unit should be one of: 'cores', 'nodes', 'gpus', 'gigabytes'
    """

    unit: str  # 'cores', 'nodes', 'gpus', 'gigabytes'
    capacity: ResourceCapacity
    availability: ResourceAvailability

    @property
    def utilization_percent(self) -> float:
        """Calculate current utilization as a percentage."""
        if self.capacity.total == 0:
            return 0.0
        return (self.availability.allocated / self.capacity.total) * 100


# =============================================================================
# Storage Model
# =============================================================================


@dataclass
class StorageInfo:
    """Storage capacity for a filesystem.

    Units:
    - total_gb, used_gb, available_gb: gigabytes (float)
    - percent_used: percentage 0-100 (float)
    """

    mount_point: str  # $HOME, $WORKDIR, /scratch
    filesystem: str  # Physical device/path
    total_gb: float  # Unit: gigabytes
    used_gb: float  # Unit: gigabytes
    available_gb: float  # Unit: gigabytes
    percent_used: float  # Unit: percent (0-100)
    storage_type: Optional[str] = None  # HOME, WORK, SCRATCH, PROJECT, ARCHIVE

    @property
    def status(self) -> StorageHealthStatus:
        """Return status based on usage percentage."""
        if self.percent_used >= 95:
            return StorageHealthStatus.CRITICAL
        elif self.percent_used >= 80:
            return StorageHealthStatus.WARNING
        return StorageHealthStatus.HEALTHY

    @property
    def status_str(self) -> str:
        """Return status as lowercase string for backward compatibility."""
        return self.status.value.lower()


@dataclass
class QueueInfo:
    """Queue information from a scheduler.

    Units:
    - max_walltime: HH:MM:SS string format
    - max_walltime_seconds: seconds (integer) - preferred for computation
    - cores: integer counts
    - jobs: integer counts
    """

    name: str
    queue_type: str  # 'BATCH', 'DEBUG', 'GPU', 'INTERACTIVE', 'BIGMEM'
    max_walltime: str  # e.g., "24:00:00" (display format)
    state: QueueState = QueueState.ACTIVE
    max_walltime_seconds: Optional[int] = None  # Unit: seconds
    max_jobs: Optional[str] = None
    min_cores: Optional[int] = None  # Unit: cores
    max_cores: Optional[int] = None  # Unit: cores
    max_nodes: Optional[int] = None  # Unit: nodes
    jobs_running: int = 0  # Unit: jobs
    jobs_pending: int = 0  # Unit: jobs
    jobs_held: int = 0  # Unit: jobs
    cores_running: int = 0  # Unit: cores
    cores_pending: int = 0  # Unit: cores
    enabled: bool = True
    reserved: bool = False
    wait_estimate_seconds: Optional[int] = None  # Unit: seconds
    wait_estimate_display: Optional[str] = None  # e.g., "~5 minutes"

    def __post_init__(self):
        """Parse walltime string to seconds if not provided."""
        if self.max_walltime_seconds is None and self.max_walltime:
            self.max_walltime_seconds = self._parse_walltime(self.max_walltime)

    @staticmethod
    def _parse_walltime(walltime: str) -> Optional[int]:
        """Parse HH:MM:SS or DD:HH:MM:SS format to seconds."""
        try:
            parts = walltime.split(":")
            if len(parts) == 3:
                h, m, s = map(int, parts)
                return h * 3600 + m * 60 + s
            elif len(parts) == 4:
                d, h, m, s = map(int, parts)
                return d * 86400 + h * 3600 + m * 60 + s
        except (ValueError, TypeError):
            pass
        return None


@dataclass
class NodeInfo:
    """Node class information from a cluster."""

    node_type: str  # 'Standard', 'GPU', 'Bigmem', etc.
    nodes_available: int
    cores_per_node: int
    cores_available: int
    cores_running: int
    cores_free: int


class AllocationStatus(str, Enum):
    """Health status of an allocation."""

    HEALTHY = "HEALTHY"  # Adequate hours remaining (>20%)
    LOW = "LOW"  # Running low (5-20% remaining)
    CRITICAL = "CRITICAL"  # Nearly exhausted (<5% remaining)
    EXHAUSTED = "EXHAUSTED"  # No hours remaining
    EXPIRED = "EXPIRED"  # Allocation period has ended


@dataclass
class AllocationInfo:
    """Allocation/usage information for a subproject.

    Units:
    - hours_allocated, hours_used, hours_remaining: compute hours (float)
    - percent_remaining, percent_used: percentage 0-100 (float)
    - burn_rate_hours_per_day: hours/day (float)
    """

    system: str
    subproject: str
    hours_allocated: int  # Unit: compute hours
    hours_used: int  # Unit: compute hours
    hours_remaining: int  # Unit: compute hours
    percent_remaining: float  # Unit: percent (0-100)
    percent_used: Optional[float] = None  # Unit: percent (0-100)
    background_hours_used: int = 0  # Unit: compute hours
    hours_pending: int = 0  # Unit: compute hours (committed to queued jobs)
    burn_rate_hours_per_day: Optional[float] = None  # Unit: hours/day
    days_remaining_at_current_rate: Optional[int] = None  # Unit: days
    status: AllocationStatus = AllocationStatus.HEALTHY

    def __post_init__(self):
        """Calculate derived fields."""
        if self.percent_used is None and self.hours_allocated > 0:
            self.percent_used = (self.hours_used / self.hours_allocated) * 100

        # Determine status based on percent_remaining
        if self.percent_remaining <= 0:
            self.status = AllocationStatus.EXHAUSTED
        elif self.percent_remaining < 5:
            self.status = AllocationStatus.CRITICAL
        elif self.percent_remaining < 20:
            self.status = AllocationStatus.LOW
        else:
            self.status = AllocationStatus.HEALTHY


@dataclass
class UserContext:
    """User-specific information on a system."""

    username: str
    groups: List[str] = field(default_factory=list)
    storage: List[StorageInfo] = field(default_factory=list)
    active_jobs: int = 0
    pending_jobs: int = 0

    def home_storage(self) -> Optional[StorageInfo]:
        """Get home directory storage info."""
        return next((s for s in self.storage if s.mount_point == "$HOME"), None)

    def workdir_storage(self) -> Optional[StorageInfo]:
        """Get work directory storage info."""
        return next((s for s in self.storage if s.mount_point == "$WORKDIR"), None)


@dataclass
class QueueHealth:
    """Queue status with actionable metrics."""

    name: str
    status: str  # 'available', 'busy', 'draining', 'offline'
    max_walltime_hours: float
    running_jobs: int
    pending_jobs: int
    available_cores: int
    total_cores: int
    estimated_wait_minutes: Optional[int] = None
    recommended_for: List[str] = field(default_factory=list)


@dataclass
class SystemCapacity:
    """Static capacity of an HPC system (changes only with hardware)."""

    total_nodes: Optional[int] = None  # Unit: nodes
    total_cores: Optional[int] = None  # Unit: cores
    total_gpus: Optional[int] = None  # Unit: gpus
    total_memory_gb: Optional[float] = None  # Unit: gigabytes
    architecture: Optional[str] = None  # e.g., "Cray EX (AMD EPYC)"


@dataclass
class SystemAvailability:
    """Dynamic availability of an HPC system (changes with load)."""

    nodes_up: Optional[int] = None  # Unit: nodes
    nodes_down: Optional[int] = None  # Unit: nodes
    cores_available: Optional[int] = None  # Unit: cores
    cores_in_use: Optional[int] = None  # Unit: cores
    utilization_percent: Optional[float] = None  # Unit: percent (0-100)


@dataclass
class SystemStatus:
    """Status information for an HPC system.

    Distinguishes between:
    - status: Operational status (UP, DOWN, DEGRADED, MAINTENANCE, UNKNOWN)
    - capacity: Total hardware resources (static)
    - availability: Current resource state (dynamic)
    """

    system: str
    status: str  # 'UP', 'DOWN', 'DEGRADED', 'MAINTENANCE', 'UNKNOWN'
    status_reason: Optional[str] = None  # Explanation for non-UP status
    dsrc: Optional[str] = None  # DSRC identifier (HPCMP-specific)
    site_name: Optional[str] = None  # Site name
    site_organization: Optional[str] = None  # Organization
    login: Optional[str] = None  # Login node hostname
    scheduler: Optional[str] = None  # 'SLURM', 'PBS'
    capacity: Optional[SystemCapacity] = None  # Static capacity
    availability: Optional[SystemAvailability] = None  # Dynamic availability
    raw_alt: Optional[str] = None  # Raw alt text from status image
    img_src: Optional[str] = None  # Status image URL
    source_url: Optional[str] = None  # Source URL for the status
    observed_at: Optional[str] = None  # ISO timestamp

    @property
    def slug(self) -> str:
        """Return slugified system name."""
        return "".join(c for c in (self.system or "").lower() if c.isalnum())

    @property
    def operational_status(self) -> SystemOperationalStatus:
        """Return status as enum."""
        try:
            return SystemOperationalStatus(self.status.upper())
        except ValueError:
            return SystemOperationalStatus.UNKNOWN


@dataclass
class ClusterData:
    """Complete data for a PW cluster."""

    name: str
    uri: str
    status: str  # 'on', 'off'
    cluster_type: str  # 'existing', etc.
    timestamp: str
    allocations: List[AllocationInfo] = field(default_factory=list)
    queues: List[QueueInfo] = field(default_factory=list)
    nodes: List[NodeInfo] = field(default_factory=list)
    header: str = ""
    fiscal_year_info: str = ""


@dataclass
class SystemInsight:
    """Generated insight about a system or cluster.

    Insights are categorized by type and severity:
    - type: What kind of insight (RECOMMENDATION, WARNING, ALERT, INFO)
    - severity: How urgent (CRITICAL, WARNING, INFO, SUGGESTION)

    The priority field (1-5) is deprecated; use severity instead.
    """

    type: str  # 'RECOMMENDATION', 'WARNING', 'ALERT', 'INFO'
    message: str
    priority: int  # 1-5, higher = more important (deprecated, use severity)
    severity: InsightSeverity = InsightSeverity.INFO
    related_metric: Optional[str] = None
    system: Optional[str] = None
    cluster: Optional[str] = None
    queue: Optional[str] = None
    project: Optional[str] = None
    storage: Optional[str] = None
    action_description: Optional[str] = None
    action_command: Optional[str] = None

    def __post_init__(self):
        """Set severity based on priority for backward compatibility."""
        if self.priority >= 5:
            self.severity = InsightSeverity.CRITICAL
        elif self.priority >= 4:
            self.severity = InsightSeverity.WARNING
        elif self.priority >= 2:
            self.severity = InsightSeverity.INFO
        else:
            self.severity = InsightSeverity.SUGGESTION


@dataclass
class FleetSummary:
    """Summary statistics for the entire fleet."""

    total_systems: int
    status_counts: Dict[str, int]
    dsrc_counts: Dict[str, int]
    scheduler_counts: Dict[str, int]
    uptime_ratio: float


@dataclass
class StatusPayload:
    """Complete status payload for API responses."""

    meta: Dict[str, Any]
    summary: FleetSummary
    systems: List[SystemStatus]
