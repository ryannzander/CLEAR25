# AGENTS.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

**C.L.E.A.R. (Canadian Lead-time Early Air Response)** — A PM2.5 wildfire smoke early warning system that uses air quality monitoring stations 100–600+ km away to provide 6–48 hours of advance warning before dangerous smoke arrives in Toronto, Montreal, Edmonton, and Vancouver.

Live site: https://clear25.xyz

## Development Commands

### Local Development
```bash
# Install dependencies
pip install -r webapp/requirements.txt

# Run development server
cd webapp && python manage.py runserver
# Access at http://127.0.0.1:8000/

# Run migrations
cd webapp && python manage.py migrate

# Create/update Site object (required for allauth)
cd webapp && python manage.py shell -c "from django.contrib.sites.models import Site; Site.objects.update_or_create(id=1, defaults={'domain': 'localhost:8000', 'name': 'C.L.E.A.R.'})"
```

### iOS App (Capacitor)
```bash
npm run cap:sync     # Sync web assets to native project
npm run cap:open:ios # Open in Xcode
```

### Deployment
Vercel deploys automatically from `main` branch. The build runs `build_files.sh` which installs dependencies, runs migrations, and collects static files.

## Architecture

### Backend (`webapp/`)
Django 4.2+ app deployed as Vercel serverless function.

```
webapp/
├── ews/                    # Django project settings
│   ├── settings.py         # Config, env vars, database, caching
│   └── wsgi.py            # Vercel entry point
└── dashboard/              # Main Django app
    ├── services.py         # Core prediction logic (station loading, regression, WAQI API)
    ├── models.py           # User profiles, API keys, suggestions, payments
    ├── urls.py             # URL routing
    ├── views/              # View modules split by domain:
    │   ├── core.py         # Dashboard, demo, live data APIs
    │   ├── api.py          # Public API v1 endpoints
    │   ├── account.py      # Settings, profile management
    │   ├── feedback.py     # Suggestion board
    │   └── landing.py      # Landing page, privacy
    ├── middleware.py       # Rate limiting, security headers
    └── push.py             # Push notification handling
```

### Prediction System (`services.py`)
The core logic implements a **3-rule detection system**:
- **Rule 1**: Regional station (100-600 km) > 40 µg/m³ → immediate alert
- **Rule 2**: Distant station (600+ km) > 35 µg/m³ + intermediate confirmation > 20 µg/m³
- **Rule 3**: Corridor station > 40 µg/m³

Predictions use **R-value weighted averaging** — stations with higher correlation coefficients have more influence on city-level predictions.

### Data Flow
1. Excel files in `data/` contain regression coefficients per station (slope, intercept, R-value)
2. `load_stations()` caches station data from `{City}_PM25_EWS_Regression.xlsx`
3. `fetch_latest_pm25()` queries WAQI API for live readings via bounding-box
4. `evaluate()` runs predictions through the 3-rule system
5. Results cached in `CachedResult` model (30-min TTL via Vercel cron)

### iOS App
Capacitor wrapper that loads `https://clear25.xyz` in a native WebView. Push notifications handled via `@capacitor/push-notifications`.

## Environment Variables

Required for production:
- `DATABASE_URL` — PostgreSQL connection string (Supabase)
- `SECRET_KEY` — Django secret key
- `WAQI_API_TOKEN` — World Air Quality Index API token
- `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` — OAuth for login
- `NOWPAYMENTS_API_KEY` / `NOWPAYMENTS_IPN_SECRET` — Crypto payments (optional)

## API Structure

Internal APIs (`/api/`):
- `GET /api/stations/` — All stations grouped by city
- `GET /api/live/` — Fetch live readings (rate-limited per user)
- `GET /api/demo/` — Sample ambient-style readings (bundled fallback catalog)
- `POST /api/refresh/` — Server-side cache refresh (cron)

Public API v1 (`/api/v1/`):
- `GET /api/v1/live/` — Current predictions (requires API key)
- `GET /api/v1/stations/` — Station metadata
- `GET /api/v1/cities/` — City information

## Key Constants (services.py)

```python
RULE1_TRIGGER = 40          # Regional station threshold (µg/m³)
RULE2_DISTANT_TRIGGER = 35  # Distant station threshold
RULE2_INTERMEDIATE = 20     # Intermediate confirmation threshold
ALERT_LEVELS = [LOW, MODERATE, HIGH, VERY HIGH, EXTREME]  # 0-20, 20-60, 60-80, 80-120, 120+
```
