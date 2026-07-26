#!/usr/bin/env python3
"""Compile the FINALIZED, EPA-corrected PurpleAir record (2021-2025) into one
compact static asset per year for the /plan/ smoke-plume tracker.

This supersedes gen_plume_2023.py and gen_plume_2024_2025.py as the tracker's
data source. Three things make the finalized record strictly better:

  1. The values are `pm2.5_epa_corrected` -- the US EPA / Barkjohn et al. (2021)
     piecewise correction applied with per-hour humidity. The older downloads
     shipped only `pm2.5_atm` (no CF=1, no RH), so no correction was possible
     and their values run roughly 2x hot.
  2. Sensor QC is already done upstream (`qc_faulty`), so a faulty sensor's
     whole year is excluded rather than leaking spikes into the surface.
  3. Latitude/longitude are columns in the file, so the generator makes NO
     network call at all (the 2024-2025 generator had to hit the PurpleAir
     metadata endpoint to recover coordinates).

Input (outside the repo, not committed -- ~2.5 GB total):
  <data-dir>/PurpleAir<YEAR>_calibrated_merged_with_locations.csv
    sensor_id, sensor_name, latitude, longitude, altitude,
    time_stamp, humidity, pm2.5_cf_1, pm2.5_epa_corrected, qc_faulty

  Rows are NOT time-ordered within a sensor (verified: the 2021 file's first row
  is 2021-12-16), so every sensor's points are sorted before delta encoding.

Output (committed, served as static assets):
  webapp/dashboard/static/dashboard/plume_finalized_<YEAR>.json.gz   (x5)
  webapp/dashboard/static/dashboard/plume_finalized_index.json       (manifest)

  NAMING IS LOAD-BEARING; do not shorten it back to plume_<YEAR>.json.gz.
  WhiteNoise serves static files in production and treats "X.gz" as the gzip
  VARIANT of "X" whenever a file named "X" also exists -- it then refuses to
  serve "X.gz" at its own URL and returns 404. The directory already holds the
  legacy plume_2023.json, so plume_2023.json.gz 404'd in production while the
  other four years worked. DEBUG=True hides this completely: WhiteNoise runs in
  autorefresh mode and resolves through the finders instead of the prebuilt file
  dict, so the dev server serves it happily. assert_no_whitenoise_shadow() below
  fails the build rather than let this recur.

Per-year payload:
    {
      "field": "pm2.5_epa_corrected", "year", "note", "source",
      "t0": "<YEAR>-01-01T00:00:00Z", "step_seconds": 3600, "n_steps",
      "encoding": "delta",
      "bbox", "stations": [{"id","lat","lon"}...],
      "series": [ [h0, pm, dh, pm, dh, pm, ...], ... ]   # parallel to stations
    }

  `series` is station-keyed and DELTA-encoded: the first number is an absolute
  hour index, every subsequent hour is a delta from the previous one. Measured
  on real 2022 data, delta encoding costs 1.22 gzip-bytes/point against 3.52 for
  the absolute form the older assets used -- a 2.9x reduction, which is what lets
  all twelve months of all five years ship instead of just the fire season.

Standard analysis-ready filter (handoff section 3): qc_faulty == "no" and a
non-empty pm2.5_epa_corrected. Values are passed through untouched otherwise --
including the few above 1000 ug/m3, which are legitimate QC-passed readings
produced by the correction's quadratic term from sub-1000 cf_1 inputs.

Pure stdlib. Run `--selftest` for the encode/decode and calendar unit tests.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gzip
import json
import sys
from array import array
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
STATIC = REPO / "webapp" / "dashboard" / "static" / "dashboard"
DEFAULT_DATA_DIR = Path(r"C:\Users\rybot\Desktop\ai_model_data_important")
YEARS = (2021, 2022, 2023, 2024, 2025)

STEP_SECONDS = 3600
# Points are packed into one int per reading as step * PM_BASE + pm10 so a
# sensor's readings can be sorted with a single cheap sort. pm10 is the value in
# tenths of a ug/m3, so PM_BASE must exceed the largest representable pm10.
PM_BASE = 65536
PM10_MAX = PM_BASE - 1          # 6553.5 ug/m3; the record's max is 1576.5
# The frontend indexes stations with a Uint16Array, so a year may not exceed
# this many sensors. The largest year (2025) has 1,556.
MAX_STATIONS = 65535
# Sensors reporting in an hour before that hour is eligible to be "the peak".
PEAK_MIN_SENSORS = 30
# Fraction trimmed off each edge when computing the map's default view, so a
# handful of far-north Quebec sensors (to ~62 N) don't zoom the whole continent
# out. The full extent is reported separately and is never trimmed.
VIEW_TRIM = 0.025

NOTE = (
    "EPA/Barkjohn (2021) humidity-corrected PM2.5 from the finalized PurpleAir "
    "record. Faulty sensors (qc_faulty) excluded. Sensor coordinates are each "
    "sensor's current position, which for older years may differ from where it "
    "stood at the time of measurement."
)


# --------------------------------------------------------------------------- #
# Time axis
# --------------------------------------------------------------------------- #
def year_axis(year):
    """(t0 datetime, t0 epoch seconds, n_steps) for a calendar year in UTC."""
    t0 = datetime(year, 1, 1, tzinfo=timezone.utc)
    n_days = 366 if calendar.isleap(year) else 365
    return t0, calendar.timegm(t0.timetuple()), n_days * 24


def step_index(ts, t0_epoch, n_steps):
    """ISO 'YYYY-MM-DDTHH:MM:SSZ' -> hour index into the year, or None."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    step = (calendar.timegm(dt.timetuple()) - t0_epoch) // STEP_SECONDS
    if step < 0 or step >= n_steps:
        return None
    return int(step)


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #
def fmt_pm(pm10):
    """Tenths of a ug/m3 -> shortest exact decimal string ('37' -> '3.7')."""
    whole, tenth = divmod(pm10, 10)
    return str(whole) if tenth == 0 else f"{whole}.{tenth}"


def encode_series(packed_sorted):
    """Sorted packed ints -> the delta-encoded JSON array text for one station.

    Returns (json_text, n_points). Duplicate readings for the same hour are
    collapsed to the last one (they would otherwise encode as a zero delta,
    which decodes to the same hour and silently double-counts it).
    """
    out, prev, n = [], 0, 0
    for pk in packed_sorted:
        step, pm10 = divmod(pk, PM_BASE)
        if n and step == prev:
            out[-1] = fmt_pm(pm10)          # same hour again -> keep the last
            continue
        out.append(str(step - prev))
        out.append(fmt_pm(pm10))
        prev = step
        n += 1
    return "[" + ",".join(out) + "]", n


def decode_series(text_or_list):
    """Inverse of encode_series -> [(step, pm), ...]. Used by the tests and the
    post-write reconciliation gate."""
    flat = json.loads(text_or_list) if isinstance(text_or_list, str) else text_or_list
    pts, prev = [], 0
    for i in range(0, len(flat), 2):
        prev += flat[i]
        pts.append((prev, flat[i + 1]))
    return pts


# --------------------------------------------------------------------------- #
# Read one year
# --------------------------------------------------------------------------- #
def read_year(path, t0_epoch, n_steps, verbose=True):
    """Stream one merged CSV -> (packed_by_sid, coords_by_sid, stats).

    Memory is held as one array('i') of packed ints per sensor (4 bytes per
    reading), so the largest year stays near 40 MB rather than the ~600 MB a
    list of Python tuples would cost.
    """
    packed, coords = {}, {}
    rows = kept = skipped_qc = skipped_pm = skipped_ts = skipped_coord = 0
    pm_max = 0.0

    with open(path, newline="", encoding="utf-8") as fh:
        rd = csv.reader(fh)
        header = next(rd)
        ix = {name: i for i, name in enumerate(header)}
        required = ("sensor_id", "latitude", "longitude", "time_stamp",
                    "pm2.5_epa_corrected", "qc_faulty")
        missing = [c for c in required if c not in ix]
        if missing:
            raise SystemExit(f"{path.name}: missing column(s) {missing}; got {header}")
        i_sid, i_lat, i_lon = ix["sensor_id"], ix["latitude"], ix["longitude"]
        i_ts, i_pm, i_q = ix["time_stamp"], ix["pm2.5_epa_corrected"], ix["qc_faulty"]

        for row in rd:
            rows += 1
            if row[i_q] != "no":
                skipped_qc += 1
                continue
            raw = row[i_pm]
            if not raw:
                skipped_pm += 1
                continue
            try:
                pm = float(raw)
            except ValueError:
                skipped_pm += 1
                continue
            step = step_index(row[i_ts], t0_epoch, n_steps)
            if step is None:
                skipped_ts += 1
                continue

            sid = row[i_sid]
            arr = packed.get(sid)
            if arr is None:
                try:
                    lat, lon = float(row[i_lat]), float(row[i_lon])
                except ValueError:
                    skipped_coord += 1
                    continue
                if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
                    skipped_coord += 1
                    continue
                coords[sid] = (lat, lon)
                arr = packed[sid] = array("i")

            if pm > pm_max:
                pm_max = pm
            pm10 = min(PM10_MAX, max(0, int(round(pm * 10))))
            arr.append(step * PM_BASE + pm10)
            kept += 1

            if verbose and rows % 2_000_000 == 0:
                print(f"    ... {rows:,} rows read", flush=True)

    stats = {
        "rows_total": rows, "rows_kept": kept, "skipped_qc": skipped_qc,
        "skipped_pm": skipped_pm, "skipped_timestamp": skipped_ts,
        "skipped_coord": skipped_coord, "pm_max": round(pm_max, 2),
    }
    return packed, coords, stats


def peak_step(packed, n_steps, min_sensors=PEAK_MIN_SENSORS):
    """Hour with the highest across-sensor median PM2.5.

    Counting-sorts every reading into per-hour buckets (2 bytes each) and takes
    each hour's median, so the tracker can open on the year's worst smoke hour.
    Returns (step, median_ug_m3, n_sensors) or (0, None, 0).
    """
    counts = array("i", [0]) * n_steps
    for arr in packed.values():
        for pk in arr:
            counts[pk // PM_BASE] += 1

    starts = array("i", [0]) * (n_steps + 1)
    for i in range(n_steps):
        starts[i + 1] = starts[i] + counts[i]
    total = starts[n_steps]

    cursor = array("i", starts[:n_steps])
    values = array("H", [0]) * total if total else array("H")
    for arr in packed.values():
        for pk in arr:
            step, pm10 = divmod(pk, PM_BASE)
            values[cursor[step]] = pm10
            cursor[step] += 1

    best, best_med, best_n = 0, None, 0
    for step in range(n_steps):
        a, b = starts[step], starts[step + 1]
        n = b - a
        if n < min_sensors:
            continue
        chunk = sorted(values[a:b])
        med = chunk[n // 2] if n % 2 else (chunk[n // 2 - 1] + chunk[n // 2]) / 2
        if best_med is None or med > best_med:
            best, best_med, best_n = step, med, n
    return best, (None if best_med is None else round(best_med / 10, 1)), best_n


# --------------------------------------------------------------------------- #
# Build one year's asset
# --------------------------------------------------------------------------- #
def build_year(year, data_dir, out_dir, write, verbose=True):
    src = Path(data_dir) / f"PurpleAir{year}_calibrated_merged_with_locations.csv"
    if not src.is_file():
        raise SystemExit(f"missing input: {src}")

    t0, t0_epoch, n_steps = year_axis(year)
    print(f"[{year}] reading {src.name} ({src.stat().st_size / 1048576:.0f} MB)", flush=True)
    packed, coords, stats = read_year(src, t0_epoch, n_steps, verbose)

    sids = sorted([s for s in packed if len(packed[s])], key=int)
    if not sids:
        raise SystemExit(f"[{year}] no usable rows")
    if len(sids) > MAX_STATIONS:
        raise SystemExit(
            f"[{year}] {len(sids)} sensors exceeds the frontend's Uint16 station "
            f"index limit ({MAX_STATIONS}); widen the index before regenerating")

    pk_step, pk_med, pk_n = peak_step(packed, n_steps)

    stations, chunks, n_points, n_dupes = [], [], 0, 0
    for sid in sids:
        text, n = encode_series(sorted(packed[sid]))
        n_dupes += len(packed[sid]) - n
        lat, lon = coords[sid]
        stations.append({"id": sid, "lat": round(lat, 5), "lon": round(lon, 5)})
        chunks.append(text)
        n_points += n

    lats = [s["lat"] for s in stations]
    lons = [s["lon"] for s in stations]
    bbox = {"nwlat": max(lats), "nwlng": min(lons),
            "selat": min(lats), "selng": max(lons)}

    head = {
        "field": "pm2.5_epa_corrected",
        "year": year,
        "note": NOTE,
        "source": src.name,
        "encoding": "delta",
        "t0": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "step_seconds": STEP_SECONDS,
        "n_steps": n_steps,
        "bbox": bbox,
        "stations": stations,
    }
    # Serialized by hand so the ~10M-point series never exists as a Python list
    # of numbers (which would cost several hundred MB); each station's array is
    # already formatted text by this point.
    body = json.dumps(head, separators=(",", ":"))
    assert body.endswith("}")
    text = body[:-1] + ',"series":[' + ",".join(chunks) + "]}"
    blob = gzip.compress(text.encode("utf-8"), 9)

    peak_iso = datetime.fromtimestamp(
        t0_epoch + pk_step * STEP_SECONDS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"    sensors {len(stations):,}   points {n_points:,}"
          f"{f'   (deduped {n_dupes:,})' if n_dupes else ''}")
    print(f"    kept {stats['rows_kept']:,} of {stats['rows_total']:,} rows"
          f"  (qc {stats['skipped_qc']:,} / pm {stats['skipped_pm']:,}"
          f" / ts {stats['skipped_timestamp']:,} / coord {stats['skipped_coord']:,})")
    print(f"    peak hour {peak_iso}  median {pk_med} ug/m3  ({pk_n} sensors)")
    print(f"    payload {len(text) / 1048576:.1f} MB raw -> {len(blob) / 1048576:.2f} MB gz"
          f"  ({len(blob) / max(1, n_points):.2f} B/pt)")

    out = Path(out_dir) / f"plume_finalized_{year}.json.gz"
    assert_no_whitenoise_shadow([out])
    if write:
        out.write_bytes(blob)
        print(f"    -> {out}")

    return {
        "year": year,
        "file": out.name,
        "t0": head["t0"],
        "step_seconds": STEP_SECONDS,
        "n_steps": n_steps,
        "stations": len(stations),
        "points": n_points,
        "bbox": bbox,
        "peak_step": pk_step,
        "peak_time": peak_iso,
        "peak_median": pk_med,
        "peak_sensors": pk_n,
        "pm_max": stats["pm_max"],
        "bytes_gz": len(blob),
        "source": src.name,
    }, blob, n_points, [(s["lat"], s["lon"]) for s in stations]


def assert_no_whitenoise_shadow(paths):
    """Fail if any '<name>.gz' output has a sibling '<name>' in the same folder.

    WhiteNoise (the production static server) reads such a pair as one file plus
    its gzip encoding: whitenoise.base.add_file_to_dictionary() returns early for
    the compressed variant, so the '.gz' URL is never registered and 404s. This
    is invisible under DEBUG=True, where WhiteNoise autorefresh resolves through
    the staticfiles finders instead of the prebuilt dict -- so it must be caught
    at build time, not in a dev browser.
    """
    for p in paths:
        p = Path(p)
        if p.suffix != ".gz":
            continue
        sibling = p.with_suffix("")          # plume_x.json.gz -> plume_x.json
        if sibling.exists():
            raise SystemExit(
                f"refusing to write {p.name}: {sibling.name} exists alongside it, "
                f"so WhiteNoise would treat {p.name} as {sibling.name}'s gzip "
                f"variant and 404 the URL in production. Rename the output stem.")


def trimmed_view(lats, lons, trim=VIEW_TRIM):
    """Percentile-trimmed bbox over sensor positions -> the default map view."""
    def pctl(sorted_vals, p):
        i = int(p * (len(sorted_vals) - 1))
        return sorted_vals[max(0, min(len(sorted_vals) - 1, i))]

    la, lo = sorted(lats), sorted(lons)
    if len(la) < 20:
        return {"nwlat": max(la), "nwlng": min(lo), "selat": min(la), "selng": max(lo)}
    return {
        "nwlat": pctl(la, 1 - trim), "nwlng": pctl(lo, trim),
        "selat": pctl(la, trim), "selng": pctl(lo, 1 - trim),
    }


# --------------------------------------------------------------------------- #
# Reconciliation gate
# --------------------------------------------------------------------------- #
def verify_blob(blob, expect_points, year):
    """Decode a written asset and assert it round-trips to the same point count.

    Guards against a silent encoding regression: a wrong delta would still parse
    as valid JSON, so the count and the monotonic-hour invariant are checked
    against what was actually read from the CSV.
    """
    payload = json.loads(gzip.decompress(blob).decode("utf-8"))
    total = 0
    for i, flat in enumerate(payload["series"]):
        pts = decode_series(flat)
        total += len(pts)
        prev = -1
        for step, pm in pts:
            if step <= prev:
                raise SystemExit(f"[{year}] station {i}: hours not strictly increasing "
                                 f"({step} after {prev})")
            if step < 0 or step >= payload["n_steps"]:
                raise SystemExit(f"[{year}] station {i}: hour {step} out of range")
            prev = step
    if total != expect_points:
        raise SystemExit(f"[{year}] round-trip mismatch: decoded {total:,} points, "
                         f"encoded {expect_points:,}")
    if len(payload["series"]) != len(payload["stations"]):
        raise SystemExit(f"[{year}] series/stations length mismatch")
    print(f"    verified: {total:,} points round-trip, hours strictly increasing")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def selftest():
    # Calendar axis, including the leap year the tracker's slider depends on.
    assert year_axis(2023)[2] == 8760
    assert year_axis(2024)[2] == 8784, "2024 is a leap year"
    assert year_axis(2025)[2] == 8760

    _, e23, n23 = year_axis(2023)
    assert step_index("2023-01-01T00:00:00Z", e23, n23) == 0
    assert step_index("2023-01-01T01:00:00Z", e23, n23) == 1
    assert step_index("2023-12-31T23:00:00Z", e23, n23) == n23 - 1
    assert step_index("2024-01-01T00:00:00Z", e23, n23) is None
    assert step_index("2022-12-31T23:00:00Z", e23, n23) is None
    assert step_index("garbage", e23, n23) is None

    _, e24, n24 = year_axis(2024)
    assert step_index("2024-02-29T12:00:00Z", e24, n24) == (31 + 28) * 24 + 12
    assert step_index("2024-12-31T23:00:00Z", e24, n24) == n24 - 1

    # Formatting keeps 0.1 resolution and stays short.
    assert fmt_pm(0) == "0" and fmt_pm(37) == "3.7" and fmt_pm(120) == "12"
    assert fmt_pm(15765) == "1576.5"

    # The WhiteNoise shadow guard: a bare .gz is fine, a .gz beside its stem is not.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        lone = Path(td) / "asset_a.json.gz"
        assert_no_whitenoise_shadow([lone])                  # no sibling -> ok
        assert_no_whitenoise_shadow([Path(td) / "plain.json"])  # not .gz -> ok
        shadowed = Path(td) / "asset_b.json.gz"
        (Path(td) / "asset_b.json").write_text("{}", encoding="utf-8")
        try:
            assert_no_whitenoise_shadow([shadowed])
        except SystemExit:
            pass
        else:
            raise AssertionError("shadow guard failed to fire on a .gz beside its stem")

    # Delta round-trip, including a large gap and a zero-valued reading.
    pts = [(0, 0.0), (1, 3.7), (2, 12.0), (500, 66.4), (8759, 1576.5)]
    text, n = encode_series(array("i", [s * PM_BASE + int(round(p * 10)) for s, p in pts]))
    assert n == len(pts)
    assert decode_series(text) == pts, decode_series(text)
    assert json.loads(text)[0] == 0 and json.loads(text)[2] == 1

    # Unsorted input sorts before encoding; a duplicate hour collapses to the last.
    shuffled = array("i", [5 * PM_BASE + 10, 1 * PM_BASE + 20, 5 * PM_BASE + 99])
    text, n = encode_series(sorted(shuffled))
    assert n == 2, n
    assert decode_series(text) == [(1, 2.0), (5, 9.9)], decode_series(text)

    # A single reading encodes as one absolute hour.
    text, n = encode_series(array("i", [42 * PM_BASE + 5]))
    assert n == 1 and decode_series(text) == [(42, 0.5)]

    # peak_step finds the highest-median hour and honours the sensor floor.
    packed = {}
    for k in range(40):
        packed[str(k)] = array("i", [3 * PM_BASE + 100, 7 * PM_BASE + 900])
    packed["hot"] = array("i", [9 * PM_BASE + 60000])       # 1 sensor only
    step, med, cnt = peak_step(packed, 24, min_sensors=30)
    assert (step, med, cnt) == (7, 90.0, 40), (step, med, cnt)

    print("selftest: all assertions passed")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the finalized EPA-corrected plume assets (one .gz per year).")
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR),
                    help="folder holding PurpleAir<YEAR>_calibrated_merged_with_locations.csv")
    ap.add_argument("--out-dir", default=str(STATIC))
    ap.add_argument("--years", default=",".join(str(y) for y in YEARS),
                    help="comma-separated years to build")
    ap.add_argument("--write", action="store_true", help="write the assets (else dry-run)")
    ap.add_argument("--selftest", action="store_true", help="run unit tests and exit")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    years = [int(y) for y in args.years.split(",") if y.strip()]
    out_dir = Path(args.out_dir)
    if args.write:
        out_dir.mkdir(parents=True, exist_ok=True)

    entries, total_bytes, total_points = [], 0, 0
    all_lats, all_lons = [], []
    for year in years:
        entry, blob, n_points, positions = build_year(
            year, args.data_dir, out_dir, args.write)
        verify_blob(blob, n_points, year)
        entries.append(entry)
        total_bytes += entry["bytes_gz"]
        total_points += n_points
        all_lats += [p[0] for p in positions]
        all_lons += [p[1] for p in positions]

    # Full extent, plus a percentile-trimmed default map view computed over every
    # sensor position across every built year. The trimmed view is what the page
    # opens on, and because it is global it does not shift when the user switches
    # year; the untrimmed extent is kept so nothing is silently lost.
    extent = {"nwlat": max(all_lats), "nwlng": min(all_lons),
              "selat": min(all_lats), "selng": max(all_lons)}
    view = trimmed_view(all_lats, all_lons)

    index = {
        "field": "pm2.5_epa_corrected",
        "note": NOTE,
        "encoding": "delta",
        "step_seconds": STEP_SECONDS,
        "extent": extent,
        "view": view,
        "years": entries,
    }
    idx_path = out_dir / "plume_finalized_index.json"
    idx_text = json.dumps(index, separators=(",", ":"), indent=None)

    print()
    print(f"total: {total_points:,} points across {len(entries)} year(s), "
          f"{total_bytes / 1048576:.1f} MB gz")
    print(f"extent: lat {extent['selat']}..{extent['nwlat']}  "
          f"lon {extent['nwlng']}..{extent['selng']}")
    if args.write:
        idx_path.write_text(idx_text, encoding="utf-8")
        print(f"-> wrote {idx_path} ({len(idx_text)} bytes)")
    else:
        print("(dry run; pass --write to save)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
