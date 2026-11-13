"""Local development settings (SQLite + local storage defaults)."""
import os
from .base import *  # noqa: F403

DEBUG = os.getenv("DJANGO_DEBUG", "false").lower() in ("1", "true", "yes")
if not SECRET_KEY:  # noqa: F405
    SECRET_KEY = "dev-only-insecure-secret-key-do-not-use-in-production-582910"
if "testserver" not in ALLOWED_HOSTS:  # noqa: F405
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, "*"]  # noqa: F405

USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
