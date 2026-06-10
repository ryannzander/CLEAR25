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

from .data import CITIES
from .evaluate import CITY_ELEVATED_THRESHOLD  # 20 µg/m³ (methodology Section)

# Sensors within this radius of a city centre count as "near" that city.
CONFIRM_RADIUS_KM = 60.0
# Below this many nearby sensors, a city's PurpleAir reading isn't robust enough
# to confirm/contradict an alert, so we report it as uncovered.
MIN_CONFIRM_SENSORS = 3


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
