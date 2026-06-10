"""
PurpleAir live PM2.5 client + standardization for the Ontario plume tracker.

Pulls real-time low-cost-sensor PM2.5 across an Ontario bounding box from the
PurpleAir API v1, applies quality control, and converts raw sensor output to a
reference-comparable concentration with the published U.S. EPA / Barkjohn
correction. Returns one cleaned "frame" (a spatial snapshot) per call; the
caller stores frames in a rolling 6-hour buffer for the 5-minute animation.

ISOLATION: this feeds ONLY the Plan/plume-tracker surface (PlumeFrame). It does
NOT touch the CLEAR 3-rule alert engine, CachedResult(key="latest"), or WAQI.

Standardization (decided 2026-06-09, see CLAUDE.md):
- US-wide PurpleAir correction, Barkjohn, Gantt & Clements (2021),
  "Development and application of a United States-wide correction for PM2.5
  data collected with the PurpleAir sensor", Atmos. Meas. Tech. 14, 4617-4637.
  The correction EPA adopted for the AirNow Fire and Smoke Map (2021):
      PM2.5_corrected = 0.524 * PAcf1 - 0.0862 * RH + 5.75
  where PAcf1 = PurpleAir CF=1 PM2.5 (A/B sensor channels averaged) in ug/m3,
  and RH = the sensor's relative humidity (%). EPA later introduced a more
  complex multi-segment piecewise equation for the live map; it is not formally
  published, so we use the citable Barkjohn (2021) form and expose a single
  swap point (`_epa_correct`) if the newer equation is adopted later.
"""

import os
import time

import requests

PURPLEAIR_BASE = "https://api.purpleair.com/v1/sensors"

# Ontario bounding box (NW corner = max-lat/min-lon, SE corner = min-lat/max-lon).
# PurpleAir returns only sensors that actually exist inside the box; density is
# concentrated in southern Ontario, which is fine for a smoke-plume view.
ONTARIO_BBOX = {
    "nwlat": 56.9, "nwlng": -95.2,
    "selat": 41.6, "selng": -74.3,
}

# Fields we request. PurpleAir reorders the columns and always prepends
# sensor_index, so the response is parsed by its own `fields` array, never by
# this request order.
#
# COST: PurpleAir bills each call per sensor (row) x per field. So we request
# the MINIMUM set we actually use -- dropping pm2.5_atm (never read) and last_seen
# (replaced by the server-side `max_age` filter below). Fewer fields + fewer rows
# = far fewer API points. The dominant cost lever is still call frequency: do not
# poll a dense bbox every few minutes on a metered key.
_REQUEST_FIELDS = "latitude,longitude,pm2.5_cf_1,humidity,confidence,channel_flags"

# ---------------------------------------------------------------------------
# Quality-control thresholds (documented; tune in one place)
# ---------------------------------------------------------------------------
# channel_flags: 0=Normal, 1=A-Downgraded, 2=B-Downgraded, 3=A+B-Downgraded.
# Keep only fully-normal sensors.
_QA_CHANNEL_FLAG_OK = 0
# confidence: 0-100, PurpleAir's aggregate of A/B agreement + sensor health.
# EPA's own QC removes sensors whose A/B channels disagree; confidence >= 80 is
# a conservative proxy for that.
_QA_MIN_CONFIDENCE = 80
# Drop sensors that have not reported within this many seconds of the snapshot
# (stale sensors would smear an old reading into a "live" frame).
_QA_MAX_AGE_SECONDS = 3600
# Physically-plausible CF=1 bounds (ug/m3). Above this is almost always a fault.
_QA_MAX_RAW_PM = 3000.0

# Barkjohn (2021) US-wide correction coefficients.
_EPA_SLOPE = 0.524
_EPA_RH_COEF = 0.0862
_EPA_INTERCEPT = 5.75


def load_api_key():
    """PurpleAir read key from the environment (never committed)."""
    return os.environ.get("PURPLEAIR_API_KEY", "")


def _epa_correct(cf1, rh):
    """Barkjohn (2021) US-wide PurpleAir correction -> reference-comparable ug/m3.

    Single swap point if the newer EPA piecewise equation is adopted later.
    Result is clamped at 0 (the linear form can go slightly negative at very
    low PM and high RH).
    """
    corrected = _EPA_SLOPE * cf1 - _EPA_RH_COEF * rh + _EPA_INTERCEPT
    return corrected if corrected > 0 else 0.0


def _to_float(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_purpleair_frame(api_key=None, bbox=None, timeout=20, retries=1):
    """Fetch + clean one PurpleAir snapshot over the bbox.

    Returns a dict:
        {
          "captured_at": <unix seconds, the provider's data_time_stamp>,
          "points": [ {"id", "lat", "lon", "pm", "raw", "rh", "conf"} , ... ],
          "stats": {"received", "kept", "dropped"},
        }
    `pm` is the EPA-corrected ug/m3 (rounded to 1 dp); `raw` is the uncorrected
    CF=1 value. Raises requests.RequestException on transport failure and
    RuntimeError on an API-level error so the caller can decide whether to skip
    writing a frame.
    """
    api_key = api_key or load_api_key()
    if not api_key:
        raise RuntimeError("PURPLEAIR_API_KEY is not set")
    box = bbox or ONTARIO_BBOX

    params = {
        "fields": _REQUEST_FIELDS,
        "nwlng": box["nwlng"], "nwlat": box["nwlat"],
        "selng": box["selng"], "selat": box["selat"],
        # Real-time snapshot (Average=0). Anything else returns time-averaged
        # values, which would blur the 5-minute plume frames.
        "average": 0,
        # Server-side freshness filter: only return sensors that reported within
        # the QA window. Fewer rows = fewer billed points, and it lets us drop the
        # last_seen field (freshness is now enforced by PurpleAir, not us).
        "max_age": _QA_MAX_AGE_SECONDS,
    }
    # Retry transient transport hiccups (Vercel egress, brief PurpleAir 5xx/429).
    # The final failure propagates with its response attached so the caller can
    # surface the actual HTTP status. A 403 here almost always means a bad/unset
    # PURPLEAIR_API_KEY in the deployment env.
    resp = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                PURPLEAIR_BASE,
                headers={"X-API-Key": api_key},
                params=params,
                timeout=timeout,
            )
            resp.raise_for_status()
            break
        except requests.RequestException:
            if attempt >= retries:
                raise
            time.sleep(1.0)
    data = resp.json()

    fields = data.get("fields")
    rows = data.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise RuntimeError("Unexpected PurpleAir response shape")

    idx = {name: i for i, name in enumerate(fields)}
    required = ("latitude", "longitude", "pm2.5_cf_1", "humidity",
                "confidence", "channel_flags")
    missing = [c for c in required if c not in idx]
    if missing:
        raise RuntimeError(f"PurpleAir response missing fields: {missing}")

    snapshot_ts = int(data.get("data_time_stamp") or data.get("time_stamp") or time.time())

    points = []
    received = len(rows)
    for row in rows:
        try:
            flags = row[idx["channel_flags"]]
            conf = row[idx["confidence"]]
            lat = _to_float(row[idx["latitude"]])
            lon = _to_float(row[idx["longitude"]])
            cf1 = _to_float(row[idx["pm2.5_cf_1"]])
            rh = _to_float(row[idx["humidity"]])
            sid = row[idx["sensor_index"]] if "sensor_index" in idx else None
        except (IndexError, KeyError):
            continue

        # --- Quality control ---
        if flags != _QA_CHANNEL_FLAG_OK:
            continue
        if conf is None or conf < _QA_MIN_CONFIDENCE:
            continue
        # Freshness is enforced server-side via the `max_age` request param above.
        if lat is None or lon is None:
            continue
        if cf1 is None or cf1 < 0 or cf1 > _QA_MAX_RAW_PM:
            continue
        if rh is None or rh < 0 or rh > 100:
            continue

        pm = _epa_correct(cf1, rh)
        points.append({
            "id": sid,
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "pm": round(pm, 1),
            "raw": round(cf1, 1),
            "rh": round(rh, 0),
            "conf": int(conf),
        })

    return {
        "captured_at": snapshot_ts,
        "points": points,
        "stats": {
            "received": received,
            "kept": len(points),
            "dropped": received - len(points),
        },
    }
