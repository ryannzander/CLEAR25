#!/usr/bin/env python3
"""
Download ERA5 10m wind (u/v components) for the project's 7-region AOI,
2018-2025, for use as a wind edge-feature in the Toronto PM2.5 ST-GNN.

Why ERA5 rather than HRRR: HRRR's domain is US-centric and likely has
degraded coverage right at the northern Quebec boreal latitudes (49-53N+)
that are the actual wildfire source region for this project - ERA5 is a
global reanalysis with uniform quality at every latitude, has a full
historical archive back to 1940 (so 2018-2025 is trivial), and its 31km
resolution is plenty for an edge feature that only needs transport
direction/speed between stations tens-hundreds of km apart.

This is a GRIDDED bulk pull, and is deliberately separate from
scripts/gen_wind_hourly.py, which pulls ERA5 single-POINT hourly wind from
Open-Meteo's free archive (speed + meteorological direction at the Toronto
centroid) and aligns it to a PM asset's timeline. That one feeds
scripts/wind_features.py; this one feeds the ST-GNN edge features, which
need the wind field over the whole AOI, not one point.

One request per calendar year by default (each ERA5 API request can cover a
whole year's worth of hours in one call, unlike the day-per-file
RAQDPS/HRRR downloads elsewhere in this project). Resumable: skips any
chunk whose output file already exists. Downloads land on a `.part`
sidecar and are renamed only once complete, so an interrupted run never
leaves a truncated file that a later run would skip as "already done".

CDS API contract (verified against the live docs 2026-09-13 - do not guess
these, the platform changed in 2024 and most tutorials online are stale):

  * Endpoint/credentials: `url: https://cds.climate.copernicus.eu/api` plus
    a Personal Access Token as `key:` (NOT the old `<uid>:<key>` pair).
    cdsapi reads `CDSAPI_URL`/`CDSAPI_KEY` from the environment first, then
    `$CDSAPI_RC`, then `~/.cdsapirc`.
  * cdsapi >= 0.7.2 is required to talk to the current CDS at all
    (>= 0.7.7 recommended); older releases only speak to the retired
    legacy platform.
  * The request key is `data_format` ('netcdf' | 'grib'). The legacy
    `format` key is what the old API used - this script sends
    `data_format` + `download_format` so it does not depend on the
    server still accepting the deprecated spelling.
  * A request may not exceed 120,000 items (fields). A year of hourly data
    for the 2 wind components is 2 x 8760 = 17,520 items, comfortably
    under, and `--chunk month` (or the automatic fallback) is there for
    when the server refuses a year for any other reason. ECMWF also warns
    that the GRIB->netCDF conversion is the part most likely to fail on
    large requests, which is the other reason the monthly fallback exists.
  * Since Nov 2024 the netCDF converter splits fields by GRIB `stepType`,
    so a netCDF download can arrive as a ZIP of several `.nc` files even
    with `download_format: unarchived`. 10m u/v are both instantaneous, so
    one file is expected - but this script detects the ZIP magic and
    unpacks it anyway rather than leaving a `.nc` that is really a ZIP.

Setup required before running (one-time, by the user - see
https://cds.climate.copernicus.eu/how-to-api):
  1. Register for a free CDS account: https://cds.climate.copernicus.eu/
  2. Accept the ERA5 dataset's license on its page (the API refuses
     requests until this is done in-browser at least once)
  3. Copy your Personal Access Token from your profile page into
     ~/.cdsapirc:
         url: https://cds.climate.copernicus.eu/api
         key: <your-personal-access-token>
  4. pip install "cdsapi>=0.7.7"

Usage:
    python3 download_era5_wind.py                    # all years 2018-2025
    python3 download_era5_wind.py --years 2023       # just one year
    python3 download_era5_wind.py --years 2023 2024
    python3 download_era5_wind.py --chunk month --years 2023
    python3 download_era5_wind.py --dry-run          # print requests, no network
    python3 download_era5_wind.py --selftest         # offline checks
    python3 download_era5_wind.py --out-dir "/Users/<you>/Desktop/CLEAR 2.0 Ver2 (Firework)/RAW Data/ERA5_Wind"

Output defaults to <repo>/era5_wind/ (gitignored; raw reanalysis is far too
large to commit). Override per-run with --out-dir, or persistently with the
ERA5_WIND_DIR environment variable.
"""
import argparse
import calendar
import os
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

DEFAULT_OUT_DIR = Path(os.environ.get("ERA5_WIND_DIR") or (REPO / "era5_wind"))
YEARS = list(range(2018, 2026))

DATASET = "reanalysis-era5-single-levels"

# North, West, South, East - same AOI used throughout this project's Firework/dashboard work.
# Override per-run with --area (note the north edge sits at 53N, i.e. the top of the
# 49-53N+ boreal source band this file's header calls out as the fire region).
AREA = [53, -96, 38, -57]

VARIABLES = ["10m_u_component_of_wind", "10m_v_component_of_wind"]

# CDS refuses any single request larger than this many fields.
MAX_ITEMS_PER_REQUEST = 120_000

SUFFIX = {"netcdf": ".nc", "grib": ".grib"}

# Server-side complaints that a smaller (monthly) request can plausibly fix.
_RETRY_SMALLER_HINTS = ("too large", "cost limit", "item limit", "conversion", "convert")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_area(text):
    """Parse 'N,W,S,E' into the 4-element list the CDS `area` key expects."""
    parts = [p.strip() for p in text.replace(" ", "").split(",")]
    if len(parts) != 4:
        raise ValueError("area must be 4 comma-separated numbers: N,W,S,E")
    try:
        north, west, south, east = (float(p) for p in parts)
    except ValueError:
        raise ValueError(f"area must be numeric, got {text!r}") from None
    if not (-90 <= south < north <= 90):
        raise ValueError(f"area needs -90 <= south < north <= 90, got N={north} S={south}")
    if not (-180 <= west < east <= 180):
        raise ValueError(f"area needs -180 <= west < east <= 180, got W={west} E={east}")
    return [north, west, south, east]


def month_days(year, month):
    """Day-of-month strings that actually exist in this month."""
    n = calendar.monthrange(year, month)[1]
    return [f"{d:02d}" for d in range(1, n + 1)]


def item_count(year, months, n_variables=len(VARIABLES), hours=24):
    """Fields a request covers - what the CDS 120,000-item limit counts."""
    days = sum(calendar.monthrange(year, m)[1] for m in months)
    return n_variables * days * hours


def build_request(year, months, area, data_format):
    """The CDS request body for one chunk (a whole year, or one month)."""
    days = sorted({d for m in months for d in month_days(year, m)})
    return {
        "product_type": ["reanalysis"],
        "variable": list(VARIABLES),
        "year": [str(year)],
        "month": [f"{m:02d}" for m in sorted(months)],
        "day": days,
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": list(area),
        "data_format": data_format,
        "download_format": "unarchived",
    }


def out_name(year, months, data_format):
    """era5_wind_2023.nc for a whole year, era5_wind_2023_06.nc for one month."""
    suffix = SUFFIX[data_format]
    if len(months) == 1:
        return f"era5_wind_{year}_{months[0]:02d}{suffix}"
    return f"era5_wind_{year}{suffix}"


def check_setup():
    """Fail fast and loudly on a missing token or a too-old cdsapi."""
    rc_path = Path(os.environ.get("CDSAPI_RC", Path.home() / ".cdsapirc"))
    has_env_creds = bool(os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY"))
    if not has_env_creds and not rc_path.exists():
        log(f"ERROR: no CDS credentials ({rc_path} not found, CDSAPI_URL/CDSAPI_KEY unset).")
        log("Before running this script:")
        log("  1. Register for a free account: https://cds.climate.copernicus.eu/")
        log("  2. Open the 'ERA5 hourly data on single levels from 1940 to present' dataset")
        log("     page and accept its license (required once, in-browser)")
        log("  3. Get your Personal Access Token: https://cds.climate.copernicus.eu/how-to-api")
        log(f"  4. Create {rc_path} with:")
        log("         url: https://cds.climate.copernicus.eu/api")
        log("         key: <your-personal-access-token>")
        sys.exit(1)

    try:
        import cdsapi
    except ImportError:
        log('ERROR: cdsapi is not installed. Run: pip install "cdsapi>=0.7.7"')
        sys.exit(1)

    version = getattr(cdsapi, "__version__", "0")
    try:
        parsed = tuple(int(p) for p in version.split(".")[:3])
    except ValueError:
        parsed = ()
    if parsed and parsed < (0, 7, 2):
        log(f"ERROR: cdsapi {version} only speaks to the retired legacy CDS.")
        log('       Upgrade with: pip install --upgrade "cdsapi>=0.7.7"')
        sys.exit(1)


def unwrap_if_zip(path):
    """
    The netCDF converter can hand back a ZIP of per-stepType files even when
    `unarchived` was requested. Detect that by magic number (never by
    extension - the file is named .nc either way) and unpack it.

    Returns the list of files that now hold the data.
    """
    with path.open("rb") as fh:
        if fh.read(4) != b"PK\x03\x04":
            return [path]

    archive = path.with_suffix(path.suffix + ".zip")
    path.replace(archive)
    try:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if not m.endswith("/")]
            if not members:
                raise RuntimeError(f"{archive.name}: ZIP download is empty")
            written = []
            for member in members:
                # Flatten: never trust archive paths to stay inside out_dir.
                leaf = Path(member).name
                target = path if len(members) == 1 else path.with_name(f"{path.stem}__{leaf}")
                with zf.open(member) as src, target.open("wb") as dst:
                    while True:
                        block = src.read(1 << 20)
                        if not block:
                            break
                        dst.write(block)
                written.append(target)
    except Exception:
        # Leave the archive on disk so the run is still recoverable by hand.
        log(f"ERROR: could not unpack {archive.name} - it is kept for inspection")
        raise
    archive.unlink()
    if len(written) == 1:
        log(f"  (download arrived zipped; unpacked -> {written[0].name})")
    else:
        log(f"  (download arrived zipped; unpacked {len(written)} files: "
            f"{', '.join(p.name for p in written)})")
    return written


def download_chunk(client, out_dir, year, months, area, data_format, dry_run=False):
    """
    Fetch one chunk (a year, or a month) unless its output already exists.
    Returns True if the data is on disk afterwards.
    """
    out_path = out_dir / out_name(year, months, data_format)
    label = f"{year}" if len(months) > 1 else f"{year}-{months[0]:02d}"
    if out_path.exists():
        log(f"{label}: already done, skipping ({out_path.name})")
        return True

    request = build_request(year, months, area, data_format)
    items = item_count(year, months)
    if items > MAX_ITEMS_PER_REQUEST:
        log(f"{label}: request is {items:,} items, over the CDS "
            f"{MAX_ITEMS_PER_REQUEST:,} limit - use --chunk month")
        return False

    if dry_run:
        log(f"{label}: would request {items:,} items -> {out_path.name}")
        log(f"        {request}")
        return True

    log(f"{label}: submitting request ({items:,} items; queued server-side, so "
        f"anywhere from a couple minutes to over an hour - be patient)...")
    t0 = time.time()
    part = out_path.with_suffix(out_path.suffix + ".part")
    client.retrieve(DATASET, request, str(part))
    if not part.exists() or part.stat().st_size == 0:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{label}: download produced no data")
    # Only now is the file complete enough to claim the real name, so a
    # resumed run cannot mistake a truncated download for a finished one.
    part.replace(out_path)
    size_mb = sum(p.stat().st_size for p in unwrap_if_zip(out_path)) / 1e6
    log(f"{label}: done ({time.time() - t0:.0f}s, {size_mb:.1f} MB) -> {out_path.parent}")
    return True


def download_year(client, out_dir, year, area, data_format, chunk, fallback, dry_run=False):
    """One year, as a single request or month by month. Returns True on success."""
    if chunk == "month":
        ok = True
        for month in range(1, 13):
            ok = download_chunk(client, out_dir, year, [month], area, data_format,
                                dry_run) and ok
        return ok

    try:
        return download_chunk(client, out_dir, year, list(range(1, 13)), area,
                              data_format, dry_run)
    except Exception as exc:  # noqa: BLE001 - any server-side refusal lands here
        message = str(exc).lower()
        if not fallback or not any(h in message for h in _RETRY_SMALLER_HINTS):
            log(f"{year}: FAILED - {exc}")
            return False
        log(f"{year}: whole-year request refused ({exc})")
        log(f"{year}: retrying month by month")
        ok = True
        for month in range(1, 13):
            try:
                ok = download_chunk(client, out_dir, year, [month], area, data_format,
                                    dry_run) and ok
            except Exception as inner:  # noqa: BLE001
                log(f"{year}-{month:02d}: FAILED - {inner}")
                ok = False
        return ok


def selftest():
    """Offline checks of everything that does not need the network."""
    assert parse_area("53,-96,38,-57") == [53.0, -96.0, 38.0, -57.0]
    for bad in ("53,-96,38", "53,-96,60,-57", "53,-40,38,-57", "a,b,c,d"):
        try:
            parse_area(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_area accepted {bad!r}")

    assert month_days(2024, 2)[-1] == "29", "2024 is a leap year"
    assert month_days(2023, 2)[-1] == "28"
    assert month_days(2023, 4)[-1] == "30"
    assert len(month_days(2023, 1)) == 31

    all_months = list(range(1, 13))
    assert item_count(2023, all_months) == 2 * 365 * 24 == 17_520
    assert item_count(2024, all_months) == 2 * 366 * 24 == 17_568
    assert item_count(2023, all_months) < MAX_ITEMS_PER_REQUEST
    assert item_count(2023, [6]) == 2 * 30 * 24

    req = build_request(2023, all_months, AREA, "netcdf")
    assert req["data_format"] == "netcdf" and req["download_format"] == "unarchived"
    assert "format" not in req, "legacy key must not be sent"
    assert req["area"] == AREA and req["year"] == ["2023"]
    assert req["month"] == [f"{m:02d}" for m in all_months]
    assert req["day"][0] == "01" and req["day"][-1] == "31" and len(req["day"]) == 31
    assert len(req["time"]) == 24 and req["time"][-1] == "23:00"
    assert req["variable"] == VARIABLES
    feb = build_request(2023, [2], AREA, "grib")
    assert feb["day"][-1] == "28" and feb["month"] == ["02"] and feb["data_format"] == "grib"

    assert out_name(2023, all_months, "netcdf") == "era5_wind_2023.nc"
    assert out_name(2023, [6], "netcdf") == "era5_wind_2023_06.nc"
    assert out_name(2023, all_months, "grib") == "era5_wind_2023.grib"

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        plain = tmp / "era5_wind_2023.nc"
        plain.write_bytes(b"CDF\x01payload")                    # netCDF-3 magic
        assert unwrap_if_zip(plain) == [plain]
        plain.write_bytes(b"\x89HDF\r\n\x1a\npayload")          # netCDF-4/HDF5 magic
        assert unwrap_if_zip(plain) == [plain]

        single = tmp / "era5_wind_2024.nc"
        with zipfile.ZipFile(single, "w") as zf:
            zf.writestr("data_stream-oper_stepType-instant.nc", b"CDF\x01one")
        got = unwrap_if_zip(single)
        assert got == [single] and single.read_bytes() == b"CDF\x01one"
        assert not single.with_suffix(".nc.zip").exists()

        multi = tmp / "era5_wind_2025.nc"
        with zipfile.ZipFile(multi, "w") as zf:
            zf.writestr("instant.nc", b"CDF\x01a")
            zf.writestr("nested/accum.nc", b"CDF\x01b")
        got = sorted(p.name for p in unwrap_if_zip(multi))
        assert got == ["era5_wind_2025__accum.nc", "era5_wind_2025__instant.nc"], got
        assert not multi.exists(), "multi-member unpack should not leave the ZIP in place"

    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(description=" ".join(__doc__.strip().splitlines()[:2]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, nargs="+", default=YEARS,
                    help=f"calendar years to fetch (default {YEARS[0]}-{YEARS[-1]})")
    ap.add_argument("--area", default=",".join(str(v) for v in AREA),
                    help="AOI as N,W,S,E (default: the project AOI)")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="where to write (default: $ERA5_WIND_DIR or <repo>/era5_wind)")
    ap.add_argument("--chunk", choices=("year", "month"), default="year",
                    help="one request per year (default) or per month")
    ap.add_argument("--data-format", choices=("netcdf", "grib"), default="netcdf",
                    help="CDS data_format (default netcdf)")
    ap.add_argument("--no-fallback", action="store_true",
                    help="do not retry a refused whole-year request month by month")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the requests that would be submitted, fetch nothing")
    ap.add_argument("--selftest", action="store_true",
                    help="run the offline checks and exit")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return 0

    try:
        area = parse_area(args.area)
    except ValueError as exc:
        log(f"ERROR: {exc}")
        return 2

    years = sorted(set(args.years))
    if any(y < 1940 for y in years):
        log("ERROR: ERA5 starts in 1940")
        return 2

    out_dir = Path(args.out_dir).expanduser()
    client = None
    if not args.dry_run:
        check_setup()
        import cdsapi
        out_dir.mkdir(parents=True, exist_ok=True)
        client = cdsapi.Client()
    log(f"AOI N{area[0]} W{area[1]} S{area[2]} E{area[3]} · {args.chunk} chunks · "
        f"{args.data_format} -> {out_dir}")

    failed = [y for y in years
              if not download_year(client, out_dir, y, area, args.data_format,
                                   args.chunk, not args.no_fallback, args.dry_run)]
    if failed:
        log(f"FINISHED WITH ERRORS - incomplete years: {', '.join(str(y) for y in failed)}")
        log("Re-run the same command; finished files are skipped.")
        return 1
    log("All requested years processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
