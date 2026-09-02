"""One fleet from three sources.

The home page used to render the status collector's payload and nothing
else, so a machine that was not on the published status page did not
exist as far as the dashboard was concerned — even one the monitor held a
live session to. A user went looking for Coral, could not find it, and
reasonably concluded it was not there.

Three sources know different things, and none of them knows everything:

    status page   which systems the centre publishes, and whether they are up
    live session  which systems this monitor can actually reach, and their load
    marketplace   which systems exist at all, and what each one is for

This merges them into one list, keeps track of which source said what,
and refuses to invent a status for a machine nothing is watching.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .topology import (
    UP_STATUSES,
    _normalize_status,
    describe_site,
    resolve_site,
    slugify,
)

# A machine we only know from a listing has no status, and saying
# "UNKNOWN" implies someone looked. Nothing looked.
UNMONITORED = "NOT MONITORED"

# Where a system came from, in the order a reader should weigh them.
SOURCE_STATUS_PAGE = "status page"
SOURCE_LIVE_SESSION = "live session"
SOURCE_CATALOG = "catalog"

# The control plane's listing: it knows connectedness, nothing else.
SOURCE_CONTROL_PLANE = "control plane"


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _cluster_name(cluster: Dict[str, Any]) -> str:
    meta = cluster.get("cluster_metadata") or {}
    name = meta.get("name") or ""
    if not name:
        uri = str(meta.get("uri") or "")
        name = uri.rsplit("/", 1)[-1]
    return str(name)


def _cluster_capacity(cluster: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Cores and utilization, when the live session reported them.

    Scheduler-driven clusters (every DSRC machine) report their inventory
    through ``queue_data`` — the same place the topology reads it — while
    some cloud boxes only report node-class rows in ``gpu_data``. Reading
    only the latter is how the fleet page showed a healthy sweep with no
    cores on any HPCMP system.
    """
    from .topology import _cluster_capacity as _topology_capacity

    summary = _topology_capacity(cluster)
    if summary.get("cores_total"):
        return {
            "cores_total": summary["cores_total"],
            "cores_running": summary["cores_running"],
            "utilization_percent": summary.get("utilization_percent"),
        }

    classes = cluster.get("gpu_data") or cluster.get("node_classes") or []
    total = running = 0
    for row in classes if isinstance(classes, list) else []:
        try:
            total += int(float(row.get("cores_available") or 0))
            running += int(float(row.get("cores_running") or 0))
        except (TypeError, ValueError):
            continue
    if not total:
        return None
    return {
        "cores_total": total,
        "cores_running": running,
        "utilization_percent": round(min(100.0, running / total * 100), 1),
    }


def _tag_site(tags: List[str], catalog_lookup) -> Optional[str]:
    """A listing's tags often name the facility: coral is tagged 'mhpcc'."""
    for tag in tags or []:
        slug = slugify(tag)
        if slug and catalog_lookup(slug):
            return slug
    return None


def _match_existing(slug: str, systems: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Find the system a slug refers to, allowing a facility suffix.

    The same machine goes by different names in different places: the
    marketplace lists "chessie" and "builder", while the clusters they
    describe are registered as "chessiearl" and "buildermhpcc". Matched
    only on the slug, those became two entries — one connected and one
    reported NOT MONITORED, which is a duplicate and a lie about the
    second.

    The difference is only ever a facility the catalog already knows, so
    that is all this accepts. "coral" and "coralreef" stay separate
    machines, because "reef" is not a site.
    """
    from .topology import SITE_CATALOG

    if slug in systems:
        return slug
    for known in systems:
        longer, shorter = (known, slug) if len(known) > len(slug) else (slug, known)
        if longer.startswith(shorter) and longer[len(shorter):] in SITE_CATALOG:
            return known
    return None


def build_fleet(
    fleet_payload: Optional[Dict[str, Any]],
    clusters: Optional[List[Dict[str, Any]]] = None,
    listings: Optional[List[Dict[str, Any]]] = None,
    connections: Optional[Dict[str, Any]] = None,
    *,
    platform: str = "generic",
    site_overrides: Optional[Dict[str, Dict]] = None,
    system_sites: Optional[Dict[str, str]] = None,
    cloud_region_default: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge the sources into one catalog of systems.

    Args:
        fleet_payload: the status collector's payload (``systems`` list).
        clusters: per-cluster telemetry from the live sessions.
        listings: marketplace compute listings.
        connections: the control plane's current listing
            (``{checked_at, active: [{name, uri}]}``) — fresher than the
            last telemetry sweep, which can be minutes old.
        platform: deployment platform, for site inference and labelling.

    Returns:
        ``{meta, summary, sites, systems}`` where every system carries the
        sources that know it, and ``monitored`` says whether anything is
        actually watching it.
    """
    from .topology import SITE_CATALOG

    systems: Dict[str, Dict[str, Any]] = {}
    # What the status page said, before a live session outranks it for UP.
    # If the session later disappears, this is what the machine falls back
    # to — the page is still vouching for it.
    page_verdicts: Dict[str, str] = {}

    # --- 1. The status page: the authority on published systems.
    for row in (fleet_payload or {}).get("systems") or []:
        name = str(row.get("system") or "").strip()
        slug = slugify(name)
        if not slug:
            continue
        page_verdicts[slug] = _normalize_status(row.get("status"))
        systems[slug] = {
            "slug": slug,
            "name": name,
            "status": page_verdicts[slug],
            "status_source": SOURCE_STATUS_PAGE,
            "login": row.get("login") or None,
            "scheduler": (str(row.get("scheduler") or "").upper() or None),
            "reported_site": row.get("dsrc"),
            "observed_at": row.get("observed_at"),
            "notes": row.get("raw_alt") or None,
            "sources": [SOURCE_STATUS_PAGE],
            "description": None,
            "capacity": None,
            "connected": False,
        }

    # --- 2. Live sessions: the authority on reachability and load.
    for cluster in clusters or []:
        meta = cluster.get("cluster_metadata") or {}
        name = _cluster_name(cluster)
        slug = slugify(name)
        if not slug:
            continue
        slug = _match_existing(slug, systems) or slug
        entry = systems.setdefault(
            slug,
            {
                "slug": slug,
                "name": name,
                "status": "UNKNOWN",
                "status_source": None,
                "login": None,
                "scheduler": None,
                "reported_site": meta.get("type"),
                "observed_at": None,
                "notes": None,
                "sources": [],
                "description": None,
                "capacity": None,
                "connected": False,
            },
        )
        entry["connected"] = str(meta.get("status") or "").lower() in {"active", "on"}
        # The capability probe names the scheduler even when it has no
        # partitions to report, which is the only signal a cloud cluster
        # with nothing scheduled has.
        probed = meta.get("scheduler")
        if probed and probed != "scheduler":
            entry["scheduler"] = entry.get("scheduler") or str(probed).upper()
        entry["uri"] = meta.get("uri")
        entry["login"] = meta.get("hostname") or entry.get("login")
        entry["capacity"] = _cluster_capacity(cluster) or entry.get("capacity")
        entry["observed_at"] = meta.get("timestamp") or entry.get("observed_at")
        if SOURCE_LIVE_SESSION not in entry["sources"]:
            entry["sources"].append(SOURCE_LIVE_SESSION)
        if not entry.get("reported_site"):
            entry["reported_site"] = meta.get("type")
        # A live session is direct evidence, and outranks a status page
        # that has not caught up — but only to say UP, never to say DOWN:
        # losing a session is a fact about the monitor, not the machine.
        if entry["connected"]:
            entry["status"] = "UP"
            entry["status_source"] = SOURCE_LIVE_SESSION

    # --- 2b. The control plane's listing: the freshest word on
    # connectedness. Telemetry sweeps take minutes; the listing is one
    # cheap call and is refreshed between sweeps, so it wins on the
    # connected flag. A machine it names that telemetry has not reached
    # yet still appears — connected, UP, and marked as awaiting its first
    # sweep — because someone who just connected a system is usually
    # watching this page for exactly that.
    if connections:
        listed = set()
        for row in connections.get("active") or []:
            slug = slugify(row.get("name") or str(row.get("uri") or "").rsplit("/", 1)[-1])
            if not slug:
                continue
            key = _match_existing(slug, systems) or slug
            listed.add(key)
            entry = systems.setdefault(
                key,
                {
                    "slug": key,
                    "name": str(row.get("name") or slug),
                    "status": "UNKNOWN",
                    "status_source": None,
                    "login": None,
                    "scheduler": None,
                    "reported_site": None,
                    "observed_at": None,
                    "notes": None,
                    "sources": [],
                    "description": None,
                    "capacity": None,
                    "connected": False,
                },
            )
            if not entry["connected"] and entry["capacity"] is None:
                entry["awaiting_telemetry"] = True
            entry["connected"] = True
            entry["status"] = "UP"
            entry["status_source"] = SOURCE_LIVE_SESSION
            entry["observed_at"] = connections.get("checked_at") or entry.get("observed_at")
            if SOURCE_LIVE_SESSION not in entry["sources"]:
                entry["sources"].append(SOURCE_LIVE_SESSION)
        # A cluster the cache still calls connected but the fresher
        # listing does not name has disconnected. That is a fact about
        # the session, not the machine: the connected flag drops, and the
        # status falls back only when the session was its sole witness.
        for entry in systems.values():
            if entry["connected"] and entry["slug"] not in listed:
                entry["connected"] = False
                if entry["status_source"] == SOURCE_LIVE_SESSION:
                    if entry["slug"] in page_verdicts:
                        # The page still vouches for it; the session was
                        # just a fresher second witness.
                        entry["status"] = page_verdicts[entry["slug"]]
                        entry["status_source"] = SOURCE_STATUS_PAGE
                    else:
                        entry["status"] = "UNKNOWN"
                        entry["status_source"] = SOURCE_CONTROL_PLANE

    # --- 3. The catalog: what exists, and what it is for.
    for listing in listings or []:
        slug = listing.get("slug")
        if not slug:
            continue
        # "existing" means a machine that is already standing. The other
        # subtypes (aws-slurm, google-slurm, ...) are recipes for creating
        # a cluster, not clusters — listing "GCP HPC" as a system would be
        # inventing a machine nobody has. They can still describe a system
        # we know about from somewhere else, which is how a cloud cluster
        # the monitor is connected to gets its description.
        matched = _match_existing(slug, systems)
        if matched is None and str(listing.get("subtype") or "") != "existing":
            continue
        slug = matched or slug
        entry = systems.setdefault(
            slug,
            {
                "slug": slug,
                "name": listing.get("name") or slug,
                "status": UNMONITORED,
                "status_source": None,
                "login": None,
                "scheduler": None,
                "reported_site": None,
                "observed_at": None,
                "notes": None,
                "sources": [],
                "description": None,
                "capacity": None,
                "connected": False,
            },
        )
        entry["description"] = entry.get("description") or listing.get("description")
        # A live session knows a cluster as "coral"; the listing calls it
        # "Coral". Prefer the curated label when all we had was the slug.
        if listing.get("name") and slugify(entry["name"]) == entry["slug"]:
            entry["name"] = listing["name"]
        entry["tags"] = listing.get("tags") or []
        entry["scheduler"] = entry.get("scheduler") or listing.get("scheduler")
        if SOURCE_CATALOG not in entry["sources"]:
            entry["sources"].append(SOURCE_CATALOG)

    # --- Place every system, and describe where it landed.
    site_ids: List[str] = []
    for entry in systems.values():
        site_id, site_source = resolve_site(
            entry.get("reported_site"),
            entry["slug"],
            entry.get("login") or "",
            system_sites,
            cloud_region_default,
            platform,
        )
        if site_id == "unassigned" and entry.get("tags"):
            # A listing tagged "mhpcc" is naming its facility.
            tagged = _tag_site(entry["tags"], lambda s: SITE_CATALOG.get(s))
            if tagged:
                site_id, site_source = tagged, "listing-tag"
        entry["site"] = site_id
        entry["site_source"] = site_source
        if site_id not in site_ids:
            site_ids.append(site_id)

        monitored = entry["status_source"] is not None
        entry["monitored"] = monitored
        if not monitored:
            entry["status"] = UNMONITORED
        entry.pop("reported_site", None)

    sites = [describe_site(site_id, site_overrides) for site_id in site_ids]
    ordered = sorted(
        systems.values(), key=lambda item: (not item["monitored"], item["name"].lower())
    )
    for site in sites:
        site["systems"] = sum(1 for s in ordered if s["site"] == site["id"])

    return {
        "meta": {
            "generated_at": _now_iso(),
            "platform": platform,
            "site_label": "DSRC" if slugify(platform) == "hpcmp" else "Site",
            "fleet_observed_at": (fleet_payload or {}).get("meta", {}).get("generated_at"),
            "sources": _sources_present(ordered),
        },
        "summary": _summary(ordered),
        "sites": sorted(sites, key=lambda s: (s["id"] == "unassigned", s["name"])),
        "systems": ordered,
    }


def _sources_present(systems: List[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    for source in (SOURCE_STATUS_PAGE, SOURCE_LIVE_SESSION, SOURCE_CATALOG):
        if any(source in system["sources"] for system in systems):
            seen.append(source)
    return seen


def _summary(systems: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fleet counts.

    Uptime is over monitored systems only. A machine nothing is watching
    is not evidence of health either way, and letting it drag the
    percentage down would make the number mean something different from
    one deployment to the next.
    """
    monitored = [s for s in systems if s["monitored"]]
    up = [s for s in monitored if s["status"] in UP_STATUSES]
    status_counts: Dict[str, int] = {}
    for system in systems:
        status_counts[system["status"]] = status_counts.get(system["status"], 0) + 1

    return {
        "total_systems": len(systems),
        "monitored": len(monitored),
        "catalog_only": len(systems) - len(monitored),
        "connected": sum(1 for s in systems if s.get("connected")),
        "up": len(up),
        "not_up": len(monitored) - len(up),
        "uptime_ratio": round(len(up) / len(monitored), 4) if monitored else None,
        "status_counts": status_counts,
        "site_counts": _count_by(systems, "site"),
        "scheduler_counts": _count_by(systems, "scheduler"),
    }


def _count_by(systems: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for system in systems:
        value = system.get(key)
        if not value:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
