"""
Health check endpoint for monitoring and load balancers.
"""

import logging
import os

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .utils import timing_safe_token_compare

logger = logging.getLogger(__name__)


def _verbose_authorized(request):
    """Return True when the caller proves they own the health-check secret.

    If ``HEALTHCHECK_TOKEN`` is not set, verbose details are never returned —
    we fail closed rather than leak driver paths/exception strings to anyone
    who can reach the URL.
    """
    secret = os.environ.get("HEALTHCHECK_TOKEN", "")
    if not secret:
        return False
    provided = request.headers.get("X-Healthcheck-Token", "")
    return timing_safe_token_compare(provided, secret)


@require_http_methods(["GET"])
def health_check(request):
    """Health check endpoint for monitoring and load balancers.

    Returns ``{"status": "healthy"|"degraded"|"unhealthy"}`` to all callers so
    uptime monitors keep working. Detailed per-component diagnostics are only
    returned to callers presenting the ``X-Healthcheck-Token`` header.
    """
    verbose = _verbose_authorized(request)
    status = {
        "status": "healthy",
        "timestamp": timezone.now().isoformat(),
    }
    checks = {}

    # Database connectivity
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["database"] = "ok"
    except Exception as exc:
        logger.exception("health_check: database failure")
        checks["database"] = "error" if not verbose else f"error: {exc}"
        status["status"] = "unhealthy"

    # Cache connectivity
    try:
        cache.set("health_check", "ok", 10)
        if cache.get("health_check") == "ok":
            checks["cache"] = "ok"
        else:
            checks["cache"] = "error"
            if status["status"] == "healthy":
                status["status"] = "degraded"
    except Exception as exc:
        logger.exception("health_check: cache failure")
        checks["cache"] = "error" if not verbose else f"error: {exc}"
        if status["status"] == "healthy":
            status["status"] = "degraded"

    if verbose:
        status["checks"] = checks

    http_status = 200 if status["status"] == "healthy" else 503
    return JsonResponse(status, status=http_status)
