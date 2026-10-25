"""Container process health check with a configured valid Host header."""
import os
import urllib.request

host = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip()
request = urllib.request.Request("http://127.0.0.1:8000/health/live/", headers={"Host": host})
with urllib.request.urlopen(request, timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
