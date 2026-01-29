#!/usr/bin/env python3
"""
HPC Status Monitor - Main entry point.

Runs the dashboard server with automatic data refresh and API endpoints.
"""

from __future__ import annotations

import argparse
import functools
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .config import Config
from .routes import DashboardRequestHandler
from .workers import DashboardState, RefreshWorker, ClusterMonitorWorker
from ..data.persistence import DataStore, get_data_dir

# Default paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WEB_DIR = PROJECT_ROOT / "web"
PUBLIC_DIR = PROJECT_ROOT / "public"  # Legacy fallback
CLUSTER_MONITOR_SCRIPT = PROJECT_ROOT / "cluster_monitor.py"

DEFAULT_REFRESH_SECONDS = 180
DEFAULT_CLUSTER_MONITOR_INTERVAL = 120


def create_generate_payload_fn(config: Config, store: DataStore):
    """Create the payload generation function based on config.

    For HPCMP platform: Uses the HPCMP collector to scrape centers.hpc.mil
    For generic/NOAA platforms: Uses PW cluster collector to get available clusters
    """
    platform = config.platform.lower()

    if platform == "hpcmp":
        # Use HPCMP collector for DoD HPC systems
        def generate_hpcmp_payload():
            from ..collectors.hpcmp import HPCMPCollector

            collector_config = config.get_collector_config("hpcmp")
            collector = HPCMPCollector(
                url=collector_config.extra.get("url", "https://centers.hpc.mil/systems/unclassified.html"),
                timeout=collector_config.timeout,
                verify=False,  # Default to insecure for DoD sites
            )

            # Use collect_with_details to get both status and markdown content
            try:
                data, markdown_dict = collector.collect_with_details()

                # Save markdown files for each system
                for slug, content in markdown_dict.items():
                    store.save_markdown(slug, content)

                print(f"[hpcmp] Collected {len(data.get('systems', []))} systems, generated {len(markdown_dict)} briefings")
            except Exception as e:
                # Fall back to basic collect if detailed collection fails
                print(f"[hpcmp] Detailed collection failed, using basic: {e}")
                data = collector.collect()
            finally:
                # Clean up session resources
                collector.close()

            return data

        return generate_hpcmp_payload
    else:
        # Use PW cluster collector for generic/noaa platforms
        def generate_pwcluster_payload():
            from ..collectors.pw_cluster import PWClusterCollector
            import datetime as dt

            collector = PWClusterCollector()

            # Check if PW CLI is available
            if not collector.is_available():
                print("[pw_cluster] PW CLI not available, returning empty status")
                return {
                    "meta": {
                        "source_url": None,
                        "source_name": "PW Clusters",
                        "generated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                        "collector": "pw_cluster",
                    },
                    "summary": {
                        "total_systems": 0,
                        "status_counts": {},
                        "dsrc_counts": {},
                        "scheduler_counts": {},
                        "uptime_ratio": 0,
                    },
                    "systems": [],
                }

            try:
                # Get active clusters from PW CLI
                clusters = collector.get_active_clusters()
                print(f"[pw_cluster] Found {len(clusters)} active clusters")

                # Build systems list from clusters
                systems = []
                now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

                for cluster in clusters:
                    cluster_name = cluster["uri"].split("/")[-1]
                    systems.append({
                        "system": cluster_name,
                        "status": "UP" if cluster["status"] == "on" else "DOWN",
                        "dsrc": cluster.get("type", "pw"),
                        "login": cluster["uri"],
                        "scheduler": "slurm",  # Default assumption
                        "raw_alt": f"PW cluster: {cluster['uri']}",
                        "source_url": None,
                        "observed_at": now_iso,
                    })

                # Calculate summary statistics
                from collections import Counter
                statuses = Counter(s["status"] for s in systems)
                dsrcs = Counter(s["dsrc"] for s in systems)
                scheds = Counter(s["scheduler"] for s in systems)
                uptime_ratio = sum(1 for s in systems if s["status"] == "UP") / len(systems) if systems else 0

                data = {
                    "meta": {
                        "source_url": None,
                        "source_name": "PW Clusters",
                        "generated_at": now_iso,
                        "collector": "pw_cluster",
                    },
                    "summary": {
                        "total_systems": len(systems),
                        "status_counts": dict(statuses),
                        "dsrc_counts": dict(dsrcs),
                        "scheduler_counts": dict(scheds),
                        "uptime_ratio": round(uptime_ratio, 3),
                    },
                    "systems": systems,
                }

                print(f"[pw_cluster] Collected {len(systems)} systems for fleet status")
                return data

            except Exception as e:
                print(f"[pw_cluster] Collection failed: {e}")
                return {
                    "meta": {
                        "source_url": None,
                        "source_name": "PW Clusters",
                        "generated_at": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
                        "collector": "pw_cluster",
                        "error": str(e),
                    },
                    "summary": {
                        "total_systems": 0,
                        "status_counts": {},
                        "dsrc_counts": {},
                        "scheduler_counts": {},
                        "uptime_ratio": 0,
                    },
                    "systems": [],
                }

        return generate_pwcluster_payload


def run_server(args) -> None:
    """Run the dashboard server."""
    # Load configuration
    config = Config.load(args.config)
    print(f"[config] Loaded: ui.title={config.ui.title!r}, platform={config.platform!r}")

    # Override config with CLI args
    if args.host:
        config.server.host = args.host
    if args.port:
        config.server.port = args.port
    if args.url_prefix:
        config.server.url_prefix = args.url_prefix
    if args.default_theme:
        config.ui.default_theme = args.default_theme

    # Initialize data store
    store = DataStore(Path(config.data_dir) if config.data_dir else None)

    # Determine web directory
    web_dir = WEB_DIR if WEB_DIR.exists() else PUBLIC_DIR

    # Create the payload generator
    generate_fn = create_generate_payload_fn(config, store)

    # Initialize dashboard state
    state = DashboardState(store, generate_fn, source_name="fleet_status")

    # Do initial refresh
    print("[dashboard] Loading initial data...")
    if not state.is_ready():
        ok, detail = state.refresh(blocking=True)
        if not ok:
            print(f"[dashboard] Initial refresh: {detail}")

    # Start refresh worker
    worker = RefreshWorker(state, interval_seconds=args.refresh_interval)
    worker.start()

    # Start cluster monitor if enabled
    cluster_worker: Optional[ClusterMonitorWorker] = None
    cluster_pages_enabled = bool(args.cluster_pages)
    cluster_monitor_enabled = bool(args.cluster_monitor) and cluster_pages_enabled
    cluster_monitor_interval = max(60, args.cluster_monitor_interval)

    if cluster_monitor_enabled:
        cluster_worker = ClusterMonitorWorker(
            store=store,
            interval_seconds=cluster_monitor_interval,
            python_executable=sys.executable,
            run_immediately=True,
        )
        cluster_worker.start()

    # Configure the request handler
    DashboardRequestHandler.server_state = state
    DashboardRequestHandler.data_store = store
    DashboardRequestHandler.web_dir = web_dir
    DashboardRequestHandler.url_prefix = config.server.url_prefix
    DashboardRequestHandler.default_theme = config.ui.default_theme
    DashboardRequestHandler.cluster_pages_enabled = cluster_pages_enabled
    DashboardRequestHandler.cluster_monitor_interval = cluster_monitor_interval if cluster_monitor_enabled else 0
    DashboardRequestHandler.config = config.to_dict()

    # Create and run the server
    handler = functools.partial(DashboardRequestHandler, directory=str(web_dir))
    server = ThreadingHTTPServer((config.server.host, config.server.port), handler)

    print(f"[dashboard] Serving on http://{config.server.host}:{config.server.port}")
    if config.server.url_prefix:
        print(f"[dashboard] URL prefix: {config.server.url_prefix}")
    print(f"[dashboard] Data directory: {store.data_dir}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[dashboard] Shutting down...")
    finally:
        worker.stop()
        worker.join(timeout=5)
        if cluster_worker:
            cluster_worker.stop()
            cluster_worker.join(timeout=5)
        server.shutdown()
        server.server_close()


def parse_args():
    parser = argparse.ArgumentParser(
        description="HPC Cross-Site Status Monitor",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Server options
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on")
    parser.add_argument("--config", type=str, help="Path to config YAML file")

    # Refresh options
    parser.add_argument(
        "--refresh-interval",
        type=int,
        default=DEFAULT_REFRESH_SECONDS,
        help="Refresh interval in seconds (min 60)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP timeout for scrapers",
    )

    # TLS options
    parser.add_argument(
        "--url",
        default=None,
        help="Override the upstream status URL",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        default=True,
        help="Skip TLS verification",
    )
    parser.add_argument(
        "--secure",
        dest="insecure",
        action="store_false",
        help="Require TLS verification",
    )
    parser.add_argument(
        "--ca-bundle",
        type=str,
        help="Path to a custom CA bundle",
    )

    # UI options
    parser.add_argument(
        "--url-prefix",
        default="",
        help="Path prefix for reverse proxy setup",
    )
    parser.add_argument(
        "--default-theme",
        choices=("dark", "light"),
        default="dark",
        help="Initial theme for clients",
    )

    # Feature flags
    parser.add_argument(
        "--enable-cluster-pages",
        dest="cluster_pages",
        action="store_true",
        default=True,
        help="Enable quota/queue pages",
    )
    parser.add_argument(
        "--disable-cluster-pages",
        dest="cluster_pages",
        action="store_false",
        help="Disable quota/queue pages",
    )
    parser.add_argument(
        "--enable-cluster-monitor",
        dest="cluster_monitor",
        action="store_true",
        default=True,
        help="Enable cluster monitoring",
    )
    parser.add_argument(
        "--disable-cluster-monitor",
        dest="cluster_monitor",
        action="store_false",
        help="Disable cluster monitoring",
    )
    parser.add_argument(
        "--cluster-monitor-interval",
        type=int,
        default=DEFAULT_CLUSTER_MONITOR_INTERVAL,
        help="Cluster monitor interval in seconds",
    )

    return parser.parse_args()


def main():
    """Entry point for the hpc-status command."""
    args = parse_args()
    run_server(args)


if __name__ == "__main__":
    main()
