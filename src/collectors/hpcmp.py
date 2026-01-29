"""HPCMP fleet status collector.

Scrapes https://centers.hpc.mil/systems/unclassified.html to collect
status information for DoD HPC systems.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

from .base import BaseCollector, CollectorError

try:
    import certifi
    DEFAULT_CA_BUNDLE = certifi.where()
except Exception:
    DEFAULT_CA_BUNDLE = True


UNCLASSIFIED_URL = "https://centers.hpc.mil/systems/unclassified.html"

# DSRC mappings
DSRC_CANON = {
    "afrl": "afrl",
    "air force": "afrl",
    "afrl dsrc": "afrl",
    "navy": "navy",
    "navy dsrc": "navy",
    "navydsrc": "navy",
    "erdc": "erdc",
    "erdc dsrc": "erdc",
    "arl": "arl",
    "army": "arl",
    "arl dsrc": "arl",
}

DSRC_DOMAIN = {
    "afrl": "afrl.hpc.mil",
    "navy": "navydsrc.hpc.mil",
    "erdc": "erdc.hpc.mil",
    "arl": "arl.hpc.mil",
}

SCHEDULER_KEYWORDS = {
    "slurm": "slurm",
    "pbs professional": "pbs",
    "pbs pro": "pbs",
    "pbspro": "pbs",
    "pbs": "pbs",
}


class HPCMPCollector(BaseCollector):
    """Collector for HPCMP (DoD HPC) fleet status.

    Scrapes centers.hpc.mil to get system status information.
    """

    def __init__(
        self,
        url: str = UNCLASSIFIED_URL,
        timeout: int = 20,
        verify: bool = False,
        ca_bundle: Optional[str] = None,
    ):
        self.url = url
        self.timeout = timeout
        # verify=False means skip SSL verification (insecure mode)
        # Pass the inverse to _determine_verify which expects insecure flag
        self._verify = self._determine_verify(not verify, ca_bundle)
        self._session: Optional[requests.Session] = None

        # Disable warnings once at init if not verifying SSL
        if self._verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @property
    def name(self) -> str:
        return "hpcmp"

    @property
    def display_name(self) -> str:
        return "HPCMP Fleet Status"

    def is_available(self) -> bool:
        """HPCMP collector is available if we can reach the URL."""
        try:
            session = self._make_session()
            resp = session.head(self.url, timeout=5)
            return resp.status_code < 500
        except Exception:
            return False

    def collect(self) -> Dict[str, Any]:
        """Fetch and parse HPCMP fleet status.

        Returns:
            Dictionary with 'systems', 'summary', and 'meta' keys.

        Raises:
            CollectorError: If scraping fails.
        """
        try:
            rows, soup = self._fetch_status()
            return self._build_payload(rows)
        except requests.exceptions.SSLError as e:
            raise CollectorError(
                self.name,
                "TLS/SSL error: certificate verify failed. Consider using --insecure or --ca-bundle.",
                e,
            )
        except Exception as e:
            raise CollectorError(self.name, str(e), e)

    def _determine_verify(self, insecure: bool, ca_bundle: Optional[str]):
        """Determine SSL verification setting."""
        if insecure:
            return False
        if ca_bundle:
            return ca_bundle
        return DEFAULT_CA_BUNDLE

    def _get_session(self) -> requests.Session:
        """Get or create a requests session with retry configuration.

        Reuses the session for connection pooling efficiency.
        """
        if self._session is None:
            session = requests.Session()
            retry = Retry(
                total=4,
                connect=4,
                read=4,
                backoff_factor=0.5,
                status_forcelist=(429, 500, 502, 503, 504),
                allowed_methods=("GET", "HEAD"),
            )
            # Limit connection pool to prevent resource exhaustion
            adapter = HTTPAdapter(
                max_retries=retry,
                pool_connections=5,
                pool_maxsize=10,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.verify = self._verify
            session.headers.update({"User-Agent": "hpc-status-monitor/2.0"})
            self._session = session
        return self._session

    def _make_session(self) -> requests.Session:
        """Create a requests session with retry configuration.

        Deprecated: Use _get_session() for connection reuse.
        """
        return self._get_session()

    def close(self) -> None:
        """Close the session and release resources."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def _fetch_status(self) -> Tuple[List[Dict[str, str]], BeautifulSoup]:
        """Fetch and parse the status page."""
        session = self._get_session()
        resp = session.get(self.url, timeout=self.timeout)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        rows: List[Dict[str, str]] = []
        now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

        for img in self._find_status_images(soup):
            alt = img.get("alt", "").strip()
            src = urljoin(self.url, img.get("src", "").strip())

            system = self._parse_system_from_alt(alt) or ""
            status = self._parse_status_from_alt(alt)

            if not status:
                status = self._guess_from_src(src) or "UNKNOWN"

            if not system:
                system = self._infer_system_name_from_context(img) or "(unknown)"

            dsrc = self._infer_dsrc_from_context(img)
            scheduler = self._infer_scheduler_from_context(img, system)
            login = self._build_login(system, dsrc) if dsrc else None

            rows.append({
                "system": system,
                "status": status,
                "dsrc": dsrc,
                "login": login,
                "scheduler": scheduler,
                "raw_alt": alt,
                "img_src": src,
                "source_url": self.url,
                "observed_at": now_iso,
            })

        return rows, soup

    def _build_payload(self, rows: List[Dict[str, str]]) -> Dict[str, Any]:
        """Build the status payload from parsed rows."""
        from collections import Counter

        statuses = Counter((r.get("status") or "UNKNOWN").upper() for r in rows)
        dsrcs = Counter((r.get("dsrc") or "UNKNOWN").upper() for r in rows)
        scheds = Counter((r.get("scheduler") or "UNKNOWN").upper() for r in rows)

        uptime_ratio = 0.0
        if rows:
            uptime_ratio = sum(1 for r in rows if (r.get("status") or "").upper() == "UP") / len(rows)

        observed_at = rows[0]["observed_at"] if rows else None

        return {
            "meta": {
                "source_url": self.url,
                "generated_at": observed_at,
                "collector": self.name,
            },
            "summary": {
                "total_systems": len(rows),
                "status_counts": dict(statuses),
                "dsrc_counts": dict(dsrcs),
                "scheduler_counts": dict(scheds),
                "uptime_ratio": round(uptime_ratio, 3),
            },
            "systems": rows,
        }

    # Status parsing helpers

    def _normalize_status(self, text: str) -> str:
        t = (text or "").strip().lower()
        if any(w in t for w in ["up", "available", "operational", "online"]):
            return "UP"
        if any(w in t for w in ["down", "offline", "unavailable"]):
            return "DOWN"
        if any(w in t for w in ["degrad", "limited", "partial", "impact"]):
            return "DEGRADED"
        if any(w in t for w in ["maint", "maintenance", "outage window"]):
            return "MAINTENANCE"
        return "UNKNOWN"

    def _guess_from_src(self, src: str) -> Optional[str]:
        base = os.path.basename((src or "")).lower()
        m = re.search(r'(?:^|[^a-z])(up|down|degrad(?:ed)?|maint(?:enance)?)\b', base)
        if m:
            return self._normalize_status(m.group(1))
        if "up." in base:
            return "UP"
        if "down." in base:
            return "DOWN"
        if "degrad" in base or "limited" in base or "partial" in base:
            return "DEGRADED"
        if "maint" in base:
            return "MAINTENANCE"
        return None

    def _parse_system_from_alt(self, alt: str) -> Optional[str]:
        if not alt:
            return None
        m = re.match(r"\s*(.*?)\s+is\s+currently\s+.+?\.\s*$", alt, flags=re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m2 = re.match(r"\s*(.*?)\s+is\s+.+", alt, flags=re.IGNORECASE)
        if m2:
            return m2.group(1).strip()
        return None

    def _parse_status_from_alt(self, alt: str) -> Optional[str]:
        if not alt:
            return None
        m = re.search(r"\bis\s+currently\s+([A-Za-z ]+)\.?", alt, flags=re.IGNORECASE)
        if m:
            return self._normalize_status(m.group(1))
        m2 = re.search(r"\bis\s+(Up|Down|Degraded|Maintenance|Maint|Limited|Partial)\b\.?", alt, flags=re.IGNORECASE)
        if m2:
            return self._normalize_status(m2.group(1))
        m3 = re.search(r"\b(Up|Down|Degraded|Maintenance|Maint|Limited|Partial)\b", alt, flags=re.IGNORECASE)
        if m3:
            return self._normalize_status(m3.group(1))
        return None

    def _find_status_images(self, soup: BeautifulSoup) -> List[Tag]:
        imgs = soup.select("img.statusImg")
        if not imgs:
            imgs = soup.find_all("img", attrs={"class": re.compile(r".*status.*", re.IGNORECASE)})
        return imgs

    def _find_nearest_heading_text(self, node: Tag) -> str:
        cur = node
        for _ in range(6):
            cur = cur.parent
            if not isinstance(cur, Tag):
                break
            heading = cur.find(["h1", "h2", "h3", "h4", "h5", "strong"])
            if heading and heading.get_text(strip=True):
                return heading.get_text(" ", strip=True)
        return ""

    def _infer_dsrc_from_context(self, node: Tag) -> Optional[str]:
        text = (self._find_nearest_heading_text(node) or "").lower()
        for key, canon in DSRC_CANON.items():
            if key in text:
                return canon
        cur = node
        for _ in range(3):
            cur = cur.parent
            if not isinstance(cur, Tag):
                break
            blob = cur.get_text(" ", strip=True).lower()
            for key, canon in DSRC_CANON.items():
                if key in blob:
                    return canon
        return None

    def _infer_scheduler_from_context(self, node: Tag, system_name: Optional[str] = None) -> Optional[str]:
        sys_slug = (system_name or "").strip().lower()

        cur = node
        for _ in range(5):
            cur = cur.parent
            if not isinstance(cur, Tag):
                break
            anchors = cur.find_all("a")
            for a in anchors:
                text = (a.get_text(" ", strip=True) or "").lower()
                href = (a.get("href") or "").lower()

                if sys_slug and sys_slug in text:
                    if "slurm" in text or "slurm" in href:
                        return "slurm"
                    if "pbs" in text or "pbs" in href:
                        return "pbs"

                if "slurm" in text or "slurm" in href:
                    return "slurm"
                if "pbs" in text or "pbs" in href:
                    return "pbs"

        texts = []
        cur = node
        for _ in range(4):
            cur = cur.parent
            if not isinstance(cur, Tag):
                break
            texts.append(cur.get_text(" ", strip=True).lower())
        blob = " ".join(texts)
        for k, v in SCHEDULER_KEYWORDS.items():
            if k in blob:
                return v
        return None

    def _build_login(self, system_name: str, dsrc: Optional[str]) -> Optional[str]:
        if not system_name or not dsrc:
            return None
        canon = DSRC_DOMAIN.get(dsrc.lower())
        if not canon:
            return None
        slug = re.sub(r"[^a-z0-9]+", "", system_name.strip().lower())
        return f"{slug}.{canon}"

    def _infer_system_name_from_context(self, node: Tag) -> Optional[str]:
        heading = self._find_nearest_heading_text(node)
        if heading:
            cleaned = re.sub(r"\bstatus\b.*", "", heading, flags=re.IGNORECASE).strip(" :-")
            return cleaned or heading

        if isinstance(node, Tag):
            prev_heading = node.find_previous(["h1", "h2", "h3", "h4", "h5", "strong"])
            if prev_heading:
                text = prev_heading.get_text(" ", strip=True)
                cleaned = re.sub(r"\bstatus\b.*", "", text, flags=re.IGNORECASE).strip(" :-")
                if cleaned:
                    return cleaned
        return None

    def collect_with_details(self) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Collect status data and detailed system briefings.

        Returns:
            Tuple of (status_payload, markdown_dict) where markdown_dict
            maps system slugs to markdown content.
        """
        try:
            rows, soup = self._fetch_status()
            payload = self._build_payload(rows)

            # Extract detailed system information
            system_details = self._extract_all_system_details(soup)

            # Generate markdown for each system
            markdown_dict = {}
            for row in rows:
                system_name = row.get("system", "")
                slug = re.sub(r"[^a-z0-9]", "", system_name.lower())
                if slug and slug in system_details:
                    markdown = self._generate_system_markdown(
                        system_name, row, system_details[slug]
                    )
                    markdown_dict[slug] = markdown

            return payload, markdown_dict
        except Exception as e:
            raise CollectorError(self.name, str(e), e)

    def _extract_all_system_details(self, soup: BeautifulSoup) -> Dict[str, Dict]:
        """Extract detailed specifications for all systems from the page."""
        details = {}

        # Find all system accordion sections
        system_divs = soup.find_all("div", class_=lambda x: x and "system" in str(x).lower())

        for div in system_divs:
            # Try to find the system name from the accordion header
            system_name = self._find_accordion_system_name(div)
            if not system_name:
                continue

            slug = re.sub(r"[^a-z0-9]", "", system_name.lower())
            if not slug:
                continue

            # Extract specs table
            specs = self._extract_specs_table(div)

            # Extract documentation links
            docs = self._extract_doc_links(div)

            details[slug] = {
                "name": system_name,
                "specs": specs,
                "docs": docs,
            }

        return details

    def _find_accordion_system_name(self, div: Tag) -> Optional[str]:
        """Find the system name from an accordion section."""
        # Structure: div.accordion-body > div.accordion-inner.system
        # Sibling: div.accordion-heading contains the system name
        parent = div.parent  # accordion-body
        if parent:
            # Get previous sibling (accordion-heading)
            heading_div = parent.find_previous_sibling(class_=re.compile(r"accordion-head", re.I))
            if heading_div:
                # The text is like "Barfoot is an HPE Cray..."
                text = heading_div.get_text(" ", strip=True)
                # Extract the system name (first word before "is")
                match = re.match(r"^\s*(\w+)\s+is\s+", text, re.I)
                if match:
                    return match.group(1)

                # Fallback: look for system name pattern
                match2 = re.match(r"^\s*(\w+)\s*[-–—]", text)
                if match2:
                    return match2.group(1)

        # Fallback: look for link in parent with anchor
        if parent and parent.parent:
            links = parent.parent.find_all("a", href=lambda h: h and "#" in h)
            for link in links:
                href = link.get("href", "")
                if href.startswith("#"):
                    return href[1:]

        return None

    def _extract_specs_table(self, div: Tag) -> Dict[str, str]:
        """Extract system specifications from the table in the div."""
        specs = {}
        table = div.find("table")
        if not table:
            return specs

        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all(["td", "th"])
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True)
                value = cells[1].get_text(strip=True)
                if label and value:
                    specs[label] = value

        return specs

    def _extract_doc_links(self, div: Tag) -> List[Dict[str, str]]:
        """Extract documentation links from the div."""
        docs = []
        links = div.find_all("a", href=True)
        for link in links:
            href = link.get("href", "")
            text = link.get_text(strip=True)
            if text and len(text) > 3 and ("guide" in text.lower() or "doc" in text.lower()):
                # Make absolute URL
                if not href.startswith("http"):
                    href = urljoin(self.url, href)
                docs.append({"title": text, "url": href})

        return docs

    def _generate_system_markdown(
        self,
        system_name: str,
        status_row: Dict,
        details: Dict
    ) -> str:
        """Generate markdown content for a system."""
        lines = []

        # Header
        lines.append(f"# {system_name}")
        lines.append("")

        # Status summary
        status = status_row.get("status", "Unknown")
        dsrc = status_row.get("dsrc", "Unknown")
        scheduler = status_row.get("scheduler", "Unknown")
        login = status_row.get("login", "N/A")

        lines.append("## Current Status")
        lines.append("")
        lines.append(f"- **Status**: {status}")
        lines.append(f"- **Site**: {dsrc.upper() if dsrc else 'Unknown'}")
        lines.append(f"- **Scheduler**: {scheduler.upper() if scheduler else 'Unknown'}")
        lines.append(f"- **Login Node**: `{login}`" if login else "- **Login Node**: N/A")
        lines.append("")

        # Specifications
        specs = details.get("specs", {})
        if specs:
            lines.append("## System Specifications")
            lines.append("")
            lines.append("| Specification | Value |")
            lines.append("|--------------|-------|")
            for label, value in specs.items():
                if label:  # Skip empty labels
                    lines.append(f"| {label} | {value} |")
            lines.append("")

        # Documentation links
        docs = details.get("docs", [])
        if docs:
            lines.append("## Documentation")
            lines.append("")
            for doc in docs:
                lines.append(f"- [{doc['title']}]({doc['url']})")
            lines.append("")

        # Source info
        lines.append("---")
        lines.append(f"*Data source: [centers.hpc.mil]({self.url})*")

        return "\n".join(lines)
