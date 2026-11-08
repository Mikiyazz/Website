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
    # DEV_SQLITE_PATH 让压测/演练用一份独立副本，避免与正在运行的开发服务器抢同一个文件。
    sqlite_path = os.getenv("DEV_SQLITE_PATH", "").strip()
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(sqlite_path) if sqlite_path else BASE_DIR / "db.sqlite3",
            # 并发压测时写事务会排队：IMMEDIATE 让事务一开始就拿写锁（遵循 timeout），
            # 否则 SQLite 的"读锁升级写锁"会绕开忙等待，直接抛 database is locked。
            "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
        }
    }

if not _is_service_reachable("127.0.0.1", 6379):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "practice-dev-cache",
        }
    }

# 本地开发不启动 Celery worker：导出、校验等任务在 Web 进程内同步执行，链路可完整跑通。
# 需要真实异步时用 production settings（该处显式关闭 eager）。
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = False

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0", "*"]
