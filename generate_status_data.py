#!/usr/bin/env python3
"""
Generate JSON data for the HPC status dashboard web page.

This script reuses the storage/hpc_status_scraper.py helpers to fetch the latest
system information and emits a summarized JSON payload that the static site can
consume. The default output path is
storage/hpc_status_site/public/data/status.json.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dashboard_data import determine_verify, generate_payload, write_payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render JSON payload used by the HPC status dashboard web page."
    )
    parser.add_argument("--url", default=None, help="Status page to scrape.")
    parser.add_argument(
        "--output",
        default=Path(__file__).resolve().parent / "public" / "data" / "status.json",
        type=Path,
        help="Where to write the JSON payload.",
    )
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds.")
    parser.add_argument("--insecure", action="store_true", default=True, help="Skip TLS verification.")
    parser.add_argument("--secure", dest="insecure", action="store_false", help="Require TLS verification.")
    parser.add_argument("--ca-bundle", type=str, help="Custom CA bundle.")
    args = parser.parse_args()

    output_path: Path = args.output
    verify = determine_verify(insecure=args.insecure, ca_bundle=args.ca_bundle)
    payload = generate_payload(
        url=args.url,
        timeout=args.timeout,
        verify=verify,
    )
    write_payload(payload, output_path)
    total = payload.get("summary", {}).get("total_systems", "unknown")
    print(f"Wrote {total} records to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
