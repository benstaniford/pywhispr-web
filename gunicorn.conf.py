# Gunicorn configuration file for production
#
# One file, two masters. TLS in Gunicorn applies to the whole process, so run.py
# launches this twice — once plain on 5000, once with TLS on 5443 — varying only
# the environment. Run by hand with no environment set it behaves exactly as it
# always did: plain HTTP on 5000.
import multiprocessing
import os

import tls_certs

# Server socket
bind = os.environ.get("PYWHISPR_BIND", "0.0.0.0:5000")
backlog = 2048

# Worker processes
workers = int(os.environ.get("PYWHISPR_WORKERS") or multiprocessing.cpu_count() * 2 + 1)
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

# TLS, when this master is the HTTPS one. run.py generates the material before
# starting us, because preload_app means we are read too late to create it here.
# Missing files are left unset rather than raising: a config that fails to load
# takes the whole container down.
_certs = tls_certs.paths_if_available()
if _certs:
    certfile = _certs.cert
    keyfile = _certs.key

# Performance
preload_app = True
