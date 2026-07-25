# PyWhispr Web

A phone-sized web front end for [PyWhispr](https://github.com/benstaniford/PyWhispr). Tap a
button, talk, and your words appear in an editor you can copy out of — the same
dictation the desktop app gives you, from any phone or tablet on your network.

PyWhispr keeps a speech-to-text model warm on a desktop machine and exposes it over
HTTP. This app is the client: it records audio in the browser, relays it to whichever
of your PyWhispr machines is currently switched on, and puts the text on screen.

## ✨ Features

- **Tap to record**: one big button, a live level ring, and a running timer
- **Editable transcript**: text lands at the cursor, so you can dictate in pieces and edit between them
- **Copy button**: one tap to put the whole transcript on the system clipboard
- **Multiple servers with automatic failover**: list your desktop, your laptop, whatever else runs PyWhispr, and the app uses the first one that answers
- **Cached availability**: the choice of server is cached so the app stays responsive, and re-checked when it goes stale or when a request fails
- **Installs like an app**: add it to your home screen and it runs full screen with no browser chrome
- **Session-based authentication**: the PyWhispr API has no auth of its own, so this app keeps it off the open network

## 🚀 Quick Start

### Prerequisites

- Docker and Docker Compose
- At least one machine on your network running PyWhispr with its API enabled
  (`api_enabled = true` in PyWhispr's `config.toml`; it listens on port `9149` by default)
- Python 3.11+ (only for local development)

### Using Docker (Recommended)

1. **Clone the repository**
   ```bash
   git clone https://github.com/benstaniford/pywhispr-web.git
   cd pywhispr-web
   ```

2. **Start the application**
   ```bash
   docker-compose up --build -d
   ```

3. **Add a server**
   - Open `http://localhost:5000` and log in with `user` / `password`
   - Tap the cog, then **Add server**, and enter the address of a machine running
     PyWhispr — `192.168.1.10` is enough, the scheme and port `9149` are assumed
   - Tap **Save changes**. **Test all** shows what each server reports

4. **Dictate**
   - Go back to the editor and tap the microphone

### Recording needs HTTPS

Browsers only grant microphone access in a *secure context*: HTTPS, or `localhost`.
Over plain HTTP from another device the record button will fail no matter what
permissions you grant, and the app will tell you so.

On a phone you therefore need either:

- **A reverse proxy with TLS** in front of this app (Caddy, nginx, Traefik, Cloudflare
  Tunnel). Set `SESSION_COOKIE_SECURE=true` as well when you do.
- **Or a port forward to localhost** for testing, e.g. `ssh -L 5000:localhost:5000 host`,
  which the browser treats as secure.

This is also why the app proxies audio rather than having the browser call PyWhispr
directly: an HTTPS page is not allowed to fetch a plain-HTTP address, which every
PyWhispr server is.

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run the development server (localhost counts as secure, so recording works)
PYWHISPR_CONFIG_PATH=./config.json python app.py
```

## ⚙️ Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_USERNAME` | `user` | Authentication username |
| `APP_PASSWORD` | `password` | Authentication password |
| `SECRET_KEY` | `your-secret-key-change-this-in-production` | Flask session secret |
| `SESSION_COOKIE_SECURE` | unset | Set to `true` when serving over HTTPS |
| `PYWHISPR_CONFIG_PATH` | `/data/config.json` | Where the server list and cache are stored |
| `PYWHISPR_SERVERS` | unset | Optional first-run seed, `name=url,name=url` |
| `APP_VERSION` | `dev` | Shown on the settings screen |

The server list is edited in the app, not in the environment. `PYWHISPR_SERVERS` only
seeds it when no config file exists yet:

```yaml
environment:
  - PYWHISPR_SERVERS=desktop=192.168.1.10:9149,laptop=192.168.1.22:9149
```

### Persistence

The server list and the cached liveness decision live in `/data/config.json`, kept in
the `pywhispr-data` volume. The container runs as a non-root user, so if you replace
the named volume with a host directory you must make it writable by that user:

```bash
sudo chown -R 1000:1000 ./data
```

## 🔀 How failover works

Servers are tried **from the top of the list down**. The first one that answers
`GET /v1/health` with `status: "ready"` is used, and that decision is cached — so
normal use costs no extra round trips.

The cache is dropped and the list re-walked when:

- the cache expires (60 seconds by default, configurable on the settings screen)
- a transcription request fails to connect, or comes back `502`/`503`/`504`
- you edit the server list

When a request fails, the app immediately re-probes and retries once on a *different*
server, so a machine being switched off mid-session costs you one retry rather than an
error. A `400` is never retried — audio the server cannot read will not read any better
elsewhere.

The cache lifetime only governs how quickly a **recovered** higher-priority server is
preferred again. Failing over away from a dead server never waits for it.

A server that is reachable but still loading its model is reported as such rather than
treated as available, and the editor retries once it is warm.

## 🧪 Testing

```bash
# Everything: import checks, unit tests, and Docker container tests
./scripts/test-all

# Unit tests only
python -m pytest tests/ -v

# Import verification
python tests/test_imports.py

# Docker container tests only
./test-docker/test-container.sh
```

### Testing failover without a PyWhispr server

`scripts/fake-pywhispr.py` speaks the same two endpoints as the real thing and needs
no model or GPU:

```bash
# Two stand-in servers
python scripts/fake-pywhispr.py --port 9149 --name desktop &
python scripts/fake-pywhispr.py --port 9150 --name laptop &

# Add both in the settings screen, then try:
#   - recording (the first is used)
#   - killing the first and recording again (fails over to the second)
#   - restarting the first (preferred again once the cache expires)

# Or pretend a model is still warming up
python scripts/fake-pywhispr.py --port 9151 --status loading
```

## 🏗️ Architecture

```
phone ──HTTPS──> pywhispr-web (Flask :5000) ──HTTP──> PyWhispr :9149
                        │
                        └── /data/config.json  servers, TTL, cached choice
```

- **`app.py`** — routes, authentication, and the transcription proxy
- **`pywhispr_client.py`** — the server registry, health probing, failover and the
  liveness cache. No Flask imports, so it is testable on its own
- **`static/js/recorder.js`** — microphone capture via Web Audio
- **`templates/`** — a `base.html` shell plus the editor, settings and login screens

### Why Web Audio and not MediaRecorder

PyWhispr decodes WAV and headerless PCM only — no Opus, MP3 or M4A. `MediaRecorder`
produces WebM/Opus, which it cannot read without ffmpeg. So the browser captures raw
samples through an `AudioWorklet`, resamples them to 16 kHz mono, and posts signed
16-bit PCM. That also keeps uploads small: about 32 KB per second of speech, roughly a
twelfth of raw 48 kHz float32.

### API endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | The editor (requires authentication) |
| `GET /settings` | Server configuration |
| `GET /login`, `POST /login`, `GET /logout` | Authentication |
| `GET /api/servers` | Configured servers, in priority order |
| `PUT /api/servers` | Replace the server list and cache lifetime |
| `GET /api/servers/status` | Live probe of every server |
| `GET /api/ready` | Whether we can transcribe, and the recording limits |
| `POST /api/transcribe` | Relay audio to PyWhispr, with failover |
| `GET /health` | Container health check (unauthenticated) |

## 📁 Project Structure

```
├── app.py                 # Routes, auth, transcription proxy
├── pywhispr_client.py     # Server registry, probing, failover, cache
├── requirements.txt       # Python dependencies
├── Dockerfile             # Multi-stage Docker build
├── docker-compose.yml     # Development compose file
├── gunicorn.conf.py       # Production server configuration
├── templates/
│   ├── base.html          # Shared shell (PWA metadata, full-screen setup)
│   ├── index.html         # The editor
│   ├── settings.html      # Server configuration
│   └── login.html         # Login page
├── static/
│   ├── css/app.css
│   ├── js/recorder.js     # Web Audio capture
│   ├── js/capture-worklet.js
│   ├── js/app.js          # Editor screen
│   ├── js/settings.js     # Settings screen
│   └── manifest.webmanifest
├── tests/                 # Unit tests
├── scripts/               # Automation, plus fake-pywhispr.py
└── test-docker/           # Container testing
```

## 🐳 Docker Details

### Multi-Stage Build
- **Builder stage**: Compiles dependencies with build tools
- **Runtime stage**: Minimal production image (~150MB)
- **Base**: Python 3.11 slim for security and size optimization

Note that `gunicorn.conf.py` sets a 180-second worker timeout: a transcription request
can legitimately block for minutes if it queues behind a dictation on the host machine.

## 🚀 Deployment

1. Set secure environment variables
2. Put a TLS-terminating reverse proxy in front (required for microphone access)
3. Set `SESSION_COOKIE_SECURE=true`
4. Keep your PyWhispr servers on a trusted network

```bash
export APP_USERNAME="your-secure-username"
export APP_PASSWORD="your-secure-password"
export SECRET_KEY="your-very-long-random-secret-key"
export SESSION_COOKIE_SECURE="true"
```

## 🔒 Security notes

- PyWhispr's own API has **no authentication and no TLS** — anyone who can reach port
  9149 can spend its CPU. Keep it on a trusted network and let this app be the only
  thing exposed
- All routes except `/health` and `/login` require a session
- The container runs as a non-root user
- Change the default credentials and `SECRET_KEY` before exposing this anywhere

## 🆘 Troubleshooting

| Symptom | Cause |
|---------|-------|
| Record button disabled, "No server" | No server configured, or none reachable — check **Test all** in settings |
| "Microphone access needs HTTPS" | Serve the app over HTTPS or via `localhost` |
| "The server is still loading its model" | PyWhispr is warming up; first run may download the model |
| "The server is busy" | PyWhispr is at its concurrency limit; try again shortly |
| Settings will not save | The `/data` volume is not writable by the container user |

## 📝 License

See [LICENSE](LICENSE).
