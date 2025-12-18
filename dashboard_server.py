#!/usr/bin/env python3
"""
Run the HPC status dashboard with automatic refreshes and a lightweight API.

Features:
- Refreshes the upstream status feed every N minutes (default: 3).
- Serves the static dashboard from `public/`.
- Provides `/api/status` for the latest payload and `/api/refresh` to trigger an
  on-demand scrape (used by the front-end refresh button).
"""

from __future__ import annotations

import argparse
import functools
import json
import re
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, unquote
from datetime import datetime

from dashboard_data import determine_verify, generate_payload, write_payload

PUBLIC_DIR = Path(__file__).resolve().parent / "public"
DATA_PATH = PUBLIC_DIR / "data" / "status.json"
CLUSTER_USAGE_PATH = PUBLIC_DIR / "data" / "cluster_usage.json"
SYSTEM_MARKDOWN_DIR = Path(__file__).resolve().parent / "system_markdown"
CLUSTER_MONITOR_SCRIPT = Path(__file__).resolve().parent / "cluster_monitor.py"
DEFAULT_REFRESH_SECONDS = 180
DEFAULT_CLUSTER_MONITOR_INTERVAL = 120


class DashboardState:
    def __init__(self, *, url: Optional[str], timeout: int, verify, output_path: Path):
        self.url = url
        self.timeout = timeout
        self.verify = verify
        self.output_path = output_path
        self._payload = self._load_existing(output_path)
        self._last_error: Optional[str] = None
        self._last_refresh_ts: Optional[float] = None
        self._payload_lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def _load_existing(self, path: Path):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def refresh(self, *, blocking: bool = True) -> Tuple[bool, str]:
        if not self._refresh_lock.acquire(blocking=blocking):
            return False, "Refresh already in progress."
        try:
            payload = generate_payload(
                url=self.url,
                timeout=self.timeout,
                verify=self.verify,
            )
            write_payload(payload, self.output_path)
            with self._payload_lock:
                self._payload = payload
                self._last_error = None
                self._last_refresh_ts = time.time()
            return True, "Refreshed."
        except Exception as exc:  # pragma: no cover - defensive
            with self._payload_lock:
                self._last_error = str(exc)
            return False, f"Refresh failed: {exc}"
        finally:
            self._refresh_lock.release()

    def snapshot(self) -> Tuple[Optional[dict], Optional[str], Optional[float]]:
        with self._payload_lock:
            return self._payload, self._last_error, self._last_refresh_ts


class RefreshWorker(threading.Thread):
    daemon = True

    def __init__(self, state: DashboardState, interval_seconds: int):
        super().__init__(name="dashboard-refresh-worker")
        self.state = state
        self.interval = max(60, interval_seconds)
        self._stop_event = threading.Event()

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            self.state.refresh(blocking=True)

    def stop(self) -> None:
        self._stop_event.set()


class ClusterMonitorWorker(threading.Thread):
    daemon = True

    def __init__(self, *, script_path: Path, interval_seconds: int, python_executable: str, run_immediately: bool = True):
        super().__init__(name="cluster-monitor-worker")
        self.script_path = script_path
        self.interval = max(60, interval_seconds)
        self.python_executable = python_executable
        self._stop_event = threading.Event()
        self._run_immediately = run_immediately

    def run(self) -> None:
        if not self._run_immediately:
            if self._stop_event.wait(self.interval):
                return
        while not self._stop_event.is_set():
            self._invoke_monitor()
            if self._stop_event.wait(self.interval):
                break

    def stop(self) -> None:
        self._stop_event.set()

    def _invoke_monitor(self) -> None:
        if not self.script_path.exists():
            print(f"[cluster-monitor] Script missing: {self.script_path}")
            self.stop()
            return
        try:
            print(f"[cluster-monitor] Running {self.script_path.name}")
            subprocess.run(
                [self.python_executable, str(self.script_path)],
                check=True,
            )
        except subprocess.CalledProcessError as exc:  # pragma: no cover - observational logging
            print(f"[cluster-monitor] Execution failed: {exc}")
        except Exception as exc:  # pragma: no cover
            print(f"[cluster-monitor] Unexpected error: {exc}")


SERVER_STATE: Optional[DashboardState] = None


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory=None, **kwargs):
        super().__init__(*args, directory=directory or str(PUBLIC_DIR), **kwargs)

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
        if parsed.path == "/api/status":
            self._handle_status()
            return
        if parsed.path == "/app-config.js":
            self._handle_app_config()
            return
        if parsed.path == "/api/fleet/summary":
            self._handle_fleet_summary()
            return
        if parsed.path == "/api/cluster-usage":
            self._handle_cluster_usage()
            return
        if parsed.path.startswith("/api/cluster-usage/"):
            slug_part = parsed.path.split("/api/cluster-usage/", 1)[-1]
            self._handle_cluster_usage_detail(slug_part)
            return
        if parsed.path.startswith("/api/system-markdown/"):
            slug_part = parsed.path.split("/api/system-markdown/", 1)[-1]
            self._handle_system_markdown(slug_part)
            return
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
        if target.path in {"/api/status", "/api/refresh"}:
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
            self._handle_refresh()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Unknown endpoint")

    def _handle_status(self):
        state = SERVER_STATE
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
        self.log_message("Served /api/status (payload ready: %s)", payload is not None)
        print("[dashboard] GET /api/status")

    def _handle_refresh(self):
        state = SERVER_STATE
        if not state:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Server not initialized.")
            return
        ok, detail = state.refresh(blocking=True)
        status = HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json({"ok": ok, "detail": detail}, status_code=status)
        self.log_message("Handled /api/refresh (ok=%s, detail=%s)", ok, detail)
        print(f"[dashboard] POST /api/refresh ok={ok} detail={detail}")

    def _handle_app_config(self):
        default_theme = getattr(self.server, "default_theme", "dark")  # type: ignore[attr-defined]
        cluster_pages_enabled = getattr(self.server, "cluster_pages_enabled", False)  # type: ignore[attr-defined]
        cluster_monitor_interval = getattr(self.server, "cluster_monitor_interval", DEFAULT_CLUSTER_MONITOR_INTERVAL)  # type: ignore[attr-defined]
        body = (
            "window.APP_CONFIG=Object.assign({},window.APP_CONFIG||{},"
            + json.dumps({
                "defaultTheme": default_theme,
                "clusterPagesEnabled": bool(cluster_pages_enabled),
                "clusterMonitorInterval": cluster_monitor_interval,
            }) +
            ");"
        ).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _handle_fleet_summary(self) -> None:
        state = SERVER_STATE
        if not state:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Server not initialized.")
            return
        payload, last_error, _ = state.snapshot()
        if payload is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, last_error or "Status data not ready.")
            return
        summary = self._build_system_summary(payload)
        self._send_json(summary)

    def _handle_cluster_usage(self) -> None:
        payload = self._load_cluster_usage_payload()
        if payload is None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, "Cluster usage data unavailable.")
            return
        clusters = self._build_cluster_profiles(payload)
        self._send_json({
            "generated_at": datetime.utcnow().isoformat(),
            "clusters": clusters,
        })

    def _handle_cluster_usage_detail(self, slug_part: str) -> None:
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

    def _handle_system_markdown(self, slug_part: str) -> None:
        raw = unquote(slug_part or "")
        if raw.endswith(".md"):
            raw = raw[:-3]
        normalized = re.sub(r"[^a-z0-9]", "", raw.lower())
        if not normalized:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid system identifier.")
            return
        base_dir = SYSTEM_MARKDOWN_DIR.resolve()
        if not base_dir.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Markdown directory not available.")
            return
        target = (base_dir / f"{normalized}.md").resolve()
        try:
            target.relative_to(base_dir)
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid markdown path.")
            return
        if not target.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Markdown not found.")
            return
        try:
            content = target.read_text(encoding="utf-8")
        except Exception as exc:  # pragma: no cover - best effort logging
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Unable to read markdown: {exc}")
            return
        self._send_json({"slug": normalized, "content": content})

    def _send_json(self, data, *, status_code: HTTPStatus = HTTPStatus.OK):
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
        prefix = getattr(self.server, "url_prefix", "")  # type: ignore[attr-defined]
        norm_prefix = (prefix or "").rstrip("/")
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
        prefix = getattr(self.server, "url_prefix", "")  # type: ignore[attr-defined]
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
        prefix = getattr(self.server, "url_prefix", "")  # type: ignore[attr-defined]
        norm_prefix = (prefix or "").rstrip("/")
        if norm_prefix and not norm_prefix.startswith("/"):
            norm_prefix = f"/{norm_prefix}"
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{norm_prefix}{path}" if norm_prefix else path

    def _filesystem_path(self, stripped_path: str) -> Optional[Path]:
        try:
            root = Path(self.directory or PUBLIC_DIR).resolve()  # type: ignore[attr-defined]
        except Exception:
            root = PUBLIC_DIR.resolve()
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
        if not CLUSTER_USAGE_PATH.exists():
            return None
        try:
            data = json.loads(CLUSTER_USAGE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Support either {"clusters": [...]} or plain list
                return data.get("clusters") or data.get("usage") or data
            return data
        except Exception as exc:
            print(f"[api] Unable to parse cluster usage data: {exc}")
            return None

    def _build_system_summary(self, payload):
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

    def _build_cluster_profiles(self, payload):
        clusters = []
        for entry in payload or []:
            meta = entry.get("cluster_metadata", {}) or {}
            usage = entry.get("usage_data", {}) or {}
            systems = usage.get("systems", []) or []
            queue_section = entry.get("queue_data", {}) or {}
            queues = queue_section.get("queues", []) or []
            nodes = queue_section.get("nodes", []) or []

            total_allocated = sum(self._safe_number(system.get("hours_allocated")) for system in systems)
            total_remaining = sum(self._safe_number(system.get("hours_remaining")) for system in systems)
            total_used = sum(self._safe_number(system.get("hours_used")) for system in systems)
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
                    "jobs": {
                        "running": running_jobs,
                        "pending": pending_jobs,
                    },
                    "cores": {
                        "running": running_cores,
                        "pending": pending_cores,
                    },
                    "utilization_percent": utilization,
                })

            least_backlogged = None
            if queue_profiles:
                sorted_queues = sorted(queue_profiles, key=lambda q: (q["jobs"]["pending"], q["cores"]["pending"]))
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


def run_server(args) -> None:
    global SERVER_STATE

    verify = determine_verify(insecure=args.insecure, ca_bundle=args.ca_bundle)
    state = DashboardState(
        url=args.url,
        timeout=args.timeout,
        verify=verify,
        output_path=DATA_PATH,
    )
    SERVER_STATE = state

    ok, detail = state.refresh(blocking=True)
    if not ok:
        print(detail)

    worker = RefreshWorker(state, interval_seconds=args.refresh_interval)
    worker.start()

    cluster_worker: Optional[ClusterMonitorWorker] = None
    cluster_pages_enabled = bool(args.cluster_pages)
    cluster_monitor_enabled = bool(args.cluster_monitor) and cluster_pages_enabled
    cluster_monitor_interval = max(60, args.cluster_monitor_interval)
    if cluster_monitor_enabled:
        if CLUSTER_MONITOR_SCRIPT.exists():
            cluster_worker = ClusterMonitorWorker(
                script_path=CLUSTER_MONITOR_SCRIPT,
                interval_seconds=cluster_monitor_interval,
                python_executable=sys.executable,
                run_immediately=True,
            )
            cluster_worker.start()
        else:
            print(f"[cluster-monitor] Skipping; script not found at {CLUSTER_MONITOR_SCRIPT}")

    normalized_prefix = (args.url_prefix or "").rstrip("/")
    handler = functools.partial(DashboardRequestHandler, directory=str(PUBLIC_DIR))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.url_prefix = normalized_prefix  # type: ignore[attr-defined]
    server.default_theme = args.default_theme  # type: ignore[attr-defined]
    server.cluster_pages_enabled = cluster_pages_enabled  # type: ignore[attr-defined]
    server.cluster_monitor_interval = cluster_monitor_interval if cluster_monitor_enabled else 0  # type: ignore[attr-defined]
    print(f"Serving dashboard on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
    finally:
        worker.stop()
        worker.join(timeout=5)
        if cluster_worker:
            cluster_worker.stop()
            cluster_worker.join(timeout=5)
        server.shutdown()
        server.server_close()


def parse_args():
    parser = argparse.ArgumentParser(description="Serve the auto-refreshing HPC status dashboard.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--refresh-interval", type=int, default=DEFAULT_REFRESH_SECONDS, help="Refresh cadence in seconds (min 60).")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout for scraper.")
    parser.add_argument("--url", default=None, help="Override the upstream status URL.")
    parser.add_argument("--insecure", action="store_true", default=True, help="Skip TLS verification.")
    parser.add_argument("--secure", dest="insecure", action="store_false", help="Require TLS verification.")
    parser.add_argument("--ca-bundle", type=str, help="Path to a custom CA bundle.")
    parser.add_argument("--url-prefix", default="", help="Path prefix to strip from incoming requests (e.g., /session/user/status).")
    parser.add_argument("--default-theme", choices=("dark", "light"), default="dark", help="Initial theme for clients without a saved preference.")
    parser.add_argument("--enable-cluster-pages", dest="cluster_pages", action="store_true", default=True, help="Expose quota/queue pages (default).")
    parser.add_argument("--disable-cluster-pages", dest="cluster_pages", action="store_false", help="Serve only the original fleet dashboard.")
    parser.add_argument("--enable-cluster-monitor", dest="cluster_monitor", action="store_true", default=True, help="Continuously run cluster_monitor.py (default).")
    parser.add_argument("--disable-cluster-monitor", dest="cluster_monitor", action="store_false", help="Skip running cluster_monitor.py in the background.")
    parser.add_argument("--cluster-monitor-interval", type=int, default=DEFAULT_CLUSTER_MONITOR_INTERVAL, help="Interval in seconds for running cluster_monitor.py (default: 300).")
    return parser.parse_args()


if __name__ == "__main__":
    run_server(parse_args())
