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
    from .workers import DashboardState
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
    data_store: Optional["DataStore"] = None
    web_dir: Path = Path("web")
    url_prefix: str = ""
    default_theme: str = "dark"
    cluster_pages_enabled: bool = True
    cluster_monitor_interval: int = 120
    config: Optional[Dict] = None

    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory or str(self.web_dir), **kwargs)

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
        self._send_json({
            "deployment": {
                "name": config_data.get("deployment_name", "HPC Status Monitor"),
                "platform": config_data.get("platform", "generic"),
            },
            "ui": {
                "home_page": config_data.get("ui", {}).get("home_page", "overview"),
                "tabs": config_data.get("ui", {}).get("tabs", {}),
                "default_theme": self.default_theme,
            },
            "features": {
                "cluster_pages": self.cluster_pages_enabled,
            },
        })

    def _handle_app_config(self):
        config_data = self.config or {}
        ui_config = config_data.get("ui", {}) if isinstance(config_data, dict) else {}
        title = ui_config.get("title", "HPC Status Monitor") if isinstance(ui_config, dict) else "HPC Status Monitor"
        eyebrow = ui_config.get("eyebrow", "HPC STATUS") if isinstance(ui_config, dict) else "HPC STATUS"
        body = (
            "window.APP_CONFIG=Object.assign({},window.APP_CONFIG||{},"
            + json.dumps({
                "defaultTheme": self.default_theme,
                "clusterPagesEnabled": bool(self.cluster_pages_enabled),
                "clusterMonitorInterval": self.cluster_monitor_interval,
                "title": title,
                "eyebrow": eyebrow,
            })
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
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, last_error or "Status data not ready.")
            return
        summary = self._build_system_summary(payload)
        self._send_json(summary)

    def _handle_cluster_usage(self):
        payload = self._load_cluster_usage_payload()
        if payload is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Cluster usage data unavailable.")
            return
        # Return raw format for frontend compatibility
        # The payload is already a list of cluster objects
        self._send_json(payload)

    def _handle_storage(self):
        """Return storage/filesystem data for all clusters."""
        payload = self._load_cluster_usage_payload()
        if payload is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Storage data unavailable.")
            return
        # The payload already contains storage_data per cluster
        # Return it as-is for the storage page
        self._send_json(payload)

    def _handle_cluster_usage_detail(self, slug_part: str):
        target_slug = self._normalize_cluster_slug(unquote(slug_part or ""))
        if not target_slug:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid cluster identifier.")
            return
        payload = self._load_cluster_usage_payload()
        if payload is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Cluster usage data unavailable.")
            return
        clusters = self._build_cluster_profiles(payload)
        for cluster in clusters:
            if cluster.get("slug") == target_slug:
                self._send_json(cluster)
                return
        self.send_error(HTTPStatus.NOT_FOUND, f"Cluster '{slug_part}' not found in usage data.")

    def _handle_system_markdown(self, slug_part: str):
        raw = unquote(slug_part or "")
        if raw.endswith(".md"):
            raw = raw[:-3]
        normalized = re.sub(r"[^a-z0-9]", "", raw.lower())
        if not normalized:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid system identifier.")
            return

        # Try data store first, then fall back to file system
        if self.data_store:
            content = self.data_store.load_markdown(normalized)
            if content:
                self._send_json({"slug": normalized, "content": content})
                return

        # Fall back to legacy location
        markdown_dir = Path(__file__).parent.parent.parent / "system_markdown"
        if not markdown_dir.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Markdown directory not available.")
            return
        target = (markdown_dir / f"{normalized}.md").resolve()
        try:
            target.relative_to(markdown_dir.resolve())
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid markdown path.")
            return
        if not target.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Markdown not found.")
            return
        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unable to read markdown: {exc}")
            return
        self._send_json({"slug": normalized, "content": content})

    def _handle_collectors_status(self):
        """Return status of all collectors."""
        # This would be connected to CollectorManager in a full implementation
        self._send_json({
            "collectors": {
                "hpcmp": {"available": True, "ready": True},
                "pw_cluster": {"available": True, "ready": True},
            },
        })

    def _handle_insights(self):
        """Generate and return insights based on current data."""
        from ..insights.recommendations import RecommendationEngine

        insights_list = []

        # Get insights from HPCMP fleet status data
        if self.server_state:
            payload, _, _ = self.server_state.snapshot()
            if payload and payload.get("systems"):
                systems_data = payload.get("systems", [])
                engine = RecommendationEngine(systems_data)
                for insight in engine.generate_insights():
                    insights_list.append({
                        "type": insight.type,
                        "message": insight.message,
                        "priority": insight.priority,
                        "metric": insight.related_metric,
                        "cluster": insight.cluster,
                    })

        # Get insights from cluster usage data
        cluster_payload = self._load_cluster_usage_payload()
        if cluster_payload:
            clusters = cluster_payload if isinstance(cluster_payload, list) else cluster_payload.get("clusters", [])
            for cluster in clusters:
                metadata = cluster.get("cluster_metadata", {})
                name = metadata.get("name", "Unknown")
                status = metadata.get("status", "").upper()

                # Check cluster status
                if status not in ("ON", "UP", "RUNNING", "ONLINE"):
                    insights_list.append({
                        "type": "warning",
                        "message": f"{name}: Cluster status is {status}",
                        "priority": 4,
                        "metric": "status",
                        "cluster": name,
                    })

                # Check allocation usage
                usage_data = cluster.get("usage_data", {})
                for system in usage_data.get("systems", []):
                    allocated = self._safe_number(system.get("hours_allocated", 0))
                    remaining = self._safe_number(system.get("hours_remaining", 0))
                    if allocated > 0:
                        percent = (remaining / allocated) * 100
                        if percent < 10:
                            insights_list.append({
                                "type": "warning",
                                "message": f"{name}: Allocation critically low ({percent:.0f}% remaining)",
                                "priority": 5,
                                "metric": "allocation",
                                "cluster": name,
                            })
                        elif percent < 25:
                            insights_list.append({
                                "type": "warning",
                                "message": f"{name}: Allocation running low ({percent:.0f}% remaining)",
                                "priority": 3,
                                "metric": "allocation",
                                "cluster": name,
                            })

                # Check queue depth
                queue_data = cluster.get("queue_data", {})
                for queue in queue_data.get("queues", []):
                    pending = self._safe_number(queue.get("jobs_pending", 0))
                    queue_name = queue.get("queue_name", "Unknown")
                    if pending > 50:
                        insights_list.append({
                            "type": "info",
                            "message": f"{name}/{queue_name}: High queue depth ({int(pending)} pending jobs)",
                            "priority": 2,
                            "metric": "queue_depth",
                            "cluster": name,
                        })

                # Check GPU utilization
                gpu_data = cluster.get("gpu_data", {})
                summary = gpu_data.get("summary", {})
                if summary.get("gpu_count", 0) > 0:
                    util = summary.get("avg_utilization_percent", 0)
                    if util > 90:
                        insights_list.append({
                            "type": "info",
                            "message": f"{name}: High GPU utilization ({util}%)",
                            "priority": 2,
                            "metric": "gpu_utilization",
                            "cluster": name,
                        })
                    elif util < 10:
                        insights_list.append({
                            "type": "info",
                            "message": f"{name}: GPUs are mostly idle ({util}% utilization)",
                            "priority": 1,
                            "metric": "gpu_utilization",
                            "cluster": name,
                        })

        # Sort by priority
        insights_list.sort(key=lambda x: x.get("priority", 0), reverse=True)

        self._send_json({
            "insights": insights_list,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        })

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
        stripped = path[len(norm_prefix):] or "/"
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
            if cached:
                if isinstance(cached, dict):
                    return cached.get("clusters") or cached.get("usage") or cached
                return cached

        # Fall back to legacy file
        legacy_path = Path(__file__).parent.parent.parent / "public" / "data" / "cluster_usage.json"
        if not legacy_path.exists():
            return None
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("clusters") or data.get("usage") or data
            return data
        except Exception as exc:
            print(f"[api] Unable to parse cluster usage data: {exc}")
            return None

    def _build_system_summary(self, payload: Dict) -> Dict:
        systems = []
        for row in payload.get("systems", []):
            systems.append({
                "system": row.get("system"),
                "status": row.get("status"),
                "dsrc": row.get("dsrc"),
                "scheduler": (row.get("scheduler") or "").upper(),
                "login_node": row.get("login"),
                "observed_at": row.get("observed_at"),
                "notes": row.get("raw_alt"),
            })
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

            total_allocated = sum(self._safe_number(s.get("hours_allocated")) for s in systems)
            total_remaining = sum(self._safe_number(s.get("hours_remaining")) for s in systems)
            total_used = sum(self._safe_number(s.get("hours_used")) for s in systems)
            percent_remaining = (total_remaining / total_allocated * 100) if total_allocated else None

            queue_profiles = []
            for queue in queues:
                running_jobs = self._safe_number(queue.get("jobs_running"))
                pending_jobs = self._safe_number(queue.get("jobs_pending"))
                running_cores = self._safe_number(queue.get("cores_running"))
                pending_cores = self._safe_number(queue.get("cores_pending"))
                total_jobs = running_jobs + pending_jobs
                total_cores = running_cores + pending_cores
                utilization = (running_cores / total_cores * 100) if total_cores else None
                queue_profiles.append({
                    "name": queue.get("queue_name"),
                    "type": queue.get("queue_type"),
                    "max_walltime": queue.get("max_walltime"),
                    "jobs": {"running": running_jobs, "pending": pending_jobs},
                    "cores": {"running": running_cores, "pending": pending_cores},
                    "utilization_percent": utilization,
                })

            least_backlogged = None
            if queue_profiles:
                sorted_queues = sorted(
                    queue_profiles,
                    key=lambda q: (q["jobs"]["pending"], q["cores"]["pending"])
                )
                least_backlogged = sorted_queues[0]

            slug = self._normalize_cluster_slug(meta.get("name") or meta.get("uri") or "")
            clusters.append({
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
                    "has_capacity": percent_remaining is None or percent_remaining > 5,
                },
            })
        return clusters

    def _normalize_cluster_slug(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", (text or "").lower())

    @staticmethod
    def _safe_number(value, default=0):
        try:
            return float(str(value).strip().replace(",", ""))
        except Exception:
            return default
