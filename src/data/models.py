"""Data models for HPC status monitoring."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Dict, Any


@dataclass
class StorageInfo:
    """Storage capacity for a filesystem."""

    mount_point: str  # $HOME, $WORKDIR, /scratch
    filesystem: str  # Physical device/path
    total_gb: float
    used_gb: float
    available_gb: float
    percent_used: float

    @property
    def status(self) -> str:
        """Return status based on usage percentage."""
        if self.percent_used >= 95:
            return "critical"
        elif self.percent_used >= 80:
            return "warning"
        return "healthy"


@dataclass
class QueueInfo:
    """Queue information from a scheduler."""

    name: str
    queue_type: str  # 'batch', 'debug', 'gpu', etc.
    max_walltime: str  # e.g., "24:00:00"
    max_jobs: Optional[str] = None
    min_cores: Optional[int] = None
    max_cores: Optional[int] = None
    jobs_running: int = 0
    jobs_pending: int = 0
    cores_running: int = 0
    cores_pending: int = 0
    enabled: bool = True
    reserved: bool = False


@dataclass
class NodeInfo:
    """Node class information from a cluster."""

    node_type: str  # 'Standard', 'GPU', 'Bigmem', etc.
    nodes_available: int
    cores_per_node: int
    cores_available: int
    cores_running: int
    cores_free: int


@dataclass
class AllocationInfo:
    """Allocation/usage information for a subproject."""

    system: str
    subproject: str
    hours_allocated: int
    hours_used: int
    hours_remaining: int
    percent_remaining: float
    background_hours_used: int = 0


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
class SystemStatus:
    """Status information for an HPC system."""

    system: str
    status: str  # 'UP', 'DOWN', 'DEGRADED', 'MAINTENANCE', 'UNKNOWN'
    dsrc: Optional[str] = None  # DSRC identifier (HPCMP-specific)
    login: Optional[str] = None  # Login node hostname
    scheduler: Optional[str] = None  # 'slurm', 'pbs'
    raw_alt: Optional[str] = None  # Raw alt text from status image
    img_src: Optional[str] = None  # Status image URL
    source_url: Optional[str] = None  # Source URL for the status
    observed_at: Optional[str] = None  # ISO timestamp

    @property
    def slug(self) -> str:
        """Return slugified system name."""
        return "".join(c for c in (self.system or "").lower() if c.isalnum())


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
    """Generated insight about a system or cluster."""

    type: str  # 'recommendation', 'warning', 'info'
    message: str
    priority: int  # 1-5, higher = more important
    related_metric: Optional[str] = None
    system: Optional[str] = None
    cluster: Optional[str] = None


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
