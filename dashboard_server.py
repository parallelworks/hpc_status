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
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

from dashboard_data import determine_verify, generate_payload, write_payload

PUBLIC_DIR = Path(__file__).resolve().parent / "public"
DATA_PATH = PUBLIC_DIR / "data" / "status.json"
DEFAULT_REFRESH_SECONDS = 180


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
        self.path = stripped + (f"?{parsed.query}" if parsed.query else "")
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self._handle_status()
            return
        return super().do_GET()

    def do_HEAD(self):
        parsed = urlparse(self.path)
        stripped = self._strip_prefix(parsed.path)
        if stripped is None:
            self.send_error(HTTPStatus.NOT_FOUND, "Invalid prefix")
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

    normalized_prefix = (args.url_prefix or "").rstrip("/")
    handler = functools.partial(DashboardRequestHandler, directory=str(PUBLIC_DIR))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    server.url_prefix = normalized_prefix  # type: ignore[attr-defined]
    print(f"Serving dashboard on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard...")
    finally:
        worker.stop()
        worker.join(timeout=5)
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
    return parser.parse_args()


if __name__ == "__main__":
    run_server(parse_args())
