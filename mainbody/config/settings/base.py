"""Shared settings. Business limits belong to Activity/MaterialRequirement."""
from pathlib import Path
import environ
from kombu import Queue

BASE_DIR = Path(__file__).resolve().parents[2]
env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

SECRET_KEY = env("DJANGO_SECRET_KEY", default="development-only-do-not-use-in-production-000000000000")
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "apps.common", "apps.accounts", "apps.activities", "apps.submissions",
    "apps.uploads", "apps.reviews", "apps.validation", "apps.news",
    "apps.exports", "apps.audit", "apps.jobs",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "apps.common.middleware.RequestContextMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request", "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
DATABASES = {"default": env.db("DATABASE_URL", default="postgresql://practice:practice@127.0.0.1:5432/practice")}
DATABASES["default"].update(CONN_MAX_AGE=60, CONN_HEALTH_CHECKS=True)
DATABASES["default"]["OPTIONS"] = {
    "connect_timeout": 3,
    "options": "-c statement_timeout=10000 -c lock_timeout=3000 -c idle_in_transaction_session_timeout=15000",
}
CACHE_URL = env("CACHE_URL", default="redis://127.0.0.1:6379/1")
CACHES = {"default": {
    "BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": CACHE_URL,
    "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
}}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
# Application requests contain metadata only. File bytes go directly to object storage.
DATA_UPLOAD_MAX_MEMORY_SIZE = 1 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 0
DATA_UPLOAD_MAX_NUMBER_FILES = 0
MAX_FILE_BYTES = min(env.int("MAX_FILE_BYTES", default=10_000_000_000), 10_000_000_000)
UPLOAD_PART_BYTES = env.int("UPLOAD_PART_BYTES", default=32 * 1024 * 1024)
MAX_ACTIVE_UPLOADS_PER_USER = env.int("MAX_ACTIVE_UPLOADS_PER_USER", default=2)
STORAGE_BACKEND = env("STORAGE_BACKEND", default="disabled")

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://127.0.0.1:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TASK_IGNORE_RESULT = True  # Job state lives in PostgreSQL, not an expiring result cache.
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_ACKS_LATE = True
# Persistent outbox recovery is bounded; avoid poison-message requeue loops on OOM.
CELERY_TASK_REJECT_ON_WORKER_LOST = False
CELERY_TASK_SOFT_TIME_LIMIT = 240
CELERY_TASK_TIME_LIMIT = 300
CELERY_WORKER_MAX_TASKS_PER_CHILD = 100
CELERY_WORKER_MAX_MEMORY_PER_CHILD = 256_000
CELERY_BROKER_CONNECTION_TIMEOUT = 3
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_BROKER_POOL_LIMIT = 2
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 900, "socket_connect_timeout": 3, "socket_timeout": 3}
CELERY_TASK_PUBLISH_RETRY = False  # Database dispatcher owns publish retries.
CELERY_TASK_DEFAULT_QUEUE = "maintenance"
CELERY_TASK_QUEUES = tuple(Queue(name) for name in ("maintenance", "validation", "exports"))
CELERY_TASK_CREATE_MISSING_QUEUES = False
OUTBOX_LEASE_SECONDS = 30
OUTBOX_REDELIVERY_SECONDS = 900
OUTBOX_MAX_ATTEMPTS = 8

LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"json": {"()": "apps.common.logging.JsonFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {"django": {"handlers": ["console"], "level": "INFO", "propagate": False}},
}
