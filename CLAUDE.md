# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

pywhispr-web is a Flask web front end for [PyWhispr](https://github.com/benstaniford/PyWhispr),
a desktop dictation app that keeps a speech-to-text model warm and exposes it over HTTP.
This app is the browser client: it records audio, relays it to whichever configured
PyWhispr server is alive, and shows the transcribed text in an editor.

It is designed to be used on a phone, installed to the home screen as a full-screen PWA.

## Development Commands

### Building and Running
```bash
# Build and run locally (development)
docker-compose up --build -d

# Stop the application
docker-compose down

# View application logs
docker-compose logs -f pywhispr-web

# Run the Flask dev server directly (localhost is a secure context, so recording works)
PYWHISPR_CONFIG_PATH=./config.json python app.py
```

### Testing

```bash
# Everything: import checks, unit tests, Docker container tests
./scripts/test-all

# Unit tests
python -m pytest tests/ -v
python -m unittest discover -s tests   # what CI runs

# Import verification (also runnable under pytest)
python tests/test_imports.py

# Docker container tests
./test-docker/test-container.sh
```

### Testing without a real PyWhispr server

`scripts/fake-pywhispr.py` implements the same two endpoints with no model or GPU.
This is the only practical way to exercise failover on a dev machine:

```bash
python scripts/fake-pywhispr.py --port 9149 --name desktop &
python scripts/fake-pywhispr.py --port 9150 --name laptop &
python scripts/fake-pywhispr.py --port 9151 --status loading   # pretend it's warming up
```

### Release Management
```bash
# Tag a new patch release, which triggers the Docker image build
./scripts/make-release
```

## Architecture Overview

```
phone ──HTTPS──> pywhispr-web (Flask :5000) ──HTTP──> PyWhispr :9149
                        │
                        └── /data/config.json  servers, TTL, cached choice
```

### Core files

- **`app.py`** — routes, session auth, and the transcription proxy. Deliberately thin
- **`pywhispr_client.py`** — everything about the upstream: server registry, health
  probing, failover, and the liveness cache. No Flask imports, so it is unit-testable
  without a request context
- **`templates/base.html`** — the shared shell carrying the PWA and full-screen metadata.
  Every other template extends it
- **`static/js/recorder.js`** + **`capture-worklet.js`** — microphone capture
- **`static/js/app.js`** — the editor screen; **`settings.js`** — server configuration

## Constraints that drive the design

These are not preferences; changing them breaks the app.

### PyWhispr accepts no compressed audio

`stt/wav.py` upstream decodes WAV and headerless PCM only. Its own docstring says
browser `MediaRecorder` output (WebM/Opus) "cannot be decoded without ffmpeg; web
clients should send raw float32 PCM from an `AudioContext` instead".

So capture goes through Web Audio: `AudioWorklet` collects Float32 samples, they are
resampled to 16 kHz mono, converted to `Int16Array`, and posted as
`application/octet-stream?sample_rate=16000&channels=1&format=s16le`.
**Do not reach for `MediaRecorder`**, and no ffmpeg is needed in the image.

### Flask must proxy the audio

PyWhispr has no auth and no TLS. Phones only grant microphone access in a secure
context, and an HTTPS page cannot fetch a plain-HTTP address. So the browser talks only
to this app. This also keeps PyWhispr's open API off the network and puts failover state
in one place. **Do not add direct browser-to-PyWhispr calls.**

### State must live on disk, not in a module global

`gunicorn.conf.py` runs `cpu_count() * 2 + 1` pre-forked workers with `preload_app = True`
and recycles them every `max_requests`. Any in-process cache would diverge between
workers and reset unpredictably. `pywhispr_client` uses one `flock`'d JSON file written
atomically via temp file + `os.replace()`.

Note the lock is held on a *sibling* `.lock` file, because `os.replace()` swaps the
config's inode out from under any lock held on the config itself.

### Bodies must have a Content-Length

PyWhispr rejects chunked bodies with HTTP 411. Post a typed array or `Blob` from the
browser, never a `ReadableStream`, and pass `bytes` (not a generator) to `requests`.

### Gunicorn's timeout

Set to 180s. PyWhispr's own queue timeout is 120s *plus* the clip duration, because a
request can queue behind a local dictation on the host. The 30s default killed workers
mid-transcription.

## The upstream API

Default `0.0.0.0:9149`, three routes, no auth, `Access-Control-Allow-Origin: *`.

- **`GET /v1/health`** → `{status, version, backend, sample_rate, max_audio_seconds,
  max_upload_bytes, queue_depth, max_queue, pcm_formats}`. Never touches the model, so
  it is cheap to poll. Returns **200 even while loading or errored**, so readiness means
  `200 && status == "ready"`. `status` ∈ `loading|ready|error|unknown`
- **`POST /v1/transcribe`** → `{text, backend, audio_seconds, processing_seconds}`
- Errors are always `{"error": {"code", "message"}}`. Codes worth handling distinctly:
  `model_loading` (503, `Retry-After: 5`), `model_unavailable` (503), `busy` (503,
  `Retry-After: 2`), `payload_too_large` (413), `bad_audio` (400), `timeout` (504)

Read `max_audio_seconds` and `max_upload_bytes` from `/v1/health` rather than hardcoding
them — they are configurable in PyWhispr's TOML.

PyWhispr does **no** text post-processing beyond `.strip()`. There is no custom
vocabulary, punctuation or LLM cleanup to expose.

## Failover behaviour

`select_server()` walks the list **sequentially, in configured order**, and takes the
first server that is ready. List order is the priority order. The choice is cached with
a TTL (default 60s, user-configurable, clamped 5–3600).

The cache is invalidated on: expiry, any transport failure or upstream 5xx, and any edit
to the server list. On failure the app re-probes and retries **once**, and only if a
*different* server is selected — otherwise it would hammer the same broken host. A 4xx is
never retried.

Consequence worth remembering: the TTL only governs how soon a recovered
higher-priority server is preferred again. Failing over away from a dead server is
immediate.

## Development Guidelines

### Adding a route
- Protected routes get `@login_required`. Routes under `/api/` return a JSON 401 rather
  than redirecting, because a redirect reaches `fetch()` as an opaque HTML success
- `/health` must stay unauthenticated and unchanged — the Docker `HEALTHCHECK` and
  `test-docker/test-container.sh` both depend on it

### Front-end conventions
- No build step, no framework, no CDN. Plain ES5-compatible JS in `static/js/`
- Full-screen phone layout depends on: `100dvh` (not `vh`), `viewport-fit=cover`,
  `env(safe-area-inset-*)`, and `min-height: 0` on flex children that scroll
- Inputs must be at least 16px or iOS Safari zooms the page on focus
- New static files need a matching `COPY` in the Dockerfile — it copies specific paths,
  not the whole tree, so a new directory silently would not ship

### Testing conventions
- `tests/test_servers.py` — `pywhispr_client` in isolation; patch
  `pywhispr_client.CONFIG_PATH` at a `TemporaryDirectory` and patch `requests`
- `tests/test_transcribe.py` — routes through `app.test_client()`
- Authenticate in tests with the established idiom:
  ```python
  with self.app.session_transaction() as sess:
      sess['authenticated'] = True
  ```
- `tests/test_imports.py` runs both as a script (non-zero exit on failure) and as a
  unittest module. Keep its checks inside functions — a module-level `sys.exit()` aborts
  the whole pytest run during collection

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_USERNAME` | `user` | Authentication username |
| `APP_PASSWORD` | `password` | Authentication password |
| `SECRET_KEY` | `your-secret-key-change-this-in-production` | Flask session secret |
| `SESSION_COOKIE_SECURE` | unset | Set to `true` when serving over HTTPS |
| `PYWHISPR_CONFIG_PATH` | `/data/config.json` | Server list and cache location |
| `PYWHISPR_SERVERS` | unset | First-run seed only, `name=url,name=url` |
| `APP_VERSION` | `dev` | Shown on the settings screen |

The server list is owned by the settings screen, not the environment. `PYWHISPR_SERVERS`
seeds it only when no config file exists.

`/data` must be writable by the non-root container user. The named `pywhispr-data`
volume handles this; a host bind mount needs `chown -R 1000:1000`.

## Deployment Notes

- Microphone access **requires HTTPS or localhost**. On a phone this means a
  TLS-terminating reverse proxy in front of the app; there is no way around it
- Multi-stage Docker build keeps the image small; no ffmpeg or audio libraries needed
- CI (`.github/workflows/`): `build-check.yml` compiles and runs the unit tests on push;
  `docker-build-release.yml` builds, runs the container tests, and pushes multi-arch
  images on a `v*` tag
