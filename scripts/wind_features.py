#!/usr/bin/env python3
"""Shared WIND feature builder for the Toronto smoke early-warning models.

Turns an Open-Meteo ERA5 hourly wind asset (see scripts/gen_wind_hourly.py) into
CAUSAL per-hour features that BOTH scripts/poc_smoke_model.py and
scripts/train_smoke_lstm.py append to their PM2.5 feature matrices.

Why wind matters: the models split the surrounding PM2.5 field into 8 fixed compass
sectors around Toronto and hope "upwind" is a static geometric guess. Real wind tells
the model WHICH sector is actually upwind at each hour, and how fast smoke is being
advected toward the city. That is exactly the signal a persistence baseline lacks.

Every feature at hour t uses only wind observed AT t (no look-ahead), so it is safe
for early-warning prediction.

Meteorological convention (Open-Meteo `wind_direction_10m`): the compass bearing the
wind blows FROM (0=N, 90=E, 180=S, 270=W). That bearing is, by construction, the
direction of the UPWIND sector as seen from Toronto — if the wind is from 270 deg (W),
the West sector is upwind, so PM2.5 sitting to the west is what is inbound.

Pure numpy + stdlib. No app imports, no look-ahead, asset-agnostic (works for the
PurpleAir 2024-2025 asset and the multi-year NAPS asset alike).
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Sector order MUST match poc_smoke_model.SECTORS and the columns of sec_mean.
SECTORS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
_CENTERS = np.array([i * 45.0 for i in range(8)])  # compass bearing of each sector
FEATURE_NAMES = ["w_logspeed", "w_dir_sin", "w_dir_cos", "w_upwind_pm", "w_upwind_flux"]


def load_wind(path):
    """Load a wind asset from .json or .json.gz (gzip magic sniffed)."""
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def parse_t0(s):
    """Parse an asset t0 string (e.g. '2024-01-01T00:00:00Z') to aware UTC."""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except (ValueError, TypeError):
            continue
    return datetime(1970, 1, 1, tzinfo=timezone.utc)


def nearest_point(wind, lat, lon):
    """Index of the wind sample point closest (planar) to (lat, lon)."""
    best, best_d = 0, float("inf")
    for i, p in enumerate(wind["points"]):
        d = (p["lat"] - lat) ** 2 + (p["lon"] - lon) ** 2
        if d < best_d:
            best_d, best = d, i
    return best


def series_for(wind, t0, n_steps, lat, lon):
    """Return (speed[n_steps], dir[n_steps]) for the point nearest (lat, lon),
    reindexed onto the target timeline (t0, hourly, n_steps). NaN = missing.

    Robust to a wind asset whose own t0 differs from the PM asset's: the two are
    aligned by the integer hour offset between their t0 values."""
    idx = nearest_point(wind, lat, lon)
    off = int(round((parse_t0(wind["t0"]) - t0).total_seconds() / 3600.0))
    ws, wd = wind["speed"][idx], wind["dir"][idx]
    speed = np.full(n_steps, np.nan)
    direc = np.full(n_steps, np.nan)
    for i in range(len(ws)):
        gi = i + off                       # wind index i -> target-timeline index
        if 0 <= gi < n_steps:
            if ws[i] is not None:
                speed[gi] = ws[i]
            if wd[i] is not None:
                direc[gi] = wd[i]
    return speed, direc


def build_features(speed, direc, sec_mean):
    """Per-hour causal wind features aligned to speed/dir/sec_mean.

    Parameters
    ----------
    speed : (n,)   10 m wind speed (m/s), NaN where missing.
    direc : (n,)   wind direction (deg the wind blows FROM), NaN where missing.
    sec_mean : (n, 8)   RAW (un-logged) mean PM2.5 per compass sector, NaN where a
               sector has no reporting sensors this hour.

    Returns (F, names) where F is (n, 5):
      w_logspeed     log1p wind speed                         (magnitude)
      w_dir_sin/cos  unit direction vector of the FROM-bearing (bounded)
      w_upwind_pm    log1p of PM2.5 sitting in the true upwind direction,
                     weighted by cos-alignment to each sector center
      w_upwind_flux  log1p of speed x that upwind PM2.5 (an advective-flux proxy:
                     how much smoke, how fast, pointed at the city)
    Rows with missing speed or direction are NaN (downstream standardizers impute
    them to the training mean, so a data gap is never read as 'calm & clean')."""
    speed = np.asarray(speed, dtype=float)
    direc = np.asarray(direc, dtype=float)
    sec_mean = np.asarray(sec_mean, dtype=float)

    with np.errstate(invalid="ignore"):
        rad = np.radians(direc)
        pos_speed = np.where(np.isnan(speed), 0.0, np.maximum(speed, 0.0))
        logspeed = np.log1p(pos_speed)
        dsin, dcos = np.sin(rad), np.cos(rad)

        # cos-alignment of each sector center to the wind's FROM-bearing, >= 0.
        align = np.maximum(0.0, np.cos(np.radians(direc[:, None] - _CENTERS[None, :])))
        pm = np.where((sec_mean < 0) | np.isnan(sec_mean), 0.0, sec_mean)
        upwind = np.nansum(align * pm, axis=1)     # 0 for hours with NaN direction
        upwind_pm = np.log1p(upwind)
        upwind_flux = np.log1p(pos_speed * upwind)

    F = np.column_stack([logspeed, dsin, dcos, upwind_pm, upwind_flux])
    F[np.isnan(speed) | np.isnan(direc), :] = np.nan   # impute-later on real gaps
    return F, list(FEATURE_NAMES)


# --------------------------------------------------------------------------- #
# Self-test: verifiable, no network. Run `python scripts/wind_features.py`.
# --------------------------------------------------------------------------- #
def _selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    # Wind from due West (270) -> the West sector must carry full alignment weight,
    # and East (90, opposite) must carry zero.
    sec = np.zeros((1, 8))
    sec[0, SECTORS.index("W")] = 50.0   # 50 ug/m3 sitting to the west
    sec[0, SECTORS.index("E")] = 50.0   # 50 to the east (downwind, should not count)
    F, names = build_features(np.array([4.0]), np.array([270.0]), sec)
    check("feature names", names == FEATURE_NAMES)
    # upwind_pm = log1p(1.0*50 + 0*50) = log1p(50)
    check("upwind uses the West (upwind) sector only", abs(F[0, 3] - np.log1p(50.0)) < 1e-9)
    # flux = log1p(4 * 50)
    check("flux scales upwind PM by speed", abs(F[0, 4] - np.log1p(4.0 * 50.0)) < 1e-9)
    # dir sin/cos of 270 deg: sin=-1, cos=0
    check("dir vector of 270deg", abs(F[0, 1] + 1.0) < 1e-9 and abs(F[0, 2]) < 1e-9)

    # Reverse the wind to the East (90): now the East sector is upwind.
    F2, _ = build_features(np.array([4.0]), np.array([90.0]), sec)
    check("reversing wind flips which sector is upwind", abs(F2[0, 3] - np.log1p(50.0)) < 1e-9)

    # Missing wind -> all features NaN (imputed later, not treated as calm).
    F3, _ = build_features(np.array([np.nan]), np.array([np.nan]), sec)
    check("missing wind -> NaN row", bool(np.all(np.isnan(F3[0]))))

    # series_for alignment across differing t0 values.
    wind = {
        "t0": "2024-01-01T02:00:00Z", "points": [{"name": "toronto", "lat": 43.65, "lon": -79.38}],
        "speed": [[1.0, 2.0, 3.0]], "dir": [[10.0, 20.0, 30.0]],
    }
    t0 = parse_t0("2024-01-01T00:00:00Z")
    spd, drc = series_for(wind, t0, 6, 43.65, -79.38)
    check("series_for offsets by t0 delta (2h)", np.isnan(spd[0]) and spd[2] == 1.0 and spd[4] == 3.0)

    print("\nAll wind-feature self-tests passed." if ok else "\nSELF-TEST FAILURES ABOVE.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
