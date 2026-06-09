#!/usr/bin/env python3
"""ECCC MSC Datamart air-quality model ingestion (step 1: ingest + store only).

Reads PM2.5 from two Environment and Climate Change Canada (ECCC) gridded
models on the MSC Datamart and samples them at CLEAR's monitoring-station
coordinates, then POSTs a compact JSON payload to the website's ingest
endpoint (which writes a CachedResult). It does NOT touch the 3-rule alert
engine — fusion is a deferred methodology decision (see CLAUDE.md / the
"eccc-purpleair-fusion-vision" memory).

Two products
------------
* RDAQA  (model_rdaqa/10km/{HH}/)            -- hourly ANALYSIS  (current state)
* RAQDPS (model_raqdps/10km/grib2/{RUN}/...) -- 72h FORECAST (RUN in {00, 12})

WHY this runs in a GitHub Actions runner and not on Vercel
----------------------------------------------------------
Reading GRIB2 needs heavy native libs (eccodes via pygrib) that do not fit the
slim Vercel lambda. So the heavy lifting happens here, off-platform, and only a
small station-sampled JSON is sent to the web app -- exactly mirroring how the
existing /api/refresh/ flow keeps WAQI parsing off the serverless function.

Discovery is by the date/run stamp embedded in each FILENAME, never by Apache
directory-listing order (which ECCC does not guarantee).

Usage
-----
    python scripts/eccc_ingest.py --analysis --forecast \
        --endpoint https://clear25.xyz/api/refresh/eccc/

Auth: set CRON_SECRET in the environment (same value as the website).
Use --dry-run to print the payload instead of POSTing (no secret needed).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

# Heavy deps (CI only) -- see scripts/requirements-eccc.txt
try:
    import numpy as np
    import pygrib
    from scipy.spatial import cKDTree
except ImportError as exc:  # pragma: no cover - only meaningful in CI
    print(
        f"Missing scientific dependency: {exc}. "
        "Install with: pip install -r scripts/requirements-eccc.txt",
        file=sys.stderr,
    )
    raise

RDAQA_BASE = "https://dd.weather.gc.ca/today/model_rdaqa/10km/"
RAQDPS_BASE = "https://dd.weather.gc.ca/today/model_raqdps/10km/grib2/"

# Product substrings we sample, keyed by the series name stored in the payload.
# RDAQA analysis: total surface PM2.5 (final analysis, not Prelim/FW preview).
RDAQA_PRODUCTS = {
    "pm25": "_MSC_RDAQA_PM2.5_Sfc_",
}
# RAQDPS forecast: total surface PM2.5 + the wildfire-smoke-only surface PM2.5.
# The wildfire product isolates the smoke contribution -- ideal for CLEAR.
RAQDPS_PRODUCTS = {
    "pm25": "_MSC_RAQDPS_PM2.5_Sfc_",
    "pm25_wildfire": "_MSC_RAQDPS_PM2.5-WildfireSmokePlume_Sfc_",
}

# Stamp like 20260531T00Z embedded in every filename.
_STAMP_RE = re.compile(r"(\d{8})T(\d{2})Z")
_HREF_RE = re.compile(r'href="([^"?][^"]*)"', re.IGNORECASE)

HERE = Path(__file__).resolve().parent
BUNDLED_STATIONS = HERE.parent / "webapp" / "dashboard" / "services" / "bundled_stations.json"

# Target-city centroids (mirror services/data.py CITIES) -- sampled in addition
# to stations so the forecast can be read directly at each city cell.
CITY_CENTROIDS = {
    "Toronto":   (43.7479, -79.2741),
    "Montreal":  (45.5027, -73.6639),
    "Edmonton":  (53.5482, -113.3681),
    "Vancouver": (49.3686, -123.2767),
}

# Far-field extension: virtual upwind sample points BEYOND the ~600 km station
# ring. The grid has values everywhere, so no physical monitor is required here --
# these are how the forecast sees smoke originating past the station network.
#
# Geometry is anchored in the methodology's documented smoke corridors: boreal
# fires arrive from the N/NW and the Québec upstream corridor reaches out to
# ~1400 km NE (Rule 3). We cast points along those bearings at a few distances.
# First-pass defaults -- easy to retune later once checked against event data.
_FAR_FIELD_BEARINGS_DEG = [315, 0, 45]        # NW, N, NE
_FAR_FIELD_DISTANCES_KM = [800, 1100, 1400]


def _destination_point(lat, lon, bearing_deg, distance_km):
    """Great-circle destination from (lat,lon) along a bearing for a distance."""
    R = 6371.0
    br = math.radians(bearing_deg)
    la1, lo1 = math.radians(lat), math.radians(lon)
    dr = distance_km / R
    la2 = math.asin(math.sin(la1) * math.cos(dr) + math.cos(la1) * math.sin(dr) * math.cos(br))
    lo2 = lo1 + math.atan2(
        math.sin(br) * math.sin(dr) * math.cos(la1),
        math.cos(dr) - math.sin(la1) * math.sin(la2),
    )
    return (round(math.degrees(la2), 4), round(((math.degrees(lo2) + 540) % 360) - 180, 4))


def _build_far_field():
    """name -> (lat, lon, city) for every far-field point around every city."""
    pts = {}
    for city, (lat, lon) in CITY_CENTROIDS.items():
        for brg in _FAR_FIELD_BEARINGS_DEG:
            for dist in _FAR_FIELD_DISTANCES_KM:
                name = f"{city}-{brg:03d}deg-{dist}km"
                dlat, dlon = _destination_point(lat, lon, brg, dist)
                pts[name] = (dlat, dlon, city)
    return pts


FAR_FIELD_POINTS = _build_far_field()


# ---------------------------------------------------------------------------
# Ontario RDAQA mesh (analysis only) -- a regular lat/lon grid over Ontario so
# the Plan plume tracker can render a true gridded 10 km model surface, not just
# sparse station points. Sampled ONLY in the hourly ANALYSIS (RDAQA), NOT in the
# 73-hour forecast, so the forecast payload stays small. Stored compactly as a
# flat row-major values array (north->south rows, west->east cols), so ~8k cells
# cost only tens of KB. Bbox MUST stay in sync with
# webapp/dashboard/services/purpleair.py ONTARIO_BBOX.
# ---------------------------------------------------------------------------
ONTARIO_BBOX = {"nwlat": 56.9, "nwlng": -95.2, "selat": 41.6, "selng": -74.3}
ONTARIO_MESH_SPACING_DEG = 0.2  # ~22 km lat; coarser than RDAQA's 10 km but bounded


def build_ontario_mesh(spacing=ONTARIO_MESH_SPACING_DEG, bbox=None):
    """Regular lat/lon grid over the Ontario bbox.

    Returns a dict with row-major cell-center coordinates (row 0 = north, col 0 =
    west) so the frontend can paint values[r*cols+c] straight onto a canvas:
        {"rows", "cols", "spacing", "bbox", "coords": [(lat, lon), ...]}
    Pure-Python (no numpy) so it is unit-testable on a box without the grib deps.
    """
    b = bbox or ONTARIO_BBOX
    cols = max(1, int(round((b["selng"] - b["nwlng"]) / spacing)))
    rows = max(1, int(round((b["nwlat"] - b["selat"]) / spacing)))
    coords = []
    for r in range(rows):
        lat = b["nwlat"] - (r + 0.5) * spacing          # north -> south
        for c in range(cols):
            lon = b["nwlng"] + (c + 0.5) * spacing       # west -> east
            coords.append((round(lat, 4), round(lon, 4)))
    return {"rows": rows, "cols": cols, "spacing": spacing, "bbox": b, "coords": coords}


# ---------------------------------------------------------------------------
# Directory-index helpers (parse Apache listing; pick by filename stamp)
# ---------------------------------------------------------------------------
def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "CLEAR25-eccc-ingest/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted gov host)
        return resp.read()


def _list_index(url: str) -> list[str]:
    """Return hrefs in an Apache directory index, excluding parent/sort links."""
    html = _http_get(url).decode("utf-8", "replace")
    out = []
    for href in _HREF_RE.findall(html):
        if href.startswith(("/", "?", "..")) or href in ("../",):
            continue
        out.append(href)
    return out


def _stamp_key(name: str) -> tuple[int, int]:
    """Sortable (yyyymmdd, hh) parsed from a filename/href; (0,0) if absent."""
    m = _STAMP_RE.search(name)
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


# ---------------------------------------------------------------------------
# Run discovery
# ---------------------------------------------------------------------------
def find_latest_rdaqa(products: dict[str, str]) -> dict | None:
    """Find the most recent RDAQA hourly run by the stamp inside its PM2.5 file.

    Returns {"run": "YYYYMMDDTHHZ", "files": {series: url}} or None.
    Checks every hour folder's filename stamp rather than trusting listing order
    (handles the yesterday-22/23 folders that linger in /today).
    """
    hour_dirs = [h for h in _list_index(RDAQA_BASE) if re.fullmatch(r"\d{2}/", h)]
    best = None
    best_key = (0, 0)
    primary = next(iter(products.values()))
    for hd in hour_dirs:
        folder = RDAQA_BASE + hd
        try:
            files = _list_index(folder)
        except OSError:
            continue
        match = next((f for f in files if primary in f and f.endswith(".grib2")), None)
        if not match:
            continue
        key = _stamp_key(match)
        if key > best_key:
            best_key = key
            urls = {}
            for series, sub in products.items():
                fm = next((f for f in files if sub in f and f.endswith(".grib2")), None)
                if fm:
                    urls[series] = folder + fm
            if urls:
                m = _STAMP_RE.search(match)
                best = {"run": f"{m.group(1)}T{m.group(2)}Z", "files": urls}
    return best


def _probe_raqdps_run(run: str, products: dict[str, str], max_hours: int):
    """Inspect a single run folder ('00/' or '12/').

    Returns ``(stamp_key, stamp, hour_dirs, sample, run_url)`` for a run that has a
    usable primary PM2.5 file in its lowest forecast-hour folder, else ``None``.
    ``stamp_key`` is the sortable (yyyymmdd, hh) read from the actual filename.
    """
    run_url = RAQDPS_BASE + run
    try:
        hour_dirs = sorted(
            int(h.rstrip("/")) for h in _list_index(run_url) if re.fullmatch(r"\d{3}/", h)
        )
    except OSError:
        return None
    hour_dirs = [h for h in hour_dirs if h <= max_hours]
    if not hour_dirs:
        return None
    primary = next(iter(products.values()))
    try:
        first_files = _list_index(f"{run_url}{hour_dirs[0]:03d}/")
    except OSError:
        return None
    sample = next((f for f in first_files if primary in f and f.endswith(".grib2")), None)
    if not sample:
        return None
    key = _stamp_key(sample)
    if key == (0, 0):
        return None
    stamp = f"{key[0]:08d}T{key[1]:02d}Z"
    return key, stamp, hour_dirs, sample, run_url


def find_latest_raqdps(products: dict[str, str], max_hours: int) -> dict | None:
    """Find the most recent RAQDPS forecast run and build per-hour file URLs.

    Returns {"run": "...", "hours": [0..N], "files": {series: {hour: url}}}.

    Run-folder names ('00'/'12') do NOT encode the date, and ECCC's /today keeps
    the previous day's files inside a run folder until that run is re-published.
    So the run is chosen by the date+hour stamp embedded in its 000-hour FILENAME
    (newest wins), never by the numeric run id -- otherwise, in the window before
    today's 12Z run lands, the stale '12' folder (still holding yesterday's files)
    would be preferred over today's fresh '00'. This mirrors find_latest_rdaqa's
    stamp-based discovery, and also lets us fall back to the other run when the
    newest folder exists but hasn't published its PM2.5 file yet.

    The date+run prefix is constant across all forecast-hour folders of a run, so
    we read it once from the 000 folder and synthesise the rest.
    """
    run_dirs = [d for d in _list_index(RAQDPS_BASE) if re.fullmatch(r"\d{2}/", d)]
    if not run_dirs:
        return None
    best = None
    for run in run_dirs:
        probe = _probe_raqdps_run(run, products, max_hours)
        if probe and (best is None or probe[0] > best[0]):
            best = probe
    if best is None:
        return None
    _key, stamp, hour_dirs, sample, run_url = best

    primary = next(iter(products.values()))
    files: dict[str, dict[int, str]] = {s: {} for s in products}
    for h in hour_dirs:
        for series, sub in products.items():
            # Filename = <stamp>_MSC_RAQDPS_<var>_Sfc_RLatLon0.09_PT<HHH>H.grib2
            # Rebuild from the sample by swapping product substring + PT hour.
            fname = sample.replace(primary, sub).replace(
                f"PT{int(hour_dirs[0]):03d}H", f"PT{h:03d}H"
            )
            files[series][h] = f"{run_url}{h:03d}/{fname}"
    return {"run": stamp, "hours": hour_dirs, "files": files}


# ---------------------------------------------------------------------------
# GRIB sampling
# ---------------------------------------------------------------------------
def _unit_sphere(lat, lon):
    """lat/lon (deg) -> 3D unit-sphere xyz for true nearest-neighbour search."""
    la = np.radians(lat)
    lo = np.radians(lon)
    cl = np.cos(la)
    return np.stack([cl * np.cos(lo), cl * np.sin(lo), np.sin(la)], axis=-1)


class _GridSampler:
    """Builds one KDTree per grid (lat/lon identical across a model run) and
    samples nearest grid values for a fixed set of query points."""

    def __init__(self, grib_path: str, point_ids: list[str], point_coords: np.ndarray):
        grbs = pygrib.open(grib_path)
        try:
            grb = grbs[1]  # these ECCC files hold a single field
            lats, lons = grb.latlons()
        finally:
            grbs.close()
        lons = np.where(lons > 180.0, lons - 360.0, lons)  # normalise to [-180,180]
        self._shape = lats.shape
        self._tree = cKDTree(_unit_sphere(lats.ravel(), lons.ravel()))
        _, self._idx = self._tree.query(_unit_sphere(point_coords[:, 0], point_coords[:, 1]))
        self._ids = point_ids

    def sample(self, grib_path: str) -> dict[str, float | None]:
        grbs = pygrib.open(grib_path)
        try:
            # pygrib returns a numpy MaskedArray when the field carries a bitmap of
            # missing cells. np.asarray() would DROP that mask and surface the raw
            # fill value as if it were real data, so fill masked cells with NaN and
            # let the math.isnan() guard below reject them.
            raw = grbs[1].values
            vals = np.ma.filled(np.ma.asarray(raw, dtype="float64"), np.nan).ravel()
        finally:
            grbs.close()
        out: dict[str, float | None] = {}
        for sid, gi in zip(self._ids, self._idx):
            v = vals[gi]
            out[sid] = None if math.isnan(v) else round(float(v), 1)
        return out


# ---------------------------------------------------------------------------
# Sample point catalogue
# ---------------------------------------------------------------------------
def load_sample_points() -> tuple[list[str], np.ndarray, dict[str, str]]:
    """Return (ids, Nx2 lat/lon array, id->target_city) for stations + cities."""
    data = json.loads(BUNDLED_STATIONS.read_text(encoding="utf-8"))
    ids: list[str] = []
    coords: list[tuple[float, float]] = []
    city_of: dict[str, str] = {}
    for city, stations in data.items():
        for st in stations:
            lat, lon = st.get("lat"), st.get("lon")
            if lat is None or lon is None:
                continue
            sid = f"{st['id']}|{city}"
            ids.append(sid)
            coords.append((float(lat), float(lon)))
            city_of[sid] = city
    for city, (lat, lon) in CITY_CENTROIDS.items():
        sid = f"CITY:{city}"
        ids.append(sid)
        coords.append((lat, lon))
        city_of[sid] = city
    for name, (lat, lon, city) in FAR_FIELD_POINTS.items():
        sid = f"FAR:{name}"
        ids.append(sid)
        coords.append((lat, lon))
        city_of[sid] = city
    return ids, np.asarray(coords, dtype="float64"), city_of


# ---------------------------------------------------------------------------
# Download + ingest
# ---------------------------------------------------------------------------
def _download(url: str, dest: Path) -> None:
    data = _http_get(url, timeout=120)
    dest.write_bytes(data)


def ingest_analysis(ids, coords, city_of, tmp: Path, mesh: dict | None = None) -> dict:
    run = find_latest_rdaqa(RDAQA_PRODUCTS)
    if not run:
        raise RuntimeError("No RDAQA run found on Datamart")
    primary_series = next(iter(RDAQA_PRODUCTS))
    mesh_coords = np.asarray(mesh["coords"], dtype="float64") if mesh else None
    mesh_values = None
    sampler = None
    points: dict[str, dict[str, float | None]] = {sid: {} for sid in ids}
    for series, url in run["files"].items():
        gpath = tmp / Path(url).name
        _download(url, gpath)
        if sampler is None:
            sampler = _GridSampler(str(gpath), ids, coords)
        vals = sampler.sample(str(gpath))
        for sid in ids:
            points[sid][series] = vals.get(sid)
        # Gridded Ontario mesh: sample the primary (total PM2.5) field only, on
        # the analysis pass, for the Plan plume-tracker model surface.
        if mesh_coords is not None and series == primary_series:
            mesh_idx = list(range(len(mesh_coords)))
            mesh_sampler = _GridSampler(str(gpath), mesh_idx, mesh_coords)
            mvals = mesh_sampler.sample(str(gpath))
            mesh_values = [mvals.get(i) for i in mesh_idx]
        gpath.unlink(missing_ok=True)
    payload = {
        "kind": "analysis",
        "model": "RDAQA",
        "run": run["run"],
        "series": list(run["files"].keys()),
        "city_of": city_of,
        "points": points,
    }
    if mesh is not None and mesh_values is not None:
        payload["mesh"] = {
            "var": primary_series,
            "rows": mesh["rows"],
            "cols": mesh["cols"],
            "spacing": mesh["spacing"],
            "bbox": mesh["bbox"],
            "values": mesh_values,  # row-major, north->south, west->east
        }
    return payload


def ingest_forecast(ids, coords, city_of, tmp: Path, max_hours: int) -> dict:
    run = find_latest_raqdps(RAQDPS_PRODUCTS, max_hours)
    if not run:
        raise RuntimeError("No RAQDPS run found on Datamart")
    sampler = None
    # points[sid][series] = [value per forecast hour, aligned with run["hours"]]
    points: dict[str, dict[str, list]] = {
        sid: {s: [] for s in run["files"]} for sid in ids
    }
    for series, hour_urls in run["files"].items():
        for h in run["hours"]:
            url = hour_urls[h]
            gpath = tmp / Path(url).name
            try:
                _download(url, gpath)
            except OSError:
                for sid in ids:
                    points[sid][series].append(None)
                continue
            if sampler is None:
                sampler = _GridSampler(str(gpath), ids, coords)
            vals = sampler.sample(str(gpath))
            for sid in ids:
                points[sid][series].append(vals.get(sid))
            gpath.unlink(missing_ok=True)
    return {
        "kind": "forecast",
        "model": "RAQDPS",
        "run": run["run"],
        "hours": run["hours"],
        "series": list(run["files"].keys()),
        "city_of": city_of,
        "points": points,
    }


def post_payload(endpoint: str, payload: dict) -> None:
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        raise SystemExit("CRON_SECRET not set (required to POST; use --dry-run to skip)")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        print(f"  -> {endpoint}: HTTP {resp.status} {resp.read().decode('utf-8', 'replace')}")


def _stored_run(endpoint: str, kind: str) -> str | None:
    """Read the run stamp already stored, to skip redundant heavy downloads."""
    read_url = endpoint.replace("/api/refresh/eccc/", "/api/eccc/") + f"?kind={kind}"
    try:
        data = json.loads(_http_get(read_url, timeout=30))
        return (data.get("data") or {}).get("run")
    except OSError:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ingest ECCC RDAQA/RAQDPS PM2.5 at CLEAR stations.")
    ap.add_argument("--analysis", action="store_true", help="ingest RDAQA hourly analysis")
    ap.add_argument("--forecast", action="store_true", help="ingest RAQDPS 72h forecast")
    ap.add_argument("--endpoint", default=os.environ.get("ECCC_INGEST_URL", ""),
                    help="ingest endpoint, e.g. https://clear25.xyz/api/refresh/eccc/")
    ap.add_argument("--max-forecast-hours", type=int, default=72)
    ap.add_argument("--skip-if-current", action="store_true",
                    help="skip forecast download if the stored run stamp matches the latest")
    ap.add_argument("--dry-run", action="store_true", help="print payload, do not POST")
    ap.add_argument("--no-mesh", action="store_true",
                    help="skip the Ontario RDAQA analysis mesh (plume-tracker model surface)")
    args = ap.parse_args(argv)

    if not (args.analysis or args.forecast):
        ap.error("choose at least one of --analysis / --forecast")
    if not args.dry_run and not args.endpoint:
        ap.error("--endpoint is required unless --dry-run")

    ids, coords, city_of = load_sample_points()
    print(f"Sampling {len(ids)} points ({len(CITY_CENTROIDS)} city centroids + stations).")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if args.analysis:
            print("RDAQA analysis...")
            mesh = None if args.no_mesh else build_ontario_mesh()
            if mesh:
                print(f"  Ontario mesh: {mesh['rows']}x{mesh['cols']} = {len(mesh['coords'])} cells "
                      f"@ {mesh['spacing']} deg")
            payload = ingest_analysis(ids, coords, city_of, tmp, mesh=mesh)
            mesh_info = f" mesh={'yes' if payload.get('mesh') else 'no'}"
            print(f"  run={payload['run']} series={payload['series']}{mesh_info}")
            _emit(payload, args)
        if args.forecast:
            print("RAQDPS forecast...")
            if args.skip_if_current and not args.dry_run:
                latest = find_latest_raqdps(RAQDPS_PRODUCTS, args.max_forecast_hours)
                if latest and _stored_run(args.endpoint, "forecast") == latest["run"]:
                    print(f"  stored run {latest['run']} already current; skipping.")
                    return 0
            payload = ingest_forecast(ids, coords, city_of, tmp, args.max_forecast_hours)
            print(f"  run={payload['run']} hours={len(payload['hours'])} series={payload['series']}")
            _emit(payload, args)
    return 0


def _emit(payload: dict, args) -> None:
    if args.dry_run:
        # Print a compact summary, not the full (large) payload.
        sample_id = next(iter(payload["points"]))
        print(json.dumps({
            "kind": payload["kind"], "run": payload["run"],
            "series": payload["series"], "n_points": len(payload["points"]),
            "example": {sample_id: payload["points"][sample_id]},
        }, indent=2)[:1500])
    else:
        post_payload(args.endpoint, payload)


if __name__ == "__main__":
    raise SystemExit(main())
