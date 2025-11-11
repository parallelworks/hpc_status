"""
Shared helpers for generating and storing HPC status dashboard data.

Exposes a `generate_payload` function that fetches the upstream status page and
returns the JSON payload consumed by the web UI, plus utilities to persist the
payload to disk.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

import sys

STORAGE_ROOT = Path(__file__).resolve().parents[1]
if str(STORAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(STORAGE_ROOT))

from hpc_status_scraper import (  # type: ignore  # noqa: E402
    DEFAULT_CA_BUNDLE,
    UNCLASSIFIED_URL,
    fetch_status,
)

Payload = Dict[str, object]


def determine_verify(insecure: bool = True, ca_bundle: Optional[str] = None):
    """Return the `verify` argument for requests based on TLS flags."""
    if insecure:
        return False
    if ca_bundle:
        return ca_bundle
    return DEFAULT_CA_BUNDLE


def _status_summary(rows: List[Dict[str, Optional[str]]]) -> Dict[str, object]:
    statuses = Counter((r.get("status") or "UNKNOWN").upper() for r in rows)
    dsrcs = Counter((r.get("dsrc") or "UNKNOWN").upper() for r in rows)
    scheds = Counter((r.get("scheduler") or "UNKNOWN").upper() for r in rows)

    uptime_ratio = 0.0
    if rows:
        uptime_ratio = sum(1 for r in rows if (r.get("status") or "").upper() == "UP") / len(rows)

    return {
        "total_systems": len(rows),
        "status_counts": dict(statuses),
        "dsrc_counts": dict(dsrcs),
        "scheduler_counts": dict(scheds),
        "uptime_ratio": round(uptime_ratio, 3),
    }


def build_payload(rows: List[Dict[str, Optional[str]]], source_url: str) -> Payload:
    observed_at = rows[0]["observed_at"] if rows else None
    return {
        "meta": {
            "source_url": source_url,
            "generated_at": observed_at,
        },
        "summary": _status_summary(rows),
        "systems": rows,
    }


def generate_payload(
    url: Optional[str] = None,
    timeout: int = 20,
    verify=None,
) -> Payload:
    target_url = url or UNCLASSIFIED_URL
    rows = fetch_status(
        url=target_url,
        timeout=timeout,
        verify=verify if verify is not None else DEFAULT_CA_BUNDLE,
        headers={"User-Agent": "pw-status-dashboard/1.1"},
    )
    return build_payload(rows, source_url=target_url)


def write_payload(payload: Payload, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
