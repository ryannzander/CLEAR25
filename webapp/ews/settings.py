import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


# =============================================================================
# CORE SECURITY: SECRET_KEY, DEBUG, ALLOWED_HOSTS
# =============================================================================

# DEBUG defaults to False — production-safe default. Set DEBUG=true in local dev.
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# SECRET_KEY must be supplied explicitly. We fail closed in any environment that
# is not explicitly DEBUG, refusing to boot rather than silently signing
# sessions/JWTs with a known constant. Local dev gets a stable dev key so it is
# obvious in logs and bug reports that the deployment is not configured.
_DEV_SECRET_KEY = "dev-only-key-DO-NOT-USE-IN-PRODUCTION"
SECRET_KEY = os.environ.get("SECRET_KEY") or os.environ.get("DJANGO_SECRET_KEY", "")

if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = _DEV_SECRET_KEY
    else:
        raise RuntimeError(
            "SECRET_KEY environment variable is required when DEBUG=false. "
            "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(64))'"
        )

if not DEBUG and SECRET_KEY == _DEV_SECRET_KEY:
    raise RuntimeError(
        "Refusing to boot with the development SECRET_KEY in a non-DEBUG environment."
    )

# Allowed hosts
ALLOWED_HOSTS = [
    "clear25.xyz",
    "www.clear25.xyz",
]
# Add any additional hosts from env var (comma-separated)
ALLOWED_HOSTS += [
    h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
]
# Vercel preview deployments are scoped to a known prefix when set.
_VERCEL_HOST_PREFIX = os.environ.get("VERCEL_HOST_PREFIX", "").strip()
if _VERCEL_HOST_PREFIX:
    ALLOWED_HOSTS.append(f".{_VERCEL_HOST_PREFIX}.vercel.app")
if DEBUG:
    ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "whitenoise.runserver_nostatic",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "dashboard",
    "corsheaders",
]

SITE_ID = 1

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "dashboard.middleware.RequestSizeLimitMiddleware",
    "dashboard.middleware.SecurityHeadersMiddleware",
    "dashboard.middleware.RateLimitMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "ews.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "ews.wsgi.application"

# Database — Supabase PostgreSQL via DATABASE_URL, fallback to SQLite for local dev
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL:
    # Parse the URL manually to avoid urlparse issues with special chars in passwords.
    # Expected format: postgresql://user:password@host:port/dbname[?params]
    import re as _re
    # Strip any query string so it doesn't end up in the db name.
    _db_url, _, _qs = DATABASE_URL.partition("?")
    _m = _re.match(r'^(\w+)://([^:]+):(.+)@([^@]+)$', _db_url)
    if _m:
        _scheme, _user, _pw, _hostpath = _m.groups()
        _hp, _, _dbname = _hostpath.partition("/")
        _host, _, _port = _hp.partition(":")
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": _dbname or "postgres",
                "USER": _user,
                "PASSWORD": _pw,
                "HOST": _host,
                "PORT": _port or "5432",
                "CONN_MAX_AGE": 600,
                "OPTIONS": {"sslmode": "require"},
            }
        }
    else:
        import dj_database_url
        DATABASES = {
            "default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)
        }
        DATABASES["default"].setdefault("OPTIONS", {})
        DATABASES["default"]["OPTIONS"]["sslmode"] = "require"
else:
    if not DEBUG:
        raise RuntimeError(
            "DATABASE_URL is required when DEBUG=false. SQLite is not a "
            "supported production backend for this deployment."
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.path.join(BASE_DIR, "db.sqlite3"),
        }
    }

STATIC_URL = "static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

# Use basic whitenoise (no manifest) so it works without collectstatic on Vercel
WHITENOISE_USE_FINDERS = True

# Path to the shared data/ folder
DATA_DIR = os.path.join(BASE_DIR.parent, "data")

# Research data lives locally only (gitignored). It has been kept under two layouts:
#   projdata/...                  (current)
#   data/LAPTOP TSF 2026/...      (older)
# Probe both so local dev loads the real Excel; on production neither exists and the app
# falls back to the bundled catalog (dashboard/services/bundled_stations.json).
_RESEARCH_BASES = [
    os.path.join(BASE_DIR.parent, "projdata"),
    os.path.join(DATA_DIR, "LAPTOP TSF 2026"),
]
_RESEARCH_SUBDIR = "07.  The 4 Cities - Regression formulas and alert network stations"
_NAPS_SUBPATH = os.path.join("05. NAPS Stations", "04.  Canada_NAPS_Stations_Active_Years.xlsx")


def _first_existing(paths, default):
    for p in paths:
        if os.path.exists(p):
            return p
    return default


# Research data: regression Excel files per city (CLEAR methodology)
RESEARCH_DATA_BASE = _first_existing(
    [os.path.join(b, _RESEARCH_SUBDIR) for b in _RESEARCH_BASES],
    os.path.join(_RESEARCH_BASES[0], _RESEARCH_SUBDIR),
)
# NAPS station coordinates lookup
NAPS_STATIONS_PATH = _first_existing(
    [os.path.join(b, _NAPS_SUBPATH) for b in _RESEARCH_BASES],
    os.path.join(_RESEARCH_BASES[0], _NAPS_SUBPATH),
)

# Auth
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Allauth — security-tightened
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
# Require email verification before account is usable. "mandatory" blocks login
# until the email link is clicked.
ACCOUNT_EMAIL_VERIFICATION = os.environ.get(
    "ACCOUNT_EMAIL_VERIFICATION", "mandatory"
)
# Allauth rate limits: per IP/user/email. See allauth docs for keys.
ACCOUNT_RATE_LIMITS = {
    "login":           "5/m",
    "login_failed":    "10/15m",
    "signup":          "5/h",
    "send_email":      "5/5m",
    "change_password": "3/h",
    "reset_password":  "3/h",
    "reset_password_from_key": "5/h",
    "confirm_email":   "5/h",
}
SOCIALACCOUNT_AUTO_SIGNUP = True
# Disable login-on-GET and logout-on-GET — both enable drive-by CSRF.
SOCIALACCOUNT_LOGIN_ON_GET = False
ACCOUNT_LOGOUT_ON_GET = False
# Social account email linking only when email is verified by the provider.
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_PROVIDERS = {
    "google": {
        "APP": {
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {"access_type": "online"},
    }
}

# Email backend — console in DEBUG, SMTP via env in production.
if DEBUG:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = os.environ.get(
        "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
    )
    EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
    DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@clear25.xyz")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{levelname}] {asctime} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        # App views — log exceptions with full tracebacks server-side
        "dashboard.views": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "dashboard.middleware": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# =============================================================================
# CACHING CONFIGURATION
# =============================================================================

# Use Redis if available, otherwise local memory cache
REDIS_URL = os.environ.get("REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "ews",
            "TIMEOUT": 300,  # 5 minutes default
            "OPTIONS": {
                "socket_connect_timeout": 5,
                "socket_timeout": 5,
            },
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "ews-cache",
            "TIMEOUT": 300,
            "OPTIONS": {
                "MAX_ENTRIES": 1000,
            },
        }
    }

# =============================================================================
# SESSION SETTINGS
# =============================================================================

# Cookie-based sessions: signed server-side, stored client-side.
# This requires no django_session table and works correctly on serverless
# (Vercel) where Lambda instances don't share memory or a persistent DB pool.
# With Redis available, upgrade to cache-backed sessions for larger payloads.
if REDIS_URL:
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    SESSION_CACHE_ALIAS = "default"
else:
    SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

SESSION_COOKIE_AGE = 60 * 60 * 24 * 7  # 1 week
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # Read by JS for fetch headers
CSRF_COOKIE_SAMESITE = "Lax"

# =============================================================================
# CSRF
# =============================================================================

CSRF_TRUSTED_ORIGINS = [
    "https://clear25.xyz",
    "https://www.clear25.xyz",
]
CSRF_TRUSTED_ORIGINS += [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if DEBUG:
    CSRF_TRUSTED_ORIGINS += ["http://localhost:8000", "http://127.0.0.1:8000"]

# =============================================================================
# TRANSPORT / TLS SECURITY
# =============================================================================

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # HSTS — start with 1 year, allow preload via env once you're ready.
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = os.environ.get(
        "SECURE_HSTS_PRELOAD", "true"
    ).lower() == "true"

# Request size cap (1 MiB by default). Honored by RequestSizeLimitMiddleware.
MAX_REQUEST_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(1024 * 1024)))
# Django's built-in upload guard, set to match.
DATA_UPLOAD_MAX_MEMORY_SIZE = MAX_REQUEST_BODY_BYTES

# Per-IP rate-limit for unauthenticated mutating endpoints.
IP_RATE_LIMIT_REQUESTS = int(os.environ.get("IP_RATE_LIMIT_REQUESTS", "30"))
IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("IP_RATE_LIMIT_WINDOW_SECONDS", "60"))

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# =============================================================================
# NOWPAYMENTS (Crypto Billing)
# =============================================================================

NOWPAYMENTS_SANDBOX = os.environ.get("NOWPAYMENTS_SANDBOX", "false").lower() == "true"

# Store both sets of keys — flip NOWPAYMENTS_SANDBOX to switch
_NP_LIVE_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
_NP_LIVE_IPN = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
_NP_SANDBOX_KEY = os.environ.get("NOWPAYMENTS_SANDBOX_API_KEY", "")
_NP_SANDBOX_IPN = os.environ.get("NOWPAYMENTS_SANDBOX_IPN_SECRET", "")

NOWPAYMENTS_API_KEY = _NP_SANDBOX_KEY if NOWPAYMENTS_SANDBOX else _NP_LIVE_KEY
NOWPAYMENTS_IPN_SECRET = _NP_SANDBOX_IPN if NOWPAYMENTS_SANDBOX else _NP_LIVE_IPN
NOWPAYMENTS_API_URL = (
    "https://api.sandbox.nowpayments.io" if NOWPAYMENTS_SANDBOX
    else "https://api.nowpayments.io"
)

# =============================================================================
# CORS
# =============================================================================

# Only API routes need CORS headers (dashboard is same-origin)
CORS_URLS_REGEX = r"^/api/.*$"

CORS_ALLOWED_ORIGINS = [
    "https://clear25.xyz",
    "https://www.clear25.xyz",
]
# Additional production origins from env (comma-separated)
CORS_ALLOWED_ORIGINS += [
    origin.strip()
    for origin in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

# Preview deployments: only allow Vercel previews scoped to our project, never
# the generic *.vercel.app namespace (any free Vercel project would qualify).
CORS_ALLOWED_ORIGIN_REGEXES = []
if _VERCEL_HOST_PREFIX:
    # Escape the prefix to use it inside a regex literal safely.
    import re as _cors_re
    CORS_ALLOWED_ORIGIN_REGEXES.append(
        rf"^https://{_cors_re.escape(_VERCEL_HOST_PREFIX)}-[a-z0-9\-]+\.vercel\.app$"
    )

if DEBUG:
    CORS_ALLOWED_ORIGINS += [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

# Credentials are NOT allowed on cross-origin requests. Sessions are same-origin
# only; API consumers should use Authorization headers.
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOW_METHODS = ["GET", "OPTIONS"]
CORS_ALLOW_HEADERS = ["accept", "authorization", "content-type", "x-api-key"]
