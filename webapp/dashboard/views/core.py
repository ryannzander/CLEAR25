"""
Core views: index, stations, demo, live data, refresh, authentication.
"""

import datetime
import logging
import os

from django.contrib import auth
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import cache_page
from django.views.decorators.http import require_http_methods

from .. import services
from ..models import ReadingSnapshot, CachedResult
from .utils import safe_redirect

logger = logging.getLogger(__name__)


def index(request):
    """Render the main dashboard page."""
    cities = list(services.CITIES.keys())
    current_plan = "free"
    plan_expires = None
    if request.user.is_authenticated:
        try:
            profile = request.user.profile
            current_plan = profile.active_plan
            plan_expires = profile.plan_expires
        except Exception:
            pass
    return render(request, "dashboard/index.html", {
        "cities": cities,
        "current_plan": current_plan,
        "plan_expires": plan_expires,
    })


@cache_page(60 * 5)  # Cache for 5 minutes
@require_http_methods(["GET"])
def api_stations(request, city=None):
    """Get station data. Cached for 5 minutes."""
    if city and city not in services.CITIES:
        return JsonResponse({"error": "Invalid city"}, status=400)

    if city:
        stations = services.load_stations(city)
    else:
        stations = services.load_all_stations()

    return JsonResponse({
        "stations": stations,
        "cities": services.CITIES,
    })


@cache_page(60 * 5)  # Cache for 5 minutes
@require_http_methods(["GET"])
def api_demo(request, city=None):
    """Get demo data. Cached for 5 minutes."""
    if city and city not in services.CITIES:
        return JsonResponse({"error": "Invalid city"}, status=400)

    if city:
        stations = [dict(s, target_city=city) for s in services.load_stations(city)]
        prev = services.build_demo_previous_readings(stations)
        result = services.evaluate(
            stations,
            {},
            previous_readings=prev,
            per_city_readings=services.DEMO_DATA,
            default_pm=services.DEMO_DEFAULT_PM25,
        )
    else:
        stations = services.load_all_stations()
        prev = services.build_demo_previous_readings(stations)
        result = services.evaluate(
            stations,
            {},
            previous_readings=prev,
            per_city_readings=services.DEMO_DATA,
            default_pm=services.DEMO_DEFAULT_PM25,
        )

    return JsonResponse({"results": result["stations"], "city_alerts": result["city_alerts"]})


@require_http_methods(["GET"])
def api_live(request):
    """Return the latest cached results from the server-side refresh.

    Public endpoint with short-term caching to reduce DB hits.
    """
    cache_key = "api_live_response"
    cached_response = cache.get(cache_key)

    if cached_response and cached_response.get("results"):
        return JsonResponse(cached_response)

    try:
        cached = CachedResult.objects.get(key="latest")
        age_seconds = (timezone.now() - cached.timestamp).total_seconds()
        # Filter out any excluded stations from cached results
        results = [r for r in (cached.results or []) if r.get("id") not in services.EXCLUDED_STATION_IDS]
        response_data = {
            "results": results,
            "city_alerts": cached.city_alerts or {},
            "timestamp": cached.timestamp.isoformat(),
            "age_seconds": int(age_seconds),
            "data_source": "live",
        }
        if not results:
            response_data = _live_demo_preview_payload()
        else:
            cache.set(cache_key, response_data, 30)
        return JsonResponse(response_data)
    except CachedResult.DoesNotExist:
        payload = _live_demo_preview_payload()
        return JsonResponse(payload)


def _live_demo_preview_payload():
    """When DB cache is missing or empty, show evaluated demo readings so the dashboard is usable."""
    stations = services.load_all_stations()
    prev = services.build_demo_previous_readings(stations)
    result = services.evaluate(
        stations,
        {},
        previous_readings=prev,
        per_city_readings=services.DEMO_DATA,
        default_pm=services.DEMO_DEFAULT_PM25,
    )
    return {
        "results": result["stations"],
        "city_alerts": result["city_alerts"],
        "timestamp": None,
        "age_seconds": 0,
        "data_source": "demo_preview",
    }


def api_refresh(request):
    """Cron endpoint: fetch WAQI data, evaluate, store in DB.

    Protected by CRON_SECRET environment variable.
    """
    cron_secret = os.environ.get("CRON_SECRET", "")
    auth_header = request.headers.get("Authorization", "")
    if not cron_secret or auth_header != f"Bearer {cron_secret}":
        return JsonResponse({"error": "Unauthorized"}, status=401)

    config = services.load_config()
    api_key = config.get("api_key", "")
    if not api_key:
        return JsonResponse({"error": "No WAQI API token configured"}, status=400)

    try:
        stations = services.load_all_stations()
        readings = services.fetch_latest_pm25(api_key, stations)

        # Load previous readings for Rule 2 (dual-station sustained check)
        now = timezone.now()
        previous_readings = {}
        for city_key in services.CITIES:
            try:
                snap = ReadingSnapshot.objects.get(city=city_key)
                age = now - snap.timestamp
                if datetime.timedelta(minutes=20) <= age <= datetime.timedelta(hours=3):
                    previous_readings.update(snap.readings)
            except ReadingSnapshot.DoesNotExist:
                pass

        result = services.evaluate(stations, readings, previous_readings=previous_readings)

        # Save current readings as snapshots for next refresh
        city_readings = {}
        for st in stations:
            pm = services.reading_for_station(st, readings)
            if pm is not None:
                tc = st.get("target_city", "")
                city_readings.setdefault(tc, {})[st["id"]] = pm
        for city_key, cr in city_readings.items():
            ReadingSnapshot.objects.update_or_create(city=city_key, defaults={"readings": cr})

        # Store evaluated results in CachedResult
        CachedResult.objects.update_or_create(
            key="latest",
            defaults={
                "results": result["stations"],
                "city_alerts": result["city_alerts"],
                "readings": readings,
            },
        )

        return JsonResponse({
            "ok": True,
            "stations_fetched": len(readings),
            "stations_evaluated": len(result["stations"]),
        })
    except Exception:
        logger.exception("api_refresh: unexpected error during data refresh")
        return JsonResponse({"error": "Data refresh failed. Check server logs."}, status=500)


def api_auth_status(request):
    """Return current authentication status."""
    if request.user.is_authenticated:
        return JsonResponse({
            "authenticated": True,
            "username": request.user.get_full_name() or request.user.email or request.user.username,
        })
    return JsonResponse({"authenticated": False})


def logout_view(request):
    """Log out the current user and redirect to home."""
    auth.logout(request)
    return safe_redirect("/")
