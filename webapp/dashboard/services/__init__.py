"""
Services package for the PM2.5 Early Warning System.

Re-exports all public names so existing code using `from .. import services`
and `services.XYZ` continues to work without changes.
"""

from .data import (
    DATA_DIR,
    CONFIG_PATH,
    CITIES,
    DEMO_DATA,
    DEMO_DEFAULT_PM25,
    EXCLUDED_STATION_IDS,
    TORONTO_NAPS_REFERENCE_IDS,
    THUNDER_BAY_STATION_IDS,
    load_stations,
    load_all_stations,
    get_all_demo_data,
    build_demo_previous_readings,
)

from .evaluate import (
    ALERT_LEVELS,
    RULE1_TRIGGER,
    RULE2_DISTANT_TRIGGER,
    RULE2_INTERMEDIATE,
    RULE3_CORRIDOR_TRIGGER,
    CITY_ELEVATED_THRESHOLD,
    EVALUATION_WINDOW_HOURS,
    EVENT_COOLDOWN_HOURS,
    CONFIRMATION_WINDOW_HOURS,
    get_alert_level,
    lead_time_str,
    reading_for_station,
    evaluate,
)

from .waqi import load_config, fetch_latest_pm25
