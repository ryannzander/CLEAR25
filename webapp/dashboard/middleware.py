"""
Security middleware for CLEAR25.

- RequestSizeLimitMiddleware:  rejects oversized request bodies before view runs
- SecurityHeadersMiddleware:   adds CSP, Permissions-Policy, Referrer-Policy,
                               COOP, CORP, X-Content-Type-Options, X-XSS-Protection
- RateLimitMiddleware:         per-IP throttle for unauthenticated mutating routes

All middleware are tolerant of upstream proxies (Vercel, Cloudflare) and read the
client IP from X-Forwarded-For when SECURE_PROXY_SSL_HEADER is configured.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from django.conf import settings
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, JsonResponse

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_ip(request: HttpRequest) -> str:
    """Best-effort client IP that respects a single trusted proxy hop."""
    if getattr(settings, "SECURE_PROXY_SSL_HEADER", None):
        xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


# ── 1. Request size cap ───────────────────────────────────────────────────────

class RequestSizeLimitMiddleware:
    """Reject request bodies larger than ``MAX_REQUEST_BODY_BYTES``.

    Defaults to 1 MiB. Override via settings or env. Applies to all methods that
    carry a body; safe methods (GET/HEAD/OPTIONS) are skipped.
    """

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, get_response: Callable):
        self.get_response = get_response
        self.max_bytes = int(
            getattr(settings, "MAX_REQUEST_BODY_BYTES", 1024 * 1024)
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method not in self.SAFE_METHODS:
            try:
                length = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > self.max_bytes:
                return JsonResponse(
                    {"error": "Request entity too large", "max_bytes": self.max_bytes},
                    status=413,
                )
        return self.get_response(request)


# ── 2. Security response headers ──────────────────────────────────────────────

class SecurityHeadersMiddleware:
    """Adds production-grade response headers.

    CSP is intentionally permissive for inline styles/scripts because the
    dashboard ships inline `style=` and `onclick=` handlers. Tighten by
    refactoring those to external listeners, then drop ``'unsafe-inline'``.
    """

    DEFAULT_CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' "
        "https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
        "https://unpkg.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: "
        "https://ui-avatars.com "
        "https://*.tile.openstreetmap.org "
        "https://unpkg.com; "
        # MapLibre (OpenFreeMap basemap) fetches style/tiles/glyphs/sprites
        # with fetch() and runs its parser in a blob: web worker.
        "connect-src 'self' https://api.waqi.info https://tiles.openfreemap.org; "
        "worker-src 'self' blob:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self' https://accounts.google.com; "
        "object-src 'none'; "
        "upgrade-insecure-requests"
    )

    DEFAULT_PERMISSIONS = (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=(), interest-cohort=()"
    )

    def __init__(self, get_response: Callable):
        self.get_response = get_response
        self.csp = getattr(settings, "CONTENT_SECURITY_POLICY", self.DEFAULT_CSP)
        self.permissions = getattr(
            settings, "PERMISSIONS_POLICY", self.DEFAULT_PERMISSIONS
        )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        # Don't overwrite headers set explicitly by a view
        response.setdefault("Content-Security-Policy", self.csp)
        response.setdefault("Permissions-Policy", self.permissions)
        response.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.setdefault("Cross-Origin-Resource-Policy", "same-site")
        # Defense in depth (legacy browsers); modern browsers ignore.
        response.setdefault("X-XSS-Protection", "0")
        return response


# ── 3. Per-IP rate limit for unauthenticated mutating endpoints ───────────────

class RateLimitMiddleware:
    """Token-bucket-ish per-IP throttle backed by the cache framework.

    Applies only to POST/PUT/PATCH/DELETE to unauthenticated paths. Authenticated
    API traffic is already limited per-key in ``APIKey.check_rate_limit``.

    Defaults: 30 requests per 60 seconds per IP per path-prefix. Override with
    ``IP_RATE_LIMIT_REQUESTS`` and ``IP_RATE_LIMIT_WINDOW_SECONDS``.
    """

    THROTTLED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    # Exempt paths that have their own per-credential throttle or that must be
    # callable by external services (webhooks).
    EXEMPT_PREFIXES = (
        "/api/v1/subscribe/webhook/",   # signed HMAC webhook
        "/api/refresh/",                # gated by CRON_SECRET (covers /api/refresh/eccc/)
        "/api/plan/refresh/",           # gated by CRON_SECRET (plume ingest cron)
        "/api/v1/subscribe/test/",      # gated by CRON_SECRET (dev only)
    )

    def __init__(self, get_response: Callable):
        self.get_response = get_response
        self.limit = int(getattr(settings, "IP_RATE_LIMIT_REQUESTS", 30))
        self.window = int(getattr(settings, "IP_RATE_LIMIT_WINDOW_SECONDS", 60))

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method in self.THROTTLED_METHODS:
            path = request.path
            if not any(path.startswith(p) for p in self.EXEMPT_PREFIXES):
                ip = _client_ip(request)
                # Bucket by minute to keep keys cheap; one key per IP+window.
                bucket = int(time.time() // self.window)
                key = f"rl:ip:{ip}:{bucket}:{path[:64]}"
                try:
                    # cache.incr raises if missing; seed then increment.
                    try:
                        count = cache.incr(key)
                    except ValueError:
                        cache.set(key, 1, timeout=self.window)
                        count = 1
                    if count > self.limit:
                        retry = self.window - int(time.time() % self.window)
                        response = JsonResponse(
                            {"error": "Too many requests", "retry_after": retry},
                            status=429,
                        )
                        response["Retry-After"] = str(retry)
                        return response
                except Exception:
                    # Cache backend failure must not break the request path.
                    logger.warning("RateLimitMiddleware: cache error", exc_info=True)
        return self.get_response(request)
