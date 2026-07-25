# Gunicorn configuration file for production
import multiprocessing

# Server socket
bind = "0.0.0.0:5000"
backlog = 2048

# Worker processes
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = "sync"
worker_connections = 1000
# Transcription requests block for as long as the upstream PyWhispr server takes,
# and it may queue a request behind a local dictation on the host machine (its own
# queue timeout is 120s plus the clip duration). The 30s default would kill the
# worker mid-transcription.
timeout = 180
keepalive = 2

# Restart workers after this many requests, to help prevent memory leaks
max_requests = 1000
max_requests_jitter = 100

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = "pywhispr-web"

# Security
limit_request_line = 0
limit_request_fields = 100
limit_request_field_size = 8190

# Performance
preload_app = True
