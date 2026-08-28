"""HTTP-level tests for the dashboard server.

These boot the real handler on an ephemeral port. The cache headers in
particular are worth a live test: a stale styles.css served after a deploy
renders the dashboard unstyled, and nothing in a unit test would catch it.
"""

import functools
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

import pytest

from src.server.routes import DashboardRequestHandler


@pytest.fixture
def server(tmp_path):
    """Run the dashboard handler against a throwaway web root."""
    web_dir = tmp_path / "web"
    (web_dir / "assets").mkdir(parents=True)
    (web_dir / "assets" / "styles.css").write_text("body { color: red; }")
    (web_dir / "index.html").write_text("<html><body>hi</body></html>")

    DashboardRequestHandler.web_dir = web_dir
    DashboardRequestHandler.config = {
        "deployment": {"name": "Test", "platform": "generic"},
        "ui": {"title": "Test", "tabs": {"quota": False}},
        "topology": {"default_layout": "radial", "uptime_window_hours": 12},
    }
    DashboardRequestHandler.server_state = None
    DashboardRequestHandler.data_store = None
    DashboardRequestHandler.cluster_worker = None

    handler = functools.partial(DashboardRequestHandler, directory=str(web_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def fetch(url, headers=None):
    with urlopen(Request(url, headers=headers or {}), timeout=10) as response:
        return response.status, dict(response.headers), response.read()


class TestCacheHeaders:
    def test_static_assets_must_revalidate(self, server):
        """Without this, browsers keep serving yesterday's CSS after a deploy."""
        status, headers, _ = fetch(f"{server}/assets/styles.css")
        assert status == 200
        assert headers["Cache-Control"] == "no-cache, must-revalidate"

    def test_html_must_revalidate(self, server):
        _, headers, _ = fetch(f"{server}/index.html")
        assert headers["Cache-Control"] == "no-cache, must-revalidate"

    def test_revalidation_is_still_cheap(self, server):
        """no-cache means "check first", not "never cache" — 304s still work."""
        _, headers, _ = fetch(f"{server}/assets/styles.css")
        request = Request(
            f"{server}/assets/styles.css",
            headers={"If-Modified-Since": headers["Last-Modified"]},
        )
        try:
            with urlopen(request, timeout=10) as response:
                assert response.status == 304
        except Exception as exc:  # urllib raises on some 304 paths
            assert "304" in str(exc)

    def test_api_keeps_its_own_no_store(self, server):
        """API responses set their own header; it must not be duplicated."""
        _, headers, _ = fetch(f"{server}/api/config")
        assert headers["Cache-Control"] == "no-store, max-age=0"
        assert list(headers.values()).count("no-cache, must-revalidate") == 0


class TestAppConfig:
    def test_app_config_exposes_tabs_and_topology_settings(self, server):
        """The frontend needs ui.tabs to hide disabled nav links."""
        _, headers, body = fetch(f"{server}/app-config.js")
        text = body.decode()
        assert headers["Content-Type"].startswith("application/javascript")
        assert '"tabs": {"quota": false}' in text
        assert '"topologyLayout": "radial"' in text
        assert '"uptimeWindowHours": 12' in text

    def test_config_endpoint_reports_deployment(self, server):
        import json

        _, _, body = fetch(f"{server}/api/config")
        payload = json.loads(body)
        assert payload["deployment"] == {"name": "Test", "platform": "generic"}
        assert payload["topology"]["default_layout"] == "radial"


class TestEndpointCatalog:
    """The catalog is only useful if the server actually serves it."""

    def test_catalog_is_served_with_groups_and_a_base_url(self, server):
        import json

        status, headers, body = fetch(f"{server}/api/endpoints")
        payload = json.loads(body)
        assert status == 200
        assert headers["Content-Type"].startswith("application/json")
        assert payload["groups"], "the API page renders one section per group"
        assert payload["base_url"].startswith("/") or payload["base_url"] == ""
        paths = {entry["path"] for entry in payload["endpoints"]}
        # Self-description is the point: it must list itself.
        assert "/api/endpoints" in paths
        for entry in payload["endpoints"]:
            assert entry["group"] in payload["groups"]

    def test_catalog_answers_cross_origin_callers(self, server):
        """Documented as CORS-open; a dashboard embedded elsewhere relies on it."""
        _, headers, _ = fetch(f"{server}/api/endpoints")
        assert headers["Access-Control-Allow-Origin"] == "*"


class TestFaviconRoute:
    """/favicon.ico is what everything outside a rendered page requests."""

    def test_it_is_served_rather_than_404(self, server, tmp_path):
        img_dir = DashboardRequestHandler.web_dir / "assets" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "favicon-generic.png").write_bytes(b"\x89PNG\r\n\x1a\nstub")

        status, headers, body = fetch(f"{server}/favicon.ico")
        assert status == 200
        assert headers["Content-Type"] == "image/png"
        assert body.startswith(b"\x89PNG")

    def test_the_deployment_platform_wins(self, server):
        img_dir = DashboardRequestHandler.web_dir / "assets" / "img"
        img_dir.mkdir(parents=True, exist_ok=True)
        (img_dir / "favicon-generic.png").write_bytes(b"\x89PNG\r\n\x1a\ngeneric")
        (img_dir / "favicon-hpcmp.png").write_bytes(b"\x89PNG\r\n\x1a\nhpcmp")
        DashboardRequestHandler.config = {
            **(DashboardRequestHandler.config or {}),
            "deployment": {"name": "Test", "platform": "hpcmp"},
        }
        try:
            _, _, body = fetch(f"{server}/favicon.ico")
            assert body.endswith(b"hpcmp")
        finally:
            DashboardRequestHandler.config = {
                **(DashboardRequestHandler.config or {}),
                "deployment": {"name": "Test", "platform": "generic"},
            }
