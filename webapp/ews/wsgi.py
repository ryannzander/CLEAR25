import os
import sys

# Ensure the webapp directory is on the Python path
webapp_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if webapp_dir not in sys.path:
    sys.path.insert(0, webapp_dir)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ews.settings")

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
app = application  # Alias for Vercel

# Run migrations at runtime if tables are missing (Vercel serverless)
_migrated = False
def _ensure_migrated():
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        needs_migrate = False
        try:
            from django.contrib.sites.models import Site
            Site.objects.get(id=1)
        except Exception:
            needs_migrate = True
        try:
            from dashboard.models import (
                ReadingSnapshot, CachedResult, Suggestion, APIKey, DeviceToken, RefreshToken
            )
            ReadingSnapshot.objects.count()
            CachedResult.objects.count()
            Suggestion.objects.count()
            DeviceToken.objects.count()
            RefreshToken.objects.count()
            ak = APIKey.objects.first()
            if ak:
                _ = ak.requests_this_hour
        except Exception:
            needs_migrate = True
        if needs_migrate:
            from django.core.management import call_command
            call_command("migrate", "--noinput")
            from django.contrib.sites.models import Site
            Site.objects.update_or_create(id=1, defaults={"domain": "clear25.xyz", "name": "C.L.E.A.R."})
    except Exception:
        import logging
        logging.getLogger(__name__).error("_ensure_migrated failed", exc_info=True)

_ensure_migrated()
