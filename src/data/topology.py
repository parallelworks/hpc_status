"""Fleet topology graph builder.

Turns the two data sources the dashboard already collects — the fleet status
payload (one row per HPC system) and the PW cluster telemetry payload (queues,
nodes, allocations for clusters we can actually log into) — into a single
node/edge graph the topology page can draw.

The graph is three tiers:

    monitor ──▶ site (DSRC / data center) ──▶ system

A "system" node is the merge of what the site *reports* about a machine
(status, scheduler, login node) and what we *observe* by connecting to it
(cores, queues, GPUs, allocation). Either half can be missing: a DSRC system
we have no account on still shows up, and a PW cluster that isn't in the
site's status page gets its own node.

Everything here is a pure function of its inputs so it can be unit tested
without a server, a network, or a database.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Site catalog
# ---------------------------------------------------------------------------

# Physical facilities behind the site identifiers our collectors emit. Used
# for grouping, for the geographic layout, and for the site tooltips. Sites
# not listed here still render — they just have no coordinates and land in
# the "unplaced" tray of the geo layout. Deployments can extend or override
# any entry via ``topology.sites`` in the config file.
SITE_CATALOG: Dict[str, Dict[str, Any]] = {
    # --- HPCMP DSRCs (DoD High Performance Computing Modernization Program)
    "afrl": {
        "name": "AFRL DSRC",
        "organization": "Air Force Research Laboratory",
        "location": "Wright-Patterson AFB, OH",
        "lat": 39.81,
        "lon": -84.05,
    },
    "arl": {
        "name": "ARL DSRC",
        "organization": "Army Research Laboratory",
        "location": "Aberdeen Proving Ground, MD",
        "lat": 39.47,
        "lon": -76.13,
    },
    "erdc": {
        "name": "ERDC DSRC",
        "organization": "Engineer Research and Development Center",
        "location": "Vicksburg, MS",
        "lat": 32.30,
        "lon": -90.87,
    },
    "navy": {
        "name": "Navy DSRC",
        "organization": "Naval Meteorology and Oceanography Command",
        "location": "Stennis Space Center, MS",
        "lat": 30.36,
        "lon": -89.60,
    },
    "mhpcc": {
        "name": "MHPCC DSRC",
        "organization": "Maui High Performance Computing Center",
        "location": "Kihei, HI",
        "lat": 20.75,
        "lon": -156.45,
    },
    # --- NOAA RDHPCS facilities
    "nessc": {
        "name": "NESCC",
        "organization": "NOAA Environmental Security Computing Center",
        "location": "Fairmont, WV",
        "lat": 39.48,
        "lon": -80.14,
    },
    "ornl": {
        "name": "ORNL",
        "organization": "Oak Ridge National Laboratory",
        "location": "Oak Ridge, TN",
        "lat": 35.93,
        "lon": -84.31,
    },
    "gfdl": {
        "name": "GFDL",
        "organization": "Geophysical Fluid Dynamics Laboratory",
        "location": "Princeton, NJ",
        "lat": 40.35,
        "lon": -74.65,
    },
}

# System-name → site for deployments whose collector does not report a site.
# Matched on a slug substring so renamed clusters (``gaea-c5``) still land at
# the right facility.
SYSTEM_SITE_HINTS: Tuple[Tuple[str, str], ...] = (
    # NOAA RDHPCS
    ("hera", "nessc"),
    ("niagara", "nessc"),
    ("gaea", "ornl"),
    ("ppan", "gfdl"),
    ("gfdl", "gfdl"),
    # HPCMP systems that are not on the public status page, so nothing else
    # in the pipeline knows where they live. Override per deployment with
    # ``topology.system_sites``.
    ("chessie", "arl"),
    ("janus", "arl"),
    ("crux", "mhpcc"),
)

# Domain labels → catalog site id. A login node's own name says where it
# lives: crux.mhpcc.hpc.mil is MHPCC whether or not the collector managed to
# label it. Matched against every label of the hostname.
DOMAIN_SITE_HINTS: Dict[str, str] = {
    "afrl": "afrl",
    "arl": "arl",
    "erdc": "erdc",
    "navy": "navy",
    "navydsrc": "navy",
    "mhpcc": "mhpcc",
    "gfdl": "gfdl",
    "ncrc": "ornl",
    "ornl": "ornl",
    "nessc": "nessc",
}

# Site ids that mean "we don't know where this runs" and should not be
# treated as a real facility.
GENERIC_SITE_IDS = frozenset({"", "unknown", "existing", "pw", "none", "null"})

UNASSIGNED_SITE_ID = "unassigned"

UP_STATUSES = frozenset({"UP", "ACTIVE", "ON", "RUNNING", "ONLINE"})
DOWN_STATUSES = frozenset({"DOWN", "OFF", "OFFLINE", "ERROR"})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def slugify(text: Any) -> str:
    """Reduce a name to the dashboard's canonical slug (lowercase alnum)."""
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def _num(value: Any, default: float = 0.0) -> float:
    """Parse a possibly comma-formatted, possibly-None number."""
    if value is None:
        return default
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    return int(_num(value, default))


def _normalize_status(status: Any) -> str:
    text = str(status or "").strip().upper()
    if not text:
        return "UNKNOWN"
    if text in UP_STATUSES:
        return "UP"
    if text in DOWN_STATUSES:
        return "DOWN"
    if text in {"DEGRADED", "MAINTENANCE", "UNKNOWN"}:
        return text
    return "UNKNOWN"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def _parse_iso(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _seconds_since(value: Any, *, now: Optional[datetime] = None) -> Optional[int]:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    reference = now or datetime.utcnow()
    return max(0, int((reference - parsed).total_seconds()))


# ---------------------------------------------------------------------------
# Site resolution
# ---------------------------------------------------------------------------


def site_from_hostname(hostname: Any) -> Optional[str]:
    """Infer a site from a login hostname's domain.

    ``crux.mhpcc.hpc.mil`` is an MHPCC machine no matter what the collector
    managed to scrape, so the hostname is a better signal than nothing.
    """
    text = str(hostname or "").strip().lower()
    if not text or "://" in text:
        return None
    for label in text.split("."):
        site = DOMAIN_SITE_HINTS.get(re.sub(r"[^a-z0-9]", "", label))
        if site:
            return site
    return None


def resolve_site_id(
    raw_site: Any,
    system_name: Any = "",
    hostname: Any = "",
    system_sites: Optional[Dict[str, str]] = None,
) -> str:
    """Map a system to a catalog site id.

    Precedence, most authoritative first:

    1. ``topology.system_sites`` — an operator saying so outright.
    2. The site the collector reported.
    3. The login hostname's domain (``crux.mhpcc.hpc.mil`` → MHPCC).
    4. A built-in system-name hint.

    So a machine only lands in "Unassigned" when nothing about it says
    where it runs.
    """
    system_slug = slugify(system_name)
    if system_sites:
        explicit = system_sites.get(system_slug)
        if explicit:
            return slugify(explicit) or UNASSIGNED_SITE_ID

    site_slug = slugify(raw_site)
    if site_slug and site_slug not in GENERIC_SITE_IDS:
        # "ERDC DSRC" and "erdc" should collapse to the same facility.
        trimmed = site_slug.replace("dsrc", "") or site_slug
        if trimmed in SITE_CATALOG:
            return trimmed
        return site_slug

    from_host = site_from_hostname(hostname)
    if from_host:
        return from_host

    for needle, site_id in SYSTEM_SITE_HINTS:
        if needle in system_slug:
            return site_id
    return UNASSIGNED_SITE_ID


def describe_site(site_id: str, overrides: Optional[Dict[str, Dict]] = None) -> Dict[str, Any]:
    """Return display metadata for a site id, catalog first, overrides last."""
    base = dict(SITE_CATALOG.get(site_id, {}))
    if overrides and site_id in overrides:
        base.update({k: v for k, v in (overrides[site_id] or {}).items() if v is not None})
    if site_id == UNASSIGNED_SITE_ID:
        base.setdefault("name", "Unassigned")
        base.setdefault("organization", "No site reported by the collector")
    base.setdefault("name", site_id.upper())
    base.setdefault("organization", None)
    base.setdefault("location", None)
    return {
        "id": site_id,
        "name": base.get("name"),
        "short": base.get("short") or site_id.upper(),
        "organization": base.get("organization"),
        "location": base.get("location"),
        "lat": base.get("lat"),
        "lon": base.get("lon"),
    }


# ---------------------------------------------------------------------------
# Cluster telemetry indexing
# ---------------------------------------------------------------------------


def _cluster_name(cluster: Dict[str, Any]) -> str:
    meta = cluster.get("cluster_metadata") or {}
    name = meta.get("name")
    if name:
        return str(name)
    uri = str(meta.get("uri") or "")
    return uri.rsplit("/", 1)[-1] if uri else ""


def index_clusters(clusters: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index cluster telemetry by slug for matching against fleet systems."""
    index: Dict[str, Dict[str, Any]] = {}
    for cluster in clusters or []:
        slug = slugify(_cluster_name(cluster))
        if slug:
            index.setdefault(slug, cluster)
    return index


def match_cluster(system_slug: str, index: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Find the telemetry key for a fleet system slug.

    Exact match first, then a containment match in either direction so that
    ``narwhal`` lines up with a PW cluster named ``narwhal-prod`` and a
    cluster named ``gaea`` lines up with a system called ``Gaea C5``.
    """
    if not system_slug:
        return None
    if system_slug in index:
        return system_slug
    # Fuzzy matching only kicks in for names long enough to be distinctive —
    # a 3-letter system name would collide with half the fleet.
    if len(system_slug) < 4:
        return None
    for key in index:
        if len(key) < 4:
            continue
        if key.startswith(system_slug) or system_slug.startswith(key):
            return key
    for key in index:
        if len(key) < 4:
            continue
        if key in system_slug or system_slug in key:
            return key
    return None


def _cluster_capacity(cluster: Dict[str, Any]) -> Dict[str, Any]:
    """Summarize compute capacity for a cluster's telemetry payload."""
    queue_data = cluster.get("queue_data") or {}
    totals = queue_data.get("cluster_totals") or {}
    nodes = queue_data.get("nodes") or []

    if totals:
        cores_total = _int(totals.get("cores_total"))
        cores_running = _int(totals.get("cores_running"))
        nodes_total = _int(totals.get("nodes_total"))
        gpus_total = _int(totals.get("gpus_total"))
    else:
        cores_total = sum(_int(n.get("cores_available")) for n in nodes)
        cores_running = sum(_int(n.get("cores_running")) for n in nodes)
        nodes_total = sum(_int(n.get("nodes_available")) for n in nodes)
        gpus_total = sum(_int(n.get("gpus_available")) for n in nodes)

    cores_free = max(cores_total - cores_running, 0)
    utilization = (cores_running / cores_total * 100) if cores_total else None
    return {
        "cores_total": cores_total,
        "cores_running": cores_running,
        "cores_free": cores_free,
        "nodes_total": nodes_total,
        "gpus_total": gpus_total,
        "utilization_percent": round(utilization, 1) if utilization is not None else None,
    }


def _cluster_queues(cluster: Dict[str, Any]) -> Dict[str, Any]:
    queues = (cluster.get("queue_data") or {}).get("queues") or []
    return {
        "count": len(queues),
        "running_jobs": sum(_int(q.get("jobs_running")) for q in queues),
        "pending_jobs": sum(_int(q.get("jobs_pending")) for q in queues),
        "pending_cores": sum(_int(q.get("cores_pending")) for q in queues),
        "names": [str(q.get("queue_name")) for q in queues if q.get("queue_name")][:12],
    }


def index_insights(
    insights: Optional[List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Group insights by the slug of the system/cluster they are about.

    Insights carry a free-text ``cluster`` (and sometimes ``system``) name;
    slugifying both sides is what lets "Narwhal" the status-page system and
    "narwhal" the PW cluster collect the same warnings.
    """
    index: Dict[str, List[Dict[str, Any]]] = {}
    for insight in insights or []:
        for key in ("cluster", "system"):
            slug = slugify(insight.get(key))
            if not slug:
                continue
            bucket = index.setdefault(slug, [])
            if insight not in bucket:
                bucket.append(insight)
    return index


def _attach_insights(
    slug: str, index: Dict[str, List[Dict[str, Any]]]
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Return a node's insights plus the worst severity among them."""
    found = index.get(slug) or []
    if not found:
        # Fall back to a containment match so a PW cluster named
        # ``narwhal-prod`` still picks up warnings filed against ``narwhal``.
        for key, bucket in index.items():
            if len(key) >= 4 and len(slug) >= 4 and (key in slug or slug in key):
                found = bucket
                break
    if not found:
        return [], None
    ranked = sorted(found, key=lambda i: i.get("priority", 0), reverse=True)
    top = ranked[0].get("priority", 0)
    severity = "critical" if top >= 5 else "warning" if top >= 3 else "info"
    return ranked, severity


def _cluster_allocation(cluster: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    systems = (cluster.get("usage_data") or {}).get("systems") or []
    allocated = sum(_num(s.get("hours_allocated")) for s in systems)
    if not allocated:
        return None
    remaining = sum(_num(s.get("hours_remaining")) for s in systems)
    return {
        "hours_allocated": round(allocated),
        "hours_remaining": round(remaining),
        "percent_remaining": round(remaining / allocated * 100, 1),
        "subprojects": len(systems),
    }


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_topology(
    fleet_payload: Optional[Dict[str, Any]],
    cluster_payload: Optional[List[Dict[str, Any]]] = None,
    *,
    platform: str = "generic",
    monitor_label: str = "Status Monitor",
    connection_stats: Optional[Dict[str, Dict[str, Any]]] = None,
    address_lookup: Optional[Callable[[str], Optional[str]]] = None,
    site_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    system_sites: Optional[Dict[str, str]] = None,
    insights: Optional[List[Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build the topology graph.

    Args:
        fleet_payload: ``/api/status`` shaped payload (``systems``, ``meta``).
        cluster_payload: list of PW cluster telemetry entries.
        platform: deployment platform, used for labelling (``hpcmp`` calls
            its sites DSRCs).
        monitor_label: label for the root node.
        connection_stats: ``DataStore.get_connection_stats()`` output.
        address_lookup: callable resolving a hostname to an IP. Must not
            block — return None when the answer isn't cached yet.
        site_overrides: per-deployment site metadata overrides.
        system_sites: explicit ``system slug -> site id`` assignments, which
            beat everything the collectors infer.
        insights: ``generate_fleet_insights()`` output, attached to the
            nodes each insight is about.
        now: injectable clock for deterministic tests.

    Returns:
        ``{meta, summary, sites, nodes, edges}``
    """
    reference_now = now or datetime.utcnow()
    fleet_payload = fleet_payload or {}
    clusters = list(cluster_payload or [])
    stats = connection_stats or {}

    cluster_index = index_clusters(clusters)
    insight_index = index_insights(insights)
    system_site_map = {
        slugify(key): value for key, value in (system_sites or {}).items() if value
    }
    claimed: set = set()

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    monitor_id = "monitor"
    nodes.append(
        {
            "id": monitor_id,
            "kind": "monitor",
            "label": monitor_label,
            "platform": platform,
            "observed_at": fleet_payload.get("meta", {}).get("generated_at"),
        }
    )

    # --- Systems reported by the fleet collector -------------------------
    for row in fleet_payload.get("systems") or []:
        name = str(row.get("system") or "").strip() or "(unnamed)"
        slug = slugify(name) or f"system{len(nodes)}"
        cluster_key = match_cluster(slug, cluster_index)
        cluster = cluster_index.get(cluster_key) if cluster_key else None
        if cluster_key:
            claimed.add(cluster_key)
        nodes.append(
            _build_system_node(
                name=name,
                slug=slug,
                row=row,
                cluster=cluster,
                stats=stats,
                address_lookup=address_lookup,
                reference_now=reference_now,
                insight_index=insight_index,
                system_sites=system_site_map,
            )
        )

    # --- PW clusters the fleet collector never mentioned -----------------
    for key, cluster in cluster_index.items():
        if key in claimed:
            continue
        meta = cluster.get("cluster_metadata") or {}
        name = _cluster_name(cluster) or key
        row = {
            "system": name,
            "status": meta.get("status"),
            "dsrc": meta.get("type"),
            "login": meta.get("uri"),
            "scheduler": "slurm" if meta.get("has_scheduler") else None,
            "observed_at": meta.get("timestamp"),
            "raw_alt": meta.get("uri"),
        }
        nodes.append(
            _build_system_node(
                name=name,
                slug=key,
                row=row,
                cluster=cluster,
                stats=stats,
                address_lookup=address_lookup,
                reference_now=reference_now,
                insight_index=insight_index,
                system_sites=system_site_map,
                origin="pw",
            )
        )

    # --- Site tier -------------------------------------------------------
    system_nodes = [n for n in nodes if n["kind"] == "system"]
    sites: List[Dict[str, Any]] = []
    for site_id in _ordered_site_ids(system_nodes):
        members = [n for n in system_nodes if n["site"] == site_id]
        site = describe_site(site_id, site_overrides)
        site.update(_site_rollup(members))
        site["node_id"] = f"site:{site_id}"
        sites.append(site)

        nodes.append(
            {
                "id": site["node_id"],
                "kind": "site",
                "label": site["name"],
                "site": site_id,
                "status": site["status"],
                "systems": site["systems"],
                "alerts": site["alerts"],
                "connected": site["connected"],
                "location": site["location"],
                "organization": site["organization"],
                "lat": site["lat"],
                "lon": site["lon"],
                "capacity": site["capacity"],
            }
        )
        edges.append(
            {
                "id": f"{monitor_id}->{site['node_id']}",
                "source": monitor_id,
                "target": site["node_id"],
                "kind": "site",
                "connected": site["connected"] > 0,
            }
        )
        for member in members:
            edges.append(
                {
                    "id": f"{site['node_id']}->{member['id']}",
                    "source": site["node_id"],
                    "target": member["id"],
                    "kind": "member",
                    "connected": bool(member["connected"]),
                    "status": member["status"],
                    "latency_ms": (member.get("connection") or {}).get("latency_ms"),
                }
            )

    for node in nodes:
        if node["kind"] == "system":
            node["site_label"] = next(
                (s["name"] for s in sites if s["id"] == node["site"]), node["site"]
            )

    return {
        "meta": {
            "generated_at": _now_iso(),
            "platform": platform,
            "site_label": "DSRC" if platform == "hpcmp" else "Site",
            "fleet_observed_at": fleet_payload.get("meta", {}).get("generated_at"),
            "fleet_source_url": fleet_payload.get("meta", {}).get("source_url"),
            "telemetry_clusters": len(clusters),
        },
        "summary": _fleet_rollup(system_nodes, sites),
        "sites": sites,
        "nodes": nodes,
        "edges": edges,
    }


def _build_system_node(
    *,
    name: str,
    slug: str,
    row: Dict[str, Any],
    cluster: Optional[Dict[str, Any]],
    stats: Dict[str, Dict[str, Any]],
    address_lookup: Optional[Callable[[str], Optional[str]]],
    reference_now: datetime,
    insight_index: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    system_sites: Optional[Dict[str, str]] = None,
    origin: str = "fleet",
) -> Dict[str, Any]:
    """Merge a fleet status row with any matching cluster telemetry."""
    meta = (cluster or {}).get("cluster_metadata") or {}
    status = _normalize_status(row.get("status"))
    login = row.get("login") or meta.get("uri") or None
    scheduler = (row.get("scheduler") or "").upper() or None

    # A hostname only counts as resolvable if it looks like DNS — PW URIs
    # (``pw://user/cluster``) never do. For those, the collector records the
    # login node's own name, which is the thing that actually says where the
    # cluster lives (``crux.mhpcc.hpc.mil``).
    hostname = login if login and "://" not in str(login) else None
    system_info = (cluster or {}).get("system_info") or {}
    for candidate in (meta.get("hostname"), system_info.get("hostname")):
        if hostname:
            break
        if candidate and str(candidate).strip().lower() not in {"", "unknown"}:
            hostname = str(candidate).strip()
    address = address_lookup(hostname) if (address_lookup and hostname) else None

    system_stats = stats.get(f"system:{slug}") or {}
    cluster_stats = stats.get(f"cluster:{slug}") or {}
    # Prefer the connection history of the link we actually hold (SSH via PW)
    # and fall back to the site-reported status history.
    history = cluster_stats or system_stats

    connected = cluster is not None and _normalize_status(meta.get("status")) != "DOWN"
    connection = {
        "source": "pw" if cluster is not None else "status-page",
        "uri": meta.get("uri"),
        "has_scheduler": meta.get("has_scheduler"),
        "capabilities": sorted(k for k, v in (meta.get("capabilities") or {}).items() if v),
        "last_telemetry_at": meta.get("timestamp"),
        "first_seen": history.get("first_seen"),
        "connected_since": history.get("connected_since"),
        "connected_for_seconds": _seconds_since(
            history.get("connected_since"), now=reference_now
        ),
        "last_change": history.get("last_change"),
        "uptime_ratio": history.get("uptime_ratio"),
        "uptime_window_hours": history.get("uptime_window_hours"),
        "transitions": history.get("transitions"),
        "window_start": history.get("window_start"),
        "window_end": history.get("window_end"),
        "spans": history.get("spans") or [],
        "latency_ms": meta.get("latency_ms"),
    }

    capacity = _cluster_capacity(cluster) if cluster else None
    queues = _cluster_queues(cluster) if cluster else None
    allocation = _cluster_allocation(cluster) if cluster else None

    site_id = resolve_site_id(row.get("dsrc"), name, hostname, system_sites)
    node_insights, alert = _attach_insights(slug, insight_index or {})

    return {
        "id": f"sys:{slug}",
        "kind": "system",
        "label": name,
        "slug": slug,
        "site": site_id,
        "status": status,
        "status_reason": row.get("status_reason"),
        "scheduler": scheduler,
        "login": login,
        "hostname": hostname,
        "address": address,
        "origin": "both" if (origin == "fleet" and cluster is not None) else origin,
        "connected": connected,
        "connection": connection,
        "capacity": capacity,
        "queues": queues,
        "allocation": allocation,
        "load": {
            "cpu_count": _int(system_info.get("cpu_count")) or None,
            "load_1m": _num(system_info.get("load_1m")) or None,
            "memory_total_mb": _int(system_info.get("memory_total_mb")) or None,
            "memory_used_mb": _int(system_info.get("memory_used_mb")) or None,
        }
        if system_info
        else None,
        "observed_at": row.get("observed_at") or meta.get("timestamp"),
        "note": row.get("raw_alt"),
        "source_url": row.get("source_url"),
        "insights": node_insights,
        "alert": alert,
        "links": {
            "queues": f"queues.html?cluster={slug}",
            "quota": f"quota.html?cluster={slug}",
            "storage": f"storage.html?cluster={slug}",
            "detail": f"index.html?system={slug}",
        },
    }


def _ordered_site_ids(system_nodes: List[Dict[str, Any]]) -> List[str]:
    """Site ids in catalog order, with unknown sites appended alphabetically."""
    present = {n["site"] for n in system_nodes}
    ordered = [s for s in SITE_CATALOG if s in present]
    extra = sorted(present - set(ordered) - {UNASSIGNED_SITE_ID})
    tail = [UNASSIGNED_SITE_ID] if UNASSIGNED_SITE_ID in present else []
    return ordered + extra + tail


def _capacity_rollup(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    cores_total = sum(_int((n.get("capacity") or {}).get("cores_total")) for n in nodes)
    cores_running = sum(
        _int((n.get("capacity") or {}).get("cores_running")) for n in nodes
    )
    gpus_total = sum(_int((n.get("capacity") or {}).get("gpus_total")) for n in nodes)
    nodes_total = sum(_int((n.get("capacity") or {}).get("nodes_total")) for n in nodes)
    return {
        "cores_total": cores_total,
        "cores_running": cores_running,
        "cores_free": max(cores_total - cores_running, 0),
        "nodes_total": nodes_total,
        "gpus_total": gpus_total,
        "utilization_percent": (
            round(cores_running / cores_total * 100, 1) if cores_total else None
        ),
    }


def _site_rollup(members: List[Dict[str, Any]]) -> Dict[str, Any]:
    statuses = [m["status"] for m in members]
    if any(s == "DOWN" for s in statuses):
        status = "DOWN" if all(s == "DOWN" for s in statuses) else "DEGRADED"
    elif any(s in {"DEGRADED", "MAINTENANCE"} for s in statuses):
        status = "DEGRADED"
    elif statuses and all(s == "UP" for s in statuses):
        status = "UP"
    else:
        status = "UNKNOWN"
    return {
        "systems": len(members),
        "connected": sum(1 for m in members if m["connected"]),
        "alerts": sum(1 for m in members if m.get("alert") in {"critical", "warning"}),
        "status": status,
        "status_counts": _counts(statuses),
        "scheduler_counts": _counts([m.get("scheduler") or "UNKNOWN" for m in members]),
        "capacity": _capacity_rollup(members),
        "members": [m["slug"] for m in members],
    }


def _fleet_rollup(
    system_nodes: List[Dict[str, Any]], sites: List[Dict[str, Any]]
) -> Dict[str, Any]:
    total = len(system_nodes)
    up = sum(1 for n in system_nodes if n["status"] == "UP")
    queues = sum(_int((n.get("queues") or {}).get("count")) for n in system_nodes)
    return {
        "sites": len(sites),
        "systems": total,
        "connected": sum(1 for n in system_nodes if n["connected"]),
        "alerts": sum(
            1 for n in system_nodes if n.get("alert") in {"critical", "warning"}
        ),
        "up": up,
        "uptime_ratio": round(up / total, 3) if total else 0.0,
        "status_counts": _counts([n["status"] for n in system_nodes]),
        "scheduler_counts": _counts(
            [n.get("scheduler") or "UNKNOWN" for n in system_nodes]
        ),
        "queues": queues,
        "capacity": _capacity_rollup(system_nodes),
    }


def _counts(values: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for value in values:
        key = str(value or "UNKNOWN").upper()
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
