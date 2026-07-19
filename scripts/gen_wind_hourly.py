#!/usr/bin/env python3
"""Fetch hourly 10 m WIND from Open-Meteo's free ERA5 archive and compile it into a
compact asset aligned hour-for-hour to a PM2.5 training asset's timeline.

This is the historical (training-side) half of the wind integration. The live/forecast
half uses ECCC model wind (reuses the eccc_ingest grib pipeline) and is not built here.

Open-Meteo ERA5 archive contract (verified live 2026-07-19):
  GET https://archive-api.open-meteo.com/v1/archive
      latitude, longitude, start_date, end_date  (YYYY-MM-DD),
      hourly=wind_speed_10m,wind_direction_10m, timezone=GMT, wind_speed_unit=ms
  -> {"hourly": {"time": ["2024-01-01T00:00", ...],   # ISO, UTC, contiguous hourly
                 "wind_speed_10m": [m/s, ...],
                 "wind_direction_10m": [deg the wind blows FROM, ...]}}
  Index 0 = start_date 00:00Z. Free, no API key for non-commercial use.

Alignment: the script reads the target PM asset ONLY for its `t0` and `n_steps`, then
places every returned hour at index (utc_time - t0)/3600 into fixed-length arrays.
That makes the output timeline identical to the PM asset's, so scripts/wind_features.py
indexes them together. Works for the PurpleAir 2024-2025 asset AND the multi-year NAPS
asset (any t0 / length) with no changes.

Sample points default to the Toronto core centroid (the only wind the Toronto smoke
model consumes). Add more with --point "lat,lon,name" (repeatable) for future cities.

Output schema mirrors the PM assets:
  {field:"wind10m", source, note, t0, step_seconds:3600, n_steps, units,
   points:[{name,lat,lon}], speed:[[per-point hourly, null=missing]], dir:[[...]]}

Pure stdlib (urllib/json/gzip). Chunked by calendar year for robustness + progress.
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
OUT_DIR = REPO / "wind_hourly"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"

TOR_LAT, TOR_LON = 43.6532, -79.3832       # Toronto core centroid (matches the models)


def _parse_t0(s):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except (ValueError, TypeError):
            continue
    raise SystemExit(f"Could not parse asset t0: {s!r}")


def _load_asset_meta(path):
    """Return (t0_datetime, n_steps) from a PM asset (.json/.json.gz), reading only
    the small metadata (we still parse the whole file, but ignore the heavy series)."""
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    d = json.loads(raw)
    if "t0" not in d or "n_steps" not in d:
        raise SystemExit(f"{path}: asset missing t0/n_steps — is this a PM asset?")
    return _parse_t0(d["t0"]), int(d["n_steps"])


def _fetch_year(lat, lon, start_date, end_date, retries=3):
    """One archive request for a [start_date, end_date] span at (lat, lon)."""
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "start_date": start_date.isoformat(), "end_date": end_date.isoformat(),
        "hourly": "wind_speed_10m,wind_direction_10m",
        "timezone": "GMT", "wind_speed_unit": "ms",
    })
    url = f"{ARCHIVE}?{q}"
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CLEAR25-wind/1"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except Exception as e:               # transient network / 429 -> backoff
            last = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise SystemExit(f"  fetch failed for {start_date}..{end_date}: {type(last).__name__}: {last}")


def _collect_point(lat, lon, t0, n_steps):
    """Fetch the full [t0, t0 + n_steps h) span for one point, chunked by year.
    Returns (speed[list|None], dir[list|None]) of length n_steps."""
    speed = [None] * n_steps
    direc = [None] * n_steps
    end_dt = t0 + timedelta(hours=n_steps - 1)
    filled = 0
    for year in range(t0.year, end_dt.year + 1):
        y_start = max(t0.date(), datetime(year, 1, 1, tzinfo=timezone.utc).date())
        y_end = min(end_dt.date(), datetime(year, 12, 31, tzinfo=timezone.utc).date())
        if y_start > y_end:
            continue
        data = _fetch_year(lat, lon, y_start, y_end)
        h = data.get("hourly", {})
        times = h.get("time", [])
        ws = h.get("wind_speed_10m", [])
        wd = h.get("wind_direction_10m", [])
        for i, tstr in enumerate(times):
            # 'YYYY-MM-DDTHH:MM' in GMT (timezone=GMT -> utc_offset 0).
            dt = datetime.strptime(tstr, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            gi = int(round((dt - t0).total_seconds() / 3600.0))
            if 0 <= gi < n_steps:
                s = ws[i] if i < len(ws) else None
                a = wd[i] if i < len(wd) else None
                if s is not None:
                    speed[gi] = round(float(s), 2)
                    filled += 1
                if a is not None:
                    direc[gi] = int(round(float(a)))
        print(f"    {year}: {y_start}..{y_end}  ({len(times)} hrs)")
        time.sleep(0.5)                      # be polite to the free endpoint
    return speed, direc, filled


def _parse_point(s):
    parts = [p.strip() for p in s.split(",")]
    if len(parts) < 2:
        raise argparse.ArgumentTypeError("--point must be 'lat,lon[,name]'")
    lat, lon = float(parts[0]), float(parts[1])
    name = parts[2] if len(parts) > 2 else f"{lat:.3f},{lon:.3f}"
    return {"name": name, "lat": lat, "lon": lon}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--asset", required=True,
                    help="target PM asset (.json/.json.gz) to align the wind timeline to")
    ap.add_argument("--point", action="append", type=_parse_point, default=None,
                    help="extra sample point 'lat,lon[,name]' (repeatable); default = Toronto")
    ap.add_argument("--out", default=None,
                    help="output path (default wind_hourly/wind_<assetstem>.json.gz)")
    args = ap.parse_args(argv)

    t0, n_steps = _load_asset_meta(args.asset)
    points = args.point or [{"name": "toronto", "lat": TOR_LAT, "lon": TOR_LON}]
    end_dt = t0 + timedelta(hours=n_steps - 1)
    print(f"Aligning wind to {Path(args.asset).name}")
    print(f"  t0={t0:%Y-%m-%dT%H:%MZ}  n_steps={n_steps:,}  ({t0:%Y-%m-%d}..{end_dt:%Y-%m-%d})")
    print(f"  points: " + ", ".join(f"{p['name']}({p['lat']:.3f},{p['lon']:.3f})" for p in points))

    speeds, dirs = [], []
    for p in points:
        print(f"  fetching {p['name']} ...")
        spd, drc, filled = _collect_point(p["lat"], p["lon"], t0, n_steps)
        cov = filled / n_steps * 100 if n_steps else 0
        print(f"    -> {filled:,}/{n_steps:,} hours covered ({cov:.0f}%)")
        speeds.append(spd)
        dirs.append(drc)

    payload = {
        "field": "wind10m",
        "source": "Open-Meteo ERA5 archive",
        "note": ("10 m wind. speed in m/s; dir = degrees the wind blows FROM "
                 "(meteorological, 0=N 90=E). null = missing hour."),
        "t0": f"{t0:%Y-%m-%dT%H:%M:%SZ}",
        "step_seconds": 3600,
        "n_steps": n_steps,
        "units": {"speed": "m/s", "dir": "deg_from"},
        "points": points,
        "speed": speeds,
        "dir": dirs,
    }

    if args.out:
        out = Path(args.out)
    else:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = Path(args.asset).name.replace(".json.gz", "").replace(".json", "")
        out = OUT_DIR / f"wind_{stem}.json.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if out.suffix == ".gz":
        out.write_bytes(gzip.compress(raw, 6))
    else:
        out.write_bytes(raw)
    print(f"\n  -> wrote {out}  ({out.stat().st_size / 1024:.0f} KiB, raw {len(raw)/1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
