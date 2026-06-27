#!/usr/bin/env python3
"""Compile the 2024 + 2025 PurpleAir HOURLY downloads into one compact JSON for
the plume tracker — replacing the 2023 daily asset. Runs the animation off local
historical data with ZERO live API cost (the only network call is a one-time
coordinate lookup, because these downloads ship no lat/lon — see below).

Input  (gitignored, local only):
  data/PurpleAir2024/PurpleAir Download <date>/
    <sensor_index> 2024-01-01 2024-12-31 60-Minute Average.csv   (x~536)
       columns: time_stamp, pm2.5_atm        (HOURLY rows, NO lat/lon)
  data/PurpleAir2025/PurpleAir Download <date>/
    <sensor_index> 2025-01-01 2025-12-31 60-Minute Average.csv   (x~510)

Coordinates: unlike the 2023 download, these CSVs carry NO latitude/longitude and
there is no station_coords.json. We resolve each sensor_index's coordinates from,
in order of preference:
  1. a local cache file (scripts/pa_coords_cache.json)  — written by --fetch-coords
  2. the committed plume_2023.json (sensors that overlapped 2023)
A sensor with no resolvable coordinate is DROPPED (logged). With --fetch-coords and
PURPLEAIR_API_KEY set, the script makes ONE PurpleAir /v1/sensors metadata call
(by sensor_index) to fill the cache for every sensor — this is metadata only, not
history, and is run once at generation time (never at runtime).

Output (committed, served as a static asset):
  webapp/dashboard/static/dashboard/plume_2024_2025.json
    {
      "field":"pm2.5_atm", "note", "source",
      "t0":"2024-01-01T00:00:00Z", "step_seconds":3600, "n_steps",
      "bbox", "stations":[{"id","lat","lon"}...],
      "series":[ [h,pm, h,pm, ...], ... ]   # parallel to stations; h = hour index
    }
  Sparse station-keyed: each station's series is a flat list of (hour_index, pm)
  pairs (ascending). A dense matrix would be ~520 x ~17.5k mostly-null cells.

NOTE: the download carries only `pm2.5_atm` (no CF=1 / humidity), so the EPA /
Barkjohn correction CANNOT be applied — values are the uncorrected ATM PM2.5 as
downloaded (same caveat as the 2023 asset). Pure stdlib except the optional
--fetch-coords path, which uses `requests`.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import glob
import gzip
import json
import os
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_OUT = REPO / "webapp" / "dashboard" / "static" / "dashboard" / "plume_2024_2025.json.gz"
COORDS_CACHE = HERE / "pa_coords_cache.json"
PLUME_2023 = REPO / "webapp" / "dashboard" / "static" / "dashboard" / "plume_2023.json"

# Global hourly axis: 2024-01-01T00:00Z .. 2025-12-31T23:00Z.
# 2024 is a leap year (366 d) + 2025 (365 d) = 731 d * 24 = 17,544 hourly steps.
T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
T0_EPOCH = calendar.timegm(T0.timetuple())
N_STEPS = 731 * 24  # 17,544
STEP_SECONDS = 3600

_QA_MAX_PM = 1000.0  # ATM PM2.5 above this (ug/m3) is almost certainly a fault.

PURPLEAIR_BASE = "https://api.purpleair.com/v1/sensors"


# --------------------------------------------------------------------------- #
# Input discovery
# --------------------------------------------------------------------------- #
def _find_year_dir(year, explicit):
    if explicit:
        p = Path(explicit)
        if not p.is_dir():
            raise SystemExit(f"--input{year} not a directory: {p}")
        return p
    pat = str(REPO / "data" / f"PurpleAir{year}" / "PurpleAir Download*")
    matches = sorted(glob.glob(pat))
    if not matches:
        raise SystemExit(f"No '{pat}' directory found; pass --input{year}")
    return Path(matches[-1])


def _step_index(ts):
    """ISO 'YYYY-MM-DDTHH:MM:SSZ' -> hour index into the global axis, or None."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    epoch = calendar.timegm(dt.timetuple())
    step = (epoch - T0_EPOCH) // STEP_SECONDS
    if step < 0 or step >= N_STEPS:
        return None
    return int(step)


def _parse_year(in_dir, series_by_sid):
    """Read every <id> *.csv in a download dir; append (step, pm) to series_by_sid."""
    n_files = n_rows = 0
    for csv_path in sorted(glob.glob(str(in_dir / "*.csv"))):
        sid = Path(csv_path).name.split(" ", 1)[0]  # leading token = sensor index
        if not sid.isdigit():
            continue
        n_files += 1
        pairs = series_by_sid.setdefault(sid, [])
        try:
            with open(csv_path, newline="", encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                for row in rd:
                    step = _step_index(row.get("time_stamp") or "")
                    if step is None:
                        continue
                    try:
                        pm = float(row["pm2.5_atm"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if pm < 0 or pm > _QA_MAX_PM:
                        continue
                    pairs.append((step, round(pm, 1)))
                    n_rows += 1
        except OSError:
            continue
    return n_files, n_rows


# --------------------------------------------------------------------------- #
# Coordinates
# --------------------------------------------------------------------------- #
def _load_cache():
    if COORDS_CACHE.is_file():
        try:
            raw = json.loads(COORDS_CACHE.read_text(encoding="utf-8"))
            out = {}
            for sid, v in raw.items():
                try:
                    out[str(sid)] = (float(v[0]), float(v[1]))
                except (TypeError, ValueError, IndexError):
                    continue
            return out
        except (OSError, ValueError):
            return {}
    return {}


def _load_2023_coords():
    if not PLUME_2023.is_file():
        return {}
    try:
        j = json.loads(PLUME_2023.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(s["id"]): (float(s["lat"]), float(s["lon"]))
            for s in j.get("stations", []) if "lat" in s and "lon" in s}


def _fetch_coords(sids, api_key):
    """One PurpleAir /v1/sensors metadata call by sensor_index -> {sid:(lat,lon)}.

    Parses by the response's own `fields` array (PurpleAir reorders columns and
    prepends sensor_index). Metadata only; no history points are read.
    """
    import requests  # local import so --estimate needs no third-party deps

    out = {}
    sids = sorted(set(sids))
    # show_only caps URL length; chunk to stay well under typical limits.
    CHUNK = 250
    for i in range(0, len(sids), CHUNK):
        chunk = sids[i:i + CHUNK]
        params = {"fields": "latitude,longitude", "show_only": ",".join(chunk)}
        resp = requests.get(PURPLEAIR_BASE, headers={"X-API-Key": api_key},
                            params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        fields = data.get("fields") or []
        rows = data.get("data") or []
        idx = {name: k for k, name in enumerate(fields)}
        if "sensor_index" not in idx or "latitude" not in idx or "longitude" not in idx:
            raise SystemExit(f"PurpleAir response missing fields; got {fields}")
        for row in rows:
            try:
                sid = str(row[idx["sensor_index"]])
                lat = float(row[idx["latitude"]])
                lon = float(row[idx["longitude"]])
            except (IndexError, TypeError, ValueError):
                continue
            if lat == 0 and lon == 0:
                continue
            out[sid] = (lat, lon)
        print(f"  fetched coords for {len(chunk)} ids (chunk {i // CHUNK + 1}); "
              f"running total {len(out)}")
    return out


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Build plume_2024_2025.json from the PurpleAir hourly downloads.")
    ap.add_argument("--input2024", default=None, help="2024 download dir (default: latest under data/PurpleAir2024/)")
    ap.add_argument("--input2025", default=None, help="2025 download dir (default: latest under data/PurpleAir2025/)")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--fetch-coords", action="store_true",
                    help="fill missing coords via one PurpleAir /v1/sensors call (needs PURPLEAIR_API_KEY)")
    ap.add_argument("--estimate", action="store_true",
                    help="measure payload size with placeholder coords (no API, no coord-drop)")
    ap.add_argument("--write", action="store_true", help="write the asset (else dry-run summary)")
    args = ap.parse_args(argv)

    d24 = _find_year_dir(2024, args.input2024)
    d25 = _find_year_dir(2025, args.input2025)
    print(f"Reading 2024: {d24}")
    print(f"Reading 2025: {d25}")

    series_by_sid = {}
    f24, r24 = _parse_year(d24, series_by_sid)
    f25, r25 = _parse_year(d25, series_by_sid)
    print(f"  2024: {f24} files, {r24:,} valid rows")
    print(f"  2025: {f25} files, {r25:,} valid rows")

    # Keep only sensors that actually produced at least one valid reading.
    sids_with_data = sorted([s for s, p in series_by_sid.items() if p], key=int)
    print(f"  sensors with data: {len(sids_with_data)}")

    # ---- Resolve coordinates -------------------------------------------------
    cache = _load_cache()
    fallback23 = _load_2023_coords()
    print(f"  coord cache: {len(cache)}   plume_2023 fallback: {len(fallback23)}")

    if args.fetch_coords:
        api_key = os.environ.get("PURPLEAIR_API_KEY", "")
        if not api_key:
            raise SystemExit("--fetch-coords set but PURPLEAIR_API_KEY is not in the environment")
        missing = [s for s in sids_with_data if s not in cache]
        print(f"  fetching coords for {len(missing)} sensors missing from cache ...")
        fetched = _fetch_coords(missing, api_key)
        cache.update(fetched)
        COORDS_CACHE.write_text(json.dumps({k: [round(v[0], 6), round(v[1], 6)]
                                            for k, v in cache.items()},
                                           separators=(",", ":")),
                                encoding="utf-8")
        print(f"  cache now holds {len(cache)} coords -> {COORDS_CACHE}")

    def coord_for(sid):
        if sid in cache:
            return cache[sid]
        if sid in fallback23:
            return fallback23[sid]
        return None

    # ---- Build payload -------------------------------------------------------
    stations, series = [], []
    n_points = 0
    dropped_no_coord = 0
    for sid in sids_with_data:
        c = coord_for(sid)
        if c is None:
            if args.estimate:
                c = (0.0, 0.0)  # placeholder so size is measured over ALL sensors
            else:
                dropped_no_coord += 1
                continue
        pairs = sorted(series_by_sid[sid])
        flat = []
        for step, pm in pairs:
            flat.append(step)
            flat.append(pm)
        stations.append({"id": sid, "lat": round(c[0], 5), "lon": round(c[1], 5)})
        series.append(flat)
        n_points += len(pairs)

    if not stations:
        raise SystemExit("No stations with resolvable coordinates — run with --fetch-coords (PURPLEAIR_API_KEY set).")

    lats = [s["lat"] for s in stations]
    lons = [s["lon"] for s in stations]
    bbox = {"nwlat": max(lats), "nwlng": min(lons), "selat": min(lats), "selng": max(lons)}

    payload = {
        "field": "pm2.5_atm",
        "note": "Uncorrected ATM PM2.5 (download has no CF=1/RH for EPA correction).",
        "source": f"{d24.parent.name}/{d24.name} + {d25.parent.name}/{d25.name}",
        "t0": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "step_seconds": STEP_SECONDS,
        "n_steps": N_STEPS,
        "bbox": bbox,
        "stations": stations,
        "series": series,
    }

    out_str = json.dumps(payload, separators=(",", ":"))
    gz = gzip.compress(out_str.encode("utf-8"), 6)
    print(f"  stations kept: {len(stations)}   dropped (no coord): {dropped_no_coord}")
    print(f"  data points: {n_points:,}   steps: {N_STEPS}")
    print(f"  bbox: lat {bbox['selat']}..{bbox['nwlat']}  lon {bbox['nwlng']}..{bbox['selng']}")
    print(f"  payload size: {len(out_str) / 1048576:.1f} MB raw   {len(gz) / 1048576:.1f} MB gzip")
    if args.estimate:
        print("  (estimate mode: placeholder coords; no asset written)")
        return 0

    if args.write:
        out = Path(args.out)
        if out.suffix == ".gz":
            out.write_bytes(gz)  # served as-is; frontend decompresses (DecompressionStream)
        else:
            out.write_text(out_str, encoding="utf-8")
        print(f"  -> wrote {out}  ({out.stat().st_size / 1048576:.1f} MB on disk)")
        if dropped_no_coord:
            print(f"  WARNING: {dropped_no_coord} sensors dropped for missing coords — "
                  f"run with --fetch-coords (PURPLEAIR_API_KEY) for full coverage.")
    else:
        print("  (dry run; pass --write to save)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
