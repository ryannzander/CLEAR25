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

def _table_exists(name):
    """Return True if the named table exists in the database."""
    from django.db import connection
    try:
        with connection.cursor() as cursor:
            cursor.execute(f'SELECT 1 FROM "{name}" LIMIT 0')
        return True
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        return False

def _ensure_migrated():
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        from django.core.management import call_command

        # If django_site is missing the table may still be recorded as applied
        # in django_migrations. Fake it back to zero so migrate recreates it.
        if not _table_exists('django_site'):
            try:
                call_command("migrate", "sites", "zero", "--fake", verbosity=0)
            except Exception:
                pass

        # Run all pending migrations (no-op if already up to date).
        call_command("migrate", "--noinput", verbosity=0)

        # Ensure the Site record exists.
        from django.contrib.sites.models import Site
        Site.objects.update_or_create(
            id=1, defaults={"domain": "clear25.xyz", "name": "C.L.E.A.R."}
        )
    except Exception:
        import logging
        logging.getLogger(__name__).error("_ensure_migrated failed", exc_info=True)

_ensure_migrated()
