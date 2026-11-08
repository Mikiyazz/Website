"""Conservative initial limits; tune only against measured CPU, memory, and DB budget."""
bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = 2
threads = 2
# 2GB 打包材料在校园网慢速上行下可能超过 5 分钟，worker 超时同步放宽到 10 分钟。
timeout = 600
graceful_timeout = 60
keepalive = 5
max_requests = 500
max_requests_jitter = 50
accesslog = None  # RequestContextMiddleware logs route names without query strings.
errorlog = "-"
capture_output = True
preload_app = False
