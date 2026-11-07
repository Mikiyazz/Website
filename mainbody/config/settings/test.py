"""PostgreSQL tests when reachable; automatic SQLite fallback when local PostgreSQL is unavailable."""
import os
import socket
from .base import *  # noqa: F403


def _is_postgres_reachable() -> bool:
    db_cfg = DATABASES.get("default", {})
    host = db_cfg.get("HOST") or "127.0.0.1"
    port = int(db_cfg.get("PORT") or 5432)
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


SECRET_KEY = "test-only-not-a-production-secret-key-0000000000000000"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
if os.getenv("TEST_SQLITE") == "1" or (os.getenv("TEST_SQLITE") != "0" and not _is_postgres_reachable()):
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
else:
    DATABASES["default"]["CONN_MAX_AGE"] = 0
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
CELERY_TASK_ALWAYS_EAGER = True
