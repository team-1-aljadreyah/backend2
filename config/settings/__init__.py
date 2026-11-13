"""Django settings loader selecting environment profile via DJANGO_ENV."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(BASE_DIR / ".env")

DJANGO_ENV = os.getenv("DJANGO_ENV", "development").strip().lower()

if DJANGO_ENV == "production":
    from .production import *  # noqa: F403
elif DJANGO_ENV == "testing":
    from .testing import *  # noqa: F403
else:
    from .development import *  # noqa: F403
