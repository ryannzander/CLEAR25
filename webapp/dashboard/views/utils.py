"""
Shared utilities for views: validation, sanitization, profanity filter.
"""

import hmac
import json
import re
import urllib.parse

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect
from django.utils.http import url_has_allowed_host_and_scheme


def timing_safe_token_compare(a, b):
    """Constant-time comparison for shared secrets / bearer tokens.

    Returns False on type mismatch or empty inputs.
    """
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# =============================================================================
# CONSTANTS
# =============================================================================

MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 5000
MAX_COMMENT_LENGTH = 2000
MAX_NAME_LENGTH = 50

VALID_NAME_PATTERN = re.compile(r'^[\w\s\-\'.]+$', re.UNICODE)

# Profanity filter word list
PROFANITY_LIST = [
    "fuck", "shit", "ass", "bitch", "damn", "crap", "dick", "cock", "pussy",
    "asshole", "bastard", "cunt", "fag", "faggot", "nigger", "nigga", "retard",
    "whore", "slut", "piss", "bollocks", "wanker", "twat", "prick", "douche",
]


# =============================================================================
# VALIDATION UTILITIES
# =============================================================================

def sanitize_text(text, max_length=None):
    """Sanitize user input text.

    - Strips whitespace
    - Removes null bytes and control characters
    - Truncates to max_length if specified
    """
    if not text:
        return ""

    text = str(text).strip()
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

    if max_length:
        text = text[:max_length]

    return text


def validate_json_body(request, required_fields=None):
    """Parse and validate JSON request body.

    Returns (data, None) on success, (None, error_response) on failure.
    """
    try:
        if not request.body:
            return None, JsonResponse({"error": "Request body is empty"}, status=400)

        data = json.loads(request.body)

        if not isinstance(data, dict):
            return None, JsonResponse({"error": "Request body must be a JSON object"}, status=400)

        if required_fields:
            missing = [f for f in required_fields if f not in data]
            if missing:
                return None, JsonResponse(
                    {"error": f"Missing required fields: {', '.join(missing)}"},
                    status=400
                )

        return data, None

    except json.JSONDecodeError as e:
        return None, JsonResponse({"error": f"Invalid JSON: {str(e)}"}, status=400)


def validate_id(value, name="id"):
    """Validate that a value is a positive integer ID."""
    try:
        id_val = int(value)
        if id_val <= 0:
            raise ValueError()
        return id_val, None
    except (ValueError, TypeError):
        return None, JsonResponse({"error": f"Invalid {name}"}, status=400)


def contains_profanity(text):
    """Check if text contains profanity. Returns the matched word or None."""
    text_lower = text.lower()
    text_clean = text_lower.replace("@", "a").replace("$", "s").replace("0", "o").replace("1", "i").replace("3", "e")

    for word in PROFANITY_LIST:
        pattern = r'\b' + re.escape(word) + r'\b'
        if re.search(pattern, text_clean):
            return word
        if word in text_clean:
            return word
    return None


def get_avatar_url(user):
    """Generate avatar URL using UI Avatars API."""
    name = user.get_full_name() or user.username or user.email.split("@")[0]
    encoded_name = urllib.parse.quote(name)
    return f"https://ui-avatars.com/api/?name={encoded_name}&background=3b82f6&color=fff&size=128&bold=true"


def serialize_author(user):
    """Return author name and avatar dict for use in JSON responses."""
    return {
        "author": user.get_full_name() or user.username,
        "author_avatar": get_avatar_url(user),
    }


# Paths that are explicitly allowed as redirect destinations
_REDIRECT_ALLOWLIST = {
    "/",
    "/accounts/google/login/",
    "/accounts/login/",
    "/accounts/logout/",
    "/settings/",
    "/billing/",
}


def safe_redirect(url, fallback="/"):
    """Redirect to `url` only if it is safe; otherwise redirect to `fallback`.

    A URL is considered safe when it:
    - Is an internal relative path (no host/scheme), AND
    - Is present in the explicit allowlist.

    This prevents open-redirect vulnerabilities where user-supplied input
    (e.g. ?next=https://evil.com) could be used to send users off-site.
    """
    allowed_hosts = set(settings.ALLOWED_HOSTS) - {"*"}
    is_safe = url_has_allowed_host_and_scheme(
        url=url,
        allowed_hosts=allowed_hosts,
        require_https=not settings.DEBUG,
    )
    if is_safe and url in _REDIRECT_ALLOWLIST:
        return redirect(url)
    return redirect(fallback)
