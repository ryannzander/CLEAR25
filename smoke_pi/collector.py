#!/usr/bin/env python3
"""CLEAR smoke collector — accumulates a live archive of wildfire-smoke model
output over Ontario + Québec, from two sources, for later model training.

Sources
-------
* BlueSky Canada (firesmoke.ca)  — hourly ground-level PM2.5 smoke, NetCDF
* FireWork / RAQDPS (ECCC MSC)   — surface PM2.5 wildfire-smoke plume, GRIB2

Neither source archives history publicly (they keep only the latest runs), so we
poll on each model's run cadence, **clip to the Ontario+Québec mesh** (a few tens
of KB instead of the 84 MB raw grid), and append a timestamped slice. Months of
these slices become the training set; the latest of each feeds the dashboard.

Designed for a Raspberry Pi: a full Linux box reads GRIB2/NetCDF happily (the
libraries that don't fit a serverless lambda). Heavy deps are imported lazily so
`--status` and source discovery work without them installed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import socket
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "web" / "data"
SLICE_DIR = DATA_DIR / "slices"

# Ontario + Québec mesh (the only region we keep — keeps slices tiny).
SMOKE_BBOX = {"nwlat": 63.0, "nwlng": -95.5, "selat": 41.6, "selng": -57.0}
MESH_SPACING = 0.15  # deg (~16 km); coarser than the models, fine for the archive

BLUESKY_CURRENT = "https://firesmoke.ca/forecasts/current/"
BLUESKY_BASE = "https://firesmoke.ca"
# ECCC migrated the Datamart to date-partitioned dirs; RAQDPS now lives at
# dd.weather.gc.ca/{YYYYMMDD}/WXO-DD/model_raqdps/10km/grib2/{HH}/{FFF}/.
RAQDPS_DATE_TMPL = "https://dd.weather.gc.ca/{date}/WXO-DD/model_raqdps/10km/grib2/"
# Surface PM2.5 from wildfire smoke only — this is the "FireWork" signal.
FIREWORK_PRODUCT = "_MSC_RAQDPS_PM2.5-WildfireSmokePlume_Sfc_"

_STAMP_RE = re.compile(r"(\d{8})T(\d{2})Z")
_HREF_RE = re.compile(r'href="([^"?][^"]*)"', re.IGNORECASE)
_BSC_RE = re.compile(r"/forecasts/(BSC\d{2}CA\d{2}p?-\d+)/(\d{10})/")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "CLEAR-smoke-collector/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 trusted hosts
        return r.read()


def _list_index(url):
    html = _http_get(url).decode("utf-8", "replace")
    return [h for h in _HREF_RE.findall(html)
            if not h.startswith(("/", "?", "..")) and h not in ("../",)]


# ---------------------------------------------------------------------------
# Ontario+Québec mesh (pure python so it's unit-testable without numpy)
# ---------------------------------------------------------------------------
def build_mesh(spacing=MESH_SPACING, bbox=None):
    b = bbox or SMOKE_BBOX
    cols = max(1, int(round((b["selng"] - b["nwlng"]) / spacing)))
    rows = max(1, int(round((b["nwlat"] - b["selat"]) / spacing)))
    lats, lons = [], []
    for r in range(rows):
        lat = b["nwlat"] - (r + 0.5) * spacing
        for c in range(cols):
            lon = b["nwlng"] + (c + 0.5) * spacing
            lats.append(lat); lons.append(lon)
    return {"rows": rows, "cols": cols, "spacing": spacing, "bbox": b,
            "lats": lats, "lons": lons}


# ---------------------------------------------------------------------------
# Source 1 — BlueSky Canada (NetCDF)
# ---------------------------------------------------------------------------
def discover_bluesky():
    """Return (run_stamp, dispersion_url) for the newest BlueSky CA forecast."""
    html = _http_get(BLUESKY_CURRENT).decode("utf-8", "replace")
    best = None
    for dom_ver, dt in _BSC_RE.findall(html):
        if "CA12" not in dom_ver:          # prefer the Canada-wide 12 km domain
            continue
        if best is None or dt > best[1]:
            best = (dom_ver, dt)
    if not best:
        raise RuntimeError("No BlueSky CA12 forecast link found on the current page")
    dom_ver, dt = best
    url = f"{BLUESKY_BASE}/forecasts/{dom_ver}/{dt}/dispersion.nc"
    return dt, url


def sample_bluesky(nc_path, mesh):
    """Subsample the BlueSky dispersion NetCDF onto the OnQC mesh (Pi-only deps).

    Returns (values[list|None per cell], valid_time, max_pm). Uses the latest
    timestep's ground-level PM2.5. Handles either lat/lon coordinates or the
    IOAPI grid attributes that BlueSky/CMAQ files carry.
    """
    import numpy as np
    import xarray as xr

    ds = xr.open_dataset(nc_path)
    try:
        var = None
        for name in ("PM25", "PM2.5", "pm25", "PM25_TOT", "smoke"):
            if name in ds.variables:
                var = ds[name]; break
        if var is None:  # fall back to the first 3-4D float variable
            var = next(ds[v] for v in ds.data_vars
                       if ds[v].ndim >= 2 and ds[v].dtype.kind == "f")

        data = var
        for d in ("TSTEP", "time", "Time"):     # latest timestep
            if d in data.dims:
                data = data.isel({d: -1})
        for d in ("LAY", "lev", "level", "z"):   # ground layer
            if d in data.dims:
                data = data.isel({d: 0})
        grid = np.asarray(data.values, dtype="float64")

        glat, glon = _grid_latlon(ds, grid.shape)
        vals, mx = _nearest_sample(glat, glon, grid, mesh)
        valid = _coord_str(ds)
        return vals, valid, mx
    finally:
        ds.close()


def _grid_latlon(ds, shape):
    import numpy as np
    for la, lo in (("latitude", "longitude"), ("lat", "lon"), ("LAT", "LON")):
        if la in ds.variables and lo in ds.variables:
            a, o = np.asarray(ds[la].values), np.asarray(ds[lo].values)
            if a.ndim == 1 and o.ndim == 1:
                o2, a2 = np.meshgrid(o, a)
                return a2, o2
            return a, o
    # IOAPI fallback (lambert/latlon defined by global attrs)
    g = ds.attrs
    nrow, ncol = shape[-2], shape[-1]
    xorig, yorig = float(g.get("XORIG", 0)), float(g.get("YORIG", 0))
    xcell, ycell = float(g.get("XCELL", 0)), float(g.get("YCELL", 0))
    if g.get("GDTYP", 1) == 1:  # 1 = lat/lon grid
        lon = xorig + (np.arange(ncol) + 0.5) * xcell
        lat = yorig + (np.arange(nrow) + 0.5) * ycell
        return np.meshgrid(lon, lat)[::-1]
    raise RuntimeError("Cannot georeference BlueSky grid (no lat/lon, non-latlon IOAPI)")


def _coord_str(ds):
    for k in ("SDATE", "TSTEP"):
        if k in ds.attrs:
            return str(ds.attrs[k])
    return _now()


# ---------------------------------------------------------------------------
# Source 2 — FireWork / RAQDPS wildfire-smoke plume (GRIB2)
# ---------------------------------------------------------------------------
def discover_firework():
    """Return (run_stamp, grib_url) for the newest RAQDPS wildfire-smoke Sfc, h000.

    The Datamart partitions by run date, and today's dir is empty until the run
    publishes, so we scan today then yesterday (UTC) and pick the newest run by
    the (date, hour) stamp in the FILENAME, never by directory order.
    """
    today = datetime.now(timezone.utc).date()
    bases = [RAQDPS_DATE_TMPL.format(date=(today - timedelta(days=k)).strftime("%Y%m%d"))
             for k in (0, 1)]
    best = None
    for base in bases:
        try:
            run_dirs = [d for d in _list_index(base) if re.fullmatch(r"\d{2}/", d)]
        except OSError:
            continue
        for run in run_dirs:
            folder = f"{base}{run}000/"
            try:
                files = _list_index(folder)
            except OSError:
                continue
            f = next((x for x in files if FIREWORK_PRODUCT in x and x.endswith(".grib2")), None)
            if not f:
                continue
            m = _STAMP_RE.search(f)
            if not m:
                continue
            key = (int(m.group(1)), int(m.group(2)))
            if best is None or key > best[0]:
                best = (key, f"{m.group(1)}T{m.group(2)}Z", folder + f)
        if best:  # newest date first — stop once a populated date is found
            break
    if not best:
        raise RuntimeError("No RAQDPS wildfire-smoke plume file found on the Datamart")
    return best[1], best[2]


def sample_firework(grib_path, mesh):
    """Sample the RAQDPS smoke-plume GRIB onto the OnQC mesh (Pi-only deps)."""
    import numpy as np
    import pygrib
    from scipy.spatial import cKDTree

    grbs = pygrib.open(grib_path)
    try:
        grb = grbs[1]
        glat, glon = grb.latlons()
        vals = np.ma.filled(np.ma.asarray(grb.values, dtype="float64"), np.nan)
    finally:
        grbs.close()
    glon = np.where(glon > 180.0, glon - 360.0, glon)

    def unit(lat, lon):
        la, lo = np.radians(lat), np.radians(lon)
        cl = np.cos(la)
        return np.stack([cl * np.cos(lo), cl * np.sin(lo), np.sin(la)], axis=-1)

    tree = cKDTree(unit(glat.ravel(), glon.ravel()))
    q = unit(np.asarray(mesh["lats"]), np.asarray(mesh["lons"]))
    _, idx = tree.query(q)
    flat = vals.ravel()
    out, mx = [], 0.0
    for i in idx:
        v = flat[i]
        if v is None or (isinstance(v, float) and math.isnan(v)):
            out.append(None)
        else:
            v = round(float(v), 1)
            out.append(v); mx = max(mx, v)
    return out, mx


# ---------------------------------------------------------------------------
# Slice / status persistence
# ---------------------------------------------------------------------------
def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")


def _disk_stats():
    total, used, free = shutil.disk_usage(str(DATA_DIR if DATA_DIR.exists() else HERE))
    data_bytes = sum(f.stat().st_size for f in SLICE_DIR.glob("*.json")) if SLICE_DIR.exists() else 0
    return {"free_gb": round(free / 1e9, 1), "used_gb": round(used / 1e9, 1),
            "data_mb": round(data_bytes / 1e6, 1)}


def _store_slice(source, run, mesh, values, valid_time, max_pm):
    SLICE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source, "run": run, "valid_time": valid_time,
        "collected_at": _now(),
        "rows": mesh["rows"], "cols": mesh["cols"], "spacing": mesh["spacing"],
        "bbox": mesh["bbox"], "max_pm": max_pm, "values": values,
    }
    slice_path = SLICE_DIR / f"{source}_{run}.json"
    new_slice = not slice_path.exists()
    _write_json(slice_path, payload)               # archive (training data)
    _write_json(DATA_DIR / f"latest_{source}.json", payload)  # dashboard
    return new_slice


def _append_history(event):
    hist = _read_json(DATA_DIR / "history.json", [])
    hist.append(event)
    _write_json(DATA_DIR / "history.json", hist[-500:])  # keep last 500


def _update_status(source, patch):
    st = _read_json(DATA_DIR / "status.json", {})
    st.setdefault("started_at", _now())
    st["host"] = socket.gethostname()
    st["updated_at"] = _now()
    st["disk"] = _disk_stats()
    src = st.setdefault("sources", {}).setdefault(source, {})
    src.update(patch)
    st["total_slices"] = len(list(SLICE_DIR.glob("*.json"))) if SLICE_DIR.exists() else 0
    _write_json(DATA_DIR / "status.json", st)


# ---------------------------------------------------------------------------
# One collection cycle per source
# ---------------------------------------------------------------------------
SOURCES = {
    "bluesky":  {"discover": discover_bluesky,  "sample": "bluesky",  "cadence_h": 6},
    "firework": {"discover": discover_firework, "sample": "firework", "cadence_h": 12},
}


def collect(source, mesh, tmp):
    cfg = SOURCES[source]
    _update_status(source, {"last_attempt": _now(), "ok": None,
                            "cadence_hours": cfg["cadence_h"]})
    try:
        run, url = cfg["discover"]()
        st = _read_json(DATA_DIR / "status.json", {})
        if (st.get("sources", {}).get(source, {}) or {}).get("last_run") == run:
            _update_status(source, {"ok": True, "error": None,
                                    "note": "already current; skipped download"})
            print(f"  [{source}] run {run} already collected; skip")
            return
        print(f"  [{source}] new run {run} -> {url}")
        local = tmp / Path(url.split("?")[0]).name
        local.write_bytes(_http_get(url, timeout=180))

        if cfg["sample"] == "bluesky":
            values, valid, mx = sample_bluesky(str(local), mesh)
        else:
            values, mx = sample_firework(str(local), mesh); valid = run
        local.unlink(missing_ok=True)

        new = _store_slice(source, run, mesh, values, valid, mx)
        live = sum(1 for v in values if v is not None)
        _update_status(source, {
            "ok": True, "error": None, "note": None, "last_success": _now(),
            "last_run": run, "last_cells": live, "last_max_pm": mx,
            "slices": len(list(SLICE_DIR.glob(f"{source}_*.json"))),
        })
        _append_history({"t": _now(), "source": source, "run": run,
                         "cells": live, "max_pm": mx, "ok": True, "new": new})
        print(f"  [{source}] stored run {run}: {live} cells, max {mx} ug/m3")
    except Exception as exc:  # noqa: BLE001 — never crash the timer; log to status
        _update_status(source, {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"})
        _append_history({"t": _now(), "source": source, "ok": False,
                         "error": exc.__class__.__name__})
        print(f"  [{source}] FAILED: {exc.__class__.__name__}: {exc}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="CLEAR smoke collector (BlueSky + FireWork).")
    ap.add_argument("--sources", default="bluesky,firework",
                    help="comma list of sources to collect")
    ap.add_argument("--loop", type=int, default=0,
                    help="run forever, sleeping this many minutes between cycles (0 = once)")
    ap.add_argument("--status", action="store_true", help="print current status.json and exit")
    ap.add_argument("--discover", action="store_true",
                    help="just resolve the latest run URLs (no download); for testing")
    args = ap.parse_args(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if args.status:
        print(json.dumps(_read_json(DATA_DIR / "status.json", {}), indent=2))
        return 0
    if args.discover:
        for s in args.sources.split(","):
            try:
                print(f"{s}: {SOURCES[s.strip()]['discover']()}")
            except Exception as exc:  # noqa: BLE001
                print(f"{s}: ERROR {exc.__class__.__name__}: {exc}")
        return 0

    mesh = build_mesh()
    sources = [s.strip() for s in args.sources.split(",") if s.strip() in SOURCES]
    while True:
        print(f"=== collection cycle {_now()} ({mesh['rows']}x{mesh['cols']} OnQC mesh) ===")
        with tempfile.TemporaryDirectory() as td:
            for s in sources:
                collect(s, mesh, Path(td))
        if not args.loop:
            return 0
        print(f"sleeping {args.loop} min...")
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    raise SystemExit(main())
