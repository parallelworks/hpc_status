#!/usr/bin/env python3
"""
Example client for the HPC status API endpoints.

Fetches the fleet summary and cluster usage payloads and prints a short snapshot
that could feed downstream job-placement logic.
"""

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def fetch_json(base_url: str, path: str):
    """Fetch a JSON payload and return the parsed object."""
    url = urljoin(base_url, path.lstrip("/"))
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=10) as resp:  # nosec - trusted internal call
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:  # pragma: no cover - demo helper
        raise RuntimeError(f"{url} returned HTTP {exc.code}: {exc.reason}") from exc
    except URLError as exc:  # pragma: no cover
        raise RuntimeError(f"Unable to reach {url}: {exc}") from exc


def summarize_fleet(summary_payload: dict):
    systems = summary_payload.get("systems", [])
    stats = summary_payload.get("fleet_stats", {})
    print("=== Fleet summary ===")
    print(f"Generated at: {summary_payload.get('generated_at')}")
    print(f"Total systems: {stats.get('total_systems')} | Uptime ratio: {stats.get('uptime_ratio')}")
    print("Sample systems:")
    for system in systems[:5]:
        print(
            f" - {system.get('system')} ({system.get('dsrc')}) "
            f"{system.get('status')} via {system.get('scheduler')} "
            f"(login: {system.get('login_node')})"
        )
    print()


def summarize_clusters(usage_payload: dict, top_n: int = 3):
    clusters = usage_payload.get("clusters", [])
    if not clusters:
        print("No cluster usage data has been generated yet.")
        return
    print("=== Cluster capacity snapshot ===")
    ranked = sorted(
        clusters,
        key=lambda c: (c.get("usage", {}).get("percent_remaining") or 0),
        reverse=True,
    )
    for cluster in ranked[:top_n]:
        usage = cluster.get("usage", {})
        percent = usage.get("percent_remaining")
        label = f"{cluster.get('cluster')} ({cluster.get('status')})"
        remaining = usage.get("total_remaining_hours")
        percent_display = f"{percent:.1f}%" if percent is not None else "N/A"
        remaining_display = f"{remaining}" if remaining is not None else "unknown"
        print(f"- {label}: {percent_display} hours available ({remaining_display} remaining)")
        hint = cluster.get("placement_hint", {}).get("least_backlogged_queue") or {}
        if hint:
            pending = hint.get("jobs", {}).get("pending")
            print(
                f"    Suggested queue '{hint.get('name')}' "
                f"(pending jobs: {pending}, type: {hint.get('type')})"
            )
    print()


def main():
    parser = argparse.ArgumentParser(description="Example client for the HPC status API")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8080/",
        help="Root URL where dashboard_server.py is running (default: http://localhost:8080/)",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/") + "/"

    summary = fetch_json(base_url, "/api/fleet/summary")
    usage = fetch_json(base_url, "/api/cluster-usage")
    summarize_fleet(summary)
    summarize_clusters(usage)


if __name__ == "__main__":
    main()
