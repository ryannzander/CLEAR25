#!/usr/bin/env python3
"""Compile the 2023 PurpleAir daily download into one compact JSON for the
plume tracker — so the animation runs off local historical data with ZERO live
API cost.

Input  (gitignored, local only):
  data/PurpleAir Download <date>/
    <sensor_index> 2023-01-01 2023-12-31 1440-Minute Average.csv   (x~523)
       columns: time_stamp, pm2.5_atm, latitude, longitude   (daily rows)
    station_coords.json   ->  { "<id>": {"latitude":.., "longitude":..}, ... }

Output (committed, served as a static asset):
  webapp/dashboard/static/dashboard/plume_2023.json
    { "year", "field", "dates":[365 "YYYY-MM-DD"], "bbox",
      "stations":[{"id","lat","lon"}...], "values":[[pm|null x365]...] }
  The frontend builds one daily frame per date: points = stations whose value
  for that day is non-null -> {lat, lon, pm}.

NOTE: the download carries only `pm2.5_atm` (no CF=1 / humidity), so the EPA /
Barkjohn correction can't be applied here — values are the uncorrected ATM
PM2.5 as downloaded. Pure stdlib (csv/json/glob/datetime).
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_OUT = REPO / "webapp" / "dashboard" / "static" / "dashboard" / "plume_2023.json"

YEAR = 2023
_QA_MAX_PM = 1000.0  # ATM PM2.5 above this (ug/m3) is almost certainly a fault.


def _find_input_dir(explicit):
    if explicit:
        return Path(explicit)
    matches = sorted(glob.glob(str(REPO / "data" / "PurpleAir Download*")))
    if not matches:
        raise SystemExit("No 'data/PurpleAir Download*' directory found; pass --input")
    return Path(matches[-1])


def _date_axis():
    d0, out = date(YEAR, 1, 1), []
    d = d0
    while d.year == YEAR:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build plume_2023.json from the PurpleAir 2023 download.")
    ap.add_argument("--input", default=None, help="download dir (default: latest data/PurpleAir Download*)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--write", action="store_true", help="write the file (otherwise dry-run prints a summary)")
    args = ap.parse_args(argv)

    in_dir = _find_input_dir(args.input)
    if not in_dir.is_dir():
        raise SystemExit(f"Input dir not found: {in_dir}")
    print(f"Reading {in_dir}")

    coords = {}
    cj = in_dir / "station_coords.json"
    if cj.is_file():
        raw = json.loads(cj.read_text(encoding="utf-8"))
        for sid, v in raw.items():
            try:
                coords[str(sid)] = (float(v["latitude"]), float(v["longitude"]))
            except (KeyError, TypeError, ValueError):
                continue
    print(f"  station_coords.json: {len(coords)} coords")

    dates = _date_axis()
    date_idx = {d: i for i, d in enumerate(dates)}
    n_days = len(dates)

    stations, values = [], []
    n_points = 0
    for csv_path in sorted(glob.glob(str(in_dir / "*.csv"))):
        sid = Path(csv_path).name.split(" ", 1)[0]  # leading token = sensor index
        row_vals = [None] * n_days
        lat = lon = None
        got = False
        try:
            with open(csv_path, newline="", encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                for row in rd:
                    ts = (row.get("time_stamp") or "")[:10]  # YYYY-MM-DD
                    di = date_idx.get(ts)
                    if di is None:
                        continue
                    try:
                        pm = float(row["pm2.5_atm"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if pm < 0 or pm > _QA_MAX_PM:
                        continue
                    row_vals[di] = round(pm, 1)
                    got = True
                    if lat is None:
                        try:
                            lat = float(row["latitude"]); lon = float(row["longitude"])
                        except (KeyError, TypeError, ValueError):
                            pass
        except OSError:
            continue

        if not got:
            continue
        if sid in coords:  # prefer the curated coords file; else the CSV's own lat/lon
            lat, lon = coords[sid]
        if lat is None or lon is None:
            continue

        stations.append({"id": sid, "lat": round(lat, 5), "lon": round(lon, 5)})
        values.append(row_vals)
        n_points += sum(1 for v in row_vals if v is not None)

    if not stations:
        raise SystemExit("No stations with data parsed — check the input directory.")

    lats = [s["lat"] for s in stations]
    lons = [s["lon"] for s in stations]
    bbox = {"nwlat": max(lats), "nwlng": min(lons), "selat": min(lats), "selng": max(lons)}

    payload = {
        "year": YEAR,
        "field": "pm2.5_atm",
        "note": "Uncorrected ATM PM2.5 (download has no CF=1/RH for EPA correction).",
        "source": in_dir.name,
        "dates": dates,
        "bbox": bbox,
        "stations": stations,
        "values": values,
    }

    out_str = json.dumps(payload, separators=(",", ":"))
    print(f"  stations: {len(stations)}  days: {n_days}  data points: {n_points:,}")
    print(f"  bbox: lat {bbox['selat']}..{bbox['nwlat']}  lon {bbox['nwlng']}..{bbox['selng']}")
    print(f"  output size: {len(out_str)/1024:.0f} KB")

    if args.write:
        Path(args.out).write_text(out_str, encoding="utf-8")
        print(f"  -> wrote {args.out}")
    else:
        print("  (dry run; pass --write to save)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
