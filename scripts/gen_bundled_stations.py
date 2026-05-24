"""
Generate webapp/dashboard/services/bundled_stations.json from the real research
Excel files + NAPS coordinate lookup.

Run from repo root:  python scripts/gen_bundled_stations.py [--write]

Without --write it only reports coverage. With --write it overwrites the bundled JSON.

Source of truth (local only, gitignored):
  projdata/07.  The 4 Cities - Regression formulas and alert network stations/{City}/01.*Regression_Formulas.xlsx
  projdata/05. NAPS Stations/04.  Canada_NAPS_Stations_Active_Years.xlsx
"""
import json
import os
import sys

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_BASE = os.path.join(
    ROOT, "projdata", "07.  The 4 Cities - Regression formulas and alert network stations"
)
NAPS_PATH = os.path.join(
    ROOT, "projdata", "05. NAPS Stations", "04.  Canada_NAPS_Stations_Active_Years.xlsx"
)
OUT_PATH = os.path.join(
    ROOT, "webapp", "dashboard", "services", "bundled_stations.json"
)

FILES = {
    "Toronto": "Toronto/01. Toronto_Network_All_Regression_Formulas.xlsx",
    "Montreal": "Montreal/01.Montreal_Network_All_Regression_Formulas.xlsx",
    "Edmonton": "Edmonton/01.Edmonton_Network_All_Regression_Formulas.xlsx",
    "Vancouver": "Vancouver/01.Vancouver_Network_All_Regression_Formulas.xlsx",
}

# Mirror data.py
EXCLUDED_STATION_IDS = {"50308", "50310", "50314", "50313", "55702"}
MIN_CORRELATION_R = 0.30


def find_col(headers, *cands):
    for i, h in enumerate(headers):
        if h is None:
            continue
        hl = str(h).lower().strip()
        for c in cands:
            if c.lower() in hl:
                return i
    return None


def load_naps_coords():
    wb = openpyxl.load_workbook(NAPS_PATH, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    headers = [str(h).strip() if h else "" for h in rows[0]]
    c_id = find_col(headers, "naps id")
    c_lat = find_col(headers, "latitude")
    c_lon = find_col(headers, "longitude")
    coords = {}
    for row in rows[1:]:
        if c_id is None or row[c_id] is None:
            continue
        sid = str(row[c_id]).strip()
        # NAPS ids may be stored as floats like 60106.0
        if sid.endswith(".0"):
            sid = sid[:-2]
        try:
            coords[sid] = (float(row[c_lat]), float(row[c_lon]))
        except (TypeError, ValueError):
            continue
    return coords


def load_existing_coords():
    """Preserve any coords already present in the current bundled JSON (esp. US EPA ids)."""
    if not os.path.isfile(OUT_PATH):
        return {}
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    if isinstance(raw, dict):
        for rows in raw.values():
            if not isinstance(rows, list):
                continue
            for r in rows:
                if isinstance(r, dict) and r.get("lat") is not None and r.get("lon") is not None:
                    out[str(r.get("id"))] = (float(r["lat"]), float(r["lon"]))
    return out


def load_city(city, rel):
    path = os.path.join(RESEARCH_BASE, rel)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    headers = [str(h).strip() if h else "" for h in rows[4]]
    c_id = find_col(headers, "station id")
    c_city = find_col(headers, "city")
    c_dist = find_col(headers, "distance")
    c_dir = find_col(headers, "direction")
    c_tier = find_col(headers, "tier")
    c_slope = find_col(headers, "slope")
    c_int = find_col(headers, "intercept")
    c_dtype = find_col(headers, "data type")
    c_r = None
    for i, h in enumerate(headers):
        if h.strip() in ("R", "R²"):
            c_r = i
            break

    stations = []
    for row in rows[5:]:
        if c_id is None or row[c_id] is None:
            continue
        sid = str(row[c_id]).strip()
        if sid.endswith(".0"):
            sid = sid[:-2]
        if not sid:
            continue
        sc = sid.replace(" ", "").replace(".", "").replace("-", "")
        if not sc.isdigit() or len(sid) > 15:
            continue
        if sid in EXCLUDED_STATION_IDS:
            continue
        rv = row[c_r] if c_r is not None and row[c_r] else 0
        try:
            if float(rv) < MIN_CORRELATION_R:
                continue
        except (TypeError, ValueError):
            continue

        def num(idx, default=0):
            if idx is None or row[idx] is None:
                return default
            try:
                return float(row[idx])
            except (TypeError, ValueError):
                return default

        tier_raw = row[c_tier] if c_tier is not None else 1
        try:
            tier = int(str(tier_raw).replace("Tier", "").strip()) if tier_raw else 1
        except (TypeError, ValueError):
            tier = 1

        stations.append({
            "id": sid,
            "city_name": str(row[c_city] or "") if c_city is not None else "",
            "distance": num(c_dist),
            "direction": str(row[c_dir] or "") if c_dir is not None else "",
            "tier": tier,
            "R": round(num(c_r), 3),
            "slope": num(c_slope),
            "intercept": num(c_int),
            "data_type": str(row[c_dtype] or "") if c_dtype is not None else "",
        })
    return stations


# Thunder Bay NAPS stations for Rule 2 (methodology Section 5). Weak correlation with Toronto;
# used only for the distant trigger (>35 µg/m³). Injected into Toronto so production (no NAPS
# file) still has them with exact coords.
THUNDER_BAY_IDS = ("60807", "60809")


def main():
    write = "--write" in sys.argv
    naps = load_naps_coords()
    existing = load_existing_coords()
    print(f"NAPS coords loaded: {len(naps)}")

    out = {}
    grand = 0
    grand_coords = 0
    no_coord_ids = []
    for city, rel in FILES.items():
        sts = load_city(city, rel)

        # Inject Thunder Bay Rule-2 stations into Toronto if absent (coords from NAPS).
        if city == "Toronto":
            have = {s["id"] for s in sts}
            for tb in THUNDER_BAY_IDS:
                if tb not in have and tb in naps:
                    sts.append({
                        "id": tb, "city_name": "Thunder Bay", "distance": 1200.0,
                        "direction": "NW", "tier": 2, "R": 0.0, "slope": 0.3,
                        "intercept": 5.0, "data_type": "Rule2",
                    })

        with_coords = 0
        for s in sts:
            c = naps.get(s["id"]) or existing.get(s["id"])
            if c:
                # Exact coordinate: bake it so production (no NAPS file) keeps precise placement.
                s["lat"], s["lon"] = c[0], c[1]
                s["coord_source"] = "naps"
                with_coords += 1
            else:
                # No exact coord (US EPA stations): leave null. load_stations() derives lat/lon
                # at runtime from city center + distance + direction (coord_source="derived").
                s["lat"] = None
                s["lon"] = None
                no_coord_ids.append(f"{city}:{s['id']}({s['data_type']})")
        out[city] = sts
        grand += len(sts)
        grand_coords += with_coords
        print(f"{city}: {len(sts)} stations, {with_coords} exact coords, {len(sts)-with_coords} runtime-derived")

    print(f"TOTAL: {grand} stations, {grand_coords} exact, {grand-grand_coords} derived")
    if no_coord_ids:
        print("Runtime-derived (US EPA):", ", ".join(no_coord_ids))

    if write:
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print(f"WROTE {OUT_PATH}")
    else:
        print("(dry run — pass --write to overwrite bundled_stations.json)")


if __name__ == "__main__":
    main()
