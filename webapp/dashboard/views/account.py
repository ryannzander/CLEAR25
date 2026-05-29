"""
Account views: settings page, profile updates, account deletion.
"""

from django.contrib import auth
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from ..models import Suggestion, SuggestionVote, Comment
from .utils import (
    MAX_NAME_LENGTH, VALID_NAME_PATTERN,
    sanitize_text, validate_json_body, contains_profanity, safe_redirect
)


@ensure_csrf_cookie
def settings_page(request):
    """Render the settings page. Requires authentication."""
    if not request.user.is_authenticated:
        return safe_redirect("/accounts/google/login/")
    current_plan = "free"
    plan_expires = None
    try:
        profile = request.user.profile
        current_plan = profile.active_plan
        plan_expires = profile.plan_expires
    except Exception:
        pass
    return render(request, "dashboard/settings.html", {
        "current_plan": current_plan,
        "plan_expires": plan_expires,
    })


@require_http_methods(["POST"])
def api_update_profile(request):
    """Update user's first and last name."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    data, error = validate_json_body(request)
    if error:
        return error

    first_name = sanitize_text(data.get("first_name", ""), MAX_NAME_LENGTH)
    last_name = sanitize_text(data.get("last_name", ""), MAX_NAME_LENGTH)

    if not first_name:
        return JsonResponse({"error": "First name is required"}, status=400)
    if len(first_name) > MAX_NAME_LENGTH:
        return JsonResponse({"error": f"First name must be at most {MAX_NAME_LENGTH} characters"}, status=400)
    if len(last_name) > MAX_NAME_LENGTH:
        return JsonResponse({"error": f"Last name must be at most {MAX_NAME_LENGTH} characters"}, status=400)

    if not VALID_NAME_PATTERN.match(first_name):
        return JsonResponse({"error": "First name contains invalid characters"}, status=400)
    if last_name and not VALID_NAME_PATTERN.match(last_name):
        return JsonResponse({"error": "Last name contains invalid characters"}, status=400)

    if contains_profanity(first_name) or contains_profanity(last_name):
        return JsonResponse({"error": "Please use appropriate names"}, status=400)

    request.user.first_name = first_name
    request.user.last_name = last_name
    request.user.save(update_fields=['first_name', 'last_name'])

    return JsonResponse({
        "ok": True,
        "first_name": request.user.first_name,
        "last_name": request.user.last_name,
        "full_name": request.user.get_full_name(),
    })


@require_http_methods(["POST"])
def api_downgrade_plan(request):
    """Downgrade user plan back to free."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    profile = request.user.profile
    if profile.active_plan == "free":
        return JsonResponse({"error": "Already on the free plan"}, status=400)

    profile.plan = "free"
    profile.plan_expires = None
    profile.save(update_fields=["plan", "plan_expires"])

    return JsonResponse({"ok": True, "plan": "free"})


@require_http_methods(["DELETE"])
def api_delete_account(request):
    """Delete user account and all associated data."""
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Login required"}, status=401)

    user = request.user

    # Delete all user's content
    Suggestion.objects.filter(author=user).delete()
    Comment.objects.filter(author=user).delete()
    SuggestionVote.objects.filter(user=user).delete()

    # Log out and delete
    auth.logout(request)
    user.delete()

    return JsonResponse({"ok": True})
