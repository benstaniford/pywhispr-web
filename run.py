"""Container entry point: certificates, then one Gunicorn master per scheme.

Gunicorn applies TLS to the whole process, so serving both HTTP and HTTPS means
two masters. They share ``gunicorn.conf.py`` and differ only by environment.

Both are worth having. HTTPS on 5443 is what the phone uses, and what makes the
microphone work at all. Plain HTTP on 5000 is how you fetch the CA certificate
with no warning to click through, and it keeps the Docker health check and the
container tests pointing at exactly the URL they always have. There is
deliberately no HTTP-to-HTTPS redirect: it would break both of those.

Certificates are generated here rather than in the Gunicorn config because
``preload_app`` means the config is read after the master has already committed
to its sockets — the files have to exist first.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys

import tls_certs

logging.basicConfig(level=logging.INFO, format='[run] %(message)s')
log = logging.getLogger(__name__)

HTTP_PORT = os.environ.get('PYWHISPR_HTTP_PORT', '5000')
HTTPS_PORT = os.environ.get('PYWHISPR_HTTPS_PORT', '5443')

# The plain-HTTP listener only bootstraps trust and answers the health check, so
# it does not need the full worker count.
HTTP_WORKERS = '2'

GUNICORN = ['gunicorn', '--config', 'gunicorn.conf.py', 'app:app']


def _spawn(bind: str, tls: str, workers: str | None = None) -> subprocess.Popen:
    env = dict(os.environ, PYWHISPR_BIND=bind, PYWHISPR_TLS=tls)
    if workers:
        env['PYWHISPR_WORKERS'] = workers
    log.info('starting %s (tls=%s)', bind, tls)
    return subprocess.Popen(GUNICORN, env=env)


def main() -> int:
    children: list[subprocess.Popen] = []

    if tls_certs.tls_enabled():
        hosts = tls_certs.configured_hosts()
        tls_certs.ensure_certificates(hosts)
        if not os.environ.get('PYWHISPR_TLS_HOSTS'):
            log.warning('PYWHISPR_TLS_HOSTS is unset, so the certificate only covers '
                        'localhost and the container hostname. Set it to the name or '
                        'address your phone will use, or HTTPS will warn.')
        children.append(_spawn(f'0.0.0.0:{HTTPS_PORT}', 'on'))
        children.append(_spawn(f'0.0.0.0:{HTTP_PORT}', 'off', HTTP_WORKERS))
    else:
        # Someone else is terminating TLS. One plain listener, full workers.
        children.append(_spawn(f'0.0.0.0:{HTTP_PORT}', 'off'))

    asked_to_stop = False

    def shutdown(signum, _frame):
        nonlocal asked_to_stop
        asked_to_stop = True
        log.info('signal %s, stopping', signum)
        for child in children:
            if child.poll() is None:
                child.send_signal(signum)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Exit as soon as any child does. A container serving only half its ports is
    # broken, and exiting lets Docker's restart policy actually restart it.
    _, status = os.wait()
    code = 0 if asked_to_stop else os.waitstatus_to_exitcode(status)
    log.info('a listener exited (%s); shutting the rest down', code)

    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()

    return code


if __name__ == '__main__':
    sys.exit(main())
