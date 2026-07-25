#!/usr/bin/env python3
"""A stand-in for PyWhispr, for exercising failover without a GPU or a model.

Speaks the same two routes as the real thing (``GET /v1/health``,
``POST /v1/transcribe``) and returns canned text. Run two on different ports to
watch pywhispr-web fail over between them:

    scripts/fake-pywhispr.py --port 9149 --name desktop &
    scripts/fake-pywhispr.py --port 9150 --name laptop &

Then configure both in the settings screen, kill the first, and record again.

    --status loading   pretend the model is still warming up (503 on transcribe)
    --status error     pretend the model failed to load
    --delay 5          take 5 seconds to "transcribe", to test timeouts
"""

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SAMPLE_RATE = 16000


class Server(ThreadingHTTPServer):
    """Carries the options, the way PyWhispr's own _Server carries its api."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, options):
        self.options = options
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'PyWhispr/0.0.0-fake'
    sys_version = ''

    @property
    def options(self):
        return self.server.options  # type: ignore[attr-defined]

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        sys.stderr.write('[%s] %s\n' % (self.options.name, format % args))

    def _json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _health(self):
        options = self.options
        return {
            'status': options.status,
            'version': '0.0.0-fake',
            'backend': f'fake-backend ({options.name})',
            'sample_rate': SAMPLE_RATE,
            'max_audio_seconds': options.max_audio_seconds,
            'max_upload_bytes': options.max_audio_seconds * SAMPLE_RATE * 4 * 2,
            'queue_depth': 0,
            'max_queue': 4,
            'pcm_formats': ['f32le', 's16le'],
        }

    def do_GET(self):
        path = self.path.split('?')[0].rstrip('/') or '/'
        if path in ('/v1/health', '/'):
            self._json(200, self._health())
        else:
            self._json(404, {'error': {'code': 'not_found', 'message': f'no such endpoint: {path}'}})

    def do_POST(self):
        path = self.path.split('?')[0].rstrip('/') or '/'
        if path != '/v1/transcribe':
            self._json(404, {'error': {'code': 'not_found', 'message': f'no such endpoint: {path}'}})
            return

        length = self.headers.get('Content-Length')
        if length is None:
            self.close_connection = True
            self._json(411, {'error': {'code': 'length_required', 'message': 'Content-Length is required'}},
                       {'Connection': 'close'})
            return
        body = self.rfile.read(int(length))

        options = self.options
        if options.status == 'loading':
            self.close_connection = True
            self._json(503, {'error': {'code': 'model_loading', 'message': 'the model is still loading'}},
                       {'Retry-After': '5', 'Connection': 'close'})
            return
        if options.status != 'ready':
            self.close_connection = True
            self._json(503, {'error': {'code': 'model_unavailable', 'message': 'the model is not available'}},
                       {'Retry-After': '30', 'Connection': 'close'})
            return

        if not body:
            self.close_connection = True
            self._json(400, {'error': {'code': 'bad_audio', 'message': 'request body is empty'}},
                       {'Connection': 'close'})
            return

        if options.delay:
            time.sleep(options.delay)

        # s16le mono is what the web client sends; good enough to report a duration.
        seconds = len(body) / (SAMPLE_RATE * 2)
        self._json(200, {
            'text': options.text,
            'backend': f'fake-backend ({options.name})',
            'audio_seconds': round(seconds, 3),
            'processing_seconds': round(options.delay, 3),
        })


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--port', type=int, default=9149)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--name', default=None, help='shown in the backend string (defaults to the port)')
    parser.add_argument('--status', choices=('ready', 'loading', 'error'), default='ready')
    parser.add_argument('--text', default='the quick brown fox jumps over the lazy dog')
    parser.add_argument('--delay', type=float, default=0.0, help='seconds to spend "transcribing"')
    parser.add_argument('--max-audio-seconds', type=int, default=300)
    options = parser.parse_args()
    if options.name is None:
        options.name = f'fake:{options.port}'

    httpd = Server((options.host, options.port), Handler, options)
    print(f'fake PyWhispr "{options.name}" on http://{options.host}:{options.port} '
          f'(status={options.status})', file=sys.stderr)
    try:
        httpd.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == '__main__':
    main()
