"""Conservative initial limits; tune only against measured CPU, memory, and DB budget."""
bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = 2
threads = 2
timeout = 30
graceful_timeout = 30
keepalive = 5
max_requests = 500
max_requests_jitter = 50
accesslog = None  # RequestContextMiddleware logs route names without query strings.
errorlog = "-"
capture_output = True
preload_app = False
