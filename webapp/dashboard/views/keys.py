"""
API key management: list, create, revoke.
"""

import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from ..models import APIKey, RefreshToken

logger = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def api_create_key(request):
    """List API keys (GET) or create a new one (POST)."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)

    # GET: List user's API keys with rate limit info
    if request.method == "GET":
        profile = request.user.profile
        api_keys = request.user.api_keys.filter(is_active=True)
        rate_limit = profile.rate_limit
        keys = []
        for ak in api_keys:
            now = timezone.now()
            has_active_window = ak.hour_started and (now - ak.hour_started).total_seconds() < 3600
            if has_active_window:
                requests_used = ak.requests_this_hour
                reset_seconds = int(3600 - (now - ak.hour_started).total_seconds())
            else:
                requests_used = 0
                reset_seconds = 0
            remaining = max(0, rate_limit - requests_used)

            keys.append({
                "key": ak.key,
                "name": ak.name,
                "created_at": ak.created_at.isoformat() if ak.created_at else None,
                "last_used": ak.last_used.isoformat() if ak.last_used else None,
                "rate_limit": rate_limit,
                "requests_used": requests_used,
                "requests_remaining": remaining,
                "reset_seconds": reset_seconds,
                "has_active_window": has_active_window,
                "total_requests": ak.total_requests,
            })
        return JsonResponse({
            "keys": keys,
            "plan": profile.active_plan,
            "max_keys": profile.max_api_keys,
        })

    # POST: Create new key
    max_keys = request.user.profile.max_api_keys
    if request.user.api_keys.filter(is_active=True).count() >= max_keys:
        return JsonResponse({"error": f"Maximum {max_keys} API key{'s' if max_keys > 1 else ''} allowed on your plan"}, status=400)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        data = {}

    name = data.get("name", "")[:100]
    api_key = APIKey.objects.create(user=request.user, name=name)

    return JsonResponse({
        "key": api_key.key,
        "name": api_key.name,
        "created_at": api_key.created_at.isoformat(),
    })


@require_http_methods(["POST"])
def api_revoke_key(request):
    """Revoke an API key."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required"}, status=401)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    key = data.get("key", "")
    if not isinstance(key, str) or not key.strip():
        return JsonResponse({"error": "key is required"}, status=400)
    try:
        api_key = APIKey.objects.get(key=key, user=request.user)
    except APIKey.DoesNotExist:
        return JsonResponse({"error": "API key not found"}, status=404)

    with transaction.atomic():
        api_key.is_active = False
        api_key.save(update_fields=["is_active"])
        # Revoke every refresh token minted from this key so a stolen
        # refresh token cannot survive its parent key.
        RefreshToken.objects.filter(api_key=api_key, revoked=False).update(revoked=True)
    return JsonResponse({"ok": True})
