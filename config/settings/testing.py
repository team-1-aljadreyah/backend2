"""Automated test suite settings."""
from .base import *  # noqa: F403

DEBUG = False
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "daphne"]  # noqa: F405
if not SECRET_KEY:  # noqa: F405
    SECRET_KEY = "test-suite-secret-key-at-least-fifty-characters-long-0001"
if "testserver" not in ALLOWED_HOSTS:  # noqa: F405
    ALLOWED_HOSTS = [*ALLOWED_HOSTS, "testserver", "localhost", "127.0.0.1"]  # noqa: F405

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Use locmem cache & InMemoryChannelLayer in unit tests unless explicitly running Redis tests
if not os.getenv("TEST_USE_REDIS"):  # noqa: F405
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }

# Relax throttling in tests unless specifically enabled inside a rate-limit test
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_CLASSES": [],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "10000/min",
        "user": "10000/min",
        "login": "10000/min",
        "register": "10000/min",
    },
}

# Faster hashing acceptable ONLY for automated test execution speed
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]
