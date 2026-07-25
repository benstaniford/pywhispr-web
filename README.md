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
- **No sign-in**: open it and dictate — see the security notes for what that means for where you expose it

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
   - Open `http://localhost:5000`
   - Tap the cog, then **Add server**, and enter the address of a machine running
     PyWhispr — `192.168.1.10` is enough, the scheme and port `9149` are assumed
   - Tap **Save changes**. **Test all** shows what each server reports

4. **Dictate**
   - Go back to the editor and tap the microphone

### Recording needs HTTPS

Browsers only grant microphone access in a *secure context*: HTTPS, or `localhost`.
Over plain HTTP from another device the record button will fail no matter what
permissions you grant, and the app will tell you so.

So the container serves HTTPS itself. On first start it generates its own certificate
authority and server certificate into the data volume, and listens on **two** ports:

| Port | Scheme | For |
|------|--------|-----|
| 5443 | HTTPS | Everything. This is the address to use from a phone |
| 5000 | HTTP | Fetching the certificate before HTTPS is trusted, and the health check |

Set `PYWHISPR_TLS_HOSTS` to every name or address a client will use to reach the app.
The certificate is only valid for what is listed there:

```yaml
environment:
  - PYWHISPR_TLS_HOSTS=moria,moria.local,192.168.1.10
```

Prefer a DNS or `.local` name over a bare IP — a new DHCP lease invalidates an IP.

### Trusting the certificate on an iPhone

Open **`http://<host>:5000/cert`** in Safari. That page carries these steps and the
fingerprint to check against.

1. **Download** the certificate — in Safari, not Chrome; only Safari can install a
   profile.
2. **Install** it: Settings → General → **VPN & Device Management** → *PyWhispr Web CA*
   → Install. Enter your passcode and accept the unsigned-profile warning.
3. **Enable full trust** — Settings → General → About → **Certificate Trust Settings** →
   turn on *PyWhispr Web CA*.
4. Go to **`https://<host>:5443`**, confirm there is no warning, and add it to your home
   screen.

**Step 3 is not optional.** iOS installs a certificate without trusting it, so skipping
it leaves Safari warning and the microphone blocked — exactly the symptom you are trying
to fix. And bookmark the HTTPS address: adding the plain-HTTP one to your home screen
gives you an app that can never record.

The authority is valid for ten years and lives on the `pywhispr-data` volume, so it
survives upgrades and redeploys — each device only ever does this once. The server
certificate is short-lived and reissues itself automatically, including when you change
`PYWHISPR_TLS_HOSTS`. Every client needs the certificate, not just the phone: a laptop
will warn until it trusts it too.

On **macOS** open the downloaded file and set it to *Always Trust* in Keychain Access;
on **Android** it is Settings → Security → Encryption & credentials → Install a
certificate; on **Linux** copy it into `/usr/local/share/ca-certificates/` and run
`update-ca-certificates`. To AirDrop it, download it on a Mac or iPad and share from
there.

### Using your own certificate instead

If something in front of this app already terminates TLS — Caddy, nginx, Traefik, a
Cloudflare Tunnel, or a Tailscale/Let's Encrypt certificate you manage yourself — set
`PYWHISPR_TLS=off`. The container then serves plain HTTP on 5000 only, and `/cert`
becomes a 404.

For quick local testing, a port forward also counts as secure:
`ssh -L 5000:localhost:5000 host`.

Note that none of this changes why the app proxies audio rather than having the browser
call PyWhispr directly: an HTTPS page is not allowed to fetch a plain-HTTP address,
which every PyWhispr server is.

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
| `PYWHISPR_CONFIG_PATH` | `/data/config.json` | Where the server list and cache are stored |
| `PYWHISPR_SERVERS` | unset | Optional first-run seed, `name=url,name=url` |
| `PYWHISPR_TLS_HOSTS` | unset | Comma-separated names and addresses the certificate must cover |
| `PYWHISPR_TLS` | `on` | `off` disables HTTPS, for when a proxy already terminates it |
| `PYWHISPR_CERT_DIR` | `/data/certs` | Where the generated CA and server certificate live |
| `APP_VERSION` | `dev` | Shown on the settings screen |

The server list is edited in the app, not in the environment. `PYWHISPR_SERVERS` only
seeds it when no config file exists yet:

```yaml
environment:
  - PYWHISPR_SERVERS=desktop=192.168.1.10:9149,laptop=192.168.1.22:9149
```

### Persistence

The server list and the cached liveness decision live in `/data/config.json`, and the
generated certificates in `/data/certs`, both kept in the `pywhispr-data` volume.
Deleting that volume means every device has to trust a new certificate authority, so it
is worth keeping. The container runs as a non-root user, so if you replace the named
volume with a host directory you must make it writable by that user:

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
phone ──HTTPS :5443──┐
                     ├─> pywhispr-web ──HTTP──> PyWhispr :9149
health/bootstrap ────┘        │
    HTTP :5000                ├── /data/config.json  servers, TTL, cached choice
                              └── /data/certs        CA and server certificate
```

- **`app.py`** — routes and the transcription proxy
- **`pywhispr_client.py`** — the server registry, health probing, failover and the
  liveness cache. No Flask imports, so it is testable on its own
- **`tls_certs.py`** — generates and renews the self-signed CA and server certificate.
  Also no Flask imports
- **`run.py`** — generates the certificates, then starts one Gunicorn master per scheme.
  Gunicorn applies TLS process-wide, so serving both takes two of them
- **`static/js/recorder.js`** — microphone capture via Web Audio
- **`templates/`** — a `base.html` shell plus the editor, settings and certificate screens

### Why Web Audio and not MediaRecorder

PyWhispr decodes WAV and headerless PCM only — no Opus, MP3 or M4A. `MediaRecorder`
produces WebM/Opus, which it cannot read without ffmpeg. So the browser captures raw
samples through an `AudioWorklet`, resamples them to 16 kHz mono, and posts signed
16-bit PCM. That also keeps uploads small: about 32 KB per second of speech, roughly a
twelfth of raw 48 kHz float32.

### API endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /` | The editor |
| `GET /settings` | Server configuration |
| `GET /api/servers` | Configured servers, in priority order |
| `PUT /api/servers` | Replace the server list and cache lifetime |
| `GET /api/servers/status` | Live probe of every server |
| `GET /api/ready` | Whether we can transcribe, and the recording limits |
| `POST /api/transcribe` | Relay audio to PyWhispr, with failover |
| `GET /cert` | Certificate download and trust instructions |
| `GET /cert/pywhispr-ca.crt` | The CA certificate to trust |
| `GET /health` | Container health check |

## 📁 Project Structure

```
├── app.py                 # Routes, transcription proxy
├── pywhispr_client.py     # Server registry, probing, failover, cache
├── tls_certs.py           # Self-signed CA and server certificate generation
├── run.py                 # Entry point: certificates, then one server per scheme
├── requirements.txt       # Python dependencies
├── Dockerfile             # Multi-stage Docker build
├── docker-compose.yml     # Development compose file
├── gunicorn.conf.py       # Production server configuration
├── templates/
│   ├── base.html          # Shared shell (PWA metadata, full-screen setup)
│   ├── index.html         # The editor
│   ├── settings.html      # Server configuration
│   └── cert.html          # Certificate download and trust instructions
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

1. Publish both ports, `5000` and `5443`, and set `PYWHISPR_TLS_HOSTS` to the names
   clients will use — or set `PYWHISPR_TLS=off` if a reverse proxy already terminates TLS
2. Keep this app and your PyWhispr servers on a trusted network, or behind
   authentication a proxy provides
3. Point `PYWHISPR_CONFIG_PATH` and `PYWHISPR_CERT_DIR` at a persistent volume — losing
   the certificate directory means re-trusting the app on every device

## 🔒 Security notes

- **This app has no authentication.** Anyone who can reach it can dictate through your
  PyWhispr servers and edit the server list. Expose it on a trusted network only, or put
  authentication in the reverse proxy you need for HTTPS anyway
- PyWhispr's own API has **no authentication and no TLS** either — anyone who can reach
  port 9149 can spend its CPU. Keep it on a trusted network and let this app be the only
  thing in front of it
- The container runs as a non-root user

## 🆘 Troubleshooting

| Symptom | Cause |
|---------|-------|
| Record button disabled, "No server" | No server configured, or none reachable — check **Test all** in settings |
| "Microphone access needs HTTPS" | You are on the plain-HTTP port. Use `https://<host>:5443` — see [Trusting the certificate](#trusting-the-certificate-on-an-iphone) |
| Safari still warns after installing the certificate | Full trust was not enabled: Settings → General → About → Certificate Trust Settings |
| "This certificate is not valid for &lt;host&gt;" | The name you are browsing by is not in `PYWHISPR_TLS_HOSTS`; add it and restart |
| "The server is still loading its model" | PyWhispr is warming up; first run may download the model |
| "The server is busy" | PyWhispr is at its concurrency limit; try again shortly |
| Settings will not save | The `/data` volume is not writable by the container user |

## 📝 License

See [LICENSE](LICENSE).
