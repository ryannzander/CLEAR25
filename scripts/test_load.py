"""Quick isolation test of the station loader (no full Django app needed).

Run from repo root:  python scripts/test_load.py [--bundled-only]

--bundled-only simulates production (no Excel/NAPS, openpyxl effectively unused) by pointing
the research/NAPS paths at a nonexistent dir, forcing the bundled_stations.json fallback.
"""
import os
import sys

from django.conf import settings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED_ONLY = "--bundled-only" in sys.argv

if BUNDLED_ONLY:
    research = os.path.join(ROOT, "does-not-exist")
    naps = os.path.join(ROOT, "does-not-exist", "naps.xlsx")
else:
    research = os.path.join(ROOT, "projdata", "07.  The 4 Cities - Regression formulas and alert network stations")
    naps = os.path.join(ROOT, "projdata", "05. NAPS Stations", "04.  Canada_NAPS_Stations_Active_Years.xlsx")

settings.configure(
    DATA_DIR=os.path.join(ROOT, "data"),
    RESEARCH_DATA_BASE=research,
    NAPS_STATIONS_PATH=naps,
)

sys.path.insert(0, os.path.join(ROOT, "webapp"))
from dashboard.services import data  # noqa: E402

mode = "BUNDLED-ONLY (production sim)" if BUNDLED_ONLY else "RESEARCH EXCEL (local)"
print(f"=== {mode} ===")

allst = data.load_all_stations()
print(f"TOTAL stations: {len(allst)}")

by_city = {}
src = {}
mappable = 0
for s in allst:
    by_city.setdefault(s["target_city"], 0)
    by_city[s["target_city"]] += 1
    cs = s.get("coord_source", "none") if (s.get("lat") is not None) else "NO-COORD"
    src[cs] = src.get(cs, 0) + 1
    if s.get("lat") is not None and s.get("lon") is not None:
        mappable += 1

for c, n in by_city.items():
    print(f"  {c}: {n}")
print(f"Mappable (lat/lon present): {mappable} / {len(allst)}")
print(f"Coord sources: {src}")

# WAQI candidate count (what live matching would consider)
waqi_candidates = [s for s in allst if s.get("lat") and s.get("lon") and s.get("coord_source") != "derived"]
print(f"WAQI-eligible (exact coords only): {len(waqi_candidates)}")

# Spot-check a derived US station lands in plausible US territory
for s in allst:
    if s.get("coord_source") == "derived":
        print(f"Sample derived: {s['target_city']} id={s['id']} dir={s['direction']} dist={s['distance']:.0f}km -> ({s['lat']}, {s['lon']})")
        break
