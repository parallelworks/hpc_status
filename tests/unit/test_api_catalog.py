"""The API catalog must describe the API that actually exists.

The committed OpenAPI document had drifted badly — two endpoints that were
never implemented, five implemented ones missing — because it lived apart
from the code. These tests pin the catalog to the router in both
directions, so neither can move without the other.
"""

import re
import sys
from pathlib import Path

import pytest

from src.server.api_catalog import ENDPOINTS, catalog, groups

ROUTES_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "server" / "routes.py"
).read_text()

# Paths the dispatcher actually matches, in either the exact or prefix form.
ROUTED = set(re.findall(r'(?:parsed|target)\.path == "([^"]+)"', ROUTES_SOURCE)) | {
    f"{prefix}{{param}}"
    for prefix in re.findall(r'parsed\.path\.startswith\("([^"]+)"\)', ROUTES_SOURCE)
}


def normalized(path: str) -> str:
    """Reduce a catalog path to the shape the router check produces."""
    return re.sub(r"\{[^}]+\}", "{param}", path)


class TestCatalogMatchesTheRouter:
    @pytest.mark.parametrize(
        "entry", ENDPOINTS, ids=lambda e: f"{e['method']} {e['path']}"
    )
    def test_every_documented_endpoint_is_routed(self, entry):
        assert normalized(entry["path"]) in ROUTED, (
            f"{entry['path']} is documented but nothing routes it — either it "
            f"was removed or the catalog invented it"
        )

    def test_every_routed_endpoint_is_documented(self):
        documented = {normalized(entry["path"]) for entry in ENDPOINTS}
        # The root redirect is plumbing, not an API surface.
        undocumented = ROUTED - documented - {"/"}
        assert not undocumented, (
            f"these endpoints route but are not in the catalog: {sorted(undocumented)}"
        )


class TestCatalogShape:
    @pytest.mark.parametrize(
        "entry", ENDPOINTS, ids=lambda e: f"{e['method']} {e['path']}"
    )
    def test_entries_carry_what_a_reader_needs(self, entry):
        assert entry["method"] in {"GET", "POST"}
        assert entry["path"].startswith("/")
        assert entry["group"] in groups()
        assert entry["summary"] and not entry["summary"].endswith(".")
        assert len(entry["description"]) > 20
        for param in entry.get("params", []):
            assert param["name"] and param["description"]
            assert param["type"] in {"string", "integer", "number", "boolean"}

    def test_paths_are_unique(self):
        pairs = [(e["method"], e["path"]) for e in ENDPOINTS]
        assert len(pairs) == len(set(pairs))

    def test_catalog_returns_copies(self):
        """Callers mutating the result must not corrupt the source."""
        first = catalog()
        first[0]["summary"] = "clobbered"
        assert catalog()[0]["summary"] != "clobbered"

    def test_secrets_are_not_advertised(self):
        """The alert webhook is a credential and must not be described as returned."""
        for entry in ENDPOINTS:
            assert "webhook_url" not in entry.get("returns", [])


REPO = Path(__file__).resolve().parents[2]


class TestPublishedDocsFollowTheCatalog:
    """Both published descriptions of the API are derived from the catalog."""

    def test_openapi_spec_is_regenerated(self):
        sys.path.insert(0, str(REPO / "scripts"))
        from build_openapi import SPEC_PATH, render

        assert SPEC_PATH.read_text() == render(), (
            "schemas/openapi.yaml is stale — run `python scripts/build_openapi.py`"
        )

    @pytest.mark.parametrize(
        "entry", ENDPOINTS, ids=lambda e: f"{e['method']} {e['path']}"
    )
    def test_prose_reference_covers_every_endpoint(self, entry):
        doc = (REPO / "docs" / "api.md").read_text()
        assert f"{entry['method']} {entry['path']}" in doc, (
            f"docs/api.md does not mention {entry['method']} {entry['path']}"
        )

    def test_the_api_page_is_shipped(self):
        """The page is useless if its script or nav link goes missing."""
        page = (REPO / "web" / "api.html").read_text()
        assert "assets/js/api.js" in page
        assert (REPO / "web" / "assets" / "js" / "api.js").exists()
        for name in ("index", "topology", "queues", "quota", "storage", "insights"):
            other = (REPO / "web" / f"{name}.html").read_text()
            assert 'href="api.html"' in other, f"{name}.html has no link to the API page"
