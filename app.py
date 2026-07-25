"""Flask front end: a phone-sized dictation editor backed by PyWhispr.

The browser records audio and this app relays it to whichever configured
PyWhispr server is alive. The relay is not optional plumbing — phones only grant
microphone access to a secure context, and an HTTPS page cannot fetch a
plain-HTTP LAN server, so the audio has to come through here. It also means
PyWhispr's unauthenticated API is never exposed to the browser.

Server selection, failover and the liveness cache all live in pywhispr_client.
"""

from flask import Flask, render_template, jsonify, request
import os
import logging

import pywhispr_client as pywhispr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

APP_VERSION = os.environ.get('APP_VERSION', 'dev')

# The app is unauthenticated: it is a personal dictation front end, and anything
# facing a hostile network is expected to sit behind a reverse proxy.
app.config.update(
    # Reject oversized uploads before reading them. Sized off PyWhispr's own
    # cap, with headroom for the query string and framing.
    MAX_CONTENT_LENGTH=pywhispr.FALLBACK_MAX_UPLOAD_BYTES + (1 << 20),
)

logger.info(f"pywhispr-web v{APP_VERSION} initialized")


@app.route('/')
def index():
    """The dictation editor"""
    return render_template('index.html', version=APP_VERSION)


@app.route('/settings')
def settings():
    """Server configuration screen"""
    return render_template('settings.html', version=APP_VERSION)


# -- server configuration API -------------------------------------------------


@app.route('/api/servers', methods=['GET'])
def get_servers():
    """The configured servers, in failover priority order."""
    config = pywhispr.load_config()
    return jsonify({
        'servers': config['servers'],
        'cache_ttl_seconds': config['cache_ttl_seconds'],
        'active': config.get('active'),
    })


@app.route('/api/servers', methods=['PUT'])
def put_servers():
    """Replace the whole server list and TTL.

    Whole-list replacement keeps list order meaningful (it *is* the failover
    priority) without needing separate add/remove/reorder endpoints.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': {'code': 'bad_request', 'message': 'expected a JSON object'}}), 400

    try:
        servers = pywhispr.normalise_servers(payload.get('servers', []))
    except pywhispr.ConfigError as exc:
        return jsonify({'error': {'code': 'bad_request', 'message': str(exc)}}), 400

    config = pywhispr.load_config()
    config['servers'] = servers
    config['cache_ttl_seconds'] = pywhispr.clamp_ttl(payload.get('cache_ttl_seconds'))
    # Any edit invalidates the cached choice, so the new order takes effect on
    # the next request rather than after the old TTL runs out.
    config['active'] = None
    pywhispr.save_config(config)
    logger.info('Server list updated: %d server(s)', len(servers))

    return jsonify({'servers': servers, 'cache_ttl_seconds': config['cache_ttl_seconds'], 'active': None})


@app.route('/api/servers/status')
def servers_status():
    """Live probe of every configured server, for the settings screen."""
    config = pywhispr.load_config()
    return jsonify({'servers': pywhispr.probe_all(config),
                    'cache_ttl_seconds': config['cache_ttl_seconds']})


@app.route('/api/ready')
def ready():
    """Whether we can transcribe right now, and the limits to record within."""
    try:
        server = pywhispr.select_server()
    except pywhispr.NoServerAvailable as exc:
        return jsonify({'ready': False, 'message': str(exc), 'servers': exc.verdicts}), 200

    health = server.get('health') or pywhispr.probe(server['url'])
    return jsonify({
        'ready': True,
        'server': {'name': server['name'], 'url': server['url']},
        'backend': health.get('backend'),
        'max_audio_seconds': health.get('max_audio_seconds') or pywhispr.FALLBACK_MAX_AUDIO_SECONDS,
        'max_upload_bytes': health.get('max_upload_bytes') or pywhispr.FALLBACK_MAX_UPLOAD_BYTES,
    })


# -- transcription ------------------------------------------------------------


@app.route('/api/transcribe', methods=['POST'])
def transcribe():
    """Relay recorded audio to the live PyWhispr server and return its text."""
    body = request.get_data()
    if not body:
        return jsonify({'error': {'code': 'bad_audio', 'message': 'no audio was sent'}}), 400

    # Only the parameters PyWhispr understands for raw PCM, so a stray query
    # string cannot change how the audio is interpreted.
    params = {key: request.args[key] for key in ('sample_rate', 'channels', 'format') if key in request.args}
    content_type = request.headers.get('Content-Type', 'application/octet-stream').split(';')[0].strip()

    try:
        result = pywhispr.transcribe(body, content_type, params)
    except pywhispr.NoServerAvailable as exc:
        return jsonify({'error': {'code': 'no_server', 'message': str(exc)},
                        'servers': exc.verdicts}), 503

    payload = dict(result.body)
    payload['server'] = {'name': result.server['name'], 'url': result.server['url']}
    response = jsonify(payload)
    response.status_code = result.status
    if result.retry_after:
        response.headers['Retry-After'] = result.retry_after
    return response


@app.errorhandler(413)
def too_large(_error):
    """JSON rather than Werkzeug's HTML, since only fetch() hits this."""
    limit = app.config['MAX_CONTENT_LENGTH']
    return jsonify({'error': {'code': 'payload_too_large',
                              'message': f'recording is too large (limit {limit} bytes)'}}), 413


@app.route('/health')
def health_check():
    """Health check endpoint"""
    return jsonify({'status': 'healthy'})

if __name__ == '__main__':
    # For development only - use gunicorn in production
    app.run(host='0.0.0.0', port=5000, debug=False)
