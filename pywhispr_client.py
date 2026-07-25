"""Server registry and failover for talking to PyWhispr's transcription API.

PyWhispr exposes a warm speech-to-text model over plain HTTP (``GET /v1/health``,
``POST /v1/transcribe``) with no authentication and no TLS. This module owns
everything about that upstream: which servers are configured, which one is
currently alive, and relaying audio to it.

Three constraints shape the design:

* **Config and the liveness decision live on disk.** Gunicorn runs several
  pre-forked workers and recycles them every ``max_requests``, so a module-level
  cache would diverge between workers and reset unpredictably. One flock'd JSON
  file gives every worker the same answer.
* **The liveness decision is cached with a TTL.** Probing on every request would
  add a round trip to each transcription; never probing would strand the app on
  a dead server. A failed request invalidates the cache immediately, so failover
  does not wait out the TTL — the TTL only governs how soon a recovered
  higher-priority server is preferred again.
* **Servers are probed sequentially, in configured order.** The first one found
  ready wins, which makes the list a priority order and short-circuits after a
  single fast probe in the common case.

There is no Flask import here on purpose: all of this is testable without a
request context.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit, urlunsplit

import requests

log = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get('PYWHISPR_CONFIG_PATH', '/data/config.json')

DEFAULT_PORT = 9149
DEFAULT_TTL_SECONDS = 60
MIN_TTL_SECONDS = 5
MAX_TTL_SECONDS = 3600

# Health probes must be quick: select_server() may walk the whole list before
# answering, and the user is waiting on it.
PROBE_TIMEOUT = 2.0

# Generous, because a request can queue behind a local dictation on the host.
# PyWhispr's own queue timeout is 120s plus the clip duration.
TRANSCRIBE_TIMEOUT = 150.0

# Upstream defaults, used only until a real /v1/health tells us otherwise.
FALLBACK_MAX_AUDIO_SECONDS = 300
FALLBACK_MAX_UPLOAD_BYTES = 38_400_000

# Upstream statuses that mean "will fail if we send audio now". PyWhispr returns
# HTTP 200 from /v1/health even while loading or broken, so the body decides.
READY_STATUS = 'ready'

# Upstream statuses/errors worth retrying elsewhere. 4xx is excluded: bad audio
# will not transcribe any better on a different server.
RETRYABLE_STATUSES = frozenset({502, 503, 504})


class ConfigError(ValueError):
    """A server entry the user supplied cannot be used."""


class NoServerAvailable(RuntimeError):
    """No configured server is ready.

    Carries the per-server verdicts so the UI can say *why* — "unreachable"
    and "still loading its model" need different reactions from the user.
    """

    def __init__(self, verdicts: list[dict]):
        self.verdicts = verdicts
        if not verdicts:
            super().__init__('No PyWhispr servers are configured yet.')
        else:
            super().__init__('No configured PyWhispr server is ready.')


# -- config file --------------------------------------------------------------


def _blank_config() -> dict:
    return {'servers': [], 'cache_ttl_seconds': DEFAULT_TTL_SECONDS, 'active': None}


def _seed_from_env() -> list[dict]:
    """Parse PYWHISPR_SERVERS ("name=url,name=url") for first-run convenience.

    A bare url with no "name=" is accepted and named after its host.
    """
    raw = os.environ.get('PYWHISPR_SERVERS', '').strip()
    if not raw:
        return []

    servers = []
    for entry in raw.split(','):
        entry = entry.strip()
        if not entry:
            continue
        name, _, url = entry.partition('=')
        if not url:
            name, url = '', name
        try:
            url = normalise_url(url)
        except ConfigError as exc:
            log.warning('Ignoring PYWHISPR_SERVERS entry %r: %s', entry, exc)
            continue
        servers.append({'id': uuid.uuid4().hex, 'name': name.strip() or _host_of(url), 'url': url})
    return servers


def _read_unlocked(handle) -> dict:
    try:
        data = json.load(handle)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A corrupt config must not brick the app; a readable one is more
        # useful than a 500, and the user can re-enter servers in the UI.
        log.error('Config at %s is unreadable (%s); starting from blank', CONFIG_PATH, exc)
        return _blank_config()

    if not isinstance(data, dict):
        log.error('Config at %s is not an object; starting from blank', CONFIG_PATH)
        return _blank_config()

    config = _blank_config()
    servers = data.get('servers')
    if isinstance(servers, list):
        config['servers'] = [s for s in servers if isinstance(s, dict) and s.get('url')]
    config['cache_ttl_seconds'] = clamp_ttl(data.get('cache_ttl_seconds'))
    active = data.get('active')
    if isinstance(active, dict) and active.get('url'):
        config['active'] = active
    return config


def load_config() -> dict:
    """Read the config, seeding from the environment on first run."""
    try:
        with open(CONFIG_PATH, encoding='utf-8') as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return _read_unlocked(handle)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        config = _blank_config()
        config['servers'] = _seed_from_env()
        return config


def save_config(config: dict) -> None:
    """Write the config atomically, so a crash mid-write cannot truncate it."""
    directory = os.path.dirname(CONFIG_PATH) or '.'
    os.makedirs(directory, exist_ok=True)

    # The lock lives on a sibling file rather than the config itself, because
    # os.replace() swaps the inode out from under any lock held on it.
    lock_path = os.path.join(directory, f'.{os.path.basename(CONFIG_PATH)}.lock')
    with open(lock_path, 'a+', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            tmp_path = f'{CONFIG_PATH}.{os.getpid()}.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as handle:
                json.dump(config, handle, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, CONFIG_PATH)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def clamp_ttl(value) -> int:
    """Coerce a user-supplied TTL into a sane range, falling back to the default."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, seconds))


def normalise_url(raw: str) -> str:
    """Turn user input into a base url, e.g. ``192.168.1.10`` -> ``http://192.168.1.10:9149``.

    People type hostnames, not urls, so assume http and PyWhispr's default port
    rather than rejecting the most natural input.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError('a server address is required')

    candidate = raw.strip()
    if '//' not in candidate:
        candidate = f'http://{candidate}'

    parts = urlsplit(candidate)
    if parts.scheme not in ('http', 'https'):
        raise ConfigError(f'unsupported scheme {parts.scheme!r}: use http or https')
    if not parts.hostname:
        raise ConfigError(f'could not find a hostname in {raw!r}')

    netloc = parts.hostname
    if ':' in netloc:  # bare IPv6 literal
        netloc = f'[{netloc}]'
    port = parts.port if parts.port is not None else (None if parts.scheme == 'https' else DEFAULT_PORT)
    if port is not None:
        netloc = f'{netloc}:{port}'

    return urlunsplit((parts.scheme, netloc, parts.path.rstrip('/'), '', ''))


def _host_of(url: str) -> str:
    """The hostname of an already-normalised url, for defaulting a display name."""
    return urlsplit(url).hostname or url


def normalise_servers(entries) -> list[dict]:
    """Validate a whole server list from the UI. Order is the failover priority."""
    if not isinstance(entries, list):
        raise ConfigError('expected a list of servers')

    servers, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConfigError('each server must be an object with a url')
        url = normalise_url(entry.get('url', ''))
        if url in seen:
            raise ConfigError(f'{url} is listed more than once')
        seen.add(url)
        name = str(entry.get('name', '') or '').strip() or _host_of(url)
        servers.append({'id': str(entry.get('id') or uuid.uuid4().hex), 'name': name[:60], 'url': url})
    return servers


# -- probing ------------------------------------------------------------------


def probe(url: str, timeout: float = PROBE_TIMEOUT) -> dict:
    """Ask one server how it is doing. Never raises.

    ``/v1/health`` never touches the model, so this is cheap. It answers 200
    even while loading or errored, so readiness needs the body too.
    """
    verdict = {'url': url, 'ok': False, 'ready': False, 'status': None, 'backend': None,
               'version': None, 'queue_depth': None, 'max_queue': None,
               'max_audio_seconds': None, 'max_upload_bytes': None, 'error': None}
    try:
        response = requests.get(f'{url}/v1/health', timeout=timeout)
    except requests.RequestException as exc:
        verdict['error'] = _friendly_transport_error(exc)
        return verdict

    if response.status_code != 200:
        verdict['error'] = f'health check returned HTTP {response.status_code}'
        return verdict

    try:
        body = response.json()
    except ValueError:
        verdict['error'] = 'health check did not return JSON — is this a PyWhispr server?'
        return verdict
    if not isinstance(body, dict):
        verdict['error'] = 'health check did not return an object'
        return verdict

    verdict.update(
        ok=True,
        status=body.get('status', 'unknown'),
        backend=body.get('backend'),
        version=body.get('version'),
        queue_depth=body.get('queue_depth'),
        max_queue=body.get('max_queue'),
        max_audio_seconds=body.get('max_audio_seconds') or FALLBACK_MAX_AUDIO_SECONDS,
        max_upload_bytes=body.get('max_upload_bytes') or FALLBACK_MAX_UPLOAD_BYTES,
    )
    verdict['ready'] = verdict['status'] == READY_STATUS
    if not verdict['ready']:
        verdict['error'] = {
            'loading': 'the model is still loading',
            'error': 'the model failed to load',
        }.get(verdict['status'], f'server reports status {verdict["status"]!r}')
    return verdict


def probe_all(config: dict | None = None) -> list[dict]:
    """Probe every configured server in parallel, for the settings screen.

    Parallel here (unlike select_server) because the user wants to see the state
    of the whole list at once, not the first usable entry.
    """
    config = config or load_config()
    servers = config['servers']
    if not servers:
        return []

    active_url = (config.get('active') or {}).get('url')
    with ThreadPoolExecutor(max_workers=min(8, len(servers))) as pool:
        verdicts = list(pool.map(lambda s: probe(s['url']), servers))

    for server, verdict in zip(servers, verdicts):
        verdict['id'] = server['id']
        verdict['name'] = server['name']
        verdict['active'] = server['url'] == active_url
    return verdicts


def _friendly_transport_error(exc: requests.RequestException) -> str:
    if isinstance(exc, requests.Timeout):
        return 'timed out'
    if isinstance(exc, requests.ConnectionError):
        return 'could not connect'
    return str(exc) or exc.__class__.__name__


# -- server selection ---------------------------------------------------------


def _cached_server(config: dict) -> dict | None:
    """Return the cached choice if it is still valid, else None.

    A cached url that has since been removed from the list is ignored, so
    editing the servers takes effect immediately.
    """
    active = config.get('active')
    if not active:
        return None
    if time.time() - active.get('chosen_at', 0) >= config['cache_ttl_seconds']:
        return None
    return next((s for s in config['servers'] if s['url'] == active['url']), None)


def select_server(force: bool = False) -> dict:
    """Return the server to use, probing only when the cache cannot answer.

    Raises NoServerAvailable if nothing is ready.
    """
    config = load_config()

    if not force:
        cached = _cached_server(config)
        if cached is not None:
            return dict(cached, cached=True)

    verdicts = []
    for server in config['servers']:
        verdict = probe(server['url'])
        verdict['name'] = server['name']
        verdicts.append(verdict)
        if verdict['ready']:
            config['active'] = {'url': server['url'], 'chosen_at': time.time()}
            save_config(config)
            log.info('Selected PyWhispr server %s (%s)', server['name'], server['url'])
            return dict(server, cached=False, health=verdict)

    if config.get('active') is not None:
        config['active'] = None
        save_config(config)
    raise NoServerAvailable(verdicts)


def invalidate() -> None:
    """Forget the cached choice, so the next call re-probes from the top."""
    config = load_config()
    if config.get('active') is not None:
        config['active'] = None
        save_config(config)


# -- transcription ------------------------------------------------------------


class TranscribeResult:
    """The upstream response, plus which server produced it."""

    def __init__(self, status: int, body: dict, server: dict, retry_after: str | None = None):
        self.status = status
        self.body = body
        self.server = server
        self.retry_after = retry_after

    @property
    def ok(self) -> bool:
        return self.status == 200


def _post_audio(url: str, body: bytes, content_type: str, params: dict) -> requests.Response:
    # An explicit Content-Length matters: PyWhispr rejects chunked bodies with
    # HTTP 411. Passing bytes (not a generator) makes requests set it.
    return requests.post(
        f'{url}/v1/transcribe',
        data=body,
        params=params,
        headers={'Content-Type': content_type, 'Content-Length': str(len(body))},
        timeout=TRANSCRIBE_TIMEOUT,
    )


def _decode(response: requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {'error': {'code': 'bad_gateway',
                          'message': f'server returned HTTP {response.status_code} with no JSON body'}}
    return body if isinstance(body, dict) else {'error': {'code': 'bad_gateway',
                                                          'message': 'server returned unexpected JSON'}}


def transcribe(body: bytes, content_type: str, params: dict) -> TranscribeResult:
    """Relay audio to the live server, failing over once if it lets us down.

    Retries only on a transport failure or a 5xx, and only if re-probing picks a
    *different* server — otherwise we would just hammer the same broken host.
    """
    server = select_server()
    attempted = set()

    while True:
        attempted.add(server['url'])
        try:
            response = _post_audio(server['url'], body, content_type, params)
        except requests.RequestException as exc:
            log.warning('Transcription via %s failed: %s', server['url'], exc)
            failure = TranscribeResult(
                502,
                {'error': {'code': 'server_unreachable',
                           'message': f'{server["name"]} {_friendly_transport_error(exc)}'}},
                server,
            )
        else:
            if response.status_code not in RETRYABLE_STATUSES:
                return TranscribeResult(response.status_code, _decode(response), server,
                                        response.headers.get('Retry-After'))
            log.warning('Transcription via %s returned HTTP %s', server['url'], response.status_code)
            failure = TranscribeResult(response.status_code, _decode(response), server,
                                       response.headers.get('Retry-After'))

        # This server is not usable right now: drop it from the cache and see
        # whether a different one is.
        invalidate()
        try:
            server = select_server(force=True)
        except NoServerAvailable:
            return failure
        if server['url'] in attempted:
            return failure
