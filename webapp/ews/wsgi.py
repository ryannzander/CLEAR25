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

    import logging
    logger = logging.getLogger(__name__)

    from django.db import connection

    # Step 1: clear stale 'sites' migration records so migrate will recreate the table.
    if not _table_exists('django_site') and _table_exists('django_migrations'):
        logger.warning("_ensure_migrated: django_site missing — clearing stale records")
        try:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM django_migrations WHERE app = 'sites'")
        except Exception:
            logger.error("_ensure_migrated: could not clear stale records", exc_info=True)
            try:
                connection.rollback()
            except Exception:
                pass

    # Step 2: run all pending migrations.
    try:
        from django.core.management import call_command
        call_command("migrate", "--noinput", verbosity=0)
        logger.info("_ensure_migrated: migrate completed")
    except Exception:
        logger.error("_ensure_migrated: migrate command failed", exc_info=True)

    # Step 3: if django_site STILL does not exist (migrate failed or skipped it),
    # create it directly with raw SQL so the app can serve requests.
    if not _table_exists('django_site'):
        logger.warning("_ensure_migrated: creating django_site with raw SQL")
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS "django_site" (
                        "id"     SERIAL       NOT NULL PRIMARY KEY,
                        "domain" VARCHAR(100) NOT NULL UNIQUE,
                        "name"   VARCHAR(50)  NOT NULL
                    )
                """)
                # Mark both site migrations as applied so future migrate calls
                # don't try to re-create the table.
                if _table_exists('django_migrations'):
                    cursor.execute("""
                        INSERT INTO django_migrations (app, name, applied)
                        VALUES
                            ('sites', '0001_initial',              NOW()),
                            ('sites', '0002_alter_domain_unique',  NOW())
                        ON CONFLICT DO NOTHING
                    """)
            logger.info("_ensure_migrated: django_site created via raw SQL")
        except Exception:
            logger.error("_ensure_migrated: raw SQL creation failed", exc_info=True)
            try:
                connection.rollback()
            except Exception:
                pass

    # Step 4: ensure the Site row exists regardless of how the table was created.
    try:
        from django.contrib.sites.models import Site
        Site.objects.update_or_create(
            id=1, defaults={"domain": "clear25.xyz", "name": "C.L.E.A.R."}
        )
        logger.info("_ensure_migrated: Site record ok")
    except Exception:
        logger.error("_ensure_migrated: Site.update_or_create failed", exc_info=True)

_ensure_migrated()
