#!/usr/bin/env python3
"""Extract ECCC NAPS 2023 reference PM2.5 -> daily plume JSON (for TESTING).

Pulls the National Air Pollution Surveillance (NAPS) 2023 hourly PM2.5 file from
ECCC's open-data catalogue, averages it to daily values, and writes the same
compact format as the PurpleAir `plume_2023.json` so the /plan/ tracker can show
ECCC reference stations. Everything lands in the gitignored `naps_2023/` folder
for testing — nothing is committed until we decide to promote it.

Source (discovered live, not guessed):
  catalogue API: https://data-donnees.az.ec.gc.ca/api/file?path=<path>  (302 -> blob)
  file: air/monitor/national-air-pollution-surveillance-naps-program/
        Data-Donnees/2023/ContinuousData-DonneesContinu/HourlyData-DonneesHoraires/PM25_2023.csv
  Layout: metadata rows, then a header row starting "Pollutant//Polluant", then
  data rows: Pollutant, MethodCode, NAPS_ID, City, Province, Latitude, Longitude,
  Date(YYYY-MM-DD), H01..H24.  -999 = no data; zeros are valid.

Reference-grade (not a low-cost sensor) -> NO EPA/Barkjohn correction; values are
the official hourly PM2.5 averaged to a daily mean (>=18 valid hours required,
the NAPS 75% completeness rule). Pure stdlib.
"""

from __future__ import annotations

import argparse
import csv
import json
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "naps_2023"
API = "https://data-donnees.az.ec.gc.ca/api/file?path="
NAPS = "air/monitor/national-air-pollution-surveillance-naps-program"
PM25_PATH = (NAPS + "/Data-Donnees/2023/ContinuousData-DonneesContinu/"
             "HourlyData-DonneesHoraires/PM25_2023.csv")

YEAR = 2023
MISSING = -999.0
MIN_VALID_HOURS = 18          # NAPS 75% daily-completeness rule
ON_QC = {"ON", "QC"}


def _download(rel_path, dest):
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  using cached {dest.name} ({dest.stat().st_size//1024} KiB)")
        return
    url = API + urllib.parse.quote(rel_path)
    print(f"  downloading {dest.name} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "CLEAR25-naps/1"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as fh:  # follows 302
        fh.write(r.read())
    print(f"  -> {dest.stat().st_size//1024} KiB")


def _date_axis():
    d0, out = date(YEAR, 1, 1), []
    d = d0
    while d.year == YEAR:
        out.append(d.isoformat()); d += timedelta(days=1)
    return out


def _daily_mean(hour_cells):
    vals = []
    for c in hour_cells:
        c = (c or "").strip()
        if not c:
            continue
        try:
            v = float(c)
        except ValueError:
            continue
        if v == MISSING or v < 0:
            continue
        vals.append(v)
    if len(vals) < MIN_VALID_HOURS:
        return None
    return round(sum(vals) / len(vals), 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build NAPS 2023 daily PM2.5 plume JSON (testing).")
    ap.add_argument("--all-provinces", action="store_true",
                    help="keep every province (default: Ontario + Québec only)")
    ap.add_argument("--out", default=str(OUT_DIR / "naps_plume_2023.json"))
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / "PM25_2023.csv"
    _download(PM25_PATH, csv_path)

    dates = _date_axis()
    date_idx = {d: i for i, d in enumerate(dates)}
    n_days = len(dates)

    coords = {}                 # naps_id -> (lat, lon)
    daily = {}                  # (naps_id, day_idx) -> [daily means]
    kept_prov = ON_QC if not args.all_provinces else None

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        in_data = False
        rows_seen = 0
        for row in reader:
            if not row:
                continue
            if not in_data:
                if row[0].strip().startswith("Pollutant//Polluant"):
                    in_data = True
                continue
            # data row: Pollutant,Method,NAPS_ID,City,Prov,Lat,Lon,Date,H01..H24
            if len(row) < 32 or row[0].strip() != "PM2.5":
                continue
            sid = row[2].strip()
            prov = row[4].strip().upper()
            if kept_prov is not None and prov not in kept_prov:
                continue
            try:
                lat = float(row[5]); lon = float(row[6])
            except ValueError:
                continue
            di = date_idx.get(row[7].strip()[:10])
            if di is None:
                continue
            dm = _daily_mean(row[8:32])
            rows_seen += 1
            coords.setdefault(sid, (round(lat, 5), round(lon, 5)))
            if dm is not None:
                daily.setdefault((sid, di), []).append(dm)

    # Build stations + values matrix.
    station_ids = sorted(coords)
    stations, values, n_points = [], [], 0
    for sid in station_ids:
        lat, lon = coords[sid]
        series = [None] * n_days
        for di in range(n_days):
            vlist = daily.get((sid, di))
            if vlist:
                series[di] = round(sum(vlist) / len(vlist), 1)
                n_points += 1
        if not any(v is not None for v in series):
            continue
        stations.append({"id": sid, "lat": lat, "lon": lon})
        values.append(series)

    if not stations:
        raise SystemExit("No NAPS stations parsed — check the CSV format.")

    lats = [s["lat"] for s in stations]; lons = [s["lon"] for s in stations]
    bbox = {"nwlat": max(lats), "nwlng": min(lons), "selat": min(lats), "selng": max(lons)}
    payload = {
        "year": YEAR, "field": "PM2.5",
        "note": ("NAPS reference hourly PM2.5 averaged to daily (>=18 valid hours; "
                 "-999 = missing). Reference-grade — no EPA correction applied."),
        "source": "ECCC NAPS PM25_2023.csv",
        "dates": dates, "bbox": bbox, "stations": stations, "values": values,
    }
    out_str = json.dumps(payload, separators=(",", ":"))
    Path(args.out).write_text(out_str, encoding="utf-8")

    scope = "all provinces" if args.all_provinces else "Ontario + Québec"
    print(f"  scope: {scope}")
    print(f"  stations: {len(stations)}  days: {n_days}  data points: {n_points:,}")
    print(f"  bbox: lat {bbox['selat']}..{bbox['nwlat']}  lon {bbox['nwlng']}..{bbox['selng']}")
    print(f"  -> wrote {args.out} ({len(out_str)//1024} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
