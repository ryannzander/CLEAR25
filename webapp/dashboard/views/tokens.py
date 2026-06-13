"""
JWT token endpoints: exchange, refresh, revoke.

Security properties:
  - Per-IP throttle on every endpoint to make API-key brute-force expensive
    even though API keys are 256 bits of entropy.
  - Per-key throttle on token issuance to cap the rate at which a leaked key
    can mint new access tokens.
  - Refresh tokens are bound to the API key that originated them; revoking
    the key revokes its refresh tokens.
  - Tokens are returned over Bearer JSON; this endpoint stays @csrf_exempt
    because it is called server-to-server.
"""

import json
import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from ..jwt_auth import ACCESS_TOKEN_LIFETIME, create_access_token
from ..models import APIKey, RefreshToken
from .utils import timing_safe_token_compare  # noqa: F401  (re-export for clarity)


# ── Throttle helpers ─────────────────────────────────────────────────────────

# Tunables — values picked for headroom over normal usage while making
# brute-force / large-scale token harvesting expensive.
_IP_WINDOW = 60          # seconds
_IP_MAX    = 20          # requests per IP per window
_KEY_WINDOW = 60         # seconds
_KEY_MAX   = 10          # token issuance per API key per window


def _client_ip(request):
    if getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


def _throttle(bucket_key, limit, window):
    """Returns (allowed, retry_after_seconds)."""
    bucket = int(time.time() // window)
    key = f"jwt:{bucket_key}:{bucket}"
    try:
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, timeout=window)
            count = 1
        if count > limit:
            return False, window - int(time.time() % window)
        return True, 0
    except Exception:
        # Cache failures must not lock out auth.
        return True, 0


def _throttled_response(retry_after):
    response = JsonResponse(
        {"error": "Too many requests", "retry_after": retry_after}, status=429,
    )
    response["Retry-After"] = str(retry_after)
    return response


# ── Endpoints ────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
def api_v1_get_token(request):
    """Exchange an API key for a JWT access token + refresh token.

    Request body: ``{"api_key": "<your_api_key>"}``
    """
    ip = _client_ip(request)
    allowed, retry = _throttle(f"token:ip:{ip}", _IP_MAX, _IP_WINDOW)
    if not allowed:
        return _throttled_response(retry)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    raw_key = data.get("api_key", "")
    if not isinstance(raw_key, str):
        return JsonResponse({"error": "api_key must be a string"}, status=400)
    raw_key = raw_key.strip()
    if not raw_key:
        return JsonResponse({"error": "api_key is required"}, status=400)

    try:
        api_key = APIKey.objects.select_related("user").get(key=raw_key, is_active=True)
    except APIKey.DoesNotExist:
        return JsonResponse({"error": "Invalid API key"}, status=401)

    allowed, retry = _throttle(f"token:key:{api_key.id}", _KEY_MAX, _KEY_WINDOW)
    if not allowed:
        return _throttled_response(retry)

    # Mark expired refresh tokens for this key as revoked to keep the table clean.
    RefreshToken.objects.filter(
        api_key=api_key, expires_at__lt=timezone.now(),
    ).update(revoked=True)

    access_token = create_access_token(api_key.user_id, api_key.id)
    raw_refresh, _ = RefreshToken.create_for_api_key(api_key)

    return JsonResponse({
        "access_token":  access_token,
        "refresh_token": raw_refresh,
        "expires_in":    int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        "token_type":    "Bearer",
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_v1_refresh_token(request):
    """Rotate a refresh token — invalidates the old one and issues a new pair."""
    ip = _client_ip(request)
    allowed, retry = _throttle(f"refresh:ip:{ip}", _IP_MAX, _IP_WINDOW)
    if not allowed:
        return _throttled_response(retry)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    raw_refresh = data.get("refresh_token", "")
    if not isinstance(raw_refresh, str):
        return JsonResponse({"error": "refresh_token must be a string"}, status=400)
    raw_refresh = raw_refresh.strip()
    if not raw_refresh:
        return JsonResponse({"error": "refresh_token is required"}, status=400)

    rt = RefreshToken.verify(raw_refresh)
    if rt is None:
        return JsonResponse({"error": "Invalid or expired refresh token"}, status=401)

    # The refresh token must be bound to a still-active API key.
    if rt.api_key_id is None or not rt.api_key or not rt.api_key.is_active:
        return JsonResponse({"error": "Bound API key is no longer active"}, status=401)

    allowed, retry = _throttle(f"refresh:key:{rt.api_key_id}", _KEY_MAX, _KEY_WINDOW)
    if not allowed:
        return _throttled_response(retry)

    # Rotation: revoke the used token immediately.
    rt.revoked = True
    rt.save(update_fields=["revoked"])

    access_token = create_access_token(rt.user_id, rt.api_key.id)
    raw_new_refresh, _ = RefreshToken.create_for_api_key(rt.api_key)

    return JsonResponse({
        "access_token":  access_token,
        "refresh_token": raw_new_refresh,
        "expires_in":    int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        "token_type":    "Bearer",
    })


@csrf_exempt
@require_http_methods(["POST"])
def api_v1_revoke_token(request):
    """Revoke a refresh token, ending the token family."""
    ip = _client_ip(request)
    allowed, retry = _throttle(f"revoke:ip:{ip}", _IP_MAX, _IP_WINDOW)
    if not allowed:
        return _throttled_response(retry)

    try:
        data = json.loads(request.body) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    raw_refresh = data.get("refresh_token", "")
    if not isinstance(raw_refresh, str):
        return JsonResponse({"error": "refresh_token must be a string"}, status=400)
    raw_refresh = raw_refresh.strip()
    if not raw_refresh:
        return JsonResponse({"error": "refresh_token is required"}, status=400)

    rt = RefreshToken.verify(raw_refresh)
    if rt is not None:
        rt.revoked = True
        rt.save(update_fields=["revoked"])

    # Always return ok — don't leak whether the token existed.
    return JsonResponse({"ok": True})
