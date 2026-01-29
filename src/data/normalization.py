"""Scheduler-agnostic data normalization.

This module provides functions to normalize scheduler-specific data into
a consistent format that can be compared across PBS, Slurm, and other schedulers.

Key normalizations:
1. Status values → Canonical enums (UP, DOWN, DEGRADED, etc.)
2. Time formats → Seconds (integers) for computation, display strings for UI
3. Resource names → Consistent terminology (cores, nodes, gpus)
4. Queue states → Canonical states (ACTIVE, DRAINING, OFFLINE)
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class SchedulerType(str, Enum):
    """Supported scheduler types."""

    PBS = "PBS"
    SLURM = "SLURM"
    LSF = "LSF"
    SGE = "SGE"
    UNKNOWN = "UNKNOWN"


# =============================================================================
# Status Normalization Mappings
# =============================================================================

# PBS node states → normalized states
PBS_NODE_STATE_MAP = {
    "free": "IDLE",
    "job-exclusive": "ALLOCATED",
    "job-busy": "ALLOCATED",
    "busy": "ALLOCATED",
    "state-unknown": "UNKNOWN",
    "offline": "DOWN",
    "down": "DOWN",
    "resv-exclusive": "RESERVED",
    "maintenance": "MAINTENANCE",
    "stale": "DOWN",
}

# Slurm node states → normalized states
SLURM_NODE_STATE_MAP = {
    "idle": "IDLE",
    "alloc": "ALLOCATED",
    "allocated": "ALLOCATED",
    "mix": "MIXED",
    "mixed": "MIXED",
    "down": "DOWN",
    "down*": "DOWN",
    "drain": "DRAINING",
    "draining": "DRAINING",
    "drained": "DRAINING",
    "drng": "DRAINING",
    "maint": "MAINTENANCE",
    "resv": "RESERVED",
    "reserved": "RESERVED",
    "reboot": "MAINTENANCE",
    "fail": "DOWN",
    "failing": "DOWN",
    "future": "OFFLINE",
    "unknown": "UNKNOWN",
    "unk": "UNKNOWN",
    "no_respond": "DOWN",
    "not_responding": "DOWN",
    "powered_off": "OFFLINE",
    "powering_down": "DRAINING",
    "powering_up": "OFFLINE",
}

# PBS queue states → normalized states
PBS_QUEUE_STATE_MAP = {
    "started": "ACTIVE",
    "enabled": "ACTIVE",
    "true": "ACTIVE",
    "stopped": "INACTIVE",
    "disabled": "INACTIVE",
    "false": "INACTIVE",
}

# Slurm partition states → normalized states
SLURM_PARTITION_STATE_MAP = {
    "up": "ACTIVE",
    "up*": "ACTIVE",
    "down": "OFFLINE",
    "down*": "OFFLINE",
    "drain": "DRAINING",
    "inactive": "INACTIVE",
    "inact": "INACTIVE",
}

# PBS job states → normalized states
PBS_JOB_STATE_MAP = {
    "Q": "PENDING",
    "R": "RUNNING",
    "E": "RUNNING",  # Exiting, still running
    "H": "HELD",
    "T": "PENDING",  # Transit
    "W": "PENDING",  # Waiting
    "S": "SUSPENDED",
    "F": "COMPLETED",
    "X": "COMPLETED",
    "C": "COMPLETED",  # Completed (in history)
    "B": "RUNNING",  # Array job running
    "M": "PENDING",  # Moved to another server
}

# Slurm job states → normalized states
SLURM_JOB_STATE_MAP = {
    "PD": "PENDING",
    "PENDING": "PENDING",
    "R": "RUNNING",
    "RUNNING": "RUNNING",
    "CG": "RUNNING",  # Completing
    "COMPLETING": "RUNNING",
    "CD": "COMPLETED",
    "COMPLETED": "COMPLETED",
    "F": "FAILED",
    "FAILED": "FAILED",
    "CA": "CANCELLED",
    "CANCELLED": "CANCELLED",
    "TO": "FAILED",  # Timeout
    "TIMEOUT": "FAILED",
    "NF": "FAILED",  # Node fail
    "NODE_FAIL": "FAILED",
    "S": "SUSPENDED",
    "SUSPENDED": "SUSPENDED",
    "ST": "SUSPENDED",  # Stopped
    "PR": "FAILED",  # Preempted
    "PREEMPTED": "FAILED",
    "BF": "FAILED",  # Boot fail
    "DL": "FAILED",  # Deadline
    "OOM": "FAILED",  # Out of memory
    "RQ": "PENDING",  # Requeued
    "REQUEUED": "PENDING",
    "RS": "PENDING",  # Resizing
    "RV": "PENDING",  # Revoked
}


# =============================================================================
# Normalization Functions
# =============================================================================


def detect_scheduler(hints: Dict[str, Any]) -> SchedulerType:
    """Detect scheduler type from various hints.

    Args:
        hints: Dictionary that may contain scheduler hints like:
            - 'scheduler': explicit scheduler name
            - 'pbs_version', 'pbsadmin': PBS indicators
            - 'slurm_version', 'sinfo': Slurm indicators
            - Queue names like 'debug@server' (PBS)

    Returns:
        Detected SchedulerType
    """
    # Check explicit scheduler field
    scheduler = str(hints.get("scheduler", "")).upper()
    if "PBS" in scheduler or "TORQUE" in scheduler or "OPENPBS" in scheduler:
        return SchedulerType.PBS
    if "SLURM" in scheduler:
        return SchedulerType.SLURM
    if "LSF" in scheduler:
        return SchedulerType.LSF
    if "SGE" in scheduler or "GRID" in scheduler:
        return SchedulerType.SGE

    # Check for PBS indicators
    if any(k in hints for k in ["pbs_version", "pbsadmin", "qmgr"]):
        return SchedulerType.PBS

    # Check for Slurm indicators
    if any(k in hints for k in ["slurm_version", "sinfo", "squeue", "sbatch"]):
        return SchedulerType.SLURM

    # Check queue name patterns (PBS uses @server)
    queues = hints.get("queues", [])
    for q in queues:
        name = q.get("name", "") if isinstance(q, dict) else str(q)
        if "@" in name:
            return SchedulerType.PBS

    return SchedulerType.UNKNOWN


def normalize_node_state(state: str, scheduler: SchedulerType = SchedulerType.UNKNOWN) -> str:
    """Normalize a node state to canonical format.

    Args:
        state: Raw node state string from scheduler
        scheduler: Scheduler type for correct mapping

    Returns:
        Normalized state: IDLE, ALLOCATED, MIXED, DOWN, DRAINING, RESERVED, MAINTENANCE, UNKNOWN
    """
    state_lower = state.lower().strip()

    # Remove common modifiers
    state_lower = re.sub(r"[*+~#!%$]", "", state_lower)

    # Try scheduler-specific mapping first
    if scheduler == SchedulerType.PBS:
        if state_lower in PBS_NODE_STATE_MAP:
            return PBS_NODE_STATE_MAP[state_lower]
    elif scheduler == SchedulerType.SLURM:
        if state_lower in SLURM_NODE_STATE_MAP:
            return SLURM_NODE_STATE_MAP[state_lower]

    # Try both maps as fallback
    if state_lower in PBS_NODE_STATE_MAP:
        return PBS_NODE_STATE_MAP[state_lower]
    if state_lower in SLURM_NODE_STATE_MAP:
        return SLURM_NODE_STATE_MAP[state_lower]

    # Generic fallback
    if "down" in state_lower or "fail" in state_lower or "error" in state_lower:
        return "DOWN"
    if "drain" in state_lower:
        return "DRAINING"
    if "maint" in state_lower:
        return "MAINTENANCE"
    if "idle" in state_lower or "free" in state_lower:
        return "IDLE"
    if "alloc" in state_lower or "busy" in state_lower:
        return "ALLOCATED"

    return "UNKNOWN"


def normalize_queue_state(state: str, scheduler: SchedulerType = SchedulerType.UNKNOWN) -> str:
    """Normalize a queue/partition state to canonical format.

    Args:
        state: Raw queue state string from scheduler
        scheduler: Scheduler type for correct mapping

    Returns:
        Normalized state: ACTIVE, INACTIVE, DRAINING, OFFLINE
    """
    state_lower = state.lower().strip()

    # Remove common modifiers
    state_lower = re.sub(r"[*+~#!%$]", "", state_lower)

    # Try scheduler-specific mapping first
    if scheduler == SchedulerType.PBS:
        if state_lower in PBS_QUEUE_STATE_MAP:
            return PBS_QUEUE_STATE_MAP[state_lower]
    elif scheduler == SchedulerType.SLURM:
        if state_lower in SLURM_PARTITION_STATE_MAP:
            return SLURM_PARTITION_STATE_MAP[state_lower]

    # Try both maps as fallback
    if state_lower in PBS_QUEUE_STATE_MAP:
        return PBS_QUEUE_STATE_MAP[state_lower]
    if state_lower in SLURM_PARTITION_STATE_MAP:
        return SLURM_PARTITION_STATE_MAP[state_lower]

    # Generic fallback
    if state_lower in ("up", "active", "enabled", "started", "running", "true"):
        return "ACTIVE"
    if state_lower in ("down", "offline", "disabled"):
        return "OFFLINE"
    if "drain" in state_lower:
        return "DRAINING"

    return "ACTIVE"  # Default to active if unknown


def normalize_job_state(state: str, scheduler: SchedulerType = SchedulerType.UNKNOWN) -> str:
    """Normalize a job state to canonical format.

    Args:
        state: Raw job state string from scheduler
        scheduler: Scheduler type for correct mapping

    Returns:
        Normalized state: PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, HELD, SUSPENDED
    """
    state_upper = state.upper().strip()

    # Try scheduler-specific mapping first
    if scheduler == SchedulerType.PBS:
        if state_upper in PBS_JOB_STATE_MAP:
            return PBS_JOB_STATE_MAP[state_upper]
    elif scheduler == SchedulerType.SLURM:
        if state_upper in SLURM_JOB_STATE_MAP:
            return SLURM_JOB_STATE_MAP[state_upper]

    # Try both maps
    if state_upper in PBS_JOB_STATE_MAP:
        return PBS_JOB_STATE_MAP[state_upper]
    if state_upper in SLURM_JOB_STATE_MAP:
        return SLURM_JOB_STATE_MAP[state_upper]

    # Generic fallback
    if state_upper in ("Q", "QUEUED", "PENDING", "PD", "W", "WAITING"):
        return "PENDING"
    if state_upper in ("R", "RUNNING", "E", "EXECUTING"):
        return "RUNNING"
    if state_upper in ("C", "COMPLETED", "CD", "F", "FINISHED"):
        return "COMPLETED"
    if state_upper in ("H", "HELD", "HOLD"):
        return "HELD"

    return "PENDING"  # Default to pending if unknown


def parse_walltime(walltime: str) -> Tuple[Optional[int], str]:
    """Parse walltime string to seconds and display format.

    Handles formats:
    - HH:MM:SS (PBS standard)
    - DD:HH:MM:SS (PBS extended)
    - D-HH:MM:SS (Slurm standard)
    - HH:MM (short form)
    - MINUTES (integer minutes - Slurm default)
    - INFINITE or UNLIMITED

    Args:
        walltime: Walltime string in various formats

    Returns:
        Tuple of (seconds or None, display string)
    """
    if not walltime or walltime == "-":
        return None, "unlimited"

    walltime = walltime.strip()

    # Handle special values
    if walltime.upper() in ("INFINITE", "UNLIMITED", "NONE", "N/A"):
        return None, "unlimited"

    # Try to parse as pure integer (minutes)
    try:
        minutes = int(walltime)
        seconds = minutes * 60
        return seconds, _format_duration(seconds)
    except ValueError:
        pass

    # Try Slurm D-HH:MM:SS format
    match = re.match(r"(\d+)-(\d+):(\d+):(\d+)", walltime)
    if match:
        days, hours, mins, secs = map(int, match.groups())
        total_seconds = days * 86400 + hours * 3600 + mins * 60 + secs
        return total_seconds, _format_duration(total_seconds)

    # Try PBS DD:HH:MM:SS format (4 colon-separated parts)
    parts = walltime.split(":")
    if len(parts) == 4:
        try:
            days, hours, mins, secs = map(int, parts)
            total_seconds = days * 86400 + hours * 3600 + mins * 60 + secs
            return total_seconds, _format_duration(total_seconds)
        except ValueError:
            pass

    # Try HH:MM:SS format
    if len(parts) == 3:
        try:
            hours, mins, secs = map(int, parts)
            total_seconds = hours * 3600 + mins * 60 + secs
            return total_seconds, _format_duration(total_seconds)
        except ValueError:
            pass

    # Try HH:MM format
    if len(parts) == 2:
        try:
            hours, mins = map(int, parts)
            total_seconds = hours * 3600 + mins * 60
            return total_seconds, _format_duration(total_seconds)
        except ValueError:
            pass

    return None, walltime  # Return original if unparseable


def _format_duration(seconds: int) -> str:
    """Format seconds as human-readable duration.

    Args:
        seconds: Duration in seconds

    Returns:
        Human-readable string like "24 hours", "7 days", "30 minutes"
    """
    if seconds < 60:
        return f"{seconds} seconds"

    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"

    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"

    days = hours // 24
    remaining_hours = hours % 24
    if remaining_hours == 0:
        return f"{days} day{'s' if days != 1 else ''}"
    return f"{days} day{'s' if days != 1 else ''}, {remaining_hours} hour{'s' if remaining_hours != 1 else ''}"


def normalize_resource_name(name: str) -> str:
    """Normalize resource names to consistent terminology.

    Args:
        name: Raw resource name (ncpus, cpus, processors, cores, etc.)

    Returns:
        Normalized name (cores, nodes, gpus, memory_gb)
    """
    name_lower = name.lower().strip()

    # CPU/core variations
    if name_lower in ("ncpus", "cpus", "cpu", "cores", "core", "procs", "processors", "np", "ppn"):
        return "cores"

    # Node variations
    if name_lower in ("nodes", "node", "nodect", "nnodes"):
        return "nodes"

    # GPU variations
    if name_lower in ("gpus", "gpu", "ngpus", "gres/gpu"):
        return "gpus"

    # Memory variations
    if name_lower in ("mem", "memory", "vmem", "pmem"):
        return "memory_gb"

    return name_lower


def normalize_memory_to_gb(value: str) -> Optional[float]:
    """Convert memory string to gigabytes.

    Handles formats: 128gb, 128g, 131072mb, 134217728kb, 137438953472b

    Args:
        value: Memory value string with optional unit suffix

    Returns:
        Value in gigabytes or None if unparseable
    """
    if not value:
        return None

    value = value.lower().strip()

    # Try to extract number and unit
    match = re.match(r"([\d.]+)\s*([a-z]*)", value)
    if not match:
        return None

    try:
        num = float(match.group(1))
        unit = match.group(2)

        # Convert to GB
        if unit in ("gb", "g"):
            return num
        if unit in ("mb", "m"):
            return num / 1024
        if unit in ("kb", "k"):
            return num / (1024 * 1024)
        if unit in ("b", ""):
            return num / (1024 * 1024 * 1024)
        if unit in ("tb", "t"):
            return num * 1024

        return num  # Assume GB if no unit
    except (ValueError, TypeError):
        return None


def normalize_cluster_data(raw_data: Dict[str, Any], scheduler: SchedulerType = SchedulerType.UNKNOWN) -> Dict[str, Any]:
    """Normalize an entire cluster data structure.

    Args:
        raw_data: Raw cluster data from collector
        scheduler: Detected or specified scheduler type

    Returns:
        Normalized cluster data with consistent field names and values
    """
    # Auto-detect scheduler if not specified
    if scheduler == SchedulerType.UNKNOWN:
        scheduler = detect_scheduler(raw_data)

    normalized = {
        "scheduler": scheduler.value,
        "scheduler_detected": scheduler != SchedulerType.UNKNOWN,
    }

    # Normalize queues
    if "queues" in raw_data:
        normalized["queues"] = []
        for q in raw_data["queues"]:
            nq = _normalize_queue(q, scheduler)
            normalized["queues"].append(nq)

    # Normalize nodes
    if "nodes" in raw_data:
        normalized["nodes"] = []
        for n in raw_data["nodes"]:
            nn = _normalize_node(n, scheduler)
            normalized["nodes"].append(nn)

    # Copy other fields
    for key in raw_data:
        if key not in normalized:
            normalized[key] = raw_data[key]

    return normalized


def _normalize_queue(queue: Dict[str, Any], scheduler: SchedulerType) -> Dict[str, Any]:
    """Normalize a single queue/partition."""
    normalized = dict(queue)

    # Normalize state
    if "state" in queue:
        normalized["state"] = normalize_queue_state(queue["state"], scheduler)
    elif "enabled" in queue:
        normalized["state"] = "ACTIVE" if queue["enabled"] else "INACTIVE"
    else:
        normalized["state"] = "ACTIVE"

    # Normalize walltime
    if "max_walltime" in queue:
        seconds, display = parse_walltime(queue["max_walltime"])
        normalized["max_walltime_seconds"] = seconds
        normalized["max_walltime_display"] = display

    # Normalize queue type
    queue_name = queue.get("name", "").lower()
    if "queue_type" not in normalized:
        if "debug" in queue_name or "test" in queue_name:
            normalized["queue_type"] = "DEBUG"
        elif "gpu" in queue_name:
            normalized["queue_type"] = "GPU"
        elif "bigmem" in queue_name or "himem" in queue_name or "large" in queue_name:
            normalized["queue_type"] = "BIGMEM"
        elif "interactive" in queue_name or "login" in queue_name:
            normalized["queue_type"] = "INTERACTIVE"
        else:
            normalized["queue_type"] = "BATCH"

    return normalized


def _normalize_node(node: Dict[str, Any], scheduler: SchedulerType) -> Dict[str, Any]:
    """Normalize a single node."""
    normalized = dict(node)

    # Normalize state
    if "state" in node:
        normalized["state"] = normalize_node_state(node["state"], scheduler)

    # Normalize resource names
    for old_key in list(normalized.keys()):
        new_key = normalize_resource_name(old_key)
        if new_key != old_key:
            normalized[new_key] = normalized.pop(old_key)

    return normalized
