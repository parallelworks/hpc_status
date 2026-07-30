"""HTTP request handlers for the dashboard API.

Provides API endpoints for status data, refresh triggers, and configuration.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING
from urllib.parse import urlparse, unquote

if TYPE_CHECKING:
    from .alerts import AlertDispatcher
    from .netinfo import HostResolver
    from .workers import ClusterMonitorWorker, DashboardState
    from ..data.persistence import DataStore


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    """HTTP request handler for the dashboard.

    Serves:
    - Static files from web/ directory
    - API endpoints for status data
    - Configuration endpoints
    """

    # These will be set by the server
    server_state: Optional["DashboardState"] = None
    cluster_state: Optional["DashboardState"] = None
    cluster_worker: Optional["ClusterMonitorWorker"] = None
    data_store: Optional["DataStore"] = None
    web_dir: Path = Path("web")
    url_prefix: str = ""
    default_theme: str = "dark"
    cluster_pages_enabled: bool = True
    cluster_monitor_interval: int = 120
    config: Optional[Dict] = None
    host_resolver: Optional["HostResolver"] = None
    uptime_window_hours: int = 24
    wait_estimate_window_hours: int = 6
    alert_dispatcher: Optional["AlertDispatcher"] = None

    def __init__(self, *args, directory=None, **kwargs):
        self._cache_control_sent = False
        super().__init__(*args, directory=directory or str(self.web_dir), **kwargs)

    # --- Cache control ---
    #
    # Static assets are served from stable URLs (assets/css/styles.css), so
    # without an explicit header browsers apply heuristic caching and keep
    # serving yesterday's CSS after a deploy — the page renders unstyled and
    # looks broken. "no-cache" still allows conditional requests, so the
    # common case stays a cheap 304.

    def send_response(self, *args, **kwargs):
        self._cache_control_sent = False
        super().send_response(*args, **kwargs)

    def send_header(self, keyword, value):
        if keyword.lower() == "cache-control":
            self._cache_control_sent = True
        super().send_header(keyword, value)

    def end_headers(self):
        if not self._cache_control_sent:
            self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if self._maybe_redirect_root(parsed):
            return
        stripped = self._strip_prefix(parsed.path)
        if stripped is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Invalid prefix")
            return
        if self._maybe_redirect_directory(stripped, parsed.query):
            return
        self.path = stripped + (f"?{parsed.query}" if parsed.query else "")
        parsed = urlparse(self.path)

        # API routes
        if parsed.path == "/api/status":
            return self._handle_status()
        if parsed.path == "/api/config":
            return self._handle_config()
        if parsed.path == "/app-config.js":
            return self._handle_app_config()
        if parsed.path == "/api/fleet/summary":
            return self._handle_fleet_summary()
        if parsed.path == "/api/cluster-usage":
            return self._handle_cluster_usage()
        if parsed.path.startswith("/api/cluster-usage/"):
            slug_part = parsed.path.split("/api/cluster-usage/", 1)[-1]
            return self._handle_cluster_usage_detail(slug_part)
        if parsed.path.startswith("/api/system-markdown/"):
            slug_part = parsed.path.split("/api/system-markdown/", 1)[-1]
            return self._handle_system_markdown(slug_part)
        if parsed.path == "/api/v2/collectors/status":
            return self._handle_collectors_status()
        if parsed.path == "/api/insights":
            return self._handle_insights()
        if parsed.path == "/api/storage":
            return self._handle_storage()
        if parsed.path == "/api/topology":
            return self._handle_topology()
        if parsed.path == "/api/events":
            return self._handle_events(parsed)
        if parsed.path == "/api/placement":
            return self._handle_placement(parsed)

        # Fall back to static file serving
        return super().do_GET()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        stripped = self._strip_prefix(parsed.path)
        if stripped is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Invalid prefix")
            return
        if self._maybe_redirect_directory(stripped, parsed.query):
            return
        self.path = stripped + (f"?{parsed.query}" if parsed.query else "")
        return super().do_HEAD()

    def do_OPTIONS(self):
        parsed = urlparse(self.path)
        stripped = self._strip_prefix(parsed.path)
        if stripped is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Invalid prefix")
            return
        target = urlparse(stripped)
        if target.path in {"/api/status", "/api/refresh", "/api/config"}:
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            return
        return super().do_OPTIONS()

    def do_POST(self):
        parsed = urlparse(self.path)
        stripped = self._strip_prefix(parsed.path)
        if stripped is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Invalid prefix")
            return
        target = urlparse(stripped)
        if target.path == "/api/refresh":
            return self._handle_refresh()
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    # --- API Handlers ---

    def _handle_status(self):
        state = self.server_state
        if not state:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Server not initialized.")
            return
        payload, last_error, last_refresh_ts = state.snapshot()
        if payload is None:
            status = {
                "error": last_error or "Data not ready yet.",
                "last_refresh_epoch": last_refresh_ts,
            }
            self._send_json(status, status_code=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        self._send_json(payload)

    def _handle_refresh(self):
        state = self.server_state
        if not state:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Server not initialized.")
            return
        ok, detail = state.refresh(blocking=True)
        status = HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json({"ok": ok, "detail": detail}, status_code=status)

    def _handle_config(self):
        """Return current configuration for frontend."""
        config_data = self.config or {}
        deployment_cfg = config_data.get("deployment", {}) or {}
        self._send_json(
            {
                "deployment": {
                    "name": deployment_cfg.get("name", "HPC Status Monitor"),
                    "platform": deployment_cfg.get("platform", "generic"),
                },
                "ui": {
                    "home_page": config_data.get("ui", {}).get("home_page", "overview"),
                    "tabs": config_data.get("ui", {}).get("tabs", {}),
                    "default_theme": self.default_theme,
                },
                "features": {
                    "cluster_pages": self.cluster_pages_enabled,
                },
                "topology": config_data.get("topology", {}),
            }
        )

    def _handle_app_config(self):
        config_data = self.config or {}
        ui_config = config_data.get("ui", {}) if isinstance(config_data, dict) else {}
        title = (
            ui_config.get("title", "HPC Status Monitor")
            if isinstance(ui_config, dict)
            else "HPC Status Monitor"
        )
        eyebrow = (
            ui_config.get("eyebrow", "HPC STATUS")
            if isinstance(ui_config, dict)
            else "HPC STATUS"
        )
        deployment_cfg = (
            config_data.get("deployment", {}) if isinstance(config_data, dict) else {}
        )
        platform = (
            deployment_cfg.get("platform", "generic")
            if isinstance(deployment_cfg, dict)
            else "generic"
        )
        body = (
            "window.APP_CONFIG=Object.assign({},window.APP_CONFIG||{},"
            + json.dumps(
                {
                    "defaultTheme": self.default_theme,
                    "clusterPagesEnabled": bool(self.cluster_pages_enabled),
                    "clusterMonitorInterval": self.cluster_monitor_interval,
                    "title": title,
                    "eyebrow": eyebrow,
                    "platform": platform,
                    "tabs": (
                        ui_config.get("tabs", {}) if isinstance(ui_config, dict) else {}
                    ),
                    "topologyLayout": (
                        config_data.get("topology", {}) or {}
                    ).get("default_layout", "hierarchy"),
                    "uptimeWindowHours": (
                        config_data.get("topology", {}) or {}
                    ).get("uptime_window_hours", 24),
                }
            )
            + ");"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_fleet_summary(self):
        state = self.server_state
        if not state:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Server not initialized.")
            return
        payload, last_error, _ = state.snapshot()
        if payload is None:
            self.send_error(
                HTTPStatus.SERVICE_UNAVAILABLE, last_error or "Status data not ready."
            )
            return
        summary = self._build_system_summary(payload)
        self._send_json(summary)

    def _handle_cluster_usage(self):
        payload = self._load_cluster_usage_payload()
        progress = (
            self.cluster_worker.get_progress() if self.cluster_worker else None
        )
        clusters_list = self._clusters_from_payload(payload)
        self._attach_wait_estimates(clusters_list)

        # No cache at all — return a 200 envelope so the UI can render
        # "collecting from N clusters" instead of treating it as an outage.
        # Exception: if the first sweep already finished and just produced
        # no clusters, send an empty list so the existing "no data" UI runs.
        if not clusters_list:
            if progress and progress.get("first_sweep_complete"):
                self._send_json([])
                return
            self._send_json(
                {
                    "status": "warming_up",
                    "progress": progress,
                    "clusters": [],
                }
            )
            return

        # First sweep still running but some clusters are already done:
        # surface what we have alongside progress so the user can see
        # partial data instead of waiting in the dark.
        if (
            progress
            and not progress.get("first_sweep_complete", True)
            and progress.get("phase") == "warming_up"
        ):
            self._send_json(
                {
                    "status": "partial",
                    "progress": progress,
                    "clusters": clusters_list,
                }
            )
            return

        # Steady state — preserve the existing raw-list response so older
        # consumers (storage page, cluster detail, insights) keep working.
        self._send_json(payload)

    def _attach_wait_estimates(self, clusters: list) -> None:
        """Annotate each queue with an estimated time-to-start, in place.

        Derived from recorded queue depth, so it only appears once there is
        enough history to justify it — the field is simply absent otherwise
        rather than carrying a made-up number.
        """
        if not clusters or not self.data_store:
            return
        try:
            estimates = self.data_store.queue_history.estimate_waits(
                window_hours=self.wait_estimate_window_hours
            )
        except Exception as exc:
            print(f"[api] Unable to compute wait estimates: {exc}", flush=True)
            return
        if not estimates:
            return

        for cluster in clusters:
            meta = cluster.get("cluster_metadata") or {}
            name = meta.get("name") or str(meta.get("uri") or "").rsplit("/", 1)[-1]
            slug = self._normalize_cluster_slug(name)
            for queue in (cluster.get("queue_data") or {}).get("queues") or []:
                estimate = estimates.get((slug, str(queue.get("queue_name") or "")))
                if estimate:
                    queue["wait_estimate"] = estimate

    @staticmethod
    def _clusters_from_payload(payload) -> list:
        if payload is None:
            return []
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            return payload.get("clusters") or payload.get("usage") or []
        return []

    def _handle_storage(self):
        """Return storage/filesystem data for all clusters.

        Shares the same warming/partial envelope as /api/cluster-usage so the
        storage page can show progress during the first sweep instead of a
        red HTTP-503 error.
        """
        payload = self._load_cluster_usage_payload()
        progress = (
            self.cluster_worker.get_progress() if self.cluster_worker else None
        )
        clusters_list = self._clusters_from_payload(payload)

        if not clusters_list:
            if progress and progress.get("first_sweep_complete"):
                self._send_json([])
                return
            self._send_json(
                {
                    "status": "warming_up",
                    "progress": progress,
                    "clusters": [],
                }
            )
            return

        if (
            progress
            and not progress.get("first_sweep_complete", True)
            and progress.get("phase") == "warming_up"
        ):
            self._send_json(
                {
                    "status": "partial",
                    "progress": progress,
                    "clusters": clusters_list,
                }
            )
            return

        self._send_json(payload)

    def _handle_topology(self):
        """Return the fleet topology graph (sites, systems, links).

        Merges the fleet status payload with PW cluster telemetry and the
        recorded connection history. Always answers 200 with whatever is
        available — the graph is still useful with only one source, and the
        page renders a warming-up state from ``meta.ready``.
        """
        from ..data.topology import build_topology
        from ..insights.engine import generate_fleet_insights

        payload = None
        if self.server_state:
            payload, _, _ = self.server_state.snapshot()

        clusters = self._clusters_from_payload(self._load_cluster_usage_payload())

        connection_stats = {}
        if self.data_store:
            try:
                connection_stats = self.data_store.get_connection_stats(
                    window_hours=self.uptime_window_hours
                )
            except Exception as exc:
                print(f"[api] Unable to read connection history: {exc}", flush=True)

        config_data = self.config or {}
        topology_cfg = config_data.get("topology") or {}
        deployment_cfg = config_data.get("deployment") or {}
        graph = build_topology(
            payload,
            clusters,
            platform=(deployment_cfg.get("platform") or "generic"),
            monitor_label=deployment_cfg.get("name") or "Status Monitor",
            connection_stats=connection_stats,
            address_lookup=(
                self.host_resolver.lookup if self.host_resolver else None
            ),
            site_overrides=topology_cfg.get("sites"),
            insights=generate_fleet_insights(payload, clusters),
        )

        progress = self.cluster_worker.get_progress() if self.cluster_worker else None
        graph["meta"]["ready"] = bool(graph.get("nodes"))
        graph["meta"]["collection_progress"] = progress
        self._send_json(graph)

    def _handle_events(self, parsed):
        """Return recent system state changes (newest first).

        This is the audit trail behind alerting: it is populated whether or
        not a webhook is configured, so you can see what changed even
        without an integration.
        """
        from urllib.parse import parse_qs

        limit = 50
        try:
            raw = parse_qs(parsed.query).get("limit", ["50"])[0]
            limit = max(1, min(200, int(raw)))
        except (TypeError, ValueError):
            pass

        events = (
            self.alert_dispatcher.recent_events(limit) if self.alert_dispatcher else []
        )
        self._send_json(
            {
                "events": events,
                "alerting_enabled": bool(
                    self.alert_dispatcher and self.alert_dispatcher.enabled
                ),
                "generated_at": datetime.utcnow().isoformat() + "Z",
            }
        )

    def _handle_placement(self, parsed):
        """Rank (cluster, queue) pairs for a job shape.

        Query params: cores, hours, gpus, queue_type, limit.
        """
        from urllib.parse import parse_qs

        from ..insights.placement import PlacementRequest, rank_placements

        params = parse_qs(parsed.query or "")
        request = PlacementRequest.from_params(params)
        try:
            limit = max(1, min(20, int(params.get("limit", ["5"])[0])))
        except (TypeError, ValueError):
            limit = 5

        clusters = self._clusters_from_payload(self._load_cluster_usage_payload())
        estimates = {}
        if self.data_store:
            try:
                estimates = self.data_store.queue_history.estimate_waits(
                    window_hours=self.wait_estimate_window_hours
                )
            except Exception as exc:
                print(f"[api] Placement wait estimates unavailable: {exc}", flush=True)

        result = rank_placements(
            clusters, request, wait_estimates=estimates, limit=limit
        )
        result["generated_at"] = datetime.utcnow().isoformat() + "Z"
        self._send_json(result)

    def _handle_cluster_usage_detail(self, slug_part: str):
        target_slug = self._normalize_cluster_slug(unquote(slug_part or ""))
        if not target_slug:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid cluster identifier.")
            return
        payload = self._load_cluster_usage_payload()
        if payload is None:
            self.send_error(
                HTTPStatus.SERVICE_UNAVAILABLE, "Cluster usage data unavailable."
            )
            return
        clusters = self._build_cluster_profiles(payload)
        for cluster in clusters:
            if cluster.get("slug") == target_slug:
                self._send_json(cluster)
                return
        self.send_error(
            HTTPStatus.NOT_FOUND, f"Cluster '{slug_part}' not found in usage data."
        )

    def _handle_system_markdown(self, slug_part: str):
        raw = unquote(slug_part or "")
        if raw.endswith(".md"):
            raw = raw[:-3]
        normalized = re.sub(r"[^a-z0-9]", "", raw.lower())
        if not normalized:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid system identifier.")
            return

        # Try the requested slug first. If nothing is stored under it, fall
        # back to NOAA's alias map — a cluster slug containing a known RDHPCS
        # system name (hera/ursa/gaea/ppan/mercury/cloud) resolves to that
        # system's canonical briefing, so renamed PW clusters still light up
        # the detail panel.
        candidates = [normalized]
        try:
            from ..collectors.noaa import resolve_briefing_slug
            aliased = resolve_briefing_slug(normalized)
            if aliased and aliased not in candidates:
                candidates.append(aliased)
        except Exception:
            pass

        if self.data_store:
            for candidate in candidates:
                content = self.data_store.load_markdown(candidate)
                if content:
                    self._send_json({"slug": normalized, "content": content})
                    return

        # Fall back to legacy on-disk markdown (HPCMP fish-name directory).
        markdown_dir = Path(__file__).parent.parent.parent / "system_markdown"
        if not markdown_dir.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Markdown directory not available.")
            return
        for candidate in candidates:
            target = (markdown_dir / f"{candidate}.md").resolve()
            try:
                target.relative_to(markdown_dir.resolve())
            except ValueError:
                continue
            if not target.exists():
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except Exception as exc:
                self.send_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"Unable to read markdown: {exc}",
                )
                return
            self._send_json({"slug": normalized, "content": content})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Markdown not found.")

    def _handle_collectors_status(self):
        """Return status of all collectors."""
        pw_ready = False
        hpcmp_ready = False

        if self.data_store:
            pw_ready = self.data_store.load_cache("cluster_usage") is not None
        if self.server_state:
            hpcmp_ready = self.server_state.is_ready()

        self._send_json(
            {
                "collectors": {
                    "hpcmp": {"available": True, "ready": hpcmp_ready},
                    "pw_cluster": {"available": True, "ready": pw_ready},
                },
            }
        )

    def _handle_insights(self):
        """Generate and return insights based on current data."""
        from ..insights.engine import generate_fleet_insights

        payload = None
        if self.server_state:
            payload, _, _ = self.server_state.snapshot()
        clusters = self._clusters_from_payload(self._load_cluster_usage_payload())

        self._send_json(
            {
                "insights": generate_fleet_insights(payload, clusters),
                "generated_at": datetime.utcnow().isoformat() + "Z",
            }
        )

    # --- Helper Methods ---

    def _send_json(self, data: Any, *, status_code: HTTPStatus = HTTPStatus.OK):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _strip_prefix(self, path: str) -> Optional[str]:
        norm_prefix = (self.url_prefix or "").rstrip("/")
        if not norm_prefix:
            return path or "/"
        if not norm_prefix.startswith("/"):
            norm_prefix = f"/{norm_prefix}"
        if not path.startswith(norm_prefix):
            return None
        stripped = path[len(norm_prefix) :] or "/"
        if not stripped.startswith("/"):
            stripped = "/" + stripped
        return stripped

    def _maybe_redirect_root(self, parsed) -> bool:
        prefix = self.url_prefix
        if not prefix:
            return False
        norm_prefix = prefix.rstrip("/") or "/"
        if not norm_prefix.startswith("/"):
            norm_prefix = f"/{norm_prefix}"
        if parsed.path == norm_prefix and not parsed.path.endswith("/"):
            location = norm_prefix + "/"
            if parsed.query:
                location += f"?{parsed.query}"
            self.send_response(HTTPStatus.MOVED_PERMANENTLY)
            self.send_header("Location", location)
            self.end_headers()
            return True
        return False

    def _build_prefixed_path(self, path: str) -> str:
        norm_prefix = (self.url_prefix or "").rstrip("/")
        if norm_prefix and not norm_prefix.startswith("/"):
            norm_prefix = f"/{norm_prefix}"
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{norm_prefix}{path}" if norm_prefix else path

    def _filesystem_path(self, stripped_path: str) -> Optional[Path]:
        try:
            root = Path(self.directory or self.web_dir).resolve()
        except Exception:
            root = self.web_dir.resolve()
        rel = stripped_path.lstrip("/")
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        return candidate

    def _maybe_redirect_directory(self, stripped_path: str, query: str) -> bool:
        fs_path = self._filesystem_path(stripped_path)
        if not fs_path or not fs_path.is_dir():
            return False
        if stripped_path.endswith("/"):
            return False
        target = stripped_path + "/"
        location = self._build_prefixed_path(target)
        if query:
            location += f"?{query}"
        self.send_response(HTTPStatus.MOVED_PERMANENTLY)
        self.send_header("Location", location)
        self.end_headers()
        return True

    def _load_cluster_usage_payload(self):
        # Try data store first
        if self.data_store:
            cached = self.data_store.load_cache("cluster_usage")
            if cached is not None:
                if isinstance(cached, dict):
                    return cached.get("clusters") or cached.get("usage") or cached
                return cached

        # Fall back to legacy file
        legacy_path = (
            Path(__file__).parent.parent.parent
            / "public"
            / "data"
            / "cluster_usage.json"
        )
        if not legacy_path.exists():
            return None
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("clusters") or data.get("usage") or data
            return data
        except Exception as exc:
            print(f"[api] Unable to parse cluster usage data: {exc}", flush=True)
            return None

    def _build_system_summary(self, payload: Dict) -> Dict:
        systems = []
        for row in payload.get("systems", []):
            systems.append(
                {
                    "system": row.get("system"),
                    "status": row.get("status"),
                    "dsrc": row.get("dsrc"),
                    "scheduler": (row.get("scheduler") or "").upper(),
                    "login_node": row.get("login"),
                    "observed_at": row.get("observed_at"),
                    "notes": row.get("raw_alt"),
                }
            )
        return {
            "generated_at": payload.get("meta", {}).get("generated_at"),
            "fleet_stats": payload.get("summary", {}),
            "systems": systems,
        }

    def _build_cluster_profiles(self, payload) -> list:
        clusters = []
        for entry in payload or []:
            meta = entry.get("cluster_metadata", {}) or {}
            usage = entry.get("usage_data", {}) or {}
            systems = usage.get("systems", []) or []
            queue_section = entry.get("queue_data", {}) or {}
            queues = queue_section.get("queues", []) or []
            nodes = queue_section.get("nodes", []) or []

            total_allocated = sum(
                self._safe_number(s.get("hours_allocated")) for s in systems
            )
            total_remaining = sum(
                self._safe_number(s.get("hours_remaining")) for s in systems
            )
            total_used = sum(self._safe_number(s.get("hours_used")) for s in systems)
            percent_remaining = (
                (total_remaining / total_allocated * 100) if total_allocated else None
            )

            queue_profiles = []
            for queue in queues:
                running_jobs = self._safe_number(queue.get("jobs_running"))
                pending_jobs = self._safe_number(queue.get("jobs_pending"))
                running_cores = self._safe_number(queue.get("cores_running"))
                pending_cores = self._safe_number(queue.get("cores_pending"))
                total_jobs = running_jobs + pending_jobs
                total_cores = running_cores + pending_cores
                utilization = (
                    (running_cores / total_cores * 100) if total_cores else None
                )
                queue_profiles.append(
                    {
                        "name": queue.get("queue_name"),
                        "type": queue.get("queue_type"),
                        "max_walltime": queue.get("max_walltime"),
                        "jobs": {"running": running_jobs, "pending": pending_jobs},
                        "cores": {"running": running_cores, "pending": pending_cores},
                        "utilization_percent": utilization,
                    }
                )

            least_backlogged = None
            if queue_profiles:
                sorted_queues = sorted(
                    queue_profiles,
                    key=lambda q: (q["jobs"]["pending"], q["cores"]["pending"]),
                )
                least_backlogged = sorted_queues[0]

            slug = self._normalize_cluster_slug(
                meta.get("name") or meta.get("uri") or ""
            )
            clusters.append(
                {
                    "cluster": meta.get("name") or meta.get("uri"),
                    "slug": slug,
                    "uri": meta.get("uri"),
                    "status": meta.get("status"),
                    "type": meta.get("type"),
                    "timestamp": meta.get("timestamp"),
                    "usage": {
                        "total_allocated_hours": total_allocated,
                        "total_used_hours": total_used,
                        "total_remaining_hours": total_remaining,
                        "percent_remaining": percent_remaining,
                        "systems": systems,
                    },
                    "queues": queue_profiles,
                    "node_classes": nodes,
                    "placement_hint": {
                        "least_backlogged_queue": least_backlogged,
                        "has_capacity": percent_remaining is None
                        or percent_remaining > 5,
                    },
                }
            )
        return clusters

    def _normalize_cluster_slug(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())

    @staticmethod
    def _safe_number(value, default=0):
        try:
            return float(str(value).strip().replace(",", ""))
        except Exception:
            return default
