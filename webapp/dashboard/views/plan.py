"""Plan / Ontario smoke-plume tracker: ingest, read, and page views.

ISOLATION GUARANTEE: these endpoints only store and read PlumeFrame rows (a
rolling ~6-hour buffer of cleaned PurpleAir PM2.5 snapshots). They do NOT feed
the CLEAR 3-rule detection in evaluate.py, do NOT touch CachedResult(key="latest"
| "eccc_*"), and have zero effect on the live alert engine.

Cadence/architecture: PurpleAir is plain JSON (no GRIB), so the heavy lifting
runs here in the Vercel lambda and is triggered every ~5-15 minutes by an
external cron hitting /api/plan/refresh/ with the CRON_SECRET bearer -- mirroring
/api/refresh/. Each fire writes one frame and prunes frames older than the
buffer window, so the table stays bounded.
"""

import datetime
import logging
import os

import requests

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.shortcuts import render

from ..models import PlumeFrame, CachedResult
from ..services import purpleair, fusion
from ..services.data import CITIES
from .utils import timing_safe_token_compare

logger = logging.getLogger(__name__)

# Rolling buffer length. 6 hours at a 5-minute cadence ~= 72 frames.
PLUME_BUFFER_HOURS = int(getattr(settings, "PLUME_BUFFER_HOURS", 6))

_VALID_SOURCES = {s for s, _ in PlumeFrame.SOURCE_CHOICES}


@csrf_exempt
@require_http_methods(["GET", "POST"])
def api_plan_refresh(request):
    """Cron/runner endpoint: fetch one PurpleAir snapshot, clean, store, prune.

    Accepts GET or POST: GET is what most free cron services (cron-job.org,
    UptimeRobot) send by default, matching the project's other CRON_SECRET-gated
    cron endpoint (/api/refresh/). CSRF-exempt is intentional (the caller is a
    cron, not a browser). The action is idempotent, so a GET trigger is safe.

    Every failure returns a precise JSON error (with the exception class) instead
    of an opaque 5xx, so a misconfigured deploy is diagnosable from the cron log.
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {cron_secret}" if cron_secret else ""
    if not cron_secret or not timing_safe_token_compare(auth_header, expected):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    source = "purpleair"

    # 1. Fetch + standardize one PurpleAir snapshot.
    try:
        frame = purpleair.fetch_purpleair_frame()
    except RuntimeError as exc:
        # Configuration / response-shape problem (e.g. PURPLEAIR_API_KEY unset).
        logger.warning("api_plan_refresh: PurpleAir config/shape error: %s", exc)
        return JsonResponse({"error": "config", "detail": str(exc)}, status=503)
    except requests.RequestException as exc:
        # Surface the upstream HTTP status when present (e.g. 403 = bad/unset key,
        # 429 = rate limited) so the cron log pinpoints the cause.
        pa_status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.warning("api_plan_refresh: PurpleAir transport error: %s (status=%s)", exc, pa_status)
        return JsonResponse(
            {"error": "purpleair_fetch_failed", "detail": exc.__class__.__name__,
             "purpleair_status": pa_status},
            status=502,
        )
    except Exception as exc:  # noqa: BLE001 - report, don't leak a stack to the caller
        logger.exception("api_plan_refresh: unexpected fetch error")
        return JsonResponse(
            {"error": "fetch_error", "detail": exc.__class__.__name__}, status=500
        )

    # 2. Store the frame + prune the rolling buffer. Wrapped so a DB problem
    #    (e.g. an unmigrated table) returns a clear error instead of a raw 500/502.
    try:
        captured = datetime.datetime.fromtimestamp(
            frame["captured_at"], tz=datetime.timezone.utc
        )
        _, created = PlumeFrame.objects.update_or_create(
            source=source,
            captured_at=captured,
            defaults={
                "sensor_count": frame["stats"]["kept"],
                "payload": {
                    "points": frame["points"],
                    "stats": frame["stats"],
                    "bbox": purpleair.ONTARIO_BBOX,
                },
            },
        )
        cutoff = timezone.now() - datetime.timedelta(hours=PLUME_BUFFER_HOURS)
        pruned, _ = PlumeFrame.objects.filter(
            source=source, captured_at__lt=cutoff
        ).delete()
    except Exception as exc:  # noqa: BLE001
        logger.exception("api_plan_refresh: storage failed")
        return JsonResponse(
            {"error": "storage_failed", "detail": exc.__class__.__name__,
             "hint": "If this is ProgrammingError/OperationalError, the "
                     "dashboard_plumeframe table is missing — run migrations."},
            status=500,
        )

    return JsonResponse({
        "ok": True,
        "source": source,
        "captured_at": captured.isoformat(),
        "created": created,
        "kept": frame["stats"]["kept"],
        "received": frame["stats"]["received"],
        "pruned": pruned,
    })


@cache_page(20)
@require_http_methods(["GET"])
def api_plan_frames(request):
    """Public read: the rolling buffer of cleaned frames for the animation.

    Query params:
      source : "purpleair" (default) | "eccc_rdaqa"
      full   : "1" to include raw/rh/conf per point; default returns slim
               {lat,lon,pm} points to keep the payload small.
    Frames are returned oldest -> newest so the client can play them in order.
    """
    source = request.GET.get("source", "purpleair")
    if source not in _VALID_SOURCES:
        return JsonResponse({"error": "Invalid source"}, status=400)
    full = request.GET.get("full") == "1"

    cutoff = timezone.now() - datetime.timedelta(hours=PLUME_BUFFER_HOURS)
    qs = PlumeFrame.objects.filter(
        source=source, captured_at__gte=cutoff
    ).order_by("captured_at")

    frames = []
    for fr in qs:
        pts = (fr.payload or {}).get("points", [])
        if full:
            out_pts = pts
        else:
            out_pts = [{"lat": p["lat"], "lon": p["lon"], "pm": p["pm"]} for p in pts]
        frames.append({
            "captured_at": fr.captured_at.isoformat(),
            "sensor_count": fr.sensor_count,
            "points": out_pts,
        })

    return JsonResponse({
        "source": source,
        "buffer_hours": PLUME_BUFFER_HOURS,
        "frame_count": len(frames),
        "bbox": purpleair.ONTARIO_BBOX,
        "frames": frames,
    })


@ensure_csrf_cookie
def plan_page(request):
    """Render the Ontario smoke-plume tracker (animated GIS view)."""
    return render(request, "dashboard/plan.html", {})


@require_http_methods(["GET"])
def api_plan_fusion(request):
    """Read-only fusion readout per city (PurpleAir confirmation + ECCC warning).

    For each CLEAR city, reports three things side by side:
      - the validated CLEAR alert,
      - the live PurpleAir observation near the city and whether it confirms,
      - the ECCC RAQDPS far-field early warning (does the model predict smoke
        reaching the city, and when — the >600 km extension).

    ISOLATED: reads the latest PlumeFrame + CachedResult(latest|eccc_forecast) and
    computes a comparison. It does NOT alter evaluate.py or any alert decision.
    Cities outside PurpleAir coverage (only Ontario is scraped) report
    "no_purpleair"; ECCC warning covers all cities once the forecast is ingested.
    """
    latest = (PlumeFrame.objects
              .filter(source="purpleair")
              .order_by("-captured_at")
              .first())
    points = (latest.payload or {}).get("points", []) if latest else []

    try:
        city_alerts = CachedResult.objects.get(key="latest").city_alerts or {}
    except CachedResult.DoesNotExist:
        city_alerts = {}
    try:
        eccc_forecast = CachedResult.objects.get(key="eccc_forecast").readings or {}
    except CachedResult.DoesNotExist:
        eccc_forecast = {}

    cities = []
    for city_key in CITIES:
        row = fusion.confirmation_for_city(city_key, city_alerts.get(city_key), points)
        row["early_warning"] = fusion.early_warning_for_city(city_key, eccc_forecast)
        cities.append(row)

    return JsonResponse({
        "as_of": latest.captured_at.isoformat() if latest else None,
        "radius_km": fusion.CONFIRM_RADIUS_KM,
        "cities": cities,
    })
