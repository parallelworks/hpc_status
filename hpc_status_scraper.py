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
from typing import Dict, List, Optional
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


def fetch_status(url: str, timeout: int, verify, headers: Optional[dict] = None) -> List[Dict[str, str]]:
    session = _make_session(verify=verify, headers=headers, timeout=timeout)
    if verify is False:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    resp = session.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows: List[Dict[str, str]] = []
    now_iso = dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

    imgs = soup.select("img.statusImg")
    if not imgs:
        imgs = soup.find_all("img", attrs={"class": re.compile(r".*status.*", re.IGNORECASE)})

    for img in imgs:
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
    return rows


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
        rows = fetch_status(args.url, timeout=args.timeout, verify=verify, headers={"User-Agent": "pw-status-scraper/1.3"})
    except requests.exceptions.SSLError as e:
        sys.stderr.write("TLS/SSL error: certificate verify failed. You may need to pass --ca-bundle pointing to the proper roots, or --insecure to bypass verification (NOT recommended).\n")
        sys.stderr.write(f"Details: {e}\n")
        return 3
    except Exception as e:
        sys.stderr.write(f"Error: {e}\n")
        return 3

    if args.csv_path:
        write_csv(rows, args.csv_path)

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
