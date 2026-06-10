#!/usr/bin/env python3
"""Pull historical PurpleAir PM2.5 for CLEAR fusion backtesting.

WHY: to fold PurpleAir into the detection network we first have to PROVE it
helps — by replaying it over past smoke events (2023/2024, where PurpleAir is
dense). This script pulls per-sensor hourly history over chosen date windows,
applies the same EPA/Barkjohn correction the live pipeline uses, and writes CSV
ready for regression / backtesting. It does NOT touch the web app or its DB.

COST WARNING: the PurpleAir API bills *historical* requests in "points" (base +
per-row); your own sensors are free, everyone else's costs credits. Keep pulls
small and targeted: a curated sensor set over the event windows, at hourly
averaging. Use --list-only first to see what you'd pull (free-ish discovery),
and --max-sensors to cap it. See https://api.purpleair.com / the API pricing
thread before pulling large ranges.

Endpoints (verified live):
  discovery: GET /v1/sensors?fields=...&nwlng&nwlat&selng&selat        (bbox)
  history:   GET /v1/sensors/{id}/history?start_timestamp&end_timestamp
                 &average&fields   ->  {"fields":[...], "data":[[...],...]}
The history rows come back UNSORTED and always include time_stamp first.

Auth: set PURPLEAIR_API_KEY in the environment.

Example:
  PURPLEAIR_API_KEY=... python scripts/purpleair_history.py \
      --start 2024-06-01 --end 2024-06-15 --max-sensors 30 \
      --out purpleair_history/2024-jun
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SENSORS_URL = "https://api.purpleair.com/v1/sensors"
HISTORY_URL = "https://api.purpleair.com/v1/sensors/{sid}/history"

# Default discovery box = Ontario (matches services/purpleair.py ONTARIO_BBOX).
ONTARIO_BBOX = {"nwlat": 56.9, "nwlng": -95.2, "selat": 41.6, "selng": -74.3}

# Max span PER history request, by averaging interval (minutes) -> days. From the
# PurpleAir documented limits; we chunk longer ranges into these windows.
_WINDOW_DAYS = {0: 2, 10: 3, 30: 7, 60: 14, 360: 90, 1440: 365}

# Barkjohn et al. (2021) US-wide / EPA correction (same as the live pipeline).
_EPA_SLOPE, _EPA_RH_COEF, _EPA_INTERCEPT = 0.524, 0.0862, 5.75


def _epa_correct(cf1, rh):
    if cf1 is None or rh is None:
        return None
    v = _EPA_SLOPE * cf1 - _EPA_RH_COEF * rh + _EPA_INTERCEPT
    return round(v if v > 0 else 0.0, 1)


def _to_ts(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _get(url, key, params, timeout=60):
    resp = requests.get(url, headers={"X-API-Key": key}, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def discover_sensors(key, bbox, min_confidence, max_sensors):
    """Find candidate sensors in the bbox, best (oldest + healthy) first."""
    data = _get(SENSORS_URL, key, {
        "fields": "latitude,longitude,confidence,channel_flags,last_seen,date_created",
        "nwlng": bbox["nwlng"], "nwlat": bbox["nwlat"],
        "selng": bbox["selng"], "selat": bbox["selat"],
    })
    idx = {n: i for i, n in enumerate(data.get("fields", []))}
    rows = data.get("data", [])
    cand = []
    for r in rows:
        try:
            if r[idx["channel_flags"]] != 0:
                continue
            if r[idx["confidence"]] is None or r[idx["confidence"]] < min_confidence:
                continue
            cand.append({
                "id": r[idx["sensor_index"]],
                "lat": r[idx["latitude"]],
                "lon": r[idx["longitude"]],
                "confidence": r[idx["confidence"]],
                "date_created": r[idx.get("date_created", -1)] if "date_created" in idx else 0,
            })
        except (IndexError, KeyError, TypeError):
            continue
    # Oldest sensors first (longest history), then highest confidence.
    cand.sort(key=lambda s: (s["date_created"] or 0, -s["confidence"]))
    return cand[:max_sensors]


def pull_history(key, sid, start_ts, end_ts, average, fields):
    """Pull one sensor's history across the date range, chunked + time-sorted."""
    win = _WINDOW_DAYS.get(average, 14) * 86400
    rows = []
    cols = None
    cursor = start_ts
    while cursor < end_ts:
        chunk_end = min(cursor + win, end_ts)
        try:
            data = _get(HISTORY_URL.format(sid=sid), key, {
                "start_timestamp": cursor, "end_timestamp": chunk_end,
                "average": average, "fields": fields,
            })
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            print(f"    ! sensor {sid} {datetime.utcfromtimestamp(cursor):%Y-%m-%d}: "
                  f"{exc.__class__.__name__} (status={status})", file=sys.stderr)
            cursor = chunk_end
            time.sleep(1.0)
            continue
        if cols is None:
            cols = data.get("fields", [])
        rows.extend(data.get("data", []))
        cursor = chunk_end
        time.sleep(0.4)  # be gentle on the API
    if not cols:
        return cols, []
    ti = cols.index("time_stamp") if "time_stamp" in cols else 0
    rows.sort(key=lambda r: r[ti])
    return cols, rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pull historical PurpleAir PM2.5 for backtesting.")
    ap.add_argument("--start", required=True, help="UTC date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="UTC date YYYY-MM-DD (exclusive)")
    ap.add_argument("--bbox", default=None, help="nwlat,nwlng,selat,selng (default Ontario)")
    ap.add_argument("--sensors", default=None, help="explicit sensor ids 'a,b,c' (skip discovery)")
    ap.add_argument("--average", type=int, default=60, choices=sorted(_WINDOW_DAYS),
                    help="averaging minutes (60=hourly, recommended)")
    ap.add_argument("--fields", default="pm2.5_cf_1,pm2.5_atm,humidity")
    ap.add_argument("--out", default="purpleair_history", help="output directory")
    ap.add_argument("--max-sensors", type=int, default=40)
    ap.add_argument("--min-confidence", type=int, default=80)
    ap.add_argument("--list-only", action="store_true", help="discover + list sensors, pull nothing")
    args = ap.parse_args(argv)

    key = os.environ.get("PURPLEAIR_API_KEY", "")
    if not key:
        ap.error("PURPLEAIR_API_KEY not set in the environment")

    if args.bbox:
        try:
            nwlat, nwlng, selat, selng = (float(x) for x in args.bbox.split(","))
            bbox = {"nwlat": nwlat, "nwlng": nwlng, "selat": selat, "selng": selng}
        except ValueError:
            ap.error("--bbox must be nwlat,nwlng,selat,selng")
    else:
        bbox = ONTARIO_BBOX

    start_ts, end_ts = _to_ts(args.start), _to_ts(args.end)
    if end_ts <= start_ts:
        ap.error("--end must be after --start")

    if args.sensors:
        sensors = [{"id": int(s), "lat": None, "lon": None, "confidence": None}
                   for s in args.sensors.replace(" ", "").split(",") if s]
    else:
        print(f"Discovering sensors in bbox {bbox} (conf>={args.min_confidence}, "
              f"max {args.max_sensors})...")
        sensors = discover_sensors(key, bbox, args.min_confidence, args.max_sensors)
    print(f"  -> {len(sensors)} sensors")

    span_days = (end_ts - start_ts) / 86400.0
    est_rows = len(sensors) * span_days * (1440 / args.average)
    print(f"Window {args.start}..{args.end} ({span_days:.0f} days) @ {args.average}-min avg "
          f"-> ~{est_rows:,.0f} rows total (POINTS COST scales with rows).")

    if args.list_only:
        for s in sensors:
            print(f"  sensor {s['id']}  conf={s.get('confidence')}  "
                  f"({s.get('lat')},{s.get('lon')})")
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    combined = out_dir / f"purpleair_{args.start}_{args.end}_{args.average}m.csv"

    written = 0
    with open(combined, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sensor_index", "time_utc", "pm2.5_cf_1", "pm2.5_atm",
                    "humidity", "pm2.5_corrected"])
        for n, s in enumerate(sensors, 1):
            sid = s["id"]
            print(f"[{n}/{len(sensors)}] sensor {sid}...")
            cols, rows = pull_history(key, sid, start_ts, end_ts, args.average, args.fields)
            if not rows:
                continue
            ci = {c: i for i, c in enumerate(cols)}
            for r in rows:
                ts = r[ci.get("time_stamp", 0)]
                cf1 = r[ci["pm2.5_cf_1"]] if "pm2.5_cf_1" in ci else None
                atm = r[ci["pm2.5_atm"]] if "pm2.5_atm" in ci else None
                rh = r[ci["humidity"]] if "humidity" in ci else None
                w.writerow([
                    sid,
                    datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    cf1, atm, rh, _epa_correct(cf1, rh),
                ])
                written += 1

    print(f"\nDone. {written:,} rows from {len(sensors)} sensors -> {combined}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
