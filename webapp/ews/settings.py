import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-key-change-in-production")
DEBUG = os.environ.get("DEBUG", "true").lower() == "true"

# Allowed hosts
ALLOWED_HOSTS = [
    "clear25.xyz",
    "www.clear25.xyz",
    ".vercel.app",  # Vercel preview deployments
]
# Add any additional hosts from env var
ALLOWED_HOSTS += [
    h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
]
if DEBUG:
    ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
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
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # Custom middleware (uncomment after testing):
    # "dashboard.middleware.RequestSizeLimitMiddleware",
    # "dashboard.middleware.SecurityHeadersMiddleware",
    # "dashboard.middleware.RateLimitMiddleware",
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
            ],
        },
    },
]

WSGI_APPLICATION = "ews.wsgi.application"

# Database — Supabase PostgreSQL via DATABASE_URL, fallback to SQLite for local dev
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL:
    # Parse the URL manually to avoid urlparse issues with special chars in passwords.
    # Expected format: postgresql://user:password@host:port/dbname
    import re as _re
    # Use last @ as delimiter: password may contain @
    # Username stops at first : after scheme://
    # Password is everything between user: and the last @
    _m = _re.match(r'^(\w+)://([^:]+):(.+)@([^@]+)$', DATABASE_URL)
    if _m:
        _scheme, _user, _pw, _hostpath = _m.groups()
        # Split host:port/dbname
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
                "OPTIONS": {"sslmode": "require"}
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

# Auth
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"

# Allauth
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = True
ACCOUNT_LOGOUT_ON_GET = True
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

# Use cache-backed sessions if Redis available
if REDIS_URL:
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    SESSION_CACHE_ALIAS = "default"
else:
    SESSION_ENGINE = "django.contrib.sessions.backends.db"

SESSION_COOKIE_AGE = 60 * 60 * 24 * 7  # 1 week

# =============================================================================
# CSRF SECURITY (minimal config - Vercel handles HTTPS)
# =============================================================================

# CSRF trusted origins
CSRF_TRUSTED_ORIGINS = [
    "https://clear25.xyz",
    "https://www.clear25.xyz",
]
# Add any additional origins from env var
CSRF_TRUSTED_ORIGINS += [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if DEBUG:
    CSRF_TRUSTED_ORIGINS += ["http://localhost:8000", "http://127.0.0.1:8000"]

# =============================================================================
# SECURITY SETTINGS (minimal - let Vercel handle HTTPS/redirects)
# =============================================================================

X_FRAME_OPTIONS = "DENY"

# Vercel handles SSL, so we just need the proxy header
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
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

# Allow Vercel preview deployments (*.vercel.app)
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.vercel\.app$",
]

if DEBUG:
    CORS_ALLOWED_ORIGINS += [
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

# Only allow the methods and headers actually needed by the API
CORS_ALLOW_METHODS = ["GET", "OPTIONS"]
CORS_ALLOW_HEADERS = ["accept", "authorization", "content-type", "x-api-key"]
