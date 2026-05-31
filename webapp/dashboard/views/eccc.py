"""ECCC MSC Datamart model ingestion + read endpoints.

ISOLATION GUARANTEE: these endpoints only STORE model-sampled PM2.5 (RAQDPS 72h
forecast + RDAQA hourly analysis) and read it back. They do NOT feed the 3-rule
detection in evaluate.py, do NOT alter CachedResult(key="latest"), and have zero
effect on the live alert engine. Fusing the model signal into detection is a
deferred methodology decision (see CLAUDE.md / the eccc-purpleair-fusion memory).

Heavy GRIB2 reading happens off-platform in scripts/eccc_ingest.py (a GitHub
Actions runner), which POSTs a compact station-sampled JSON here -- mirroring
how /api/refresh/ receives WAQI-derived data, keeping grib libs off Vercel.
"""

import json
import logging
import os

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..models import CachedResult
from .utils import timing_safe_token_compare

logger = logging.getLogger(__name__)

# Distinct CachedResult keys so model data never collides with key="latest".
_KIND_TO_KEY = {"forecast": "eccc_forecast", "analysis": "eccc_analysis"}


@csrf_exempt
@require_http_methods(["POST"])
def api_refresh_eccc(request):
    """Cron/runner endpoint: store an ECCC model sample. CRON_SECRET-gated.

    CSRF-exempt is intentional and consistent with the project's other
    secret-gated external endpoints (the caller is the CI runner, not a browser
    session). Auth is a constant-time bearer-token compare.
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization", "")
    expected = f"Bearer {cron_secret}" if cron_secret else ""
    if not cron_secret or not timing_safe_token_compare(auth_header, expected):
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON body"}, status=400)

    kind = payload.get("kind")
    key = _KIND_TO_KEY.get(kind)
    if not key:
        return JsonResponse(
            {"error": "Missing or invalid 'kind' (expected 'forecast' or 'analysis')"},
            status=400,
        )

    points = payload.get("points")
    if not isinstance(points, dict):
        return JsonResponse({"error": "Missing 'points' object"}, status=400)

    CachedResult.objects.update_or_create(
        key=key,
        defaults={"readings": payload, "results": [], "city_alerts": {}},
    )
    logger.info("ECCC ingest stored: kind=%s run=%s points=%d",
                kind, payload.get("run"), len(points))
    return JsonResponse({"ok": True, "kind": kind, "run": payload.get("run"),
                         "points_stored": len(points)})


@require_http_methods(["GET"])
def api_eccc(request):
    """Public read of the latest stored ECCC sample (?kind=forecast|analysis).

    Read-only convenience for eyeballing the ingested model data; does not affect
    any alert surface.
    """
    kind = request.GET.get("kind", "forecast")
    key = _KIND_TO_KEY.get(kind)
    if not key:
        return JsonResponse({"error": "Invalid 'kind' (expected 'forecast' or 'analysis')"}, status=400)

    try:
        cached = CachedResult.objects.get(key=key)
    except CachedResult.DoesNotExist:
        return JsonResponse({"error": f"No {kind} data ingested yet", "kind": kind}, status=404)

    return JsonResponse({
        "kind": kind,
        "timestamp": cached.timestamp.isoformat(),
        "age_seconds": int((timezone.now() - cached.timestamp).total_seconds()),
        "data": cached.readings,
    })
