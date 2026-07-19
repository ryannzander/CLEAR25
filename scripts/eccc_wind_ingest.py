#!/usr/bin/env python3
"""ECCC RDPS 10 m WIND ingestion — the LIVE / forecast half of the wind integration.

Companion to scripts/gen_wind_hourly.py (the HISTORICAL half, Open-Meteo ERA5).
That script builds training wind; this one samples ECCC's operational weather model
so the deployed early-warning model can eventually read *current + forecast* wind.

WHAT THIS DOES (step 1: ingest + store only — mirrors scripts/eccc_ingest.py):
  * discovers the latest RDPS run on the MSC Datamart,
  * samples 10 m wind SPEED + DIRECTION at CLEAR's station / city / far-field
    coordinates (the same catalogue the PM ingest uses),
  * POSTs a compact JSON to /api/refresh/eccc/ under NEW kinds
    (wind_analysis / wind_forecast → CachedResult eccc_wind_analysis / _forecast).
It does NOT touch evaluate.py, the 3-rule alert engine, or CachedResult(key="latest").
Feeding this wind into a live prediction is a separate, deferred step.

WHY RDPS (model_rdps), not the air-quality model
------------------------------------------------
Wind is a meteorological field. The RDPS (Regional Deterministic Prediction System,
10 km) publishes the IDENTICAL `RLatLon0.09` grid and
`{stamp}_MSC_RDPS_{Var}_{Level}_..._PT{FFF}H.grib2` filename convention as the RAQDPS
air-quality model, so the same nearest-cell KDTree sampler works unchanged.

Products (verified live on the Datamart 2026-07-19):
  _MSC_RDPS_WindSpeed_AGL-10m_  → 10 m wind speed  (m/s)
  _MSC_RDPS_WindDir_AGL-10m_    → 10 m wind DIRECTION (deg the wind blows FROM,
                                  meteorological — matches Open-Meteo wind_direction_10m,
                                  so scripts/wind_features.py consumes it with NO change)

⚠️ DATAMART PATH: uses the NEW date-partitioned layout
    https://dd.weather.gc.ca/{YYYYMMDD}/WXO-DD/model_rdps/10km/{HH}/{FFF}/
(scan today→yesterday UTC, pick the run by the FILENAME stamp — never by directory
order). This mirrors smoke_pi/collector.py. The old `/today/` base still hardcoded in
scripts/eccc_ingest.py is DEAD; do not copy it.

Heavy deps (numpy/pygrib/scipy) are imported LAZILY inside the sampler so this module
imports and its discovery logic runs on any box; only the actual GRIB read needs them
(CI only — see scripts/requirements-eccc.txt). Use --discover-only to exercise
discovery + URL synthesis against the live Datamart with no scientific deps at all.

Usage
-----
    python scripts/eccc_wind_ingest.py --analysis --forecast \
        --endpoint https://clear25.xyz/api/refresh/eccc/
    python scripts/eccc_wind_ingest.py --discover-only        # no pygrib needed
Auth: CRON_SECRET in the environment (same value as the website). --dry-run samples
but does not POST (still needs pygrib). --discover-only lists URLs and exits.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLED_STATIONS = HERE.parent / "webapp" / "dashboard" / "services" / "bundled_stations.json"

# NEW date-partitioned Datamart base (see module docstring). {date} = YYYYMMDD UTC.
RDPS_DATE_TMPL = "https://dd.weather.gc.ca/{date}/WXO-DD/model_rdps/10km/"

# 10 m wind products, keyed by the series name stored in the payload.
WIND_PRODUCTS = {
    "wind_speed": "_MSC_RDPS_WindSpeed_AGL-10m_",
    "wind_dir": "_MSC_RDPS_WindDir_AGL-10m_",
}

_STAMP_RE = re.compile(r"(\d{8})T(\d{2})Z")
_HREF_RE = re.compile(r'href="([^"?][^"]*)"', re.IGNORECASE)

# --------------------------------------------------------------------------- #
# Sample-point catalogue.
# KEEP IN SYNC with scripts/eccc_ingest.py (CITY_CENTROIDS / FAR_FIELD geometry /
# load_sample_points). The wind sample points MUST match the PM sample points id-for-id
# so a future fusion can pair wind[sid] with PM[sid]. Duplicated (not imported) only
# because eccc_ingest.py's module-top pygrib import makes it un-importable off-CI; the
# repo already uses this "duplicate + sync note" pattern for ONTARIO_BBOX.
# --------------------------------------------------------------------------- #
CITY_CENTROIDS = {
    "Toronto":   (43.7479, -79.2741),
    "Montreal":  (45.5027, -73.6639),
    "Edmonton":  (53.5482, -113.3681),
    "Vancouver": (49.3686, -123.2767),
}
_FAR_FIELD_BEARINGS_DEG = [315, 0, 45]        # NW, N, NE (documented smoke corridors)
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
    pts = {}
    for city, (lat, lon) in CITY_CENTROIDS.items():
        for brg in _FAR_FIELD_BEARINGS_DEG:
            for dist in _FAR_FIELD_DISTANCES_KM:
                name = f"{city}-{brg:03d}deg-{dist}km"
                dlat, dlon = _destination_point(lat, lon, brg, dist)
                pts[name] = (dlat, dlon, city)
    return pts


FAR_FIELD_POINTS = _build_far_field()


def load_sample_points():
    """Return (ids, coords, city_of): stations + city centroids + far-field points.

    coords is a plain list of (lat, lon) tuples (no numpy) so this stays importable
    and testable without the CI-only scientific deps.
    """
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
    return ids, coords, city_of


# --------------------------------------------------------------------------- #
# Datamart directory helpers (stdlib only)
# --------------------------------------------------------------------------- #
def _http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "CLEAR25-wind-ingest/1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted gov host)
        return resp.read()


def _list_index(url: str) -> list[str]:
    """Hrefs in an Apache directory index, excluding parent/sort links."""
    html = _http_get(url).decode("utf-8", "replace")
    out = []
    for href in _HREF_RE.findall(html):
        if href.startswith(("/", "?", "..")):
            continue
        out.append(href)
    return out


def _rdps_bases():
    """Today then yesterday (UTC) date-partitioned RDPS bases."""
    today = datetime.now(timezone.utc).date()
    return [RDPS_DATE_TMPL.format(date=(today - timedelta(days=k)).strftime("%Y%m%d"))
            for k in (0, 1)]


def find_latest_rdps(products: dict[str, str], max_hours: int) -> dict | None:
    """Find the newest RDPS run and synthesise per-(series, hour) file URLs.

    Returns {"run": "YYYYMMDDTHHZ", "base", "run_folder", "hours": [...],
             "files": {series: {hour: url}}} or None.

    The run is chosen by the (date, hour) stamp in its 000-hour PRIMARY filename
    (newest wins), scanning today→yesterday so a not-yet-published today folder
    falls back to yesterday. The date+run prefix is constant across all
    forecast-hour folders of a run, so we read one 000 filename and rebuild the rest.
    """
    primary = next(iter(products.values()))
    best = None  # (stamp_key, stamp, base, run, hour_dirs, sample_name)
    for base in _rdps_bases():
        try:
            run_dirs = [d for d in _list_index(base) if re.fullmatch(r"\d{2}/", d)]
        except OSError:
            continue
        for run in run_dirs:
            try:
                files = _list_index(f"{base}{run}000/")
            except OSError:
                continue
            sample = next((f for f in files if primary in f and f.endswith(".grib2")), None)
            if not sample:
                continue
            m = _STAMP_RE.search(sample)
            if not m:
                continue
            key = (int(m.group(1)), int(m.group(2)))
            if best is None or key > best[0]:
                try:
                    hour_dirs = sorted(
                        int(h.rstrip("/")) for h in _list_index(f"{base}{run}")
                        if re.fullmatch(r"\d{3}/", h)
                    )
                except OSError:
                    hour_dirs = [0]
                hour_dirs = [h for h in hour_dirs if h <= max_hours] or [0]
                best = (key, f"{m.group(1)}T{m.group(2)}Z", base, run, hour_dirs, sample)
        if best is not None:      # today's newest run beats yesterday's; stop.
            break
    if best is None:
        return None
    _key, stamp, base, run, hour_dirs, sample = best
    files: dict[str, dict[int, str]] = {s: {} for s in products}
    for h in hour_dirs:
        for series, sub in products.items():
            fname = sample.replace(primary, sub).replace("PT000H", f"PT{h:03d}H")
            files[series][h] = f"{base}{run}{h:03d}/{fname}"
    return {"run": stamp, "base": base, "run_folder": run, "hours": hour_dirs, "files": files}


# --------------------------------------------------------------------------- #
# GRIB sampling (heavy deps imported lazily — CI only)
# --------------------------------------------------------------------------- #
def _ensure_heavy():
    try:
        import numpy  # noqa: F401
        import pygrib  # noqa: F401
        import scipy.spatial  # noqa: F401
    except ImportError as exc:  # pragma: no cover - only meaningful off-CI
        raise SystemExit(
            f"Missing GRIB dependency: {exc}. Install with: "
            "pip install -r scripts/requirements-eccc.txt (CI only)."
        )


class _WindGridSampler:
    """One KDTree on the RDPS grid (identical across the run's wind fields), reused
    to sample nearest grid values for a fixed set of query points."""

    def __init__(self, grib_path: str, ids: list[str], coords: list[tuple[float, float]]):
        import numpy as np
        import pygrib
        from scipy.spatial import cKDTree

        def unit(lat, lon):
            la, lo = np.radians(lat), np.radians(lon)
            cl = np.cos(la)
            return np.stack([cl * np.cos(lo), cl * np.sin(lo), np.sin(la)], axis=-1)

        grbs = pygrib.open(grib_path)
        try:
            lats, lons = grbs[1].latlons()
        finally:
            grbs.close()
        lons = np.where(lons > 180.0, lons - 360.0, lons)   # normalise to [-180, 180]
        tree = cKDTree(unit(lats.ravel(), lons.ravel()))
        pc = np.asarray(coords, dtype="float64")
        _, self._idx = tree.query(unit(pc[:, 0], pc[:, 1]))
        self._ids = ids

    def sample(self, grib_path: str) -> dict[str, float | None]:
        import numpy as np
        import pygrib

        grbs = pygrib.open(grib_path)
        try:
            # MaskedArray -> NaN for missing cells (np.asarray would drop the mask and
            # surface the fill value as if it were a real wind reading).
            raw = grbs[1].values
            vals = np.ma.filled(np.ma.asarray(raw, dtype="float64"), np.nan).ravel()
        finally:
            grbs.close()
        out: dict[str, float | None] = {}
        for sid, gi in zip(self._ids, self._idx):
            v = vals[gi]
            out[sid] = None if math.isnan(v) else float(v)
        return out


def _round_series(series: str, v):
    """speed → 1 decimal m/s; direction → integer degrees in [0, 360)."""
    if v is None:
        return None
    if series == "wind_dir":
        return int(round(v)) % 360
    return round(v, 1)


# --------------------------------------------------------------------------- #
# Download + ingest
# --------------------------------------------------------------------------- #
def _download(url: str, dest: Path) -> None:
    dest.write_bytes(_http_get(url, timeout=120))


def _sample_run(run: dict, ids, coords, tmp: Path) -> dict:
    """points[sid][series] = [value per forecast hour, aligned with run['hours']]."""
    hours = run["hours"]
    sampler = None
    points: dict[str, dict[str, list]] = {sid: {s: [] for s in run["files"]} for sid in ids}
    for series, hour_urls in run["files"].items():
        for h in hours:
            gpath = tmp / Path(hour_urls[h]).name
            try:
                _download(hour_urls[h], gpath)
            except OSError:
                for sid in ids:
                    points[sid][series].append(None)
                continue
            if sampler is None:   # RDPS wind fields share one grid → build the tree once
                sampler = _WindGridSampler(str(gpath), ids, coords)
            vals = sampler.sample(str(gpath))
            for sid in ids:
                points[sid][series].append(_round_series(series, vals.get(sid)))
            gpath.unlink(missing_ok=True)
    return points


def ingest_wind_analysis(ids, coords, city_of, tmp: Path) -> dict:
    run = find_latest_rdps(WIND_PRODUCTS, 0)
    if not run:
        raise RuntimeError("No RDPS run found on Datamart")
    per_hour = _sample_run(run, ids, coords, tmp)      # each series list has length 1
    points = {sid: {s: vals[0] if vals else None for s, vals in series_map.items()}
              for sid, series_map in per_hour.items()}
    return {
        "kind": "wind_analysis",
        "model": "RDPS",
        "level": "AGL-10m",
        "run": run["run"],
        "series": list(WIND_PRODUCTS),
        "units": {"wind_speed": "m/s", "wind_dir": "deg_from"},
        "city_of": city_of,
        "points": points,
    }


def ingest_wind_forecast(ids, coords, city_of, tmp: Path, max_hours: int) -> dict:
    run = find_latest_rdps(WIND_PRODUCTS, max_hours)
    if not run:
        raise RuntimeError("No RDPS run found on Datamart")
    points = _sample_run(run, ids, coords, tmp)
    return {
        "kind": "wind_forecast",
        "model": "RDPS",
        "level": "AGL-10m",
        "run": run["run"],
        "hours": run["hours"],
        "series": list(WIND_PRODUCTS),
        "units": {"wind_speed": "m/s", "wind_dir": "deg_from"},
        "city_of": city_of,
        "points": points,
    }


def post_payload(endpoint: str, payload: dict) -> None:
    secret = os.environ.get("CRON_SECRET", "")
    if not secret:
        raise SystemExit("CRON_SECRET not set (required to POST; use --dry-run to skip)")
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        print(f"  -> {endpoint}: HTTP {resp.status} {resp.read().decode('utf-8', 'replace')}")


def _stored_run(endpoint: str, kind: str) -> str | None:
    read_url = endpoint.replace("/api/refresh/eccc/", "/api/eccc/") + f"?kind={kind}"
    try:
        data = json.loads(_http_get(read_url, timeout=30))
        return (data.get("data") or {}).get("run")
    except OSError:
        return None


def _emit(payload: dict, args) -> None:
    if args.dry_run:
        sample_id = next(iter(payload["points"]))
        print(json.dumps({
            "kind": payload["kind"], "run": payload["run"],
            "series": payload["series"], "n_points": len(payload["points"]),
            "hours": payload.get("hours"),
            "example": {sample_id: payload["points"][sample_id]},
        }, indent=2)[:1500])
    else:
        post_payload(args.endpoint, payload)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ingest ECCC RDPS 10 m wind at CLEAR points.")
    ap.add_argument("--analysis", action="store_true", help="ingest RDPS analysis (hour 000)")
    ap.add_argument("--forecast", action="store_true", help="ingest RDPS wind forecast")
    ap.add_argument("--endpoint", default=os.environ.get("ECCC_INGEST_URL", ""),
                    help="ingest endpoint, e.g. https://clear25.xyz/api/refresh/eccc/")
    ap.add_argument("--max-forecast-hours", type=int, default=48,
                    help="max RDPS forecast hour to sample (default 48)")
    ap.add_argument("--skip-if-current", action="store_true",
                    help="skip the forecast download if the stored run stamp already matches")
    ap.add_argument("--dry-run", action="store_true", help="sample but do not POST")
    ap.add_argument("--discover-only", action="store_true",
                    help="print the discovered run + sample URLs and exit (no GRIB deps)")
    args = ap.parse_args(argv)

    if args.discover_only:
        for kind, mh in (("analysis", 0), ("forecast", args.max_forecast_hours)):
            run = find_latest_rdps(WIND_PRODUCTS, mh)
            if not run:
                print(f"{kind}: no RDPS run found"); continue
            print(f"{kind}: run={run['run']} hours={run['hours'][:3]}..{run['hours'][-1:]} "
                  f"({len(run['hours'])} hrs)")
            for series in WIND_PRODUCTS:
                print(f"    {series}[h0] = {run['files'][series][run['hours'][0]]}")
        return 0

    if not (args.analysis or args.forecast):
        ap.error("choose at least one of --analysis / --forecast")
    if not args.dry_run and not args.endpoint:
        ap.error("--endpoint is required unless --dry-run")

    _ensure_heavy()
    ids, coords, city_of = load_sample_points()
    print(f"Sampling {len(ids)} points (stations + {len(CITY_CENTROIDS)} cities + "
          f"{len(FAR_FIELD_POINTS)} far-field).")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if args.analysis:
            print("RDPS wind analysis (hour 000)...")
            payload = ingest_wind_analysis(ids, coords, city_of, tmp)
            print(f"  run={payload['run']} series={payload['series']}")
            _emit(payload, args)
        if args.forecast:
            print("RDPS wind forecast...")
            if args.skip_if_current and not args.dry_run:
                latest = find_latest_rdps(WIND_PRODUCTS, args.max_forecast_hours)
                if latest and _stored_run(args.endpoint, "wind_forecast") == latest["run"]:
                    print(f"  stored run {latest['run']} already current; skipping.")
                    return 0
            payload = ingest_wind_forecast(ids, coords, city_of, tmp, args.max_forecast_hours)
            print(f"  run={payload['run']} hours={len(payload['hours'])} series={payload['series']}")
            _emit(payload, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
