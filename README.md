# C.L.E.A.R. — Canadian Lead-Time Early Air Response

A PM2.5 wildfire smoke early warning system that uses existing air quality monitoring stations located 100–600+ km away to provide **6–48 hours of advance warning** before dangerous smoke arrives in major Canadian cities.

**Source of truth:** CLEAR_Methodology_ScienceFair Ver#1 (see data folder)

**Authors:** Hugo Bui & Ryan Zander — University of Toronto Schools

## Cities Covered

- Toronto
- Montreal
- Edmonton
- Vancouver

## How It Works

The system uses simple linear regression models between distant monitoring stations and target cities:

```
PM2.5_city = slope × PM2.5_station + intercept
```

When a remote station's PM2.5 reading exceeds a computed threshold, a colour-coded health alert is triggered hours before the smoke reaches the city.

### Alert Levels

| Level | PM2.5 (µg/m³) | Action |
|-------|---------------|--------|
| Low | < 20 | No precautions needed |
| Moderate | 21–60 | Sensitive groups avoid strenuous activities |
| High | 61–80 | Reduce exertion, wear N95, close windows, run HEPA |
| Very High | 81–120 | Avoid all outdoor activity |
| Extreme | > 120 | Halt indoor pollution sources |

### Station Selection Criteria

- R ≥ 0.30, P < 0.001, N ≥ 100 observations
- Tier 1: >250 km (12–48 hr lead time)
- Tier 2: 100–250 km (6–18 hr lead time)

## Validation (per methodology)

- **90.9% accuracy** | **100% sensitivity** (zero missed events)
- **15.7h mean lead time** | **87h maximum**
- Study period: 2003–2023, wildfire season (May–September)
- 36M+ hourly observations from NAPS and U.S. EPA networks

## Setup

```bash
pip install -r requirements.txt
cd webapp
python manage.py migrate
python manage.py runserver
```

Open http://127.0.0.1:8000/

## Configuration

### Environment Variables

- **WAQI_API_TOKEN** — World Air Quality Index API key for live PM2.5 data (required for production refresh)
- **CRON_SECRET** — Secret for cron-triggered data refresh
- **SECRET_KEY** — Django secret (required in production)
- **DATABASE_URL** — PostgreSQL connection string (optional; falls back to SQLite)

### Config File (optional)

Create `data/config.json` (copy from `data/config.example.json`):

```json
{
    "api_key": "YOUR_WAQI_API_KEY_HERE"
}
```

The app prefers `WAQI_API_TOKEN` env var over `config.json`.

## Data Folder Layout

Research data lives in `data/LAPTOP TSF 2026/`. Do not guess — refer to .cursorrules for paths.

| Path | Description |
|------|-------------|
| `01. Summary Documents/` | CLEAR_Methodology_ScienceFair Ver#1.pdf |
| `07. The 4 Cities - Regression formulas.../` | Per-city Excel regression files |
| `10. Validation/` | SAQS validation data |
| `05. NAPS Stations/` | NAPS station coordinates |

Run `python manage.py validate_saqs` to report SAQS baseline stats.

## Features

- **Dashboard** — Real-time alert banner, station table, stats cards
- **Live Map** — Interactive Leaflet map with station markers and alert colours
- **Research** — Full research content including methodology, validation, and confusion matrix
- **Demo Mode** — Simulated wildfire scenarios for each city
- **Auto-fetch** — Live data fetched automatically on load and every 15 minutes
- **Feedback** — Contact form for user feedback

## Data Sources

- **NAPS** — National Air Pollution Surveillance Program (Environment Canada)
- **U.S. EPA AQS / AirNow** — Border station data
- **WAQI** — World Air Quality Index API for live PM2.5
