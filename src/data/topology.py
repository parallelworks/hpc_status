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
# --- HPCMP DSRCs (DoD High Performance Computing Modernization Program)
HPCMP_SITES: Dict[str, Dict[str, Any]] = {
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
}

# --- NOAA RDHPCS facilities
NOAA_SITES: Dict[str, Dict[str, Any]] = {
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

# --- Cloud regions. Any deployment can run in the cloud, so these apply
# whatever the platform.
#
# Coordinates are the published region locality, not a datacenter
    # address — providers do not publish those, and a region spans several
    # availability zones anyway. They are precise enough to put a pin in
    # the right state and no more, which is what the map is for.
CLOUD_SITES: Dict[str, Dict[str, Any]] = {
    "usgovwest1": {
        "name": "AWS GovCloud (US-West)",
        "organization": "Amazon Web Services · us-gov-west-1",
        "location": "Eastern Oregon (approx.)",
        "lat": 45.84,
        "lon": -119.70,
        "cloud": True,
        "provider": "aws",
    },
    "usgoveast1": {
        "name": "AWS GovCloud (US-East)",
        "organization": "Amazon Web Services · us-gov-east-1",
        "location": "Ohio (approx.)",
        "lat": 39.96,
        "lon": -83.00,
        "cloud": True,
        "provider": "aws",
    },
    "useast1": {
        "name": "AWS US East (N. Virginia)",
        "organization": "Amazon Web Services · us-east-1",
        "location": "Northern Virginia (approx.)",
        "lat": 39.04,
        "lon": -77.49,
        "cloud": True,
        "provider": "aws",
    },
    "useast2": {
        "name": "AWS US East (Ohio)",
        "organization": "Amazon Web Services · us-east-2",
        "location": "Ohio (approx.)",
        "lat": 39.96,
        "lon": -83.00,
        "cloud": True,
        "provider": "aws",
    },
    "uswest1": {
        "name": "AWS US West (N. California)",
        "organization": "Amazon Web Services · us-west-1",
        "location": "Northern California (approx.)",
        "lat": 37.34,
        "lon": -121.89,
        "cloud": True,
        "provider": "aws",
    },
    "uswest2": {
        "name": "AWS US West (Oregon)",
        "organization": "Amazon Web Services · us-west-2",
        "location": "Oregon (approx.)",
        "lat": 45.84,
        "lon": -119.70,
        "cloud": True,
        "provider": "aws",
    },
    # Provider without a known region: still better than "Unassigned".
    "aws": {
        "name": "AWS",
        "organization": "Amazon Web Services · region unknown",
        "cloud": True,
        "provider": "aws",
    },
    "azure": {
        "name": "Azure",
        "organization": "Microsoft Azure",
        "cloud": True,
        "provider": "azure",
    },
    "gcp": {
        "name": "Google Cloud",
        "organization": "Google Cloud",
        "cloud": True,
        "provider": "gcp",
    },
}

# Every facility we know of. Lookup stays platform-agnostic on purpose: if
# a collector reports "erdc", describing it as the ERDC DSRC in Vicksburg
# is right whatever the deployment calls itself. It is *guessing* that has
# to be scoped — see the hint tables below.
SITE_CATALOG: Dict[str, Dict[str, Any]] = {**HPCMP_SITES, **NOAA_SITES, **CLOUD_SITES}

# System-name → site, for deployments whose collector does not report one.
# Matched on a slug substring so renamed clusters (``gaea-c5``) still land
# at the right facility.
#
# These are guesses from a name, so they are scoped to the deployment they
# describe. A generic deployment with a cluster called "janus" is not the
# Army Research Laboratory's, and putting a pin on Aberdeen Proving Ground
# because the names collide is worse than admitting we do not know. Any
# deployment can name its own with ``topology.system_sites``.
HPCMP_SYSTEM_HINTS: Tuple[Tuple[str, str], ...] = (
    # HPCMP systems that are not on the public status page, so nothing else
    # in the pipeline knows where they live.
    ("chessie", "arl"),
    ("janus", "arl"),
    ("crux", "mhpcc"),
)

NOAA_SYSTEM_HINTS: Tuple[Tuple[str, str], ...] = (
    ("hera", "nessc"),
    ("niagara", "nessc"),
    ("gaea", "ornl"),
    ("ppan", "gfdl"),
    ("gfdl", "gfdl"),
)

PLATFORM_SYSTEM_HINTS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "hpcmp": HPCMP_SYSTEM_HINTS,
    "noaa": NOAA_SYSTEM_HINTS,
    "rdhpcs": NOAA_SYSTEM_HINTS,
}

# Every hint, for callers that have no platform to scope by.
SYSTEM_SITE_HINTS: Tuple[Tuple[str, str], ...] = NOAA_SYSTEM_HINTS + HPCMP_SYSTEM_HINTS

# Domain labels → catalog site id. A login node's own name says where it
# lives: crux.mhpcc.hpc.mil is MHPCC whether or not the collector managed
# to label it. Matched against every label of the hostname.
HPCMP_DOMAIN_HINTS: Dict[str, str] = {
    "afrl": "afrl",
    "arl": "arl",
    "erdc": "erdc",
    "navy": "navy",
    "navydsrc": "navy",
    "mhpcc": "mhpcc",
}

NOAA_DOMAIN_HINTS: Dict[str, str] = {
    "gfdl": "gfdl",
    "ncrc": "ornl",
    "ornl": "ornl",
    "nessc": "nessc",
}

# EC2 puts the region straight into the instance's own name:
# ip-10-1-2-3.us-gov-west-1.compute.internal. True on any platform.
CLOUD_DOMAIN_HINTS: Dict[str, str] = {
    "usgovwest1": "usgovwest1",
    "usgoveast1": "usgoveast1",
    "useast1": "useast1",
    "useast2": "useast2",
    "uswest1": "uswest1",
    "uswest2": "uswest2",
}

# Unlike the name hints, these are not scoped by platform. A cluster
# called "janus" on an unrelated deployment is not the Army's, but
# crux.mhpcc.hpc.mil is MHPCC's whoever is watching it — the host is
# stating its own address rather than colliding with someone else's short
# name. A deployment that disagrees can say so with ``topology.sites``.
DOMAIN_SITE_HINTS: Dict[str, str] = {
    **CLOUD_DOMAIN_HINTS,
    **HPCMP_DOMAIN_HINTS,
    **NOAA_DOMAIN_HINTS,
}


def system_hints_for(platform: Any) -> Tuple[Tuple[str, str], ...]:
    """Name hints that apply to a deployment.

    ``None`` means "no platform to scope by" and gets all of them, which
    is what the standalone helpers do. A platform we have no hints for —
    ``generic`` — gets none, and its unplaceable systems land in
    Unassigned rather than somewhere confidently wrong.
    """
    if platform is None:
        return SYSTEM_SITE_HINTS
    return PLATFORM_SYSTEM_HINTS.get(slugify(platform), ())


# us-east-1 is the exception: instances there use a bare ec2.internal /
# compute-1.amazonaws.com suffix with no region in the name at all.
AWS_LEGACY_SUFFIXES: Tuple[Tuple[str, str], ...] = (
    (".ec2.internal", "useast1"),
    (".compute-1.amazonaws.com", "useast1"),
)

# Tokens that name a cloud provider but not a place. Matched per word, so
# the labels collectors actually emit resolve: PW reports a cluster's type
# as "aws-slurm" / "google-slurm" / "azure-slurm", not "aws".
CLOUD_PROVIDER_TOKENS: Dict[str, str] = {
    "aws": "aws",
    "amazon": "aws",
    "ec2": "aws",
    "azure": "azure",
    "gcp": "gcp",
    "google": "gcp",
    "gce": "gcp",
    "oci": "oracle",
    "oracle": "oracle",
}


def cloud_provider_from_label(raw_site: Any) -> Optional[str]:
    """Return the provider a collector's label names, if it names one.

    Whole words only: "aws-slurm" is AWS, "awsome-cluster" is not.
    """
    for token in re.split(r"[^a-z0-9]+", str(raw_site or "").lower()):
        provider = CLOUD_PROVIDER_TOKENS.get(token)
        if provider:
            return provider
    return None

# Site ids that mean "we don't know where this runs" and should not be
# treated as a real facility.
GENERIC_SITE_IDS = frozenset({"", "unknown", "existing", "pw", "none", "null"})

UNASSIGNED_SITE_ID = "unassigned"

# What a deployment calls its facilities. HPCMP's are DSRCs; everyone
# else's are sites.
SITE_LABELS: Dict[str, str] = {"hpcmp": "DSRC"}

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
    managed to scrape, so the hostname is a better signal than nothing —
    and unlike a name hint it holds on any deployment, because the host is
    naming itself rather than colliding with somebody else's short name.
    """
    text = str(hostname or "").strip().lower()
    if not text or "://" in text:
        return None
    for suffix, site in AWS_LEGACY_SUFFIXES:
        if text.endswith(suffix):
            return site
    for label in text.split("."):
        site = DOMAIN_SITE_HINTS.get(re.sub(r"[^a-z0-9]", "", label))
        if site:
            return site
    return None


def resolve_site(
    raw_site: Any,
    system_name: Any = "",
    hostname: Any = "",
    system_sites: Optional[Dict[str, str]] = None,
    cloud_region_default: Optional[str] = None,
    platform: Any = None,
) -> Tuple[str, str]:
    """Map a system to a catalog site id, and say where the answer came from.

    Returns ``(site_id, source)``. The source is carried through to the API
    and the inspector, because "why is this machine there?" was otherwise
    unanswerable without reading this function.

    Precedence, most authoritative first:

    1. ``topology.system_sites`` — an operator saying so outright.
    2. The site the collector reported.
    3. The login hostname's domain (``crux.mhpcc.hpc.mil`` → MHPCC).
    4. A built-in system-name hint.
    5. ``topology.cloud_region_default``, for fleets that run all their
       cloud in one region and whose hostnames do not say so.

    So a machine only lands in "Unassigned" when nothing about it says
    where it runs — or, on a deployment we have no hints for, when the
    only thing that would have placed it was a guess from its name.

    ``platform`` scopes step 4, the one that guesses from a short name.
    ``None`` applies every hint we know, which is what a caller with no
    deployment in hand wants. Step 3 is not scoped: a hostname is the
    machine stating its own address.
    """
    system_slug = slugify(system_name)
    if system_sites:
        explicit = system_sites.get(system_slug)
        if explicit:
            return slugify(explicit) or UNASSIGNED_SITE_ID, "config"

    site_slug = slugify(raw_site)
    provider = cloud_provider_from_label(raw_site)
    if site_slug and site_slug not in GENERIC_SITE_IDS and not provider:
        # "ERDC DSRC" and "erdc" should collapse to the same facility.
        trimmed = site_slug.replace("dsrc", "") or site_slug
        if trimmed in SITE_CATALOG:
            return trimmed, "collector"
        return site_slug, "collector"

    from_host = site_from_hostname(hostname)
    if from_host:
        return from_host, "hostname"
    if provider:
        # The provider is a true answer but a weak one — it names a company,
        # not a place. A deployment that runs all its cloud in one region can
        # say which, rather than watching every instance land on a pin with
        # no coordinates.
        default = slugify(cloud_region_default)
        # Only if the default region belongs to this provider: a Google
        # cluster does not live in an AWS region because the config named
        # one.
        if default and SITE_CATALOG.get(default, {}).get("provider") == provider:
            return default, "cloud-default"
        return provider, "provider"

    for needle, site_id in system_hints_for(platform):
        if needle in system_slug:
            return site_id, "name-hint"
    return UNASSIGNED_SITE_ID, "none"


def resolve_site_id(*args, **kwargs) -> str:
    """Site id only, for callers that do not care where it came from."""
    return resolve_site(*args, **kwargs)[0]


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
        "cloud": bool(base.get("cloud")),
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
    cloud_region_default: Optional[str] = None,
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
        cloud_region_default: region id to assume for cloud clusters whose
            hostname does not name one (e.g. ``usgovwest1``).
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
                cloud_region_default=cloud_region_default,
                platform=platform,
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
                cloud_region_default=cloud_region_default,
                platform=platform,
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
                "cloud": site["cloud"],
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
            "site_label": SITE_LABELS.get(slugify(platform), "Site"),
            # Which hint set placed the unlabelled systems, so "why is this
            # machine there?" stays answerable from the payload alone.
            "site_hints": slugify(platform) if system_hints_for(platform) else "none",
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
    cloud_region_default: Optional[str] = None,
    origin: str = "fleet",
    platform: Any = None,
) -> Dict[str, Any]:
    """Merge a fleet status row with any matching cluster telemetry."""
    meta = (cluster or {}).get("cluster_metadata") or {}
    reported = _normalize_status(row.get("status"))
    cluster_status = _normalize_status(meta.get("status")) if cluster else "UNKNOWN"
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

    connected = cluster is not None and cluster_status != "DOWN"

    # Being logged in and reading queues is direct evidence a system is up —
    # better evidence than a page that does not mention it. So a live
    # session fills in an unknown status, and is the whole story for systems
    # that no status page covers. It never overrides a site that says its
    # own machine is down or in maintenance: the site knows things we
    # cannot see from a login node.
    status = reported
    status_source = "status page" if origin == "fleet" else "control plane"
    if origin == "pw":
        status = "UP" if connected else _normalize_status(meta.get("status"))
        status_source = "live session" if connected else "control plane"
    elif reported == "UNKNOWN" and connected:
        status = "UP"
        status_source = "live session"
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

    site_id, site_source = resolve_site(
        row.get("dsrc"), name, hostname, system_sites, cloud_region_default, platform
    )
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
        "site_source": site_source,
        "status_source": status_source,
        "reported_status": reported,
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
