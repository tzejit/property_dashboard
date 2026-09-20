"""
Geocode HDB block addresses via OneMap
=======================================

Reads unique (block, street_name) pairs from the HDB resale CSV,
geocodes each via OneMap, and writes lat/lon + nearest MRT to
hdb_locations.csv keyed by project_name (= "{block} {street_name}").

Resumable: re-running skips already-geocoded entries.

RUN
---
    python pipeline/geocode_hdb.py \\
        --hdb data/hdb_resale.csv \\
        --output data/hdb_locations.csv \\
        --include-mrt

Pass --limit N to test with the first N blocks before a full run.
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd

# Reuse auth and lookup helpers from the existing geocoder.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from geocode import OneMapAuth, search_onemap, find_nearest_mrt  # noqa: E402

HDB_LOC_FIELDNAMES = [
    "project_name",
    "latitude",
    "longitude",
    "nearest_mrt",
    "nearest_mrt_distance_m",
    "geocoded_at",
]


def load_unique_hdb_blocks(hdb_csv: str) -> List[Dict[str, str]]:
    """Return one dict per unique (block, street_name) combination."""
    df = pd.read_csv(hdb_csv, dtype=str, usecols=["block", "street_name"])
    df["block"]        = df["block"].str.strip()
    df["street_name"]  = df["street_name"].str.strip()
    df["project_name"] = df["block"] + " " + df["street_name"]
    unique = (
        df.drop_duplicates(subset=["project_name"])[["project_name"]]
        .reset_index(drop=True)
    )
    return unique.to_dict("records")


def load_existing(path: str) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    existing: Dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k = row.get("project_name", "").strip()
            if k:
                existing[k] = row
    print(f"Loaded {len(existing)} previously-geocoded HDB blocks from {path}.")
    return existing


def write_locations(existing: Dict[str, dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HDB_LOC_FIELDNAMES)
        writer.writeheader()
        writer.writerows(existing.values())


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--hdb", default="data/hdb_resale.csv",
        help="HDB resale flat prices CSV (default: data/hdb_resale.csv).",
    )
    parser.add_argument(
        "--output", default="data/hdb_locations.csv",
        help="Output locations CSV (default: data/hdb_locations.csv).",
    )
    parser.add_argument(
        "--onemap-email",
        default=os.environ.get("ONEMAP_EMAIL", "tzejit@gmail.com"),
        help="OneMap account email (or set ONEMAP_EMAIL env var).",
    )
    parser.add_argument(
        "--onemap-password",
        default=os.environ.get("ONEMAP_PASSWORD", "gyhT5i9wp$tfR4x"),
        help="OneMap account password (or set ONEMAP_PASSWORD env var).",
    )
    parser.add_argument(
        "--include-mrt", action="store_true",
        help="Also look up nearest MRT walking distance (extra API calls).",
    )
    parser.add_argument(
        "--mrt-radius-m", type=int, default=1500,
        help="Nearest-MRT search radius in metres (default: 1500).",
    )
    parser.add_argument(
        "--sleep-seconds", type=float, default=0.3,
        help="Delay between API requests (default: 0.3 s).",
    )
    parser.add_argument(
        "--save-every", type=int, default=25,
        help="Checkpoint every N lookups (default: 25).",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Process only the first N not-yet-geocoded blocks. Useful for testing.",
    )
    args = parser.parse_args()

    if not args.onemap_email or not args.onemap_password:
        sys.exit(
            "OneMap credentials required. "
            "Set ONEMAP_EMAIL / ONEMAP_PASSWORD, or pass --onemap-email / --onemap-password."
        )

    auth   = OneMapAuth(args.onemap_email, args.onemap_password)
    blocks = load_unique_hdb_blocks(args.hdb)
    print(f"Found {len(blocks)} unique HDB blocks in {args.hdb}.")

    existing = load_existing(args.output)
    todo = [b for b in blocks if b["project_name"] not in existing]
    print(f"{len(todo)} not yet geocoded.")

    if args.limit:
        todo = todo[:args.limit]
        print(f"--limit: processing {len(todo)} this run.")

    since_save = 0
    for i, b in enumerate(todo, 1):
        name = b["project_name"]
        print(f"[{i}/{len(todo)}] {name}", end="  ", flush=True)

        # Try the full block + street name first.
        result = search_onemap(auth, name)

        # Fall back to street name only if the block number confuses the search.
        if result is None:
            street = name.split(" ", 1)[1] if " " in name else name
            result = search_onemap(auth, street)

        if result is None:
            print("not found.")
            existing[name] = {
                "project_name":           name,
                "latitude":               "",
                "longitude":              "",
                "nearest_mrt":            "",
                "nearest_mrt_distance_m": "",
                "geocoded_at":            datetime.now(timezone.utc).isoformat(),
            }
        else:
            lat = result.get("LATITUDE", "")
            lon = result.get("LONGITUDE", "")
            mrt_info: dict = {}
            if args.include_mrt and lat and lon:
                mrt_info = find_nearest_mrt(
                    auth, lat, lon, radius_m=args.mrt_radius_m,
                )
            mrt_name = mrt_info.get("nearest_mrt_name", "")
            mrt_dist = mrt_info.get("nearest_mrt_distance_m", "")
            print(f"({lat}, {lon})  MRT: {mrt_name or '—'} {f'{mrt_dist} m' if mrt_dist else ''}")
            existing[name] = {
                "project_name":           name,
                "latitude":               lat,
                "longitude":              lon,
                "nearest_mrt":            mrt_name,
                "nearest_mrt_distance_m": mrt_dist,
                "geocoded_at":            datetime.now(timezone.utc).isoformat(),
            }

        since_save += 1
        if since_save >= args.save_every:
            write_locations(existing, args.output)
            since_save = 0
            print(f"  ↳ checkpoint saved ({len(existing)} total).")

        time.sleep(args.sleep_seconds)

    write_locations(existing, args.output)
    print(f"\nDone. {len(existing)} HDB block locations saved to {args.output}.")


if __name__ == "__main__":
    main()
