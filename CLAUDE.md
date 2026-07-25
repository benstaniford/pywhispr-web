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

# Same, but over real TLS — only needed to test the certificates themselves
PYWHISPR_TLS=on PYWHISPR_CERT_DIR=/tmp/pwcerts PYWHISPR_TLS_HOSTS=192.168.1.61 \
  PYWHISPR_CONFIG_PATH=./config.json python app.py

# Inspect the generated certificate (SANs, expiry, CA fingerprint)
PYWHISPR_CERT_DIR=/tmp/pwcerts python -m tls_certs
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
phone ──HTTPS :5443──┐
                     ├─> pywhispr-web ──HTTP──> PyWhispr :9149
health/bootstrap ────┘        │
    HTTP :5000                ├── /data/config.json  servers, TTL, cached choice
                              └── /data/certs        CA and server certificate
```

### Core files

- **`app.py`** — routes and the transcription proxy. Deliberately thin
- **`pywhispr_client.py`** — everything about the upstream: server registry, health
  probing, failover, and the liveness cache. No Flask imports, so it is unit-testable
  without a request context
- **`tls_certs.py`** — generates and renews the self-signed CA and server certificate.
  Also no Flask imports
- **`run.py`** — the container entry point: certificates first, then one Gunicorn master
  per scheme
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

### The certificate rules are Apple's, not ours

iOS 13+ rejects a server certificate outright unless it has a `subjectAltName` (the CN
is ignored entirely, and browsing by IP needs an `iPAddress` entry), an
`extendedKeyUsage` of `serverAuth`, a SHA-256 signature, and a lifetime of 825 days or
less. A stock `openssl req -x509` satisfies none of those. That is why `tls_certs.py`
builds certificates with `cryptography` — the rules are pinned in
`tests/test_tls_certs.py`, because the symptom of breaking one is an unexplained browser
warning on a phone, a long way from the code.

Trust is also two steps on iOS, and the second is easy to miss: installing the profile
is not enough, the user must *also* enable it under Settings → General → About →
Certificate Trust Settings. `templates/cert.html` says so emphatically. **Do not trim
that page down** — without step 3 the microphone stays blocked and the feature looks
broken.

The **CA must be reused, never regenerated**. It lives on the `/data` volume so it
survives upgrades; regenerating it would mean re-trusting on every device after every
release. The leaf is the disposable half — reissued on expiry or a host-list change.

### Two Gunicorn masters, and no HTTP-to-HTTPS redirect

TLS in Gunicorn is process-wide, so `run.py` starts the config twice with different
environment (`PYWHISPR_BIND`, `PYWHISPR_TLS`, `PYWHISPR_WORKERS`): TLS on 5443, plain on
5000. Certificates are generated in `run.py` and not in `gunicorn.conf.py`, because
`preload_app` means the config loads too late to create them.

Port 5000 serves the *whole* app in the clear on purpose. It is how a phone fetches the
CA before HTTPS is trusted, and it is what keeps the Docker `HEALTHCHECK`,
`test-docker/test-container.sh` and the CI compose heredoc pointing at the URL they
always have. **Do not add a redirect to HTTPS** — it would break all of those. The
HTTP-served pages link to `/cert` instead.

`run.py` exits as soon as either master dies, so `restart: unless-stopped` restarts a
container that would otherwise serve only half its ports.

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
- There is no authentication: every route is open, and nothing may redirect to a login
  page. `/api/` routes always answer JSON, since only `fetch()` calls them
- `/health` must stay unchanged — the Docker `HEALTHCHECK` and
  `test-docker/test-container.sh` both depend on it
- `/cert` and `/cert/pywhispr-ca.crt` must stay reachable over **plain HTTP**, since they
  are what a client needs before it will accept HTTPS at all

### Front-end conventions
- No build step, no framework, no CDN. Plain ES5-compatible JS in `static/js/`
- Full-screen phone layout depends on: `100dvh` (not `vh`), `viewport-fit=cover`,
  `env(safe-area-inset-*)`, and `min-height: 0` on flex children that scroll
- Inputs must be at least 16px or iOS Safari zooms the page on focus
- The microphone stream and the `AudioContext` are acquired once and held for the life of
  the page: stopping the tracks between clips makes the next `getUserMedia` a fresh
  request, which is a permission prompt before *every* recording on a phone. Between
  clips the tracks are disabled and the context suspended; they are only really released
  on `pagehide`. **Do not put `track.stop()` back into the per-clip teardown**
- New static files need a matching `COPY` in the Dockerfile — it copies specific paths,
  not the whole tree, so a new directory silently would not ship

### Testing conventions
- `tests/test_servers.py` — `pywhispr_client` in isolation; patch
  `pywhispr_client.CONFIG_PATH` at a `TemporaryDirectory` and patch `requests`
- `tests/test_transcribe.py` — routes through `app.test_client()`. No session setup is
  needed; `TestOpenAccess` guards against auth creeping back in
- `tests/test_tls_certs.py` — `tls_certs` in isolation; patch `tls_certs.CERT_DIR` at a
  `TemporaryDirectory`. Most of it asserts Apple's rules rather than ours
- `tests/test_cert_routes.py` — the `/cert` page and download headers
- `tests/test_imports.py` runs both as a script (non-zero exit on failure) and as a
  unittest module. Keep its checks inside functions — a module-level `sys.exit()` aborts
  the whole pytest run during collection

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PYWHISPR_CONFIG_PATH` | `/data/config.json` | Server list and cache location |
| `PYWHISPR_SERVERS` | unset | First-run seed only, `name=url,name=url` |
| `PYWHISPR_TLS_HOSTS` | unset | Comma-separated names/addresses the certificate must cover |
| `PYWHISPR_TLS` | `on` | `off` disables HTTPS, for when a proxy already terminates it |
| `PYWHISPR_CERT_DIR` | `/data/certs` | Where the generated CA and server certificate live |
| `APP_VERSION` | `dev` | Shown on the settings screen |

`run.py` also sets `PYWHISPR_BIND` and `PYWHISPR_WORKERS` per child; they are internal.

The server list is owned by the settings screen, not the environment. `PYWHISPR_SERVERS`
seeds it only when no config file exists.

`PYWHISPR_TLS_HOSTS` has no useful default and is **not** auto-detected: inside a bridge
network the container only sees a `172.x` address no phone can reach, so guessing would
produce a certificate that looks fine and fails in use. An unset value is surfaced as a
warning on `/cert` and in the logs instead.

`/data` must be writable by the non-root container user. The named `pywhispr-data`
volume handles this; a host bind mount needs `chown -R 1000:1000`.

## Deployment Notes

- Microphone access **requires HTTPS or localhost**; there is no way around it. The
  container now provides HTTPS itself with a self-signed CA, so a reverse proxy is
  optional rather than required — set `PYWHISPR_TLS=off` when you use one
- Both ports must be published, and `PYWHISPR_TLS_HOSTS` must list every name a client
  will use, or HTTPS warns however well the CA is trusted
- The live stack's compose lives **in Portainer**, so port and environment changes do not
  arrive with an image pull — they have to be edited there as well as in the repo
- Multi-stage Docker build keeps the image small; no ffmpeg or audio libraries needed.
  `cryptography` ships wheels for both `amd64` and `arm64`, so the multi-arch build needs
  no Rust toolchain
- CI (`.github/workflows/`): `build-check.yml` compiles and runs the unit tests on push;
  `docker-build-release.yml` builds, runs the container tests, and pushes multi-arch
  images on a `v*` tag

## Releasing & deploying (the `push-to-portainer` skill)

After **successfully completing a feature** (change implemented, tests/build passing),
run the **`push-to-portainer`** skill to release and deploy it: it commits & pushes to
`main`, cuts a release with `scripts/make-release`, watches the GitHub Actions build
(fixing any failures), and then redeploys the live `pywhispr-web` stack on the Portainer
server (moria) to pull the new image.

**Always get explicit confirmation from the user before the Portainer update (Stage 4).**
The redeploy restarts the live service, so pause after the release build is green and ask
the user to approve before running the redeploy — never update Portainer automatically.
