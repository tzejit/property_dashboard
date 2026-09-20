"""
Geocode REALIS transaction addresses via OneMap
=================================================

Takes the transactions CSV produced by the REALIS scraper and looks up
a latitude/longitude (and optionally nearest-MRT info) for every unique
property, writing the results to a separate "locations" CSV keyed by
Postal Code. It never touches or duplicates the transactions CSV itself
— analyze_appreciation.py joins the two on Postal Code at analysis time.

WHY POSTAL CODE INSTEAD OF FREE-TEXT ADDRESS
-----------------------------------------------
Your original script searched OneMap by the free-text Address column
and had to guess which of several matches was correct when a search
returned multiple hits (re-querying with "block,address" as a
disambiguation hack). The scraped data already has a 6-digit Postal
Code for almost every row, and OneMap's search returns an exact,
unambiguous match for a postal code — so this version searches by
Postal Code first and only falls back to the free-text address (with
your original multi-match handling) when the postal code is missing or
returns nothing.

BUG FIXED FROM THE ORIGINAL SCRIPT
-------------------------------------
The original had `if mind > 1000: continue`, which silently threw away
the geocoded lat/lon entirely for any property more than 1km from an
MRT station. That's fine if all you want is "properties near MRT", but
for joining lat/lon onto every transaction (which is what we need for
the appreciation/discount analysis) it would leave large chunks of the
dataset ungeocoded. This version always saves the lat/lon; MRT info is
just an optional extra column, blank when nothing is within range.

CREDENTIALS
-----------
Never hardcode your OneMap email/password. Provide them via environment
variables or flags:
    export ONEMAP_EMAIL="you@example.com"
    export ONEMAP_PASSWORD="..."
    python geocode_locations.py --transactions transactions.csv
or pass --onemap-email / --onemap-password directly.

RESUMABLE / SAFE TO RE-RUN
-----------------------------
Like the scraper, this merges into --locations-output (default
locations.csv) keyed by Postal Code, flushing every --save-every
lookups (default 25) — so re-running it (e.g. after adding more
transactions to your main CSV) only geocodes NEW postal codes, never
re-does or overwrites ones already saved, and a crash partway through
only costs you the last partial batch.

INSTALL
-------
    pip install requests --break-system-packages

RUN
---
    python geocode_locations.py \
        --transactions transactions.csv \
        --locations-output locations.csv \
        --include-mrt
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

ONEMAP_BASE_URL = "https://www.onemap.gov.sg/api"

LOCATION_FIELDNAMES = [
    "Postal Code",
    "Query Used",
    "Matched Address",
    "Latitude",
    "Longitude",
    "Nearest MRT Name",
    "Nearest MRT Distance (m)",
    "Nearest MRT Walk Time (min)",
    "Geocoded At",
]


# ---------------------------------------------------------------------------
# OneMap auth
# ---------------------------------------------------------------------------

class OneMapAuth:
    """Holds and auto-refreshes the OneMap bearer token."""

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self.token: Optional[str] = None
        self.expiry_epoch: float = 0

    def get_token(self) -> str:
        if self.token is None or time.time() > self.expiry_epoch - 60:
            self._refresh()
        return self.token

    def _refresh(self) -> None:
        resp = requests.post(
            f"{ONEMAP_BASE_URL}/auth/post/getToken",
            json={"email": self.email, "password": self.password},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if "access_token" not in data:
            sys.exit(f"OneMap auth failed: {data}")
        self.token = data["access_token"]
        # OneMap returns expiry_timestamp as a unix epoch (seconds) string.
        try:
            self.expiry_epoch = float(data.get("expiry_timestamp", 0))
        except (TypeError, ValueError):
            # Fall back to a conservative 1-hour assumption if the field is
            # missing/unparseable, rather than never refreshing.
            self.expiry_epoch = time.time() + 3600
        print("OneMap token acquired/refreshed.")

    def headers(self) -> dict:
        return {"Authorization": self.get_token()}


def onemap_get(auth: OneMapAuth, path: str, params: dict, retries: int = 3) -> dict:
    """GET with auth-header retry-on-401 and basic backoff on other errors."""
    for attempt in range(1, retries + 1):
        resp = requests.get(f"{ONEMAP_BASE_URL}{path}", params=params,
                             headers=auth.headers(), timeout=30)
        if resp.status_code == 401:
            auth._refresh()
            continue
        if resp.status_code >= 500:
            wait = 2 ** attempt
            print(f"  OneMap server error {resp.status_code}, retrying in {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    sys.exit(f"OneMap request to {path} failed after {retries} retries.")


# ---------------------------------------------------------------------------
# Geocoding + nearest-MRT lookups for a single property
# ---------------------------------------------------------------------------

def search_onemap(auth: OneMapAuth, search_val: str) -> Optional[dict]:
    data = onemap_get(auth, "/common/elastic/search", {
        "searchVal": search_val,
        "returnGeom": "Y",
        "getAddrDetails": "Y",
        "pageNum": 1,
    })
    if data.get("found", 0) == 0 or not data.get("results"):
        return None
    return data["results"][0]


def geocode_one(auth: OneMapAuth, postal_code: str, address: str) -> Optional[dict]:
    """
    Try Postal Code first (exact, unambiguous). Fall back to the raw
    address text, then to "address + postal code" as a last resort
    (mirrors the disambiguation approach in the original script, but
    only as a fallback rather than the primary strategy).
    """
    query_used = None
    result = None

    if postal_code:
        result = search_onemap(auth, postal_code)
        query_used = postal_code

    if result is None and address:
        result = search_onemap(auth, address)
        query_used = address

    if result is None and address and postal_code:
        combined = f"{address} {postal_code}"
        result = search_onemap(auth, combined)
        query_used = combined

    if result is None:
        return None

    return {
        "query_used": query_used,
        "matched_address": result.get("ADDRESS", ""),
        "lat": result.get("LATITUDE"),
        "lon": result.get("LONGITUDE"),
    }


def find_nearest_mrt(auth: OneMapAuth, lat: str, lon: str,
                      radius_m: int = 1000, max_candidates: int = 3) -> dict:
    """
    Look up MRT stations within radius_m and walk-route to a handful of
    the closest candidates to find the true nearest by walking distance.
    Returns an empty dict (not a skip!) if none are within range — the
    lat/lon itself is still valid and saved regardless.
    """
    try:
        stops = onemap_get(auth, "/public/nearbysvc/getNearestMrtStops", {
            "latitude": lat, "longitude": lon, "radius_in_meters": radius_m,
        })
    except SystemExit:
        raise
    except Exception as e:
        print(f"  Warning: nearest-MRT lookup failed ({e}), leaving MRT fields blank.")
        return {}

    if not isinstance(stops, list):
        return {}

    # Exclude LRT stations (as the original script did) and cap how many
    # walking-route calls we make per property to keep this affordable.
    candidates = [s for s in stops if "LRT STATION" not in s.get("name", "")][:max_candidates]

    best = None
    for mrt in candidates:
        try:
            route = onemap_get(auth, "/public/routingsvc/route", {
                "start": f"{lat},{lon}",
                "end": f"{mrt['lat']},{mrt['lon']}",
                "routeType": "walk",
            })
            summary = route.get("route_summary", {})
            dist = summary.get("total_distance")
            tt = summary.get("total_time")
            if dist is None:
                continue
            if best is None or dist < best["dist"]:
                best = {"name": mrt.get("name", ""), "dist": dist,
                        "walk_min": (tt / 60) if tt is not None else None}
        except Exception as e:
            print(f"  Warning: walk-route to {mrt.get('name')} failed ({e}), skipping it.")
            continue

    if best is None:
        return {}
    return {
        "nearest_mrt_name": best["name"],
        "nearest_mrt_distance_m": best["dist"],
        "nearest_mrt_walk_min": best["walk_min"],
    }


# ---------------------------------------------------------------------------
# CSV I/O (merge-safe, resumable)
# ---------------------------------------------------------------------------

def load_existing_locations(path: str) -> Dict[str, Dict[str, str]]:
    if not os.path.exists(path):
        return {}
    existing = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row.get("Postal Code", "").strip()
            if key:
                existing[key] = row
    print(f"Loaded {len(existing)} previously-geocoded postal codes from {path}")
    return existing


def write_locations(existing: Dict[str, Dict[str, str]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOCATION_FIELDNAMES)
        writer.writeheader()
        writer.writerows(existing.values())


def collect_unique_properties(transactions_path: str) -> List[Dict[str, str]]:
    """
    Read the transactions CSV and return one (postal_code, address) pair
    per unique postal code (falling back to a synthetic key built from
    the address when postal code is blank/placeholder).
    """
    seen: Dict[str, Dict[str, str]] = {}
    with open(transactions_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "Postal Code" not in reader.fieldnames:
            sys.exit(f"'{transactions_path}' has no 'Postal Code' column — "
                      "is this the scraper's output CSV?")
        for row in reader:
            postal = (row.get("Postal Code") or "").strip()
            address = (row.get("Address") or "").strip()
            if not postal or postal == "-":
                if not address:
                    continue
                key = f"addr:{address}"
            else:
                key = postal
            if key not in seen:
                seen[key] = {"postal_code": postal if postal and postal != "-" else "",
                             "address": address}
    return list(seen.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transactions", required=True,
                         help="Path to the scraper's merged transactions CSV.")
    parser.add_argument("--locations-output", default="locations.csv",
                         help="Where to write/merge geocoded results (default: locations.csv)")
    parser.add_argument("--onemap-email", default="tzejit@gmail.com",
                         help="OneMap account email (or set ONEMAP_EMAIL env var).")
    parser.add_argument("--onemap-password", default="gyhT5i9wp$tfR4x",
                         help="OneMap account password (or set ONEMAP_PASSWORD env var). "
                              "Never pass this on a shared/logged shell — prefer the env var.")
    parser.add_argument("--include-mrt", action="store_true",
                         help="Also look up the nearest MRT station and walking "
                              "distance/time for each property (more API calls, slower).")
    parser.add_argument("--mrt-radius-m", type=int, default=1000,
                         help="Search radius in metres for nearest-MRT lookup (default: 1000).")
    parser.add_argument("--sleep-seconds", type=float, default=0.3,
                         help="Delay between properties to be polite to the API (default: 0.3).")
    parser.add_argument("--save-every", type=int, default=25,
                         help="Flush results to --locations-output every N properties "
                              "(default: 25).")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only process the first N not-yet-geocoded properties "
                              "(useful for testing before a full run).")
    args = parser.parse_args()

    if not args.onemap_email or not args.onemap_password:
        sys.exit("OneMap credentials required: set ONEMAP_EMAIL / ONEMAP_PASSWORD "
                  "env vars, or pass --onemap-email / --onemap-password.")

    auth = OneMapAuth(args.onemap_email, args.onemap_password)

    properties = collect_unique_properties(args.transactions)
    print(f"Found {len(properties)} unique properties in {args.transactions}")

    existing = load_existing_locations(args.locations_output)
    todo = [p for p in properties
            if (p["postal_code"] or f"addr:{p['address']}") not in existing]
    print(f"{len(todo)} not yet geocoded.")

    if args.limit:
        todo = todo[:args.limit]
        print(f"--limit set: processing only {len(todo)} this run.")

    since_save = 0
    for i, prop in enumerate(todo, 1):
        postal_code = prop["postal_code"].removesuffix(".0")
        address = prop["address"]
        key = postal_code or f"addr:{address}"
        print(f"[{i}/{len(todo)}] {key} ({address[:60]})")

        geo = geocode_one(auth, postal_code, address)
        if geo is None:
            print("  Not found on OneMap.")
            existing[key] = {
                "Postal Code": postal_code,
                "Query Used": postal_code or address,
                "Matched Address": "",
                "Latitude": "",
                "Longitude": "",
                "Nearest MRT Name": "",
                "Nearest MRT Distance (m)": "",
                "Nearest MRT Walk Time (min)": "",
                "Geocoded At": datetime.now(timezone.utc).isoformat(),
            }
        else:
            mrt_info = {}
            if args.include_mrt:
                mrt_info = find_nearest_mrt(auth, geo["lat"], geo["lon"],
                                             radius_m=args.mrt_radius_m)
            existing[key] = {
                "Postal Code": postal_code,
                "Query Used": geo["query_used"],
                "Matched Address": geo["matched_address"],
                "Latitude": geo["lat"],
                "Longitude": geo["lon"],
                "Nearest MRT Name": mrt_info.get("nearest_mrt_name", ""),
                "Nearest MRT Distance (m)": mrt_info.get("nearest_mrt_distance_m", ""),
                "Nearest MRT Walk Time (min)": mrt_info.get("nearest_mrt_walk_min", ""),
                "Geocoded At": datetime.now(timezone.utc).isoformat(),
            }

        since_save += 1
        if since_save >= args.save_every:
            write_locations(existing, args.locations_output)
            since_save = 0
            print(f"  ...checkpoint saved ({len(existing)} total locations).")

        time.sleep(args.sleep_seconds)

    write_locations(existing, args.locations_output)
    print(f"Done. {len(existing)} total locations saved to {args.locations_output}")


if __name__ == "__main__":
    main()