"""
Public API v1 data endpoints for CLEAR25.
"""

import logging
from functools import wraps

import jwt as _jwt

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .. import services
from ..jwt_auth import decode_access_token
from ..models import APIKey, CachedResult
from .core import _live_demo_preview_payload

logger = logging.getLogger(__name__)


# Level name to integer mapping (matches Toronto PM2.5 Methodology v3.0)
LEVEL_MAP = {
    "LOW": 1,
    "MODERATE": 2,
    "HIGH": 3,
    "VERY HIGH": 4,
    "EXTREME": 5,
}


def require_api_key(view_func):
    """Decorator: accept either a JWT access token or a raw API key.

    Priority:
    1. Try to decode the bearer value as a JWT access token.
       If valid, look up the embedded ``key_id`` for rate-limiting.
    2. Fall back to treating it as a plain API key (backward-compat).
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JsonResponse({
                "error": "Missing or invalid Authorization header",
                "hint": "Use 'Authorization: Bearer YOUR_API_KEY_OR_JWT'",
            }, status=401)

        token = auth_header[7:]
        api_key = None

        # ── 1. Try JWT access token ──────────────────────────────────────────
        try:
            payload = decode_access_token(token)
            try:
                api_key = APIKey.objects.get(id=payload["key_id"], is_active=True)
            except APIKey.DoesNotExist:
                return JsonResponse({"error": "API key associated with this token has been revoked"}, status=401)
        except _jwt.ExpiredSignatureError:
            return JsonResponse({"error": "Access token has expired", "hint": "Use /api/v1/auth/refresh/ to get a new one"}, status=401)
        except _jwt.InvalidTokenError:
            # Not a JWT — fall through to raw API key check
            pass

        # ── 2. Fall back to raw API key ──────────────────────────────────────
        if api_key is None:
            try:
                api_key = APIKey.objects.get(key=token, is_active=True)
            except APIKey.DoesNotExist:
                return JsonResponse({"error": "Invalid API key or token"}, status=401)

        # ── Rate limit ───────────────────────────────────────────────────────
        allowed, remaining, reset = api_key.check_rate_limit()
        if not allowed:
            rate_limit = api_key.get_rate_limit()
            response = JsonResponse({"error": "Rate limit exceeded", "retry_after": reset}, status=429)
            response["X-RateLimit-Limit"] = str(rate_limit)
            response["X-RateLimit-Remaining"] = "0"
            response["X-RateLimit-Reset"] = str(reset)
            return response

        request.api_key = api_key
        response = view_func(request, *args, **kwargs)

        rate_limit = api_key.get_rate_limit()
        response["X-RateLimit-Limit"] = str(rate_limit)
        response["X-RateLimit-Remaining"] = str(remaining)
        response["X-RateLimit-Reset"] = str(reset)
        return response
    return wrapper


def _format_station_for_api(station_result):
    """Format a station result for API response with integer level."""
    level_name = station_result.get("level_name", "NONE")
    return {
        "id": station_result.get("id"),
        "name": station_result.get("station"),
        "city": station_result.get("target_city"),
        "lat": station_result.get("lat"),
        "lon": station_result.get("lon"),
        "pm25": round(station_result.get("pm25", 0), 1),
        "predicted": round(station_result.get("predicted", 0), 1),
        "level": LEVEL_MAP.get(level_name, 0),
        "level_name": level_name,
        "health_advisory": station_result.get("health", ""),
    }


@require_http_methods(["GET"])
@require_api_key
def api_v1_live(request):
    """Get current PM2.5 readings and predictions for stations."""
    try:
        cached = CachedResult.objects.get(key="latest")
        results = cached.results or []
        timestamp = cached.timestamp.isoformat()
        age_seconds = int((timezone.now() - cached.timestamp).total_seconds())
        data_source = "live"
    except CachedResult.DoesNotExist:
        preview = _live_demo_preview_payload()
        results = preview["results"]
        timestamp = preview["timestamp"]
        age_seconds = preview["age_seconds"]
        data_source = preview["data_source"]
    if not results:
        preview = _live_demo_preview_payload()
        results = preview["results"]
        timestamp = preview["timestamp"]
        age_seconds = preview["age_seconds"]
        data_source = preview["data_source"]

    # Filter out excluded stations
    results = [r for r in results if r.get("id") not in services.EXCLUDED_STATION_IDS]

    # Allow filtering by station ID via ?station=<id>
    station_id = request.GET.get("station")
    if station_id:
        results = [r for r in results if r.get("id") == station_id]
        if not results:
            return JsonResponse({"error": f"Station '{station_id}' not found"}, status=404)

    try:
        limit = int(request.GET.get("limit", "50"))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 200))

    stations_out = [_format_station_for_api(r) for r in results[:limit]]

    return JsonResponse({
        "stations": stations_out,
        "count": len(stations_out),
        "total_available": len(results),
        "timestamp": timestamp,
        "age_seconds": age_seconds,
        "data_source": data_source,
    })


@require_http_methods(["GET"])
@require_api_key
def api_v1_stations(request):
    """Get list of all monitoring stations."""
    city_filter = request.GET.get("city")

    if city_filter and city_filter not in services.CITIES:
        return JsonResponse({
            "error": f"Invalid city. Valid options: {', '.join(services.CITIES.keys())}"
        }, status=400)

    if city_filter:
        stations = services.load_stations(city_filter)
    else:
        stations = services.load_all_stations()

    formatted = []
    for st in stations:
        formatted.append({
            "id": st.get("id"),
            "name": st.get("station"),
            "city": st.get("target_city"),
            "lat": st.get("lat"),
            "lon": st.get("lon"),
            "tier": st.get("tier"),
        })

    return JsonResponse({
        "stations": formatted,
        "count": len(formatted),
        "cities": list(services.CITIES.keys()),
    })


@require_http_methods(["GET"])
@require_api_key
def api_v1_cities(request):
    """Get list of supported cities."""
    cities = []
    for key, data in services.CITIES.items():
        cities.append({
            "id": key,
            "name": data["label"],
            "lat": data["lat"],
            "lon": data["lon"],
        })

    return JsonResponse({
        "cities": cities,
        "count": len(cities),
    })


def api_docs(request):
    """Render the API documentation page."""
    api_keys = []
    if request.user.is_authenticated:
        api_keys = list(request.user.api_keys.filter(is_active=True).values(
            "key", "name", "created_at", "last_used"
        ))

    return render(request, "dashboard/api_docs.html", {
        "api_keys": api_keys,
        "cities": list(services.CITIES.keys()),
    })
