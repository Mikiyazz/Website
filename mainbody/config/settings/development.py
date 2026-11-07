import os
import socket
from pathlib import Path
from .base import *  # noqa: F403

DEBUG = True
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}


def _is_service_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


db_host = DATABASES.get("default", {}).get("HOST") or "127.0.0.1"
db_port = int(DATABASES.get("default", {}).get("PORT") or 5432)

if os.getenv("DEV_SQLITE") == "1" or not _is_service_reachable(db_host, db_port):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

if not _is_service_reachable("127.0.0.1", 6379):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "practice-dev-cache",
        }
    }
    CELERY_TASK_ALWAYS_EAGER = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "*"]
