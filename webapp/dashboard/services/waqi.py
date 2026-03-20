"""
WAQI (World Air Quality Index) API client and PM2.5 fetching.
"""

import json
import math
import os

import requests

from .data import CONFIG_PATH

WAQI_BASE = "https://api.waqi.info"

# US EPA PM2.5 AQI breakpoints for AQI → µg/m³ conversion
_AQI_BREAKPOINTS = [
    (0,   50,   0.0,   12.0),
    (51,  100,  12.1,  35.4),
    (101, 150,  35.5,  55.4),
    (151, 200,  55.5,  150.4),
    (201, 300,  150.5, 250.4),
    (301, 400,  250.5, 350.4),
    (401, 500,  350.5, 500.4),
]


def _aqi_to_ugm3(aqi):
    """Convert PM2.5 AQI value to µg/m³ using US EPA breakpoints."""
    if aqi <= 0:
        return 0.0
    for aqi_lo, aqi_hi, c_lo, c_hi in _AQI_BREAKPOINTS:
        if aqi_lo <= aqi <= aqi_hi:
            return round(((aqi - aqi_lo) * (c_hi - c_lo)) / (aqi_hi - aqi_lo) + c_lo, 1)
    # Above 500 AQI — linear extrapolation
    return round(aqi * 1.0, 1)


def load_config():
    # Prefer environment variable over config file
    if os.environ.get("WAQI_API_TOKEN"):
        return {"api_key": os.environ["WAQI_API_TOKEN"]}
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def _haversine(lat1, lon1, lat2, lon2):
    """Distance in km between two lat/lon points."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _fetch_waqi_bbox(token, lat1, lng1, lat2, lng2):
    """Fetch WAQI stations within a bounding box. Returns list of station dicts."""
    try:
        resp = requests.get(
            f"{WAQI_BASE}/v2/map/bounds",
            params={
                "latlng": f"{lat1},{lng1},{lat2},{lng2}",
                "networks": "all",
                "token": token,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
    except requests.RequestException:
        return []

    if data.get("status") != "ok":
        return []

    result = []
    for entry in data.get("data", []):
        try:
            aqi_val = entry.get("aqi")
            if aqi_val is None or aqi_val == "-" or int(aqi_val) < 0:
                continue
            lat = entry["lat"]
            lon = entry["lon"]
            pm25 = _aqi_to_ugm3(int(aqi_val))
            result.append({
                "lat": float(lat),
                "lon": float(lon),
                "pm25": pm25,
                "name": entry.get("station", {}).get("name", ""),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return result


def fetch_latest_pm25(api_key, stations):
    """Fetch PM2.5 for stations using per-city WAQI bounding-box queries.

    Groups stations by target_city and makes one bounding-box request
    per city. WAQI returns AQI values which are converted to µg/m³.
    """
    # Only stations with coordinates
    with_coords = [s for s in stations if s.get("lat") and s.get("lon")]
    if not with_coords:
        return {}

    # Group stations by target city
    city_groups = {}
    for s in with_coords:
        city = s.get("target_city", "")
        city_groups.setdefault(city, []).append(s)

    readings = {}
    pad = 0.5  # ~55 km padding

    for city, city_stations in city_groups.items():
        lats = [s["lat"] for s in city_stations]
        lons = [s["lon"] for s in city_stations]
        # WAQI bbox: lat1,lng1 = SW corner, lat2,lng2 = NE corner
        lat1 = min(lats) - pad
        lng1 = min(lons) - pad
        lat2 = max(lats) + pad
        lng2 = max(lons) + pad

        waqi_stations = _fetch_waqi_bbox(api_key, lat1, lng1, lat2, lng2)
        if not waqi_stations:
            continue

        # Match each station to nearest WAQI station within 30 km
        for st in city_stations:
            best_dist = 30  # km max
            best_pm = None
            for ws in waqi_stations:
                d = _haversine(st["lat"], st["lon"], ws["lat"], ws["lon"])
                if d < best_dist:
                    best_dist = d
                    best_pm = ws["pm25"]
            if best_pm is not None:
                # Composite key so the same NAPS id under two target cities does not overwrite.
                readings[f"{st['id']}|{city}"] = best_pm

    return readings
