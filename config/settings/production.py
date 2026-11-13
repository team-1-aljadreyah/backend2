"""Production & Staging strict security settings."""
import os

from .base import *  # noqa: F403

DEBUG = False

if not SECRET_KEY or len(SECRET_KEY) < 32:  # noqa: F405
    raise RuntimeError("DJANGO_SECRET_KEY must be configured (>= 32 chars) in production.")

if os.getenv("DB_ENGINE", "sqlite").strip().lower() != "postgres":
    raise RuntimeError("Production environment must use PostgreSQL (DB_ENGINE=postgres).")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "true").lower() in ("1", "true", "yes")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
