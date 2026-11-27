from django.core.exceptions import ImproperlyConfigured
from .base import *  # noqa: F403

if len(SECRET_KEY) < 50 or SECRET_KEY.startswith(("development-only", "replace-")):
    raise ImproperlyConfigured("Production requires a unique DJANGO_SECRET_KEY of at least 50 characters.")
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS or set(ALLOWED_HOSTS) <= {"localhost", "127.0.0.1"}:
    raise ImproperlyConfigured("Configure actual production DJANGO_ALLOWED_HOSTS.")

# 校内接入模式开关（默认开启 HTTPS 生产安全；校内纯 HTTP 内网无证书部署时设 DJANGO_USE_HTTPS=false 即可消灭 301 循环）。
USE_HTTPS = env.bool("DJANGO_USE_HTTPS", default=True)
SECURE_SSL_REDIRECT = env.bool("DJANGO_SECURE_SSL_REDIRECT", default=USE_HTTPS)
SESSION_COOKIE_SECURE = env.bool("DJANGO_SESSION_COOKIE_SECURE", default=USE_HTTPS)
CSRF_COOKIE_SECURE = env.bool("DJANGO_CSRF_COOKIE_SECURE", default=USE_HTTPS)
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=31_536_000 if USE_HTTPS else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env.bool("DJANGO_SECURE_HSTS_PRELOAD", default=False)

if USE_HTTPS:
    if not CSRF_TRUSTED_ORIGINS or any(not origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS):
        raise ImproperlyConfigured("Production HTTPS mode requires https:// CSRF trusted origins.")
    # Trust only an HTTPS reverse proxy (Nginx TLS or campus HTTPS gateway) that overwrites this header.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
else:
    if not CSRF_TRUSTED_ORIGINS or any(
        not origin.startswith(("http://", "https://")) for origin in CSRF_TRUSTED_ORIGINS
    ):
        raise ImproperlyConfigured("Production HTTP mode requires http:// or https:// CSRF trusted origins.")
    SECURE_PROXY_SSL_HEADER = None

DEBUG = False
# 生产环境必须由独立的 Celery worker 执行导出等长任务，禁止在 Web 进程内同步执行。
CELERY_TASK_ALWAYS_EAGER = False
# 生产环境默认将大文件与导出包下载通过 X-Accel-Redirect 卸载给 Nginx。
NGINX_ACCEL_REDIRECT_ENABLED = env.bool("NGINX_ACCEL_REDIRECT_ENABLED", default=True)
SECURE_REDIRECT_EXEMPT = [r"^health/live/$", r"^health/ready/$"]
