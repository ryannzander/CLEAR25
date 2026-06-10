"""Read-only fusion: cross-check the validated CLEAR alert against the live
PurpleAir observation near each city.

ISOLATION GUARANTEE: this module only READS the latest PurpleAir frame and the
already-computed CLEAR alert and reports whether they agree. It does NOT modify
evaluate.py, CachedResult(key="latest"), or any alert decision. Wiring a fused
signal into the live 3-rule engine is a separate, deferred methodology step that
must be backtested against the historical events first.

What "confirmation" means here: for a city, take the median EPA-corrected PM2.5
of the PurpleAir sensors within CONFIRM_RADIUS_KM of the city centre and compare
it to the methodology's CITY_ELEVATED_THRESHOLD (20 µg/m³). Then state how that
observation lines up with whether CLEAR is currently alerting for that city.
"""

import math
import re

from .data import CITIES
from .evaluate import CITY_ELEVATED_THRESHOLD, RULE2_DISTANT_TRIGGER  # 20, 35 µg/m³

# Sensors within this radius of a city centre count as "near" that city.
CONFIRM_RADIUS_KM = 60.0
# Below this many nearby sensors, a city's PurpleAir reading isn't robust enough
# to confirm/contradict an alert, so we report it as uncovered.
MIN_CONFIRM_SENSORS = 3

# ECCC early-warning thresholds (µg/m³). A city's *forecast* PM2.5 crossing the
# city-elevated threshold = modelled smoke arrival; a far-field upwind point
# crossing the distant-station trigger = smoke already aloft beyond the ring.
EW_CITY_THRESHOLD = CITY_ELEVATED_THRESHOLD      # 20
EW_FARFIELD_THRESHOLD = RULE2_DISTANT_TRIGGER    # 35

# Parses the far-field sample-point ids the ECCC ingest casts, e.g.
# "FAR:Toronto-315deg-800km" -> (city, bearing_deg, distance_km).
_FAR_RE = re.compile(r"^FAR:(?P<city>.+)-(?P<brg>\d{3})deg-(?P<dist>\d+)km$")
_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def _compass(bearing_deg):
    return _COMPASS[int((bearing_deg % 360) / 22.5 + 0.5) % 16]


def _haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def city_purpleair_observation(lat, lon, points, radius_km=CONFIRM_RADIUS_KM):
    """Median corrected PM2.5 of PurpleAir sensors within radius_km of (lat, lon).

    `points` is a PlumeFrame payload's point list ({"lat","lon","pm"}). Returns
    {"n": <sensors used>, "pm": <median µg/m³ or None>}.
    """
    vals = []
    for p in points:
        plat, plon, pm = p.get("lat"), p.get("lon"), p.get("pm")
        if plat is None or plon is None or pm is None:
            continue
        if _haversine_km(lat, lon, float(plat), float(plon)) <= radius_km:
            vals.append(float(pm))
    if not vals:
        return {"n": 0, "pm": None}
    vals.sort()
    n = len(vals)
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return {"n": n, "pm": round(median, 1)}


def confirmation_for_city(city_key, clear_alert, points, threshold=CITY_ELEVATED_THRESHOLD):
    """Compare CLEAR's alert for a city with the live PurpleAir observation.

    clear_alert: the city's entry from CachedResult.city_alerts (or None).
    Returns a flat dict describing both sides and an agreement ``status``:
        confirmed       — CLEAR alerting AND PurpleAir elevated  (strong)
        unconfirmed     — CLEAR alerting BUT PurpleAir calm       (check)
        purpleair_only  — PurpleAir elevated BUT CLEAR calm       (possible early/extra signal)
        agree_calm      — both calm
        no_purpleair    — no PurpleAir sensors near this city (e.g. outside Ontario coverage)
    """
    c = CITIES.get(city_key, {})
    obs = city_purpleair_observation(c.get("lat"), c.get("lon"), points) \
        if c.get("lat") is not None else {"n": 0, "pm": None}

    clear_alerting = bool(clear_alert and clear_alert.get("alert"))
    pa_pm = obs["pm"]
    pa_elevated = pa_pm is not None and pa_pm >= threshold

    if obs["n"] < MIN_CONFIRM_SENSORS:
        status = "no_purpleair"
    elif clear_alerting and pa_elevated:
        status = "confirmed"
    elif clear_alerting and not pa_elevated:
        status = "unconfirmed"
    elif not clear_alerting and pa_elevated:
        status = "purpleair_only"
    else:
        status = "agree_calm"

    return {
        "city": city_key,
        "clear_alerting": clear_alerting,
        "clear_level": (clear_alert or {}).get("level_name"),
        "clear_predicted_pm25": (clear_alert or {}).get("predicted_pm25"),
        "purpleair_pm25": pa_pm,
        "purpleair_sensors": obs["n"],
        "purpleair_elevated": pa_elevated,
        "threshold": threshold,
        "status": status,
    }


def _series_for(point, prefer_wildfire=True):
    """Pick a point's forecast value list: wildfire-smoke-only PM if present
    (cleanest smoke signal), else total PM."""
    if not isinstance(point, dict):
        return None
    if prefer_wildfire and isinstance(point.get("pm25_wildfire"), list):
        return point["pm25_wildfire"]
    return point.get("pm25") if isinstance(point.get("pm25"), list) else None


def early_warning_for_city(city_key, eccc_forecast):
    """ECCC RAQDPS far-field early warning for a city (READ-ONLY).

    Reads the stored 72h forecast and answers: does the model predict smoke
    reaching this city, and roughly when? This is the piece that extends warning
    *beyond* the ~600 km station ring — the model carries smoke from upwind
    sources the station network can't see yet. Returns lead time to the city plus
    the upwind far-field points that are already elevated (where it's coming from).
    """
    if not isinstance(eccc_forecast, dict):
        return {"available": False}
    points = eccc_forecast.get("points") or {}
    hours = eccc_forecast.get("hours") or []
    if not hours:
        return {"available": False}

    # When does the city centroid's forecast cross the arrival threshold?
    city_series = _series_for(points.get(f"CITY:{city_key}"))
    arriving_in, peak, peak_h = None, None, None
    if city_series:
        for h, v in zip(hours, city_series):
            if v is None:
                continue
            if peak is None or v > peak:
                peak, peak_h = v, h
            if arriving_in is None and v >= EW_CITY_THRESHOLD:
                arriving_in = h

    # Upwind far-field points for this city that are elevated anywhere in the run.
    origins = []
    for sid, pt in points.items():
        m = _FAR_RE.match(sid)
        if not m or m.group("city") != city_key:
            continue
        vals = _series_for(pt)
        if not vals:
            continue
        mx = max((v for v in vals if v is not None), default=None)
        if mx is not None and mx >= EW_FARFIELD_THRESHOLD:
            origins.append({
                "direction": _compass(int(m.group("brg"))),
                "distance_km": int(m.group("dist")),
                "peak_pm25": round(mx, 1),
            })
    origins.sort(key=lambda o: o["distance_km"])

    return {
        "available": True,
        "run": eccc_forecast.get("run"),
        "incoming": arriving_in is not None,
        "arriving_in_hours": arriving_in,
        "forecast_peak_pm25": round(peak, 1) if peak is not None else None,
        "peak_in_hours": peak_h,
        "city_threshold": EW_CITY_THRESHOLD,
        "origins": origins,
    }
