"""NOAA RDHPCS system collector.

Provides status information for NOAA Research and Development HPC Systems,
plus an on-demand scraper of docs.rdhpcs.noaa.gov user-guide pages that turns
each system's "System Overview" / "System Configuration" sections into the
markdown briefing the dashboard renders when a cluster is clicked.
"""

from __future__ import annotations

import re
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

from .base import BaseCollector, CollectorError

# NOAA RDHPCS systems and their properties.
# Source: https://docs.rdhpcs.noaa.gov/systems/index.html
NOAA_SYSTEMS = {
    "hera": {
        "name": "Hera",
        "location": "NESCC (Fairmont, WV)",
        "scheduler": "Slurm",
        "description": "Dell PowerEdge cluster for weather and climate research",
        "login_node": "hera.rdhpcs.noaa.gov",
    },
    "ursa": {
        "name": "Ursa",
        "location": "NESCC (Fairmont, WV)",
        "scheduler": "Slurm",
        "description": "Dell + AMD/NVIDIA cluster (H100, MI300X, Grace Hopper)",
        "login_node": "ursa.rdhpcs.noaa.gov",
    },
    "jet": {
        "name": "Jet",
        "location": "ESRL (Boulder, CO)",
        "scheduler": "Slurm",
        "description": "Research system for NOAA laboratories",
        "login_node": "jet.rdhpcs.noaa.gov",
    },
    "gaea": {
        "name": "Gaea",
        "location": "ORNL (Oak Ridge, TN)",
        "scheduler": "Slurm",
        "description": "HPE Cray for GFDL climate modeling (C5/C6 partitions)",
        "login_node": "gaea.rdhpcs.noaa.gov",
    },
    "hercules": {
        "name": "Hercules",
        "location": "MSU (Starkville, MS)",
        "scheduler": "Slurm",
        "description": "AMD-based cluster for research workloads",
        "login_node": "hercules.rdhpcs.noaa.gov",
    },
    "ppan": {
        "name": "PPAN",
        "location": "GFDL (Princeton, NJ)",
        "scheduler": "none (analysis nodes)",
        "description": "Post-processing and analysis nodes, GFDL filesystems",
        "login_node": "ppan.rdhpcs.noaa.gov",
    },
    "mercury": {
        "name": "Mercury",
        "location": "NESCC (Fairmont, WV)",
        "scheduler": "none (data-mover)",
        "description": "Data-mover / HPSS archive gateway",
        "login_node": "mercury.rdhpcs.noaa.gov",
    },
}


class NOAADocsCollector(BaseCollector):
    """Collector for NOAA RDHPCS system information.

    Provides static system definitions and can be extended to scrape
    the NOAA RDHPCS documentation site for status updates.
    """

    def __init__(self, url: str = None, timeout: int = 30):
        """Initialize the collector.

        Args:
            url: NOAA docs URL (optional, for future scraping)
            timeout: Request timeout in seconds
        """
        self._url = url or "https://docs.rdhpcs.noaa.gov/systems/index.html"
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "noaa_docs"

    @property
    def display_name(self) -> str:
        return "NOAA RDHPCS Systems"

    def is_available(self) -> bool:
        """Check if collector is available.

        Returns:
            Always True since we have static system definitions.
        """
        return True

    def collect(self) -> Dict[str, Any]:
        """Collect NOAA system information.

        Returns:
            Dictionary with system information.
        """
        systems = []

        for system_id, info in NOAA_SYSTEMS.items():
            systems.append({
                "id": system_id,
                "system": info["name"],
                "status": "UP",  # Default to UP; actual status from PW cluster monitor
                "location": info["location"],
                "scheduler": info["scheduler"],
                "login_node": info["login_node"],
                "description": info["description"],
            })

        return {
            "source": "noaa_docs",
            "platform": "noaa",
            "systems": systems,
            "total_systems": len(systems),
        }

    def get_system_info(self, system_name: str) -> Dict[str, Any]:
        """Get information for a specific system.

        Args:
            system_name: System name (case-insensitive)

        Returns:
            System information dictionary, or empty dict if not found.
        """
        key = system_name.lower()
        if key in NOAA_SYSTEMS:
            info = NOAA_SYSTEMS[key]
            return {
                "id": key,
                "system": info["name"],
                "location": info["location"],
                "scheduler": info["scheduler"],
                "login_node": info["login_node"],
                "description": info["description"],
            }
        return {}

    def list_systems(self) -> List[str]:
        """Get list of known NOAA systems.

        Returns:
            List of system names.
        """
        return [info["name"] for info in NOAA_SYSTEMS.values()]


# ---------------------------------------------------------------------------
# System briefing scraper (docs.rdhpcs.noaa.gov → markdown)
# ---------------------------------------------------------------------------

# PW cluster slugs (as they appear in the dashboard payload) → tuple of
# (system display name, doc URL, anchor sections to extract).
#
# The slug is what ``_normalize_cluster_slug`` produces from the cluster's
# ``name`` field — for NOAA the PW cluster URI segment is already kebab-free,
# so the slug matches the URI segment exactly.
NOAA_BRIEFING_SOURCES: Dict[str, Dict[str, Any]] = {
    "noaaheracluster": {
        "name": "Hera",
        "location": "NESCC (Fairmont, WV)",
        "scheduler": "Slurm",
        "login_node": "hera.rdhpcs.noaa.gov",
        "url": "https://docs.rdhpcs.noaa.gov/systems/hera_user_guide.html",
        "overview_id": "system-overview",
        "specs_id": "system-configuration",
        "partitions_id": "hera-partitions",
    },
    "noaaursacluster": {
        "name": "Ursa",
        "location": "NESCC (Fairmont, WV)",
        "scheduler": "Slurm",
        "login_node": "ursa.rdhpcs.noaa.gov",
        "url": "https://docs.rdhpcs.noaa.gov/systems/ursa_user_guide.html",
        "overview_id": "system-overview",
        "specs_id": "system-configuration",
        "partitions_id": "ursa-partitions",
    },
    "noaagaeac5cluster": {
        "name": "Gaea (C5)",
        "location": "ORNL (Oak Ridge, TN)",
        "scheduler": "Slurm",
        "login_node": "gaea.rdhpcs.noaa.gov",
        "url": "https://docs.rdhpcs.noaa.gov/systems/gaea_user_guide.html",
        "overview_id": "system-overview",
        "specs_id": "system-configuration",
        "partitions_id": "partitions",
    },
    "noaappancluster": {
        "name": "PPAN",
        "location": "GFDL (Princeton, NJ)",
        "scheduler": "none (analysis nodes)",
        "login_node": "ppan.rdhpcs.noaa.gov",
        "url": "https://docs.rdhpcs.noaa.gov/systems/ppan_user_guide.html",
        # PPAN's doc has no #system-overview anchor — fall back to the top
        # paragraph of the first content section.
        "overview_id": None,
        "specs_id": None,
        "partitions_id": None,
    },
    "noaamercurysystem": {
        "name": "Mercury",
        "location": "NESCC (Fairmont, WV)",
        "scheduler": "none (data-mover)",
        "login_node": "mercury.rdhpcs.noaa.gov",
        "url": "https://docs.rdhpcs.noaa.gov/systems/mercury_user_guide.html",
        "overview_id": "system-overview",
        "specs_id": None,
        "partitions_id": None,
    },
    "gclusternoaav3": {
        "name": "NOAA Cloud (v3)",
        "location": "Parallel Works (Google Cloud)",
        "scheduler": "Slurm (cloud-managed)",
        "login_node": "parallel.works console",
        "url": "https://docs.rdhpcs.noaa.gov/systems/cloud_user_guide.html",
        # Cloud guide is enormous; grab the very first paragraph as the
        # overview and skip any specs table.
        "overview_id": None,
        "specs_id": None,
        "partitions_id": None,
    },
}


class NOAABriefingScraper:
    """Fetch and convert RDHPCS user-guide pages to per-cluster markdown.

    Used by the NOAA payload generator to populate the system-detail panel
    that pops open when a user clicks a cluster card.
    """

    def __init__(self, timeout: int = 20, sources: Optional[Dict[str, Dict[str, Any]]] = None):
        self.timeout = timeout
        self.sources = sources or NOAA_BRIEFING_SOURCES
        self._session: Optional[requests.Session] = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            s = requests.Session()
            s.headers.update({"User-Agent": "hpc-status-monitor/2.1 (RDHPCS briefings)"})
            self._session = s
        return self._session

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def collect_all(self) -> Dict[str, str]:
        """Fetch every known cluster's briefing.

        Returns dict mapping cluster slug → markdown content. Failures for one
        cluster never block the others.
        """
        out: Dict[str, str] = {}
        for slug, spec in self.sources.items():
            try:
                md = self.fetch_one(slug)
                if md:
                    out[slug] = md
            except Exception:
                # Best-effort: never let a 503/timeout block other systems.
                continue
        return out

    def fetch_one(self, slug: str) -> Optional[str]:
        spec = self.sources.get(slug)
        if not spec:
            return None
        session = self._get_session()
        resp = session.get(spec["url"], timeout=self.timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return self._render_briefing(soup, spec)

    # ----- HTML → markdown rendering --------------------------------------

    def _render_briefing(self, soup: BeautifulSoup, spec: Dict[str, Any]) -> str:
        """Render a compact per-cluster briefing.

        Sections (matches HPCMP layout):
          1. # Cluster name
          2. ## Quick facts            — location, scheduler, login node
          3. ## Overview               — first paragraph (or a short intro)
          4. ## System configuration   — single specs table (if available)
          5. ## Documentation          — link back to the full user guide
        """
        url = spec["url"]
        name = spec["name"]
        overview_id = spec.get("overview_id")
        specs_id = spec.get("specs_id")

        # Locate the "overview" section.
        overview_section = self._find_section(soup, overview_id)
        # If we didn't find a dedicated overview section, fall back to the
        # first content section under <div role="main">.
        if overview_section is None:
            main = soup.find("div", attrs={"role": "main"}) or soup
            overview_section = main.find("section") if isinstance(main, Tag) else None

        # Specs table — prefer the section explicitly tagged for it, else the
        # first table inside the overview section.
        specs_section = self._find_section(soup, specs_id) if specs_id else None
        if specs_section is None and overview_section is not None:
            specs_section = overview_section
        specs_table = specs_section.find("table") if isinstance(specs_section, Tag) else None

        parts: List[str] = []
        parts.append(f"# {name}")
        parts.append("")

        # --- Quick facts (from static metadata) -------------------------
        parts.append("## Quick facts")
        parts.append("")
        parts.append(f"- **Location**: {spec.get('location', '—')}")
        parts.append(f"- **Scheduler**: {spec.get('scheduler', '—')}")
        parts.append(f"- **Login node**: `{spec.get('login_node', '—')}`")
        parts.append("")

        # --- Overview paragraph -----------------------------------------
        overview_text = self._first_paragraph(overview_section, url)
        if overview_text:
            parts.append("## Overview")
            parts.append("")
            parts.append(overview_text)
            parts.append("")

        # --- System configuration table ---------------------------------
        if isinstance(specs_table, Tag):
            parts.append("## System configuration")
            parts.append("")
            parts.append(self._table_to_md(specs_table, url))
            parts.append("")

        # --- Partitions section -----------------------------------------
        partitions_section = self._find_section(soup, spec.get("partitions_id"))
        if isinstance(partitions_section, Tag):
            parts.append("## Partitions")
            parts.append("")
            # Prefer a partition table when one exists, else the first
            # paragraph / bullet list. Tables on these pages list partition
            # name, max walltime, node count etc.
            part_table = partitions_section.find("table")
            if isinstance(part_table, Tag):
                parts.append(self._table_to_md(part_table, url))
                parts.append("")
            else:
                lead = self._first_paragraph(partitions_section, url)
                if lead:
                    parts.append(lead)
                    parts.append("")

        # --- Documentation link -----------------------------------------
        parts.append("## Documentation")
        parts.append("")
        parts.append(f"- [{name} user guide]({url})")
        parts.append("")

        return "\n".join(parts).strip() + "\n"

    @staticmethod
    def _find_section(soup: BeautifulSoup, section_id: Optional[str]) -> Optional[Tag]:
        if not section_id:
            return None
        node = soup.find(id=section_id)
        if isinstance(node, Tag) and node.name == "section":
            return node
        if isinstance(node, Tag):
            parent = node.find_parent("section")
            if isinstance(parent, Tag):
                return parent
        return None

    def _first_paragraph(self, section: Optional[Tag], source_url: str) -> str:
        """Return the first prose chunk of a section as markdown.

        Walks block-level children in order. The first <p> wins. If the
        section opens with a <ul> (Hera/Ursa pattern), grab that bullet list
        instead so we don't lose the headline facts.
        """
        if not isinstance(section, Tag):
            return ""
        for child in section.children:
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name == "p":
                text = self._inline_to_md(child, source_url)
                if text:
                    return text
            if name == "ul":
                bullets = self._list_to_md(child, source_url, ordered=False)
                if bullets:
                    return "\n".join(bullets)
            # Don't recurse into deeply nested sections; we want the lead.
        return ""

    def _inline_to_md(self, node: Tag, source_url: str) -> str:
        """Render inline children of ``node`` as markdown (bold/italic/code/links)."""
        bits: List[str] = []
        for child in node.children:
            if isinstance(child, NavigableString):
                bits.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            inner = self._inline_to_md(child, source_url)
            if name in ("strong", "b"):
                bits.append(f"**{inner}**")
            elif name in ("em", "i"):
                bits.append(f"*{inner}*")
            elif name == "code":
                bits.append(f"`{child.get_text('', strip=False)}`")
            elif name == "a":
                href = child.get("href", "")
                # Drop Sphinx's headerlink permalinks — they use a FontAwesome
                # glyph (or ¶) as their only content, so they render as empty
                # link text and pollute every heading.
                a_classes = child.get("class") or []
                if "headerlink" in a_classes:
                    continue
                link_text = re.sub(r"[¶]", "", inner).strip()
                if not link_text:
                    continue
                if href:
                    abs_href = urljoin(source_url, href)
                    bits.append(f"[{link_text}]({abs_href})")
                else:
                    bits.append(link_text)
            elif name == "br":
                bits.append("\n")
            elif name in ("span", "abbr"):
                bits.append(inner)
            else:
                bits.append(inner)
        text = "".join(bits)
        # Collapse runs of whitespace introduced by the HTML formatter.
        text = re.sub(r"[ \t]+", " ", text)
        return text.strip()

    def _list_to_md(self, node: Tag, source_url: str, *, ordered: bool) -> List[str]:
        out: List[str] = []
        for idx, li in enumerate(node.find_all("li", recursive=False), start=1):
            bullet = f"{idx}." if ordered else "-"
            # Treat top-level text as the bullet content; nested lists get indented
            line_bits: List[str] = []
            nested: List[str] = []
            for child in li.children:
                if isinstance(child, Tag) and child.name.lower() in ("ul", "ol"):
                    sub = self._list_to_md(
                        child, source_url, ordered=child.name.lower() == "ol"
                    )
                    nested.extend("  " + s for s in sub)
                elif isinstance(child, NavigableString):
                    txt = str(child).strip()
                    if txt:
                        line_bits.append(txt)
                elif isinstance(child, Tag):
                    line_bits.append(self._inline_to_md(child, source_url))
            line = (" ".join(line_bits)).strip()
            out.append(f"{bullet} {line}")
            out.extend(nested)
        return out

    def _table_to_md(self, table: Tag, source_url: str) -> str:
        rows: List[List[str]] = []
        for tr in table.find_all("tr"):
            cells = [
                self._sanitize_cell(self._inline_to_md(c, source_url))
                for c in tr.find_all(["th", "td"])
            ]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        ncols = max(len(r) for r in rows)
        for r in rows:
            while len(r) < ncols:
                r.append("")
        header = "| " + " | ".join(rows[0]) + " |"
        sep = "| " + " | ".join(["---"] * ncols) + " |"
        body = "\n".join("| " + " | ".join(r) + " |" for r in rows[1:])
        return f"{header}\n{sep}" + (f"\n{body}" if body else "")

    @staticmethod
    def _sanitize_cell(text: str) -> str:
        """Make a cell safe for a single-line markdown table row.

        Markdown tables require every cell to live on one line, and a
        literal ``|`` would close the cell early. Multi-line descriptions
        from the RDHPCS docs (with hard wraps and ``<br>``) need to
        collapse to spaces and any pipe character needs escaping.
        """
        if not text:
            return ""
        # Collapse all whitespace runs (including newlines) into single spaces
        flat = re.sub(r"\s+", " ", text).strip()
        # Escape the pipe so it doesn't close the cell
        flat = flat.replace("|", "\\|")
        return flat
