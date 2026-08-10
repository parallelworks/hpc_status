"""Marketplace catalog collector.

The fleet page used to show only what the status page published, so a
system that exists, is documented, and is usable — Coral, say — was
invisible unless someone happened to know it was there. The platform
already carries that knowledge: every cluster has a marketplace listing
with a description and tags.

This reads those listings so the dashboard can show the whole catalog and
say what each machine is for, rather than only the subset one scraper
knows about.

Listings are a catalog, not a status source. Nothing here reports whether
a machine is up — that stays with the status page and the live sessions.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .base import BaseCollector

# The catalog changes when somebody publishes a listing, which is rare
# compared to how often the fleet is polled.
_CACHE_TTL = timedelta(minutes=30)

# Tags are free-form, so only these two vocabularies are read: a site the
# rest of the pipeline already knows, and a scheduler we can name.
_SCHEDULER_TAGS = {"slurm": "SLURM", "pbs": "PBS", "torque": "PBS", "lsf": "LSF"}


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def slugify(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


class PWMarketplaceCollector(BaseCollector):
    """Compute listings from ``pw marketplace ls``."""

    def __init__(self, timeout: int = 30, pw_context: Optional[str] = None):
        self.timeout = timeout
        self.pw_context = pw_context
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cached_at: Optional[datetime] = None

    @property
    def name(self) -> str:
        return "pw_marketplace"

    @property
    def display_name(self) -> str:
        return "Marketplace Catalog"

    def _command(self, *args: str) -> List[str]:
        cmd = ["pw"]
        if self.pw_context:
            cmd += ["--context", self.pw_context]
        return cmd + list(args)

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                self._command("marketplace", "--help"),
                capture_output=True,
                timeout=self.timeout,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def collect(self) -> Dict[str, Any]:
        """Return ``{"listings": [...], "generated_at": ...}``.

        Never raises: a deployment with no marketplace, no ``pw``, or no
        permission to list it simply has no catalog to add, and the fleet
        page carries on with what the collectors found.
        """
        listings = self.listings()
        return {
            "listings": listings,
            "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        }

    def listings(self, *, force: bool = False) -> List[Dict[str, Any]]:
        """Compute listings, cached for a while."""
        fresh = (
            self._cached_at is not None
            and datetime.utcnow() - self._cached_at < _CACHE_TTL
        )
        if self._cache is not None and fresh and not force:
            return self._cache

        raw = self._fetch()
        parsed = [self._parse(item) for item in raw]
        self._cache = [entry for entry in parsed if entry]
        self._cached_at = datetime.utcnow()
        if self._cache:
            _log(f"[pw_marketplace] {len(self._cache)} compute listing(s)")
        return self._cache

    def _fetch(self) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                self._command("marketplace", "ls", "-o", "json"),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _log(f"[pw_marketplace] listing failed: {exc}")
            return []

        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            _log(f"[pw_marketplace] listing failed: {detail[-1] if detail else 'no output'}")
            return []

        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            _log(f"[pw_marketplace] unreadable listing output: {exc}")
            return []

        if isinstance(payload, dict):
            payload = payload.get("items") or payload.get("marketplace") or []
        return [item for item in payload if isinstance(item, dict)]

    @staticmethod
    def _parse(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Reduce a listing to the fields the fleet page can use."""
        if str(item.get("type") or "").lower() != "compute":
            return None
        slug = slugify(item.get("slug") or item.get("name"))
        if not slug:
            return None

        tags = [str(tag).lower() for tag in (item.get("tags") or []) if tag]
        scheduler = next(
            (_SCHEDULER_TAGS[tag] for tag in tags if tag in _SCHEDULER_TAGS), None
        )

        return {
            "slug": slug,
            "name": str(item.get("name") or slug),
            # Descriptions are what make the catalog worth showing: they
            # answer "what is this machine for", which no status page does.
            "description": str(item.get("description") or "").strip() or None,
            "tags": tags,
            "scheduler": scheduler,
            "publisher": item.get("publisher") or None,
            "subtype": item.get("subtype") or None,
        }
