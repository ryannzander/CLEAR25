"""
Three-rule detection and evaluation logic.

Source: CLEAR_Methodology_ScienceFair Ver#1
(data/LAPTOP TSF 2026/01. Summary Documents/CLEAR_Methodology_ScienceFair Ver#1.pdf)

C.L.E.A.R. (Canadian Lead-Time Early Air Response) system uses three complementary
detection rules for 360-degree coverage of incoming wildfire smoke.
"""

# ---------------------------------------------------------------------------
# Alert levels & colors (per methodology Section 3)
# ---------------------------------------------------------------------------
ALERT_LEVELS = [
    {"name": "LOW",       "min": 0,   "max": 20,    "hex": "#22c55e", "text_color": "black",
     "health": "No significant risk. No action required."},
    {"name": "MODERATE",  "min": 20,  "max": 60,    "hex": "#eab308", "text_color": "black",
     "health": "Sensitive groups (children, elderly, respiratory conditions) should reduce outdoor activity."},
    {"name": "HIGH",      "min": 60,  "max": 80,    "hex": "#f97316", "text_color": "black",
     "health": "General population affected. Reduce prolonged outdoor exertion. Use N95/KN95 mask outdoors."},
    {"name": "VERY HIGH", "min": 80,  "max": 120,   "hex": "#ef4444", "text_color": "white",
     "health": "Significant risk for all. Avoid outdoor exertion. Keep doors and windows closed."},
    {"name": "EXTREME",   "min": 120, "max": 1e9,   "hex": "#7f1d1d", "text_color": "white",
     "health": "Emergency conditions. Stay indoors. Close windows. Run HEPA filter. No indoor pollution sources."},
]

# ---------------------------------------------------------------------------
# Three-Rule Detection System (methodology Sections 4–6)
# ---------------------------------------------------------------------------
# Rule 1: Regional Station Alert - Ontario/Quebec 100–650 km, > 40 µg/m³
# Rule 2: NW Ontario Sequential - Thunder Bay/distant > 35 µg/m³ + intermediate > 20 µg/m³
# Rule 3: Quebec Upstream - Same as Rule 1 for Quebec fires 500–1400 km NE

RULE1_TRIGGER = 40           # µg/m³ - Regional station exceeds this → evaluation begins
RULE2_DISTANT_TRIGGER = 35   # µg/m³ - Thunder Bay / distant station (600+ km) trigger
RULE2_INTERMEDIATE = 20      # µg/m³ - Intermediate station confirmation threshold
RULE3_CORRIDOR_TRIGGER = 40  # µg/m³ - Corridor station trigger

CITY_ELEVATED_THRESHOLD = 20    # µg/m³ - City PM2.5 level that confirms smoke arrival
EVALUATION_WINDOW_HOURS = 120   # 5 days - Time window to check for city elevation
EVENT_COOLDOWN_HOURS = 168      # 7 days - Minimum separation between events
CONFIRMATION_WINDOW_HOURS = 96  # 4 days - Time for intermediate confirmation (Rule 2)


def get_alert_level(pm25):
    for lvl in reversed(ALERT_LEVELS):
        if pm25 >= lvl["min"]:
            return lvl
    return ALERT_LEVELS[0]


def lead_time_str(tier, dist):
    """Calculate estimated lead time based on station distance and tier.

    Lead times per CLEAR_Methodology_ScienceFair Ver#1:
    - Distant stations (1000+ km): 15-92 hours (avg 15.3h)
    - Regional stations (100-600 km): 0-48 hours (varies by wind)
    - Corridor stations (300-500 km): 0-24 hours (often simultaneous)
    """
    if dist > 1000:
        return "24-72 hrs"
    if dist > 600:
        return "18-48 hrs"
    if dist > 400:
        return "12-36 hrs"
    if dist > 250:
        return "8-24 hrs"
    if dist > 150:
        return "4-18 hrs"
    return "2-12 hrs"


def _weighted_prediction(city_rows):
    """Calculate R-value weighted average prediction for a city.

    Stations with higher R-values (better correlation) have more influence.
    Uses R² as weight to emphasize high-correlation stations even more.
    Falls back to simple average if no valid R-values.
    """
    weighted_sum = 0.0
    weight_total = 0.0

    for r in city_rows:
        R = r.get("R", 0)
        pred = r["predicted"]
        # Use R² as weight (squares emphasize high-R stations)
        # Minimum weight of 0.1 to include all stations somewhat
        weight = max(R * R, 0.1)
        weighted_sum += weight * pred
        weight_total += weight

    if weight_total > 0:
        return weighted_sum / weight_total
    # Fallback to simple average
    return sum(r["predicted"] for r in city_rows) / len(city_rows) if city_rows else 0


def evaluate(stations, readings, previous_readings=None):
    """Evaluate stations using 3-rule detection system.

    Source: CLEAR_Methodology_ScienceFair Ver#1

    Three-Rule Detection:
    - Rule 1: Regional Alert - Station 100–650 km > 40 µg/m³, regression predicts city ≥ 20 µg/m³
    - Rule 2: NW Ontario Sequential - Distant (600+ km) > 35 µg/m³ + intermediate 200–600 km
              > 20 µg/m³ (both current and previous hour). Methodology specifies Thunder Bay.
    - Rule 3: Quebec Upstream - Same as Rule 1 for corridor stations < 400 km

    previous_readings: dict of {station_id: pm25} from the previous hour,
                       used for Rule 2 (sequential confirmation within 96 hrs).

    Predictions are weighted by R-value (correlation coefficient) so that
    more reliable stations have greater influence on city-level predictions.
    """
    if previous_readings is None:
        previous_readings = {}

    # Build per-station results
    results = []
    for st in stations:
        sid = st["id"]
        if sid not in readings:
            continue
        pm = readings[sid]
        pred = st["slope"] * pm + st["intercept"]
        lvl = get_alert_level(pred)
        results.append({
            "station": st["city_name"], "id": sid,
            "dist": st["distance"], "dir": st["direction"],
            "tier": st["tier"], "R": st["R"], "pm25": pm,
            "predicted": round(pred, 1),
            "level_name": lvl["name"], "level_hex": lvl["hex"],
            "level_text_color": lvl["text_color"], "health": lvl["health"],
            "lead": lead_time_str(st["tier"], st["distance"]),
            "target_city": st.get("target_city", ""),
            "lat": st.get("lat"), "lon": st.get("lon"),
        })
    results.sort(key=lambda x: x["predicted"], reverse=True)

    # --- City-level alert determination using 3-rule system ---
    city_results = {}
    for r in results:
        city = r.get("target_city", "")
        city_results.setdefault(city, []).append(r)

    city_alerts = {}
    for city, city_rows in city_results.items():
        alert_triggered = False
        trigger_rule = None
        trigger_stations = []

        weighted_pred = _weighted_prediction(city_rows)
        max_predicted = max((r["predicted"] for r in city_rows), default=0)

        regional_stations = [r for r in city_rows if r["tier"] == 1 and r["dist"] <= 600]
        distant_stations = [r for r in city_rows if r["dist"] > 600]
        corridor_stations = [r for r in city_rows if r["tier"] >= 2 and r["dist"] <= 400]

        # Rule 1: Regional Station Alert (100-600 km)
        for r in regional_stations:
            if r["pm25"] >= RULE1_TRIGGER:
                alert_triggered = True
                trigger_rule = "rule1"
                trigger_stations.append(r["station"])
                break

        # Rule 2: Distant Sequential Detection (600+ km)
        if not alert_triggered and previous_readings:
            distant_triggers = [
                r for r in distant_stations
                if r["pm25"] >= RULE2_DISTANT_TRIGGER
            ]
            intermediate_confirmed = [
                r for r in city_rows
                if 200 <= r["dist"] <= 600
                and r["pm25"] >= RULE2_INTERMEDIATE
                and previous_readings.get(r["id"], 0) >= RULE2_INTERMEDIATE
            ]
            if distant_triggers and intermediate_confirmed:
                alert_triggered = True
                trigger_rule = "rule2"
                trigger_stations = [distant_triggers[0]["station"], intermediate_confirmed[0]["station"]]

        # Rule 3: Corridor Detection (upwind stations < 400 km)
        if not alert_triggered:
            for r in corridor_stations:
                if r["pm25"] >= RULE3_CORRIDOR_TRIGGER:
                    alert_triggered = True
                    trigger_rule = "rule3"
                    trigger_stations.append(r["station"])
                    break

        alert_prediction = weighted_pred

        if alert_triggered:
            lvl = get_alert_level(alert_prediction)
            if lvl["name"] != "LOW":
                city_alerts[city] = {
                    "alert": True,
                    "rule": trigger_rule,
                    "trigger_stations": trigger_stations,
                    "predicted_pm25": round(alert_prediction, 1),
                    "weighted_pm25": round(weighted_pred, 1),
                    "max_pm25": round(max_predicted, 1),
                    "level_name": lvl["name"],
                    "level_hex": lvl["hex"],
                    "level_text_color": lvl["text_color"],
                    "health": lvl["health"],
                }

        if city not in city_alerts:
            low_lvl = ALERT_LEVELS[0]
            city_alerts[city] = {
                "alert": False,
                "rule": None,
                "trigger_stations": [],
                "predicted_pm25": round(weighted_pred, 1),
                "weighted_pm25": round(weighted_pred, 1),
                "max_pm25": round(max_predicted, 1),
                "level_name": low_lvl["name"],
                "level_hex": low_lvl["hex"],
                "level_text_color": low_lvl["text_color"],
                "health": low_lvl["health"],
            }

    return {"stations": results, "city_alerts": city_alerts}
