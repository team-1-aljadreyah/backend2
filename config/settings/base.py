"""Base settings shared across all environments."""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "")
DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() in ("1", "true", "yes")
ALLOWED_HOSTS = [h.strip() for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third-party
    "channels",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "corsheaders",
    "drf_spectacular",
    # Project apps
    "apps.common",
    "apps.audit",
    "apps.notifications",
    "apps.warehouses",
    "apps.accounts",
    "apps.customers",
    "apps.catalog",
    "apps.supply_orders",
    "apps.receipts",
    "apps.inventory",
    "apps.transfers",
    "apps.stocktakes",
    "apps.orders",
    "apps.deliveries",
    "apps.invoices",
    "apps.payments",
    "apps.exchange_rates",
    "apps.commissions",
    "apps.expenses",
    "apps.reports",
    "apps.chat",
    "apps.realtime",
]

AUTH_USER_MODEL = "accounts.User"
REST_USE_JWT = True

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.common.middleware.RequestLanguageAndAuditMiddleware",
]

ROOT_URLCONF = "config.urls"
APPEND_SLASH = False

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
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

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


def build_database_config() -> dict:
    """Return Django DATABASES['default'] for SQLite or PostgreSQL (Psycopg 3)."""
    engine = os.getenv("DB_ENGINE", "sqlite").strip().lower()
    if engine == "postgres":
        raw_url = os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DATABASE_URL") or ""
        if not raw_url:
            raise RuntimeError("DB_ENGINE=postgres requires DATABASE_URL or SUPABASE_DATABASE_URL.")
        parsed = urlparse(raw_url)
        query = parse_qs(parsed.query)
        options: dict[str, object] = {}
        sslmode = query.get("sslmode", [None])[0]
        if sslmode:
            options["sslmode"] = sslmode
        elif os.getenv("DB_SSL_REQUIRE", "true").lower() in ("1", "true", "yes"):
            options["sslmode"] = "require"
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "localhost",
            "PORT": str(parsed.port or 5432),
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": options,
        }

    sqlite_rel = os.getenv("SQLITE_PATH", "db.sqlite3")
    sqlite_path = Path(sqlite_rel)
    if not sqlite_path.is_absolute():
        sqlite_path = BASE_DIR / sqlite_path
    return {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(sqlite_path),
        "OPTIONS": {
            "timeout": 30,
        },
    }


DATABASES = {"default": build_database_config()}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 6}},
]

LANGUAGE_CODE = "ar"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
BUSINESS_TIMEZONE = "Asia/Baghdad"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = os.getenv("MEDIA_URL", "/media/")
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", str(BASE_DIR / "media")))
if not MEDIA_ROOT.is_absolute():
    MEDIA_ROOT = BASE_DIR / MEDIA_ROOT

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Storage & Integrations
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").strip().lower()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_BUCKET_PRODUCT_IMAGES = os.getenv("SUPABASE_BUCKET_PRODUCT_IMAGES", "product-images")
SUPABASE_BUCKET_DELIVERY_PROOFS = os.getenv("SUPABASE_BUCKET_DELIVERY_PROOFS", "delivery-proofs")
SIGNED_URL_TTL_SECONDS = int(os.getenv("SIGNED_URL_TTL_SECONDS", "900"))
FIELD_ENCRYPTION_KEY = os.getenv("FIELD_ENCRYPTION_KEY", "")

# Qi Card config
QI_CARD_MODE = os.getenv("QI_CARD_MODE", "disabled").strip().lower()
QI_CARD_BASE_URL = os.getenv("QI_CARD_BASE_URL", "").rstrip("/")
QI_CARD_MERCHANT_ID = os.getenv("QI_CARD_MERCHANT_ID", "")
QI_CARD_TERMINAL_ID = os.getenv("QI_CARD_TERMINAL_ID", "")
QI_CARD_API_KEY = os.getenv("QI_CARD_API_KEY", "")
QI_CARD_WEBHOOK_SECRET = os.getenv("QI_CARD_WEBHOOK_SECRET", "")

METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")
API_ENABLE_DOCS = os.getenv("API_ENABLE_DOCS", "true").lower() in ("1", "true", "yes")

# CORS & Security headers
CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if o.strip()
]
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^https://.*\.ama8\.uk$",
]
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = [
    *default_headers,
    "idempotency-key",
    "accept-language",
    "access-control-request-private-network",
]
CORS_ALLOW_PRIVATE_NETWORK = os.getenv("CORS_ALLOW_PRIVATE_NETWORK", "true").lower() in ("1", "true", "yes")
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.getenv("CSRF_TRUSTED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if o.strip()
]

# DRF & JWT
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.accounts.authentication.SessionAwareJWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.common.exceptions.custom_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "120/min",
        "user": "600/min",
        "login": "10/min",
        "register": "10/min",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(os.getenv("JWT_ACCESS_MINUTES", "60"))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.getenv("JWT_REFRESH_DAYS", "14"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Al-Nahrain Wholesale ERP API",
    "DESCRIPTION": "Production backend API (/api/v1) for Project-2 Wholesale Distribution ERP.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

# Cache, Channel Layers & Celery
REDIS_URL = os.getenv("REDIS_URL", "")
CHANNELS_REDIS_URL = os.getenv("CHANNELS_REDIS_URL", REDIS_URL)
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }

if CHANNELS_REDIS_URL:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [CHANNELS_REDIS_URL]},
        }
    }
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }

REALTIME_TICKET_TTL_SECONDS = int(os.getenv("REALTIME_TICKET_TTL_SECONDS", "60"))
REALTIME_MAX_CONNECTIONS_PER_USER = int(os.getenv("REALTIME_MAX_CONNECTIONS_PER_USER", "5"))
REALTIME_MAX_MESSAGE_BYTES = int(os.getenv("REALTIME_MAX_MESSAGE_BYTES", "16384"))
REALTIME_RATE_LIMIT_PER_10S = int(os.getenv("REALTIME_RATE_LIMIT_PER_10S", "30"))

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "memory://")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "cache+memory://")
CELERY_TASK_ALWAYS_EAGER = os.getenv("CELERY_TASK_ALWAYS_EAGER", "true").lower() in ("1", "true", "yes")
CELERY_TIMEZONE = "UTC"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
}
