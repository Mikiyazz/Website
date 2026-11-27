"""Production Gunicorn config sized for 2000+ campus users (4C8G+ baseline)."""
import os

bind = "0.0.0.0:8000"
worker_class = "gthread"
# 4 workers x 4 threads = 16 concurrent slots; preload_app=True shares read-only app memory via CoW.
workers = int(os.getenv("GUNICORN_WORKERS", "4"))
threads = int(os.getenv("GUNICORN_THREADS", "4"))
# 2GB 打包材料在校园网慢速上行下可能超过 5 分钟，worker 超时同步放宽到 10 分钟。
timeout = 600
graceful_timeout = 60
keepalive = 5
max_requests = 500
max_requests_jitter = 50
accesslog = None  # RequestContextMiddleware logs route names without query strings.
errorlog = "-"
capture_output = True
preload_app = True
