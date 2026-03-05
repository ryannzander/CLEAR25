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

    # Each entry maps a Django migration app-name to a representative table that
    # must exist.  If the table is missing but the migration is recorded as applied,
    # migrate --noinput silently skips it.  We delete those stale records first so
    # migrate actually recreates the missing tables.
    REQUIRED = {
        'sites':         'django_site',
        'sessions':      'django_session',
        'auth':          'auth_user',
        'contenttypes':  'django_content_type',
        'account':       'account_emailaddress',
        'socialaccount': 'socialaccount_socialapp',
        'dashboard':     'dashboard_userprofile',
    }

    # Step 1: clear stale migration records for every app whose table is missing.
    if _table_exists('django_migrations'):
        stale = [app for app, tbl in REQUIRED.items() if not _table_exists(tbl)]
        if stale:
            placeholders = ', '.join(['%s'] * len(stale))
            logger.warning("_ensure_migrated: missing tables — clearing stale records for: %s", stale)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"DELETE FROM django_migrations WHERE app IN ({placeholders})",
                        stale,
                    )
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

    # Step 3: raw-SQL fallbacks for the two tables the app cannot start without.
    # These run only if migrate still failed to create them.

    if not _table_exists('django_site'):
        logger.warning("_ensure_migrated: creating django_site via raw SQL")
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS "django_site" (
                        "id"     SERIAL       NOT NULL PRIMARY KEY,
                        "domain" VARCHAR(100) NOT NULL UNIQUE,
                        "name"   VARCHAR(50)  NOT NULL
                    )
                """)
                if _table_exists('django_migrations'):
                    cursor.execute("""
                        INSERT INTO django_migrations (app, name, applied)
                        VALUES ('sites','0001_initial',NOW()),
                               ('sites','0002_alter_domain_unique',NOW())
                        ON CONFLICT DO NOTHING
                    """)
            logger.info("_ensure_migrated: django_site created")
        except Exception:
            logger.error("_ensure_migrated: django_site raw SQL failed", exc_info=True)
            try:
                connection.rollback()
            except Exception:
                pass

    if not _table_exists('django_session'):
        logger.warning("_ensure_migrated: creating django_session via raw SQL")
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS "django_session" (
                        "session_key"  VARCHAR(40) NOT NULL PRIMARY KEY,
                        "session_data" TEXT        NOT NULL,
                        "expire_date"  TIMESTAMP WITH TIME ZONE NOT NULL
                    )
                """)
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS "django_session_expire_date"
                    ON "django_session" ("expire_date")
                """)
                if _table_exists('django_migrations'):
                    cursor.execute("""
                        INSERT INTO django_migrations (app, name, applied)
                        VALUES ('sessions','0001_initial',NOW())
                        ON CONFLICT DO NOTHING
                    """)
            logger.info("_ensure_migrated: django_session created")
        except Exception:
            logger.error("_ensure_migrated: django_session raw SQL failed", exc_info=True)
            try:
                connection.rollback()
            except Exception:
                pass

    # Step 4: ensure the Site row exists.
    try:
        from django.contrib.sites.models import Site
        Site.objects.update_or_create(
            id=1, defaults={"domain": "clear25.xyz", "name": "C.L.E.A.R."}
        )
        logger.info("_ensure_migrated: Site record ok")
    except Exception:
        logger.error("_ensure_migrated: Site.update_or_create failed", exc_info=True)

_ensure_migrated()
