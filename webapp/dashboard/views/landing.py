"""Landing page view."""
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie


@ensure_csrf_cookie
def landing_page(request):
    """Render the landing page."""
    return render(request, "dashboard/landing.html")


def privacy_page(request):
    """Render the privacy policy page."""
    return render(request, "dashboard/privacy.html")
