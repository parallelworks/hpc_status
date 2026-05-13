"""Cluster capability detection and Slurm-native data parsers.

Used by pw_cluster.py to collect data from clusters that don't have
HPCMP-style show_usage/show_queues/show_storage scripts. Covers two
families:

1. NOAA RDHPCS clusters with ``saccount_params`` (Hera, Ursa). This
   single command yields home quota + per-project fairshare + per-directory
   disk/file quotas.
2. Plain Slurm clusters (e.g. Gaea C5) where we use ``sshare``, ``sacctmgr``,
   ``scontrol``, ``squeue``, and ``sinfo`` directly.

Each parser produces rows compatible with the existing pw_cluster.py
schema (usage_data.systems / queue_data.queues / queue_data.nodes /
storage_data), so the UI keeps working without changes.
"""

from __future__ import annotations

import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------

# Commands we probe for on each cluster. Each becomes a key in the returned
# dict mapped to True/False.
PROBE_COMMANDS = (
    "show_usage",
    "show_queues",
    "show_storage",
    "saccount_params",
    "sfairshare",
    "sreport",
    "sshare",
    "sinfo",
    "squeue",
    "sacctmgr",
    "scontrol",
    "lfs",
    "quota",
    "nvidia-smi",
)


def build_capability_probe() -> str:
    """Build a single shell snippet that prints '<cmd>=1' or '<cmd>=0' lines.

    Designed to be one ``pw ssh <uri> '<snippet>'`` call so we hit the
    cluster once.
    """
    cmds = " ".join(PROBE_COMMANDS)
    return (
        f"for c in {cmds}; do "
        'if command -v "$c" >/dev/null 2>&1; then echo "$c=1"; '
        'else echo "$c=0"; fi; done'
    )


def parse_capability_probe(output: str) -> Dict[str, bool]:
    """Parse the output of ``build_capability_probe()``."""
    caps: Dict[str, bool] = {c: False for c in PROBE_COMMANDS}
    for line in (output or "").splitlines():
        line = line.strip()
        if "=" not in line:
            continue
        name, val = line.split("=", 1)
        if name in caps:
            caps[name] = val.strip() == "1"
    return caps


# ---------------------------------------------------------------------------
# saccount_params parser (NOAA Hera, Ursa)
# ---------------------------------------------------------------------------

_HOME_RE = re.compile(
    r"Home Quota \(([^)]+)\)\s*Used:\s*(\d+)\s*MB\s*Quota:\s*(\d+)\s*MB"
)
_PROJECT_RE = re.compile(r"^\s*Project:\s*(\S+)\s*$")
_FAIRSHARE_RE = re.compile(r"FairShare=([\d.]+)\s*\((\d+)/(\d+)\)")
_PARTITION_RE = re.compile(r"Partition Access:\s*(.+)$")
_QOSES_RE = re.compile(r"Available QOSes:\s*(.+)$")
_DIR_RE = re.compile(
    r"Directory:\s*(\S+)\s*"
    r"DiskInUse=(\d+)\s*GB,\s*Quota=(\d+)\s*GB,\s*"
    r"Files=(\d+),\s*FileQuota=(\d+)"
)


def parse_saccount_params(
    output: str, cluster_name: str
) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Parse ``saccount_params`` output.

    Returns:
        (usage_systems, storage_dirs, home_quota) where:
          - usage_systems is a list of project rows in the pw_cluster usage
            schema, augmented with NOAA-specific fields (fairshare_score,
            fairshare_rank, partition_access, qoses).
          - storage_dirs maps a short key (e.g. "home", "scratch3_<project>")
            to a storage_data row.
          - home_quota is the parsed home row (or empty dict if not present).
    """
    usage_systems: List[Dict[str, Any]] = []
    storage_dirs: Dict[str, Dict[str, Any]] = {}
    home_quota: Dict[str, Any] = {}

    home_match = _HOME_RE.search(output or "")
    if home_match:
        home_path, used_mb, quota_mb = home_match.groups()
        used_mb_i = int(used_mb)
        quota_mb_i = int(quota_mb)
        home_quota = {
            "filesystem": home_path,
            "size": _format_size_mb(quota_mb_i),
            "used": _format_size_mb(used_mb_i),
            "available": _format_size_mb(max(quota_mb_i - used_mb_i, 0)),
            "percent_used": str(
                round(used_mb_i / quota_mb_i * 100, 1) if quota_mb_i else 0
            ),
        }
        storage_dirs["home"] = home_quota

    current_project: Optional[Dict[str, Any]] = None

    for raw_line in (output or "").splitlines():
        line = raw_line.rstrip()
        # Project header
        proj_match = _PROJECT_RE.match(line)
        if proj_match:
            if current_project:
                usage_systems.append(current_project)
            current_project = {
                "system": cluster_name,
                "subproject": proj_match.group(1),
                "hours_allocated": 0,
                "hours_used": 0,
                "hours_remaining": 0,
                "percent_remaining": 0.0,
                "background_hours_used": 0,
                "fairshare_score": None,
                "fairshare_rank": None,
                "partition_access": None,
                "qoses": [],
            }
            continue

        if current_project is None:
            continue

        # Fairshare line
        fs_match = _FAIRSHARE_RE.search(line)
        if fs_match:
            score, rank, total = fs_match.groups()
            current_project["fairshare_score"] = float(score)
            current_project["fairshare_rank"] = f"{rank}/{total}"

        # Partition access line
        part_match = _PARTITION_RE.search(line)
        if part_match:
            current_project["partition_access"] = part_match.group(1).strip()

        # QOSes line
        qos_match = _QOSES_RE.search(line)
        if qos_match:
            current_project["qoses"] = [
                q.strip() for q in qos_match.group(1).split(",") if q.strip()
            ]

        # Directory storage line
        dir_match = _DIR_RE.search(line)
        if dir_match:
            path, disk_gb, quota_gb, files, file_quota = dir_match.groups()
            disk_gb_i = int(disk_gb)
            quota_gb_i = int(quota_gb)
            key = _storage_key_for_path(path, current_project["subproject"])
            storage_dirs[key] = {
                "filesystem": path,
                "size": f"{quota_gb_i}G" if quota_gb_i else "-",
                "used": f"{disk_gb_i}G",
                "available": f"{max(quota_gb_i - disk_gb_i, 0)}G"
                if quota_gb_i
                else "-",
                "percent_used": str(
                    round(disk_gb_i / quota_gb_i * 100, 1) if quota_gb_i else 0
                ),
                "files_used": int(files),
                "files_quota": int(file_quota),
                "subproject": current_project["subproject"],
            }

    if current_project:
        usage_systems.append(current_project)

    return usage_systems, storage_dirs, home_quota


def _storage_key_for_path(path: str, subproject: str) -> str:
    """Derive a short, dedup-friendly key from a storage path."""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}_{subproject}".lower()
    return path.strip("/").replace("/", "_").lower()


def _format_size_mb(mb: int) -> str:
    """Format a size in MB as a human-readable string (closest unit)."""
    if mb >= 1024 * 1024:
        return f"{mb / 1024 / 1024:.1f}T"
    if mb >= 1024:
        return f"{mb / 1024:.1f}G"
    return f"{mb}M"


# ---------------------------------------------------------------------------
# sfairshare parser (NOAA Hera/Ursa) — adds allocation context to saccount_params
# ---------------------------------------------------------------------------


def parse_sfairshare_csv(output: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``sfairshare -C`` (CSV-with-header) output.

    Returns a dict keyed by Project, e.g. ``{"my-proj": {"fairshare": 0.54, ...}}``.
    Expected columns from sfairshare:
        Project, FairShare, Rank, maxRank, NormShares, EffUsage, RawShares, RawUsage
    """
    rows: Dict[str, Dict[str, Any]] = {}
    lines = [ln for ln in (output or "").splitlines() if ln.strip()]
    if not lines:
        return rows
    header = [h.strip() for h in lines[0].split(",")]
    # Map header → index for resilience to column order changes
    idx = {name: header.index(name) for name in header}

    def _get(cells: List[str], name: str) -> str:
        i = idx.get(name, -1)
        return cells[i].strip() if 0 <= i < len(cells) else ""

    for line in lines[1:]:
        cells = [c.strip() for c in line.split(",")]
        project = _get(cells, "Project")
        if not project:
            continue
        try:
            fairshare = float(_get(cells, "FairShare") or 0)
        except ValueError:
            fairshare = 0.0
        try:
            norm_shares = float(_get(cells, "NormShares") or 0)
        except ValueError:
            norm_shares = 0.0
        try:
            eff_usage = float(_get(cells, "EffUsage") or 0)
        except ValueError:
            eff_usage = 0.0
        try:
            raw_shares = float(_get(cells, "RawShares") or 0)
        except ValueError:
            raw_shares = 0.0
        try:
            raw_usage = float(_get(cells, "RawUsage") or 0)
        except ValueError:
            raw_usage = 0.0
        rank = _get(cells, "Rank")
        max_rank = _get(cells, "maxRank")
        rows[project] = {
            "fairshare": fairshare,
            "rank": rank,
            "max_rank": max_rank,
            "rank_str": f"{rank}/{max_rank}" if rank and max_rank else None,
            "norm_shares": norm_shares,
            "eff_usage": eff_usage,
            "raw_shares": raw_shares,
            "raw_usage_seconds": raw_usage,
            "raw_usage_hours": int(raw_usage / 3600) if raw_usage else 0,
        }
    return rows


# ---------------------------------------------------------------------------
# sreport parser — windowed core-hours per account
# ---------------------------------------------------------------------------


def parse_sreport_account_user(output: str) -> Dict[str, int]:
    """Parse ``sreport cluster AccountUtilizationByUser -P --noheader`` output.

    Format per line: ``cluster|account|user|fullname|used|energy``. The row
    with an empty user is the account-level total. Returns a dict mapping
    account → total core-hours used (within the sreport time window).
    """
    totals: Dict[str, int] = {}
    for line in (output or "").splitlines():
        if not line.strip():
            continue
        cells = line.split("|")
        if len(cells) < 5:
            continue
        account = cells[1].strip()
        user = cells[2].strip()
        if user:
            continue  # per-user breakdown, skip
        try:
            hours = int(float(cells[4]))
        except ValueError:
            hours = 0
        if account:
            totals[account] = hours
    return totals


def fiscal_year_start(today: Optional["datetime.date"] = None) -> str:
    """Return the NOAA fiscal year start date (October 1) as YYYY-MM-DD.

    NOAA FYxx runs Oct 1 (xx-1) → Sep 30 (xx).
    """
    import datetime as _dt

    d = today or _dt.date.today()
    year = d.year if d.month >= 10 else d.year - 1
    return f"{year}-10-01"


# ---------------------------------------------------------------------------
# sshare-based usage parser (fallback for Slurm-only clusters)
# ---------------------------------------------------------------------------


def parse_sshare_usage(
    output: str, cluster_name: str, user: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Parse ``sshare --parsable2 --format=Account,User,RawUsage`` for one user.

    Returns rows in the pw_cluster usage schema, with hours_used populated
    from RawUsage (seconds → hours) and allocation fields left at 0 since
    NOAA-style fairshare clusters don't expose per-project hour caps.
    """
    rows: List[Dict[str, Any]] = []
    for line in (output or "").splitlines():
        if not line.strip() or line.startswith("Account"):
            continue
        # parsable2 separator is '|'
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        account, row_user, raw_usage = parts[0], parts[1], parts[2]
        if not account or account == "root":
            continue
        if user and row_user and row_user != user:
            # Skip rows for other users when we filtered to one user
            continue
        try:
            hours = int(float(raw_usage) / 3600) if raw_usage else 0
        except ValueError:
            hours = 0
        rows.append(
            {
                "system": cluster_name,
                "subproject": account,
                "hours_allocated": 0,
                "hours_used": hours,
                "hours_remaining": 0,
                "percent_remaining": 0.0,
                "background_hours_used": 0,
                "user": row_user or None,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Slurm queue / node parser (replacement for show_queues)
# ---------------------------------------------------------------------------

# Statuses considered "up" for node accounting (mirrors HPCMP show_queues)
_NODE_UP_STATES = ("ALLOC", "MIXED", "IDLE", "COMPLETING", "RESERVED", "PERFCTRS")


def parse_scontrol_partitions(output: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``scontrol -o show partition`` output.

    Returns dict keyed by partition name with fields:
        {
            "max_time": str (HH:MM:SS or "UNLIMITED"),
            "default_time": str,
            "max_nodes": str,
            "allow_qos": List[str],  # ['ALL'] expanded later by caller
            "state": str,
        }
    """
    info: Dict[str, Dict[str, Any]] = {}
    for line in (output or "").splitlines():
        line = line.strip()
        if not line.startswith("PartitionName="):
            continue
        fields = _parse_scontrol_oneline(line)
        name = fields.get("PartitionName", "").strip("*")
        if not name:
            continue
        allow_qos_raw = fields.get("AllowQos", "") or ""
        allow_qos = (
            [q.strip() for q in allow_qos_raw.split(",") if q.strip()]
            if allow_qos_raw and allow_qos_raw.upper() != "ALL"
            else ["ALL"]
        )
        info[name] = {
            "max_time": fields.get("MaxTime", "-"),
            "default_time": fields.get("DefaultTime", "-"),
            "max_nodes": fields.get("MaxNodes", "-"),
            "min_nodes": fields.get("MinNodes", "-"),
            "allow_qos": allow_qos,
            "state": fields.get("State", "-"),
        }
    return info


def parse_slurm_nodes(scontrol_output: str) -> Dict[str, Any]:
    """Parse ``scontrol -o show nodes`` output.

    Returns a dict mapping partition name to:
        {
            "nodes_up":       int,
            "nodes_down":     int,
            "cores_up":       int,
            "cores_down":     int,
            "cores_per_node": int (per-partition average, not cluster modal),
            "gpus_up":        int (parsed from Gres=gpu:<type>:N),
            "gpus_down":      int,
            "gpus_per_node":  int (per-partition average),
            "gpu_types":      List[str] (e.g. ["h100", "gh200"])
        }
    Plus an ``overall`` aggregate row across the whole cluster.

    Heterogeneous partitions (e.g. Ursa u1-gh has 72-core CPUTot vs the
    cluster's 192) are common, so cores_per_node is computed per-partition
    rather than picking the cluster-wide modal.

    Nodes that scontrol reports without a ``Partitions=`` field (login,
    management, drained-only nodes) are skipped — they're not user-
    submittable and only confused the table with an "unknown" bucket.
    """
    by_partition: Dict[str, Dict[str, Any]] = {}
    overall = {
        "nodes_up": 0,
        "nodes_down": 0,
        "cores_up": 0,
        "cores_down": 0,
        "gpus_up": 0,
        "gpus_down": 0,
    }
    # Track each node exactly once for the "true unique capacity" figures.
    # A node belonging to N partitions would otherwise be counted N times
    # if you sum partition rows — bad for fleet utilization math.
    seen_nodes: set = set()

    for line in (scontrol_output or "").splitlines():
        line = line.strip()
        if not line.startswith("NodeName="):
            continue

        fields = _parse_scontrol_oneline(line)
        partitions = fields.get("Partitions") or ""
        if not partitions:
            # Login / management / orphaned nodes — not part of any queue.
            continue
        node_name = fields.get("NodeName", "")
        state = (fields.get("State") or "").upper()

        # CPUTot or Sockets * CoresPerSocket
        cores: int = 0
        try:
            cores = int(fields.get("CPUTot", "0"))
        except ValueError:
            cores = 0
        if not cores:
            try:
                sockets = int(fields.get("Sockets", "1"))
                cps = int(fields.get("CoresPerSocket", "0"))
                cores = sockets * cps
            except ValueError:
                cores = 0

        gres = fields.get("Gres", "") or ""
        node_gpus, node_gpu_types = _parse_gres_gpus(gres)

        is_up = any(s in state for s in _NODE_UP_STATES)

        # Per-partition rows (intentionally double-count nodes shared across
        # partitions, because each partition view is showing what's reachable
        # *via that partition*).
        for part in partitions.split(","):
            part = part.strip("*")
            if not part:
                continue
            row = by_partition.setdefault(
                part,
                {
                    "nodes_up": 0,
                    "nodes_down": 0,
                    "cores_up": 0,
                    "cores_down": 0,
                    "gpus_up": 0,
                    "gpus_down": 0,
                    "gpu_types": set(),
                },
            )
            if is_up:
                row["nodes_up"] += 1
                row["cores_up"] += cores
                row["gpus_up"] += node_gpus
            else:
                row["nodes_down"] += 1
                row["cores_down"] += cores
                row["gpus_down"] += node_gpus
            row["gpu_types"].update(node_gpu_types)

        # Cluster-wide aggregates count each physical node ONCE so the fleet
        # utilization donut isn't inflated when partitions overlap.
        if node_name and node_name not in seen_nodes:
            seen_nodes.add(node_name)
            if is_up:
                overall["nodes_up"] += 1
                overall["cores_up"] += cores
                overall["gpus_up"] += node_gpus
            else:
                overall["nodes_down"] += 1
                overall["cores_down"] += cores
                overall["gpus_down"] += node_gpus

    # Per-partition averages — these now reconcile to nodes × cpn = cores.
    for row in by_partition.values():
        n_up = row["nodes_up"]
        row["cores_per_node"] = row["cores_up"] // n_up if n_up else 0
        row["gpus_per_node"] = row["gpus_up"] // n_up if n_up else 0
        row["gpu_types"] = sorted(row["gpu_types"])

    overall["cores_per_node"] = (
        overall["cores_up"] // overall["nodes_up"] if overall["nodes_up"] else 0
    )

    return {"by_partition": by_partition, "overall": overall}


_GRES_GPU_RE = re.compile(r"gpu:([^:,()]+)(?::(\d+))?")


def _parse_gres_gpus(gres: str) -> Tuple[int, List[str]]:
    """Parse a Slurm ``Gres`` string and return (total_gpus, [gpu_type, ...]).

    Examples:
        ``Gres=gpu:h100:4``       → (4, ["h100"])
        ``Gres=gpu:mi300x:8``     → (8, ["mi300x"])
        ``Gres=gpu:gh200:1``      → (1, ["gh200"])
        ``Gres=(null)``           → (0, [])
        ``Gres=gpu:h100:4,gpu:a100:8`` → (12, ["h100", "a100"])
    """
    if not gres or gres in ("(null)", "null", ""):
        return 0, []
    total = 0
    types: List[str] = []
    for m in _GRES_GPU_RE.finditer(gres):
        gtype = m.group(1)
        count = int(m.group(2)) if m.group(2) else 1
        total += count
        if gtype and gtype != "no_consume" and gtype not in types:
            types.append(gtype)
    return total, types


def _tightest(qos_names: List[str], qos_info: Dict[str, Dict[str, Any]], field: str) -> str:
    """Return the smallest non-empty integer value across ``qos_names`` for ``field``.

    Used to surface the user-facing job/core ceiling on a partition: when
    multiple QoSes are allowed, the operational limit is the smallest one a
    user would hit, not the most generous.
    """
    best: Optional[int] = None
    for q in qos_names:
        raw = (qos_info.get(q) or {}).get(field) or ""
        if not raw or raw in ("-",):
            continue
        try:
            val = int(raw)
        except ValueError:
            continue
        if val <= 0:
            continue
        if best is None or val < best:
            best = val
    return str(best) if best is not None else "-"


def _parse_scontrol_oneline(line: str) -> Dict[str, str]:
    """Parse a single ``scontrol -o`` line into a key/value dict.

    Values can contain '=' (e.g. ``Reason=foo=bar``) but field separators
    are whitespace runs. Simple split-on-whitespace works because ``scontrol -o``
    flattens the multi-line record into one line with space-separated key=value.
    """
    out: Dict[str, str] = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            out[k] = v
    return out


def parse_squeue_jobs(squeue_output: str) -> List[Dict[str, str]]:
    """Parse ``squeue --noheader --format=...`` (pipe-delimited)."""
    rows: List[Dict[str, str]] = []
    for line in (squeue_output or "").splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 9:
            continue
        rows.append(
            {
                "jobid": parts[0].strip(),
                "user": parts[1].strip(),
                "account": parts[2].strip(),
                "partition": parts[3].strip(),
                "qos": parts[4].strip(),
                "state": parts[5].strip(),
                "nodes": parts[6].strip(),
                "cpus": parts[7].strip(),
                "name": parts[8].strip() if len(parts) > 8 else "",
            }
        )
    return rows


def parse_sacctmgr_assoc(assoc_output: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``sacctmgr --parsable2 show association user=$USER`` output.

    Returns dict keyed by account name with fields:
        {
            "user": str,
            "qos": List[str],
            "fairshare": str,
            "max_jobs": str (raw),
            "grp_jobs": str (raw),
        }

    For NOAA RDHPCS the user-row carries the operationally relevant
    MaxJobs/GrpJobs limits — the account row often shows ``parent``.
    """
    rows: Dict[str, Dict[str, Any]] = {}
    lines = [ln for ln in (assoc_output or "").splitlines() if ln.strip()]
    if not lines:
        return rows
    header = [h.strip() for h in lines[0].split("|")]

    def idx(name: str) -> int:
        return header.index(name) if name in header else -1

    i_acct = idx("Account")
    i_user = idx("User")
    i_qos = idx("QOS")
    i_share = idx("Share") if "Share" in header else idx("Fairshare")
    i_max = idx("MaxJobs")
    i_grp = idx("GrpJobs")

    for line in lines[1:]:
        cells = [c.strip() for c in line.split("|")]

        def get(i: int) -> str:
            return cells[i] if 0 <= i < len(cells) else ""

        account = get(i_acct)
        if not account:
            continue
        qos_raw = get(i_qos)
        rows[account] = {
            "user": get(i_user),
            "qos": [q.strip() for q in qos_raw.split(",") if q.strip()] if qos_raw else [],
            "fairshare": get(i_share),
            "max_jobs": get(i_max),
            "grp_jobs": get(i_grp),
        }
    return rows


def parse_sacctmgr_qos(qos_output: str) -> Dict[str, Dict[str, Any]]:
    """Parse ``sacctmgr --parsable2 show qos`` output."""
    info: Dict[str, Dict[str, Any]] = {}
    lines = (qos_output or "").splitlines()
    if not lines:
        return info
    header = [h.strip() for h in lines[0].split("|")]
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split("|")]
        if len(cells) < len(header):
            cells += [""] * (len(header) - len(cells))
        rec = dict(zip(header, cells))
        name = rec.get("Name")
        if not name:
            continue
        info[name] = rec
    return info


def build_slurm_queue_data(
    qos_info: Dict[str, Dict[str, Any]],
    node_info: Dict[str, Any],
    squeue_rows: List[Dict[str, str]],
    partition_info: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Combine sacctmgr/scontrol/squeue results into queue_data schema.

    The pw_cluster.py UI expects ``queue_data = {"queues": [...], "nodes": [...]}``
    where each queue has fields matching the show_queues output. We treat
    Slurm *partitions* as the primary queue dimension because users submit
    to a partition (with an optional QOS), and the node inventory is naturally
    sliced by partition.

    ``partition_info`` (from ``parse_scontrol_partitions``) is the
    authoritative source for partition MaxTime and AllowQos. When given,
    its values override anything derived from sacctmgr QoS records.
    """
    queues: List[Dict[str, Any]] = []
    nodes: List[Dict[str, Any]] = []

    by_part = node_info.get("by_partition", {})
    partition_info = partition_info or {}

    # Aggregate squeue rows by partition
    per_part_running_jobs: Dict[str, int] = {}
    per_part_pending_jobs: Dict[str, int] = {}
    per_part_running_cores: Dict[str, int] = {}
    per_part_pending_cores: Dict[str, int] = {}
    for row in squeue_rows:
        part = row["partition"].strip("*") or "unknown"
        try:
            cores = int(row.get("cpus") or 0)
        except ValueError:
            cores = 0
        if row["state"].upper() == "RUNNING":
            per_part_running_jobs[part] = per_part_running_jobs.get(part, 0) + 1
            per_part_running_cores[part] = per_part_running_cores.get(part, 0) + cores
        elif row["state"].upper() in ("PENDING", "CONFIGURING", "RESV_DEL_HOLD"):
            per_part_pending_jobs[part] = per_part_pending_jobs.get(part, 0) + 1
            per_part_pending_cores[part] = per_part_pending_cores.get(part, 0) + cores

    partitions = sorted(set(
        list(by_part.keys())
        + list(per_part_running_jobs.keys())
        + list(per_part_pending_jobs.keys())
        + list(partition_info.keys())
    ))

    for part in partitions:
        pinfo = partition_info.get(part, {})
        allow_qos = pinfo.get("allow_qos") or []
        # Resolve the partition's effective QoSes. ``ALL`` means every QoS
        # in qos_info is reachable, so use them all to find the most
        # permissive ceilings.
        if allow_qos == ["ALL"]:
            effective_qoses = list(qos_info.keys())
        else:
            effective_qoses = [q for q in allow_qos if q in qos_info]

        # Walltime comes from the partition itself (authoritative); fall
        # back to the most permissive QoS MaxWall if the partition didn't
        # publish one.
        max_wall = pinfo.get("max_time") or "-"
        if max_wall in ("UNLIMITED", "infinite"):
            max_wall = "Unlimited"
        if max_wall == "-":
            for q in effective_qoses:
                qm = (qos_info.get(q) or {}).get("MaxWall")
                if qm:
                    max_wall = qm
                    break

        # Job/core caps come from sacctmgr. Pick the tightest non-empty
        # value across the allowed QoSes so the displayed limit is one a
        # user would actually hit.
        max_jobs = _tightest(effective_qoses, qos_info, "MaxJobs")
        grp_jobs = _tightest(effective_qoses, qos_info, "GrpJobs")
        max_cores = "-"
        for q in effective_qoses:
            tres = (qos_info.get(q) or {}).get("MaxTRES") or ""
            m = re.search(r"node=(\d+)", tres)
            if m:
                cpn = by_part.get(part, {}).get("cores_per_node", 0)
                max_cores = str(int(m.group(1)) * cpn) if cpn else m.group(1)
                break

        # Friendly QoS list — at most 4, then "+N more"
        qos_display = (
            "ALL"
            if allow_qos == ["ALL"]
            else (
                ",".join(allow_qos[:4])
                + (f",+{len(allow_qos) - 4}" if len(allow_qos) > 4 else "")
            )
        )

        queues.append(
            {
                "queue_name": part,
                "max_walltime": max_wall,
                "max_jobs": max_jobs or grp_jobs or "-",
                "max_cores": max_cores,
                "max_cores_per_job": max_cores,
                "jobs_running": str(per_part_running_jobs.get(part, 0)),
                "jobs_pending": str(per_part_pending_jobs.get(part, 0)),
                "cores_running": str(per_part_running_cores.get(part, 0)),
                "cores_pending": str(per_part_pending_cores.get(part, 0)),
                "queue_type": "Exe",
                "allow_qos": qos_display,
                "state": pinfo.get("state", "-"),
            }
        )

    # Build node rows — one per partition (treated as a "node class")
    for part, info in by_part.items():
        gpu_types = info.get("gpu_types") or []
        nodes.append(
            {
                "node_type": part,
                "nodes_available": str(info.get("nodes_up", 0)),
                "cores_per_node": str(info.get("cores_per_node", 0)),
                "cores_available": str(info.get("cores_up", 0)),
                "cores_running": str(
                    per_part_running_cores.get(part, 0)
                ),
                "cores_free": str(
                    max(info.get("cores_up", 0) - per_part_running_cores.get(part, 0), 0)
                ),
                "gpus_per_node": str(info.get("gpus_per_node", 0)),
                "gpus_available": str(info.get("gpus_up", 0)),
                "gpu_types": ",".join(gpu_types) if gpu_types else "",
            }
        )

    # Cluster-wide unique totals (each physical node counted once even when
    # it belongs to multiple partitions). The Queues page uses these for
    # the fleet utilization donut so the math reconciles.
    overall = node_info.get("overall", {})
    total_unique_cores = int(overall.get("cores_up", 0) or 0)
    total_unique_nodes = int(overall.get("nodes_up", 0) or 0)
    total_unique_gpus = int(overall.get("gpus_up", 0) or 0)
    # Running cores aren't double-counted per partition (a job sits in exactly
    # one partition), so summing per_part_running_cores is safe.
    total_running_cores = sum(per_part_running_cores.values())

    return {
        "queues": queues,
        "nodes": nodes,
        "cluster_totals": {
            "cores_total": total_unique_cores,
            "cores_running": total_running_cores,
            "cores_free": max(total_unique_cores - total_running_cores, 0),
            "nodes_total": total_unique_nodes,
            "gpus_total": total_unique_gpus,
        },
    }


# ---------------------------------------------------------------------------
# Filesystem / quota helpers
# ---------------------------------------------------------------------------


def parse_df_blob(output: str) -> Dict[str, Dict[str, str]]:
    """Parse a multi-section ``df -h`` blob produced by our probe script.

    Expects lines like:
        HOME:
        nfs:/home  100G  60G  40G  60% /home
        WORK:
        ...
    """
    storage: Dict[str, Dict[str, str]] = {}
    current_key: Optional[str] = None
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith(":"):
            current_key = line[:-1].lower()
            continue
        if not current_key:
            continue
        parts = line.split()
        if len(parts) >= 5:
            storage[current_key] = {
                "filesystem": parts[0],
                "size": parts[1],
                "used": parts[2],
                "available": parts[3],
                "percent_used": parts[4].rstrip("%"),
            }
    return storage
