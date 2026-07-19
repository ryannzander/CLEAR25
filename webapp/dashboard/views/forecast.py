"""Live Toronto smoke-forecast endpoints (read-only, isolated).

Scores the frozen wind-aware LSTM (services/smoke_forecast.py — pure numpy, no torch)
from the live CLEAR-network field + the live ECCC wind, and serves the result.

ISOLATION GUARANTEE: this only READS CachedResult(key="latest") and the stored ECCC wind,
and WRITES only its own keys (smoke_forecast, smoke_fc_buffer). It never feeds evaluate.py,
never touches CachedResult(key="latest"), and has zero effect on the 3-rule alert engine.
It is a FORECAST surface, explicitly NOT an alert.

Runs on Vercel (numpy only). Triggered by an external hourly cron hitting
POST /api/refresh/forecast/ with the CRON_SECRET bearer; read at GET /api/forecast/.
"""

import logging
import os
from datetime import timezone as dt_timezone

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..models import CachedResult
from .utils import timing_safe_token_compare

logger = logging.getLogger(__name__)

FORECAST_KEY = "smoke_forecast"      # <= 20 chars (CachedResult.key max_length)
BUFFER_KEY = "smoke_fc_buffer"       # rolling window of raw feature rows


def _scorer():
    """Import the numpy-free scorer LAZILY. Isolation guarantee: if the service (or the
    model asset) is broken, only the forecast endpoints degrade to 503 — the import can
    never run at app-boot time and take the whole site down."""
    try:
        from ..services import smoke_forecast as sf
        return sf
    except Exception:               # pragma: no cover - defensive
        logger.exception("smoke_forecast import failed")
        return None


def _toronto_wind_now():
    """Current 10 m Toronto wind (speed, dir) from the stored ECCC wind analysis.

    Returns (speed, dir, run) — (None, None, None) if no wind ingested yet. Missing wind
    is fine: build_feature_row emits NaN wind features, imputed to the train mean."""
    try:
        c = CachedResult.objects.get(key="eccc_wind_analysis")
    except CachedResult.DoesNotExist:
        return None, None, None
    data = c.readings or {}
    pt = (data.get("points") or {}).get("CITY:Toronto") or {}
    return pt.get("wind_speed"), pt.get("wind_dir"), data.get("run")


@csrf_exempt
@require_http_methods(["POST"])
def api_refresh_forecast(request):
    """Cron endpoint: append the current hour's feature row to the rolling buffer and
    (re)score the forecast. CRON_SECRET-gated; CSRF-exempt (caller is an external cron,
    consistent with the other secret-gated ingest endpoints)."""
    cron_secret = os.environ.get("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {cron_secret}" if cron_secret else ""
    if not cron_secret or not timing_safe_token_compare(auth_header, expected):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    sf = _scorer()
    if sf is None:
        return JsonResponse({"error": "Forecast scorer unavailable"}, status=503)
    model = sf.load_model()
    if not model:
        return JsonResponse({"error": "No model asset deployed"}, status=503)

    # Live surrounding field — READ-ONLY.
    try:
        latest = CachedResult.objects.get(key="latest")
        results = latest.results or []
    except CachedResult.DoesNotExist:
        results = []
    if not results:
        return JsonResponse({"error": "No live field yet (key=latest empty)"}, status=503)

    wind_speed, wind_dir, wind_run = _toronto_wind_now()
    now = timezone.now().astimezone(dt_timezone.utc)
    hour_key = now.strftime("%Y-%m-%dT%H")

    row = sf.build_feature_row(results, wind_speed, wind_dir, now, model)

    # Rolling buffer: dedup by UTC hour, keep the last `window` rows.
    window = int(model.get("window", 24))
    buf_obj, _ = CachedResult.objects.get_or_create(
        key=BUFFER_KEY, defaults={"readings": {"rows": [], "hours": []}})
    b = buf_obj.readings or {}
    rows = list(b.get("rows", []))
    hours = list(b.get("hours", []))
    if hours and hours[-1] == hour_key:
        rows[-1], hours[-1] = row, hour_key      # same hour → replace
    else:
        rows.append(row)
        hours.append(hour_key)
    rows, hours = rows[-window:], hours[-window:]
    buf_obj.readings = {"rows": rows, "hours": hours}
    buf_obj.results, buf_obj.city_alerts = [], {}
    buf_obj.save()

    # Score once the window is full; otherwise report warm-up progress.
    if len(rows) < window:
        payload = {"warming_up": True, "have": len(rows), "need": window,
                   "updated": now.isoformat()}
    else:
        horizons = sf.score(rows, now, model)
        payload = {
            "warming_up": False,
            "horizons": {k: round(v, 4) for k, v in horizons.items()},
            "elevated_threshold": model.get("elevated", 35.0),
            "wind_run": wind_run,
            "updated": now.isoformat(),
            "note": "read-only forecast, not an alert",
        }
    CachedResult.objects.update_or_create(
        key=FORECAST_KEY, defaults={"readings": payload, "results": [], "city_alerts": {}})
    logger.info("smoke forecast updated: warming_up=%s rows=%d/%d",
                payload["warming_up"], len(rows), window)
    return JsonResponse({"ok": True, "warming_up": payload["warming_up"],
                         "rows": len(rows), "need": window})


@require_http_methods(["GET"])
def api_forecast(request):
    """Public read of the latest smoke forecast (read-only, never an alert)."""
    try:
        c = CachedResult.objects.get(key=FORECAST_KEY)
    except CachedResult.DoesNotExist:
        return JsonResponse({"warming_up": True, "have": 0, "need": 24,
                             "note": "forecast not initialized yet"})
    data = dict(c.readings or {})
    data["age_seconds"] = int((timezone.now() - c.timestamp).total_seconds())
    return JsonResponse(data)
