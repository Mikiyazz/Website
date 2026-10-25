from django.core.exceptions import ImproperlyConfigured
from .base import *  # noqa: F403

if len(SECRET_KEY) < 50 or SECRET_KEY.startswith(("development-only", "replace-")):
    raise ImproperlyConfigured("Production requires a unique DJANGO_SECRET_KEY of at least 50 characters.")
if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS or set(ALLOWED_HOSTS) <= {"localhost", "127.0.0.1"}:
    raise ImproperlyConfigured("Configure actual production DJANGO_ALLOWED_HOSTS.")
if not CSRF_TRUSTED_ORIGINS or any(not origin.startswith("https://") for origin in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured("Production requires HTTPS CSRF trusted origins.")
DEBUG = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31_536_000
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
# Trust only an HTTPS reverse proxy that overwrites this header; never expose web directly.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_REDIRECT_EXEMPT = [r"^health/live/$", r"^health/ready/$"]
