#!/usr/bin/env python3
"""
HPC.MIL systems status scraper (inventory-aware, fixed scheduler detection)

Parses https://centers.hpc.mil/systems/unclassified.html and inspects DOM
<img class="statusImg" ... alt="SystemName is currently Up/Down/Degraded/...">
to determine the status of each computing system.

Outputs:
- Table view including SYSTEM, STATUS, DSRC, LOGIN NODE, SCHEDULER, OBSERVED_AT
- --json for full JSON per system
- --inventory-only for {system: {dsrc, login, scheduler}} mapping

TLS/Certs:
- Uses Requests/certifi by default, BUT --insecure defaults to True (per user preference).
- Use --ca-bundle /path/to/roots.pem to point at a custom CA bundle (e.g., DoD roots).
- Use --insecure to skip certificate verification (NOT recommended on public networks).

Extras:
- Retries with exponential backoff for transient failures.
- Absolute image URLs via urljoin.
- Optional CSV export via --csv /path/to/file.csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
import urllib3

try:
    import certifi
    DEFAULT_CA_BUNDLE = certifi.where()
except Exception:  # pragma: no cover
    DEFAULT_CA_BUNDLE = True  # fallback to requests' default


UNCLASSIFIED_URL = "https://centers.hpc.mil/systems/unclassified.html"
DEFAULT_MARKDOWN_DIR = "system_markdown"


# --- Status parsing helpers ---------------------------------------------------

def normalize_status(text: str) -> str:
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


def guess_from_src(src: str) -> Optional[str]:
    base = os.path.basename((src or "")).lower()
    m = re.search(r'(?:^|[^a-z])(up|down|degrad(?:ed)?|maint(?:enance)?)\b', base)
    if m:
        return normalize_status(m.group(1))
    if "up." in base:
        return "UP"
    if "down." in base:
        return "DOWN"
    if "degrad" in base or "limited" in base or "partial" in base:
        return "DEGRADED"
    if "maint" in base:
        return "MAINTENANCE"
    return None


def parse_system_from_alt(alt: str) -> Optional[str]:
    if not alt:
        return None
    m = re.match(r"\s*(.*?)\s+is\s+currently\s+.+?\.\s*$", alt, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = re.match(r"\s*(.*?)\s+is\s+.+", alt, flags=re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return None


def parse_status_from_alt(alt: str) -> Optional[str]:
    if not alt:
        return None
    m = re.search(r"\bis\s+currently\s+([A-Za-z ]+)\.?", alt, flags=re.IGNORECASE)
    if m:
        return normalize_status(m.group(1))
    m2 = re.search(r"\bis\s+(Up|Down|Degraded|Maintenance|Maint|Limited|Partial)\b\.?", alt, flags=re.IGNORECASE)
    if m2:
        return normalize_status(m2.group(1))
    m3 = re.search(r"\b(Up|Down|Degraded|Maintenance|Maint|Limited|Partial)\b", alt, flags=re.IGNORECASE)
    if m3:
        return normalize_status(m3.group(1))
    return None


# --- Inventory inference (DSRC, scheduler, login) -----------------------------

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
    "arl":  "arl.hpc.mil",
}

SCHEDULER_KEYWORDS = {
    "slurm": "slurm",
    "pbs professional": "pbs",
    "pbs pro": "pbs",
    "pbspro": "pbs",
    "pbs": "pbs",
}

PARTITION_KEYWORDS = ("partition", "queue", "class")
LIMIT_KEYWORDS = ("limit", "maximum", "wall", "time", "node", "core", "cpu", "memory", "job")
LEADING_PAD_HEADERS = ("priority", "category", "class")


def slugify_system(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.strip().lower())


def find_nearest_heading_text(node: Tag) -> str:
    cur = node
    for _ in range(6):
        cur = cur.parent
        if not isinstance(cur, Tag):
            break
        heading = cur.find(["h1", "h2", "h3", "h4", "h5", "strong"])
        if heading and heading.get_text(strip=True):
            return heading.get_text(" ", strip=True)
    return ""


def infer_dsrc_from_context(node: Tag) -> Optional[str]:
    text = (find_nearest_heading_text(node) or "").lower()
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


def infer_scheduler_from_context(node: Tag, system_name: Optional[str] = None) -> Optional[str]:
    """
    Detect scheduler by scanning nearby anchor/link text and hrefs, e.g.:
      <li><a href=".../bluebackSlurmGuide.html">Blueback Slurm Guide</a></li>
    """
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
                if "pbs professional" in text or "pbs pro" in text or "pbspro" in text or "pbs" in text or "pbs" in href:
                    return "pbs"

            if "slurm" in text or "slurm" in href:
                return "slurm"
            if ("pbs professional" in text or "pbs pro" in text or "pbspro" in text or "pbs" in text) or "pbs" in href:
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


def build_login(system_name: str, dsrc: Optional[str]) -> Optional[str]:
    if not system_name or not dsrc:
        return None
    canon = DSRC_DOMAIN.get(dsrc.lower())
    if not canon:
        return None
    return f"{slugify_system(system_name)}.{canon}"


def infer_system_name_from_context(node: Tag) -> Optional[str]:
    """
    Attempt to fall back to the nearest heading/strong text when <img> alt text is missing.
    """
    heading = find_nearest_heading_text(node)
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


def find_status_images(soup: BeautifulSoup) -> List[Tag]:
    imgs = soup.select("img.statusImg")
    if not imgs:
        imgs = soup.find_all("img", attrs={"class": re.compile(r".*status.*", re.IGNORECASE)})
    return imgs


def find_system_container(node: Tag) -> Optional[Tag]:
    """
    Walk up the DOM to find the nearest container that holds descriptive text for the system.
    """
    cur = node
    for _ in range(6):
        if not isinstance(cur, Tag):
            break
        if cur.name in {"li", "div", "section", "article"} and cur.get_text(" ", strip=True):
            cls = " ".join(cur.get("class", [])).lower()
            if any(key in cls for key in ("system", "card", "status", "panel")):
                return cur
            return cur
        cur = cur.parent
    return node.parent if isinstance(node, Tag) else None


def find_detail_section(soup: BeautifulSoup, system_name: str) -> Optional[Tag]:
    if not system_name:
        return None
    suffix = re.sub(r"[^A-Za-z0-9]", "", system_name)
    if not suffix:
        return None
    for candidate in (f"collapse{suffix}", suffix):
        detail = soup.find(id=candidate)
        if isinstance(detail, Tag):
            return detail
    return None


def collect_links(container: Optional[Tag], base_url: str) -> List[Dict[str, str]]:
    if not container:
        return []
    links: List[Dict[str, str]] = []
    seen = set()
    for a in container.find_all("a", href=True):
        href = urljoin(base_url, a.get("href", "").strip())
        text = a.get_text(" ", strip=True) or href
        key = (text.lower(), href.lower())
        if key in seen:
            continue
        seen.add(key)
        links.append({"text": text, "href": href})
    return links


def merge_links(*groups: List[Dict[str, str]]) -> List[Dict[str, str]]:
    merged: List[Dict[str, str]] = []
    seen = set()
    for group in groups:
        for link in group or []:
            text = (link.get("text") or "").strip()
            href = (link.get("href") or "").strip()
            key = (text.lower(), href.lower())
            if key in seen:
                continue
            seen.add(key)
            merged.append({"text": text or href, "href": href})
    return merged


def categorize_links(links: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    guide_keywords = ("guide", "user", "manual", "training", "tutorial", "reference")
    guides: List[Dict[str, str]] = []
    others: List[Dict[str, str]] = []

    for link in links:
        text = (link.get("text") or "").lower()
        href = (link.get("href") or "").lower()
        if any(keyword in text for keyword in guide_keywords) or any(keyword in href for keyword in guide_keywords):
            guides.append(link)
        else:
            others.append(link)
    return guides, others


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def split_label_value(text: str) -> Optional[Tuple[str, str]]:
    if ":" not in text:
        return None
    label, value = text.split(":", 1)
    label = normalize_whitespace(label)
    value = normalize_whitespace(value)
    if not label or not value:
        return None
    return label, value


def extract_html_tables(container: Optional[Tag]) -> List[Dict[str, Any]]:
    if not container:
        return []
    tables: List[Dict[str, Any]] = []
    for idx, table in enumerate(container.find_all("table"), start=1):
        headers: List[str] = []
        rows: List[List[str]] = []
        caption_tag = table.find("caption")
        title = normalize_whitespace(caption_tag.get_text(" ", strip=True)) if caption_tag else f"Table {idx}"

        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            row = [normalize_whitespace(cell.get_text(" ", strip=True)) for cell in cells]
            if not any(row):
                continue
            if tr.find_all("th") and not headers:
                headers = row
                continue
            rows.append(row)

        if rows:
            if not headers:
                max_cols = max(len(row) for row in rows)
                headers = [f"Column {i+1}" for i in range(max_cols)]
            normalized_rows: List[List[str]] = []
            for row in rows:
                if len(row) < len(headers):
                    diff = len(headers) - len(row)
                    first_header = (headers[0] or "").lower()
                    if headers and first_header and any(key in first_header for key in LEADING_PAD_HEADERS):
                        row = [""] * diff + row
                    else:
                        row = row + [""] * diff
                elif len(row) > len(headers):
                    row = row[:len(headers)]
                normalized_rows.append(row)
            rows = normalized_rows
            tables.append({"title": title, "headers": headers, "rows": rows})
    return tables


def analyze_container_text(container: Optional[Tag]) -> Dict[str, Any]:
    if not container:
        return {
            "description": "",
            "facts": [],
            "partitions": [],
            "limits": [],
            "tables": [],
        }

    paragraphs: List[str] = []
    facts: List[Dict[str, str]] = []
    partitions: List[Dict[str, str]] = []
    limits: List[Dict[str, str]] = []
    seen_text = set()

    elements = container.find_all(["p", "li"], recursive=True)
    if not elements:
        elements = [container]

    for tag in elements:
        if not isinstance(tag, Tag):
            continue
        if tag.find("img", attrs={"class": re.compile(r".*status.*", re.IGNORECASE)}):
            continue
        if tag.find_parent("table"):
            continue
        anchors = tag.find_all("a")
        if anchors:
            anchor_text = normalize_whitespace(" ".join(a.get_text(" ", strip=True) for a in anchors))
            if anchor_text and anchor_text == normalize_whitespace(tag.get_text(" ", strip=True)):
                continue
        text = normalize_whitespace(tag.get_text(" ", strip=True))
        if not text or len(text) < 3:
            continue
        if re.search(r"\bis\s+currently\s+", text, flags=re.IGNORECASE):
            continue
        if text.lower() in {"more info", "less info"}:
            continue
        if text.lower() in seen_text:
            continue
        seen_text.add(text.lower())

        kv = split_label_value(text)
        if kv:
            label, value = kv
            label_lower = label.lower()
            entry = {"label": label, "value": value}
            if any(keyword in label_lower for keyword in PARTITION_KEYWORDS):
                partitions.append(entry)
            elif any(keyword in label_lower for keyword in LIMIT_KEYWORDS):
                limits.append(entry)
            else:
                facts.append(entry)
        else:
            paragraphs.append(text)

    return {
        "description": "\n\n".join(paragraphs).strip(),
        "facts": facts,
        "partitions": partitions,
        "limits": limits,
        "tables": extract_html_tables(container),
    }


def merge_detail_sections(sections: List[Dict[str, Any]]) -> Dict[str, Any]:
    description_parts: List[str] = []
    facts: List[Dict[str, str]] = []
    partitions: List[Dict[str, str]] = []
    limits: List[Dict[str, str]] = []
    tables: List[Dict[str, Any]] = []

    def dedup_append(target: List[Dict[str, str]], entry: Dict[str, str], seen: set) -> None:
        key = (entry.get("label", "").lower(), entry.get("value", "").lower())
        if key in seen:
            return
        seen.add(key)
        target.append(entry)

    facts_seen: set = set()
    partition_seen: set = set()
    limit_seen: set = set()

    for section in sections:
        if not section:
            continue
        desc = section.get("description")
        if desc:
            description_parts.append(desc.strip())
        for entry in section.get("facts") or []:
            if not isinstance(entry, dict):
                continue
            dedup_append(facts, entry, facts_seen)
        for entry in section.get("partitions") or []:
            if not isinstance(entry, dict):
                continue
            dedup_append(partitions, entry, partition_seen)
        for entry in section.get("limits") or []:
            if not isinstance(entry, dict):
                continue
            dedup_append(limits, entry, limit_seen)
        tables.extend(section.get("tables") or [])

    return {
        "description": "\n\n".join(description_parts).strip(),
        "facts": facts,
        "partitions": partitions,
        "limits": limits,
        "tables": tables,
    }


def extract_system_contexts(soup: BeautifulSoup, base_url: str) -> List[Dict[str, Any]]:
    contexts: List[Dict[str, Any]] = []
    for idx, img in enumerate(find_status_images(soup), start=1):
        system = parse_system_from_alt(img.get("alt", "")) or infer_system_name_from_context(img)
        if not system:
            system = f"System {idx}"
        slug = slugify_system(system) or f"system{idx}"
        primary_container = find_system_container(img)
        detail_container = find_detail_section(soup, system)
        detail_sections: List[Dict[str, Any]] = [analyze_container_text(primary_container)]
        if detail_container and detail_container is not primary_container:
            detail_sections.append(analyze_container_text(detail_container))
        container_details = merge_detail_sections(detail_sections)
        link_groups = [collect_links(primary_container, base_url)]
        if detail_container and detail_container is not primary_container:
            link_groups.append(collect_links(detail_container, base_url))
        contexts.append(
            {
                "slug": slug,
                "description": container_details.get("description", ""),
                "facts": container_details.get("facts") or [],
                "partitions": container_details.get("partitions") or [],
                "limits": container_details.get("limits") or [],
                "tables": container_details.get("tables") or [],
                "links": merge_links(*link_groups),
                "heading": find_nearest_heading_text(img),
            }
        )
    return contexts


# --- HTTP Session & fetch -----------------------------------------------------

def _wrap_timeout(request_func, default_timeout: int):
    def wrapped(method, url, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = default_timeout
        return request_func(method, url, **kwargs)
    return wrapped


def _make_session(verify, headers=None, timeout=20) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=4, connect=4, read=4,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD")
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    s.verify = verify
    s.headers.update(headers or {"User-Agent": "pw-status-scraper/1.3"})
    s.request = _wrap_timeout(s.request, default_timeout=timeout)
    return s


def fetch_status(url: str, timeout: int, verify, headers: Optional[dict] = None) -> Tuple[List[Dict[str, str]], BeautifulSoup]:
    session = _make_session(verify=verify, headers=headers, timeout=timeout)
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    resp = session.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows: List[Dict[str, str]] = []
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    for img in find_status_images(soup):
        alt = img.get("alt", "").strip()
        src = urljoin(url, img.get("src", "").strip())

        system = parse_system_from_alt(alt) or ""
        status = parse_status_from_alt(alt)

        if not status:
            status = guess_from_src(src) or "UNKNOWN"

        if not system:
            system = infer_system_name_from_context(img) or "(unknown)"

        dsrc = infer_dsrc_from_context(img)
        scheduler = infer_scheduler_from_context(img, system)
        login = build_login(system, dsrc) if dsrc else None

        rows.append(
            {
                "system": system,
                "status": status,
                "dsrc": dsrc,
                "login": login,
                "scheduler": scheduler,
                "raw_alt": alt,
                "img_src": src,
                "source_url": url,
                "observed_at": now_iso,
            }
        )
    return rows, soup


# --- Output helpers -----------------------------------------------------------

def print_table(rows: List[Dict[str, str]]) -> None:
    if not rows:
        print("No status images found.")
        return

    # Determine column widths dynamically
    sys_w = max(6, max(len(r["system"]) for r in rows))
    st_w = max(6, max(len(r.get("status", "") or "") for r in rows))
    dsrc_w = max(4, max(len(r.get("dsrc", "") or "") for r in rows))
    login_w = max(10, max(len(r.get("login", "") or "") for r in rows))
    sched_w = max(9, max(len(r.get("scheduler", "") or "") for r in rows))

    header = (
        f'{"SYSTEM".ljust(sys_w)}  '
        f'{"STATUS".ljust(st_w)}  '
        f'{"DSRC".ljust(dsrc_w)}  '
        f'{"LOGIN NODE".ljust(login_w)}  '
        f'{"SCHEDULER".ljust(sched_w)}  '
        f'OBSERVED_AT'
    )
    print(header)
    print("-" * len(header))

    for r in rows:
        print(
            f'{r["system"].ljust(sys_w)}  '
            f'{(r.get("status") or "").ljust(st_w)}  '
            f'{(r.get("dsrc") or "").ljust(dsrc_w)}  '
            f'{(r.get("login") or "").ljust(login_w)}  '
            f'{(r.get("scheduler") or "").ljust(sched_w)}  '
            f'{r["observed_at"]}'
        )


def write_csv(rows: List[Dict[str, str]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "system","status","dsrc","login","scheduler","observed_at","img_src","raw_alt","source_url"
        ])
        w.writeheader()
        for r in rows:
            w.writerow(r)


def inventory_mapping(rows: List[Dict[str, str]]) -> Dict[str, Dict[str, Optional[str]]]:
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for r in rows:
        name = r["system"]
        if not name or name == "(unknown)":
            continue
        out[name] = {
            "dsrc": r.get("dsrc"),
            "login": r.get("login"),
            "scheduler": r.get("scheduler"),
        }
    return out


def render_markdown(row: Dict[str, Any], context: Optional[Dict[str, Any]]) -> str:
    context = context or {}
    system_name = row.get("system") or context.get("heading") or "Unknown System"
    description = (context.get("description") or "").strip()
    if not description:
        description = "No additional details were available on the source page."

    user_links, other_links = categorize_links(context.get("links") or [])
    facts = context.get("facts") or []
    partitions = context.get("partitions") or []
    limits = context.get("limits") or []
    html_tables = context.get("tables") or []

    def safe(value: Optional[str]) -> str:
        return value or "Unknown"

    def add_table_section(title: str, rows: List[Dict[str, str]]) -> List[str]:
        if not rows:
            return []
        block = [title, "| Field | Value |", "| --- | --- |"]
        for entry in rows:
            block.append(f"| {entry.get('label', 'N/A')} | {entry.get('value', '')} |")
        block.append("")
        return block

    lines: List[str] = [f"# {system_name}", ""]
    observed = row.get("observed_at") or "Unknown time"
    lines.append(f"_Status observed at {observed}_")
    lines.append("")

    lines.append("## Status Overview")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Status | {safe(row.get('status'))} |")
    lines.append(f"| DSRC | {safe(row.get('dsrc'))} |")
    lines.append(f"| Login Node | {safe(row.get('login'))} |")
    lines.append(f"| Scheduler | {safe(row.get('scheduler'))} |")
    lines.append(f"| Source | [{row.get('source_url', 'Unknown source')}]({row.get('source_url', '#')}) |")
    lines.append("")

    if row.get("img_src"):
        lines.append(f"![Status badge for {system_name}]({row['img_src']})")
        lines.append("")

    lines.append("## System Overview")
    lines.append(description)
    lines.append("")

    lines.extend(add_table_section("## System Details", facts))
    lines.extend(add_table_section("## Partitions & Queues", partitions))
    lines.extend(add_table_section("## Resource & Job Limits", limits))

    for idx, table in enumerate(html_tables, start=1):
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        if not headers or not rows:
            continue
        title = table.get("title") or f"Table {idx}"
        lines.append(f"## {title}")
        header_row = "| " + " | ".join(headers) + " |"
        divider = "| " + " | ".join(["---"] * len(headers)) + " |"
        lines.append(header_row)
        lines.append(divider)
        for row_vals in rows:
            padded = row_vals + [""] * (len(headers) - len(row_vals))
            lines.append("| " + " | ".join(padded) + " |")
        lines.append("")

    if user_links:
        lines.append("## User Guides & Training")
        for link in user_links:
            lines.append(f"- [{link['text']}]({link['href']})")
        lines.append("")

    if other_links:
        lines.append("## Additional References")
        for link in other_links:
            lines.append(f"- [{link['text']}]({link['href']})")
        lines.append("")

    lines.append("## Raw Status Data")
    lines.append("```json")
    lines.append(json.dumps(row, indent=2))
    lines.append("```")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_markdown_files(rows: List[Dict[str, Any]], soup: BeautifulSoup, output_dir: str, base_url: str) -> None:
    contexts = extract_system_contexts(soup, base_url)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, row in enumerate(rows, start=1):
        slug = slugify_system(row.get("system") or "") or f"system{idx}"
        context = None
        if slug:
            for ctx in contexts:
                if ctx.get("slug") == slug:
                    context = ctx
                    break
        if context is None and idx - 1 < len(contexts):
            context = contexts[idx - 1]
        md = render_markdown(row, context)
        (out_dir / f"{slug}.md").write_text(md, encoding="utf-8")


# --- CLI ----------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scrape HPC.MIL unclassified systems status + dynamic inventory.")
    parser.add_argument("--url", default=UNCLASSIFIED_URL, help="Page to scrape (default: unclassified systems page)")
    parser.add_argument("--json", action="store_true", help="Output full JSON (status + inventory fields)")
    parser.add_argument("--inventory-only", action="store_true", help="Output only {system:{dsrc,login,scheduler}} mapping")
    parser.add_argument("--fail-on-down", action="store_true", help="Exit with code 2 if any system not UP")
    parser.add_argument("--timeout", type=int, default=20, help="Network timeout in seconds")
    parser.add_argument("--ca-bundle", default=None, help="Path to a custom CA bundle PEM (e.g., DoD roots)")
    parser.add_argument("--insecure", action="store_true", default=True, help="Disable TLS certificate verification (NOT recommended)")
    parser.add_argument("--csv", dest="csv_path", help="Write results to CSV at this path")
    parser.add_argument(
        "--markdown-dir",
        default=DEFAULT_MARKDOWN_DIR,
        help=f"Write per-system Markdown files to this directory (default: {DEFAULT_MARKDOWN_DIR}). Pass an empty string to skip.",
    )
    args = parser.parse_args(argv)

    # Determine verification behavior
    verify = True
    if args.insecure:
        verify = False
    elif args.ca_bundle:
        verify = args.ca_bundle
    else:
        verify = DEFAULT_CA_BUNDLE

    try:
        rows, soup = fetch_status(args.url, timeout=args.timeout, verify=verify, headers={"User-Agent": "pw-status-scraper/1.3"})
    except requests.exceptions.SSLError as e:
        sys.stderr.write("TLS/SSL error: certificate verify failed. You may need to pass --ca-bundle pointing to the proper roots, or --insecure to bypass verification (NOT recommended).\n")
        sys.stderr.write(f"Details: {e}\n")
        return 3
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        return 3

    if args.csv_path:
        write_csv(rows, args.csv_path)

    markdown_dir = (args.markdown_dir or "").strip()
    if markdown_dir:
        write_markdown_files(rows, soup, markdown_dir, args.url)

    if args.inventory_only:
        print(json.dumps(inventory_mapping(rows), indent=2))
    elif args.json:
        print(json.dumps(rows, indent=2))
    else:
        print_table(rows)

    if args.fail_on_down:
        any_bad = any(r["status"] in {"DOWN", "DEGRADED", "MAINTENANCE", "UNKNOWN"} for r in rows)
        return 2 if any_bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
