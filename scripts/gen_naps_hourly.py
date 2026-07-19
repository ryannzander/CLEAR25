#!/usr/bin/env python3
"""Pull many years of ECCC NAPS reference HOURLY PM2.5 and compile them into one
compact hourly asset — the long training set for the smoke early-warning model.

Unlike gen_naps_2023.py (which averages to DAILY for the /plan/ tracker), this keeps
the raw hourly values and spans many years, because the neural net needs an hourly
timeline with as many real wildfire-smoke events as possible.

Source (same catalogue API as gen_naps_2023.py, verified live for 2000-2024):
  https://data-donnees.az.ec.gc.ca/api/file?path=<path>            (302 -> CSV blob)
  air/monitor/national-air-pollution-surveillance-naps-program/
    Data-Donnees/{YEAR}/ContinuousData-DonneesContinu/HourlyData-DonneesHoraires/PM25_{YEAR}.csv
  Layout: metadata rows, a header row starting "Pollutant//Polluant", then data rows:
    Pollutant, MethodCode, NAPS_ID, City, Prov, Lat, Lon, Date(YYYY-MM-DD), H01..H24
  -999 = missing; zeros valid; lat/lon embedded per row.

Reference-grade -> NO EPA/Barkjohn correction. Hourly index is built off the H01..H24
columns (H01 -> hour 0 of that date), giving a consistent hourly axis. Output matches
the plume asset schema so scripts/train_smoke_lstm.py can consume it unchanged:
  {field, note, source, t0, step_seconds:3600, n_steps, years,
   bbox, stations:[{id,lat,lon}], series:[[hour,pm,...]]}

Everything lands in the gitignored naps_hourly/ folder (raw CSVs cached + the asset).
Pure stdlib (urllib/csv/gzip/array).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import urllib.parse
import urllib.request
from array import array
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE.parent / "naps_hourly"
API = "https://data-donnees.az.ec.gc.ca/api/file?path="
NAPS = "air/monitor/national-air-pollution-surveillance-naps-program"
MISSING = -999.0
QA_MAX_PM = 1000.0            # above this ug/m3 is almost certainly a fault
DEFAULT_PROVS = "ON,QC"


def _year_path(year):
    return (f"{NAPS}/Data-Donnees/{year}/ContinuousData-DonneesContinu/"
            f"HourlyData-DonneesHoraires/PM25_{year}.csv")


def _download(year, cache_dir):
    dest = cache_dir / f"PM25_{year}.csv"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = API + urllib.parse.quote(_year_path(year))
    req = urllib.request.Request(url, headers={"User-Agent": "CLEAR25-naps/1"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as fh:
            fh.write(r.read())  # follows the 302 redirect
    except Exception as e:
        if dest.exists():
            dest.unlink()
        print(f"  {year}: download failed ({type(e).__name__}) — skipping")
        return None
    print(f"  {year}: {dest.stat().st_size // 1024} KiB")
    return dest


def main(argv=None):
    ap = argparse.ArgumentParser(description="Compile multi-year NAPS hourly PM2.5 -> compact asset.")
    ap.add_argument("--start", type=int, default=2003, help="first year (default 2003; data thin pre-2003)")
    ap.add_argument("--end", type=int, default=2025, help="last year (inclusive)")
    ap.add_argument("--provinces", default=DEFAULT_PROVS, help="comma list, or 'ALL'")
    ap.add_argument("--cache-dir", default=str(OUT_DIR))
    ap.add_argument("--out", default=str(OUT_DIR / "naps_pm25_hourly.json.gz"))
    args = ap.parse_args(argv)

    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    provs = None if args.provinces.upper() == "ALL" else {p.strip().upper() for p in args.provinces.split(",")}

    start_date = date(args.start, 1, 1)
    end_date = date(args.end, 12, 31)
    n_days = (end_date - start_date).days + 1
    n_steps = n_days * 24
    print(f"Years {args.start}-{args.end}  ({n_days:,} days, {n_steps:,} hourly steps)  "
          f"provinces={'ALL' if provs is None else ','.join(sorted(provs))}")

    coords = {}                                  # sid -> (lat, lon)
    hidx_by_sid = {}                             # sid -> array('i') hour indices
    pm_by_sid = {}                               # sid -> array('f') values
    years_present = []

    for year in range(args.start, args.end + 1):
        path = _download(year, cache_dir)
        if path is None:
            continue
        rows = 0
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            in_data = False
            for row in reader:
                if not row:
                    continue
                if not in_data:
                    if row[0].strip().startswith("Pollutant//Polluant"):
                        in_data = True
                    continue
                if len(row) < 32 or row[0].strip() != "PM2.5":
                    continue
                prov = row[4].strip().upper()
                if provs is not None and prov not in provs:
                    continue
                sid = row[2].strip()
                try:
                    lat = float(row[5]); lon = float(row[6])
                except ValueError:
                    continue
                try:
                    d = date.fromisoformat(row[7].strip()[:10])
                except ValueError:
                    continue
                day_off = (d - start_date).days
                if day_off < 0 or day_off >= n_days:
                    continue
                base = day_off * 24
                coords.setdefault(sid, (round(lat, 5), round(lon, 5)))
                hi = hidx_by_sid.setdefault(sid, array("i"))
                pv = pm_by_sid.setdefault(sid, array("f"))
                for h, cell in enumerate(row[8:32]):
                    cell = (cell or "").strip()
                    if not cell:
                        continue
                    try:
                        v = float(cell)
                    except ValueError:
                        continue
                    if v == MISSING or v < 0 or v > QA_MAX_PM:
                        continue
                    hi.append(base + h)
                    pv.append(v)
                    rows += 1
        years_present.append(year)
        print(f"    parsed {rows:,} hourly values")

    # Build stations + flat interleaved series, sorted by hour index per station.
    station_ids = sorted(coords, key=lambda s: (coords[s][0], s))
    stations, series, n_points = [], [], 0
    for sid in station_ids:
        hi = hidx_by_sid.get(sid)
        if not hi:
            continue
        pairs = sorted(zip(hi, pm_by_sid[sid]))          # ascending hour
        flat = []
        for h, v in pairs:
            flat.append(int(h)); flat.append(round(float(v), 1))
        stations.append({"id": sid, "lat": coords[sid][0], "lon": coords[sid][1]})
        series.append(flat)
        n_points += len(pairs)

    if not stations:
        raise SystemExit("No NAPS stations parsed — check provinces / years / CSV format.")

    lats = [s["lat"] for s in stations]; lons = [s["lon"] for s in stations]
    bbox = {"nwlat": max(lats), "nwlng": min(lons), "selat": min(lats), "selng": max(lons)}
    payload = {
        "field": "PM2.5",
        "note": ("NAPS reference hourly PM2.5 (H01->hour0). -999=missing. "
                 "Reference-grade — no EPA correction."),
        "source": f"ECCC NAPS PM25 {years_present[0]}-{years_present[-1]}" if years_present else "ECCC NAPS",
        "t0": f"{args.start}-01-01T00:00:00Z",
        "step_seconds": 3600,
        "n_steps": n_steps,
        "years": years_present,
        "bbox": bbox,
        "stations": stations,
        "series": series,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if out.suffix == ".gz":
        out.write_bytes(gzip.compress(raw, 6))
    else:
        out.write_bytes(raw)
    print(f"\n  stations: {len(stations)}   data points: {n_points:,}   steps: {n_steps:,}")
    print(f"  bbox: lat {bbox['selat']}..{bbox['nwlat']}  lon {bbox['nwlng']}..{bbox['selng']}")
    print(f"  -> wrote {out}  ({out.stat().st_size/1048576:.1f} MB, raw {len(raw)/1048576:.0f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
