#!/usr/bin/env python3
"""Discover PurpleAir sensor indexes PER YEAR for the PurpleAir "Download Data" tool.

WHY: the smoke ML model needs PurpleAir coverage for 2021-2025. We already have the
2024 + 2025 downloads; this produces the sensor-index lists for the MISSING years
(2021, 2022, 2023) so they can be pasted straight into the PurpleAir Download tool.

It MAXIMIZES the sensor set per year by including sensors that are OFFLINE today:
the /v1/sensors endpoint defaults to a ~1-week `max_age` filter, so a sensor that
ran in 2021 but has since been retired would be invisible. We pass `max_age=0`
(match sensors of ANY age) to sweep the full all-time roster, then window per year.

A sensor is counted "available in year Y" when its lifetime OVERLAPS that calendar
year (UTC):
    date_created <= Dec 31 23:59:59 of Y   AND   last_seen >= Jan 1 00:00:00 of Y

Region: defaults to the SAME Ontario+Quebec footprint the existing 2024/2025 pull
covers (observed extent lat 42.07..62.48, lon -94.55..-57.20), padded slightly, so
all five years share ONE spatial footprint. Override with --bbox.

Endpoint (verified live; identical shape to purpleair_history.py's discovery):
    GET https://api.purpleair.com/v1/sensors
        ?fields=latitude,longitude,date_created,last_seen,channel_flags,confidence
        &nwlng&nwlat&selng&selat&max_age=0
        header: X-API-Key
    -> {"fields":[...], "data":[[...], ...]}   # read columns by the returned `fields`
       (PurpleAir prepends `sensor_index` and may reorder the rest — never assume order)

COST: exactly ONE /v1/sensors metadata call (not history). Discovery is far cheaper
than history pulls, but max_age=0 over a large box returns the full all-time roster,
so it does bill some points. Run once. Use --dry-run to see the region/params first.

Auth: set PURPLEAIR_API_KEY in the environment (or the project .env, then export).

Output (gitignored: sensor_indexes/):
    sensors_<Y>.txt   comma-separated sensor indexes -> paste into the Download tool
    summary.json      per-year counts + bbox + params used

All five years (2021-2025) share the SAME ON+QC footprint here, so 2024/2025 get
the same all-time, offline-inclusive coverage as the missing years (rather than
whatever the original manual pull happened to capture).

Example:
    PURPLEAIR_API_KEY=... python scripts/gen_sensor_indexes.py
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SENSORS_URL = "https://api.purpleair.com/v1/sensors"
DEFAULT_OUT = REPO / "sensor_indexes"

# Ontario + Quebec footprint matching the existing 2024/2025 download extent
# (observed lat 42.07..62.48, lon -94.55..-57.20), padded a touch to catch
# sensors just outside the recorded corners.
DEFAULT_BBOX = {"nwlat": 63.5, "nwlng": -96.0, "selat": 41.5, "selng": -56.5}

# Fields requested (sensor_index is always returned first by PurpleAir regardless).
# location_type: 0 = outdoor, 1 = indoor. Indoor units measure indoor air, not
# ambient smoke, so they are excluded by default (the PurpleAir map/download tool
# shows outdoor-only too, which is why the manual 2024/2025 pull was ~500 sensors).
FIELDS = "latitude,longitude,date_created,last_seen,channel_flags,confidence,location_type"


def _get(url, key, params, timeout=90):
    resp = requests.get(url, headers={"X-API-Key": key}, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _year_window(year):
    """(start_epoch, end_epoch) inclusive bounds for a calendar year in UTC."""
    start = int(datetime(year, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp())
    end = int(datetime(year, 12, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    return start, end


def fetch_roster(key, bbox, max_age):
    """One /v1/sensors call -> list of dicts for every sensor in the bbox."""
    data = _get(SENSORS_URL, key, {
        "fields": FIELDS,
        "nwlng": bbox["nwlng"], "nwlat": bbox["nwlat"],
        "selng": bbox["selng"], "selat": bbox["selat"],
        "max_age": max_age,
    })
    idx = {n: i for i, n in enumerate(data.get("fields", []))}
    for req in ("sensor_index", "date_created", "last_seen"):
        if req not in idx:
            raise SystemExit(f"PurpleAir response missing '{req}'; got fields {list(idx)}")
    out = []
    for r in data.get("data", []):
        try:
            out.append({
                "id": r[idx["sensor_index"]],
                "lat": r[idx["latitude"]] if "latitude" in idx else None,
                "lon": r[idx["longitude"]] if "longitude" in idx else None,
                "date_created": r[idx["date_created"]],
                "last_seen": r[idx["last_seen"]],
                "channel_flags": r[idx["channel_flags"]] if "channel_flags" in idx else None,
                "confidence": r[idx["confidence"]] if "confidence" in idx else None,
                "location_type": r[idx["location_type"]] if "location_type" in idx else None,
            })
        except (IndexError, TypeError):
            continue
    return out


def sensors_for_year(roster, year, min_confidence, good_channels_only, include_indoor):
    """Sensor ids whose lifetime overlaps `year` (+ optional QA), sorted ascending."""
    start, end = _year_window(year)
    ids = []
    for s in roster:
        dc, ls = s["date_created"], s["last_seen"]
        if not isinstance(dc, (int, float)) or not isinstance(ls, (int, float)):
            continue
        if dc > end or ls < start:          # no lifetime overlap with the year
            continue
        if not include_indoor and s["location_type"] not in (0, None):
            continue                        # drop indoor (location_type=1); keep 0/unknown
        if good_channels_only and s["channel_flags"] not in (0, None):
            continue
        if min_confidence is not None:
            c = s["confidence"]
            if c is None or c < min_confidence:
                continue
        ids.append(int(s["id"]))
    return sorted(set(ids))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", default="2021,2022,2023,2024,2025",
                    help="comma-separated years to build lists for (default 2021-2025)")
    ap.add_argument("--bbox", default=None,
                    help="nwlat,nwlng,selat,selng (default = ON+QC footprint of the 2024/2025 pull)")
    ap.add_argument("--max-age", type=int, default=0,
                    help="PurpleAir max_age seconds; 0 = all-time roster (default, maximizes coverage)")
    ap.add_argument("--min-confidence", type=int, default=None,
                    help="drop sensors below this confidence NOW (default off; the download/cleaning step does QA)")
    ap.add_argument("--good-channels-only", action="store_true",
                    help="drop sensors whose channel_flags != 0 NOW (default off; maximize coverage)")
    ap.add_argument("--include-indoor", action="store_true",
                    help="include indoor sensors (location_type=1); default OUTDOOR-ONLY, correct for ambient smoke")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output directory (default sensor_indexes/)")
    ap.add_argument("--dry-run", action="store_true", help="print region/params and exit without any API call")
    args = ap.parse_args(argv)

    try:
        years = [int(y) for y in args.years.replace(" ", "").split(",") if y]
    except ValueError:
        ap.error("--years must be comma-separated integers, e.g. 2021,2022,2023")
    if not years:
        ap.error("--years is empty")

    if args.bbox:
        try:
            nwlat, nwlng, selat, selng = (float(x) for x in args.bbox.split(","))
            bbox = {"nwlat": nwlat, "nwlng": nwlng, "selat": selat, "selng": selng}
        except ValueError:
            ap.error("--bbox must be nwlat,nwlng,selat,selng")
    else:
        bbox = DEFAULT_BBOX

    print(f"Years : {years}")
    print(f"Region: nwlat={bbox['nwlat']} nwlng={bbox['nwlng']} "
          f"selat={bbox['selat']} selng={bbox['selng']}")
    print(f"max_age={args.max_age} (0 = all-time), min_confidence={args.min_confidence}, "
          f"good_channels_only={args.good_channels_only}, "
          f"outdoor_only={not args.include_indoor}")
    if args.dry_run:
        print("[dry-run] no API call made.")
        return 0

    key = os.environ.get("PURPLEAIR_API_KEY", "")
    if not key:
        ap.error("PURPLEAIR_API_KEY not set in the environment")

    print("Fetching all-time sensor roster (one /v1/sensors call)...")
    roster = fetch_roster(key, bbox, args.max_age)
    print(f"  -> {len(roster)} sensors in region")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bbox": bbox,
        "max_age": args.max_age,
        "min_confidence": args.min_confidence,
        "good_channels_only": args.good_channels_only,
        "outdoor_only": not args.include_indoor,
        "roster_size": len(roster),
        "years": {},
    }
    for y in years:
        ids = sensors_for_year(roster, y, args.min_confidence,
                               args.good_channels_only, args.include_indoor)
        txt = out_dir / f"sensors_{y}.txt"
        txt.write_text(",".join(str(i) for i in ids), encoding="utf-8")
        summary["years"][str(y)] = {"count": len(ids), "file": txt.name}
        print(f"  {y}: {len(ids):>5} sensors -> {txt}")

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nDone. Lists + summary.json in {out_dir}")
    print("Paste each sensors_<year>.txt into the PurpleAir Download tool for that year's range.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
