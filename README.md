# Toast Audio Sidecar

Standalone audio-only sidecar for ToastFlix DUAL playback.

The video path is not handled here. The existing ToastFlix VPS continues to
serve the DUAL video playlist, and Stremio's local proxy continues to fetch the
video segments. This service handles only vixsrc audio playlist registration,
TS-to-fMP4 transmuxing, local audio caching, and optional offset reporting.

The image is standalone: it uses direct network egress by default and does not
require WARP, a ToastFlix Docker network, or any other container on the host.
Set `SIDECAR_AUDIO_PROXY` only when the sidecar's own public IP cannot reach the
audio CDN.

## Modes

- `dualAudioHost` absent: the existing ToastFlix VPS audio path is used.
- `dualAudioHost` set to this service: audio is downloaded and served here.
- The URL may be local, LAN, or remote. It must be reachable by the Stremio
  client, not merely by the VPS.

The integration into `toast-stream-develop` is intentionally not included in
this folder yet. This repository is the sidecar implementation and contract.

## Session Tokens

No token is placed in the user's media configuration.

1. The integration requests `POST /session`.
2. The sidecar generates a random short-lived token.
3. The integration sends that token as `Authorization: Bearer <token>` or as
   the `t` query parameter on subsequent audio requests.

For unattended private deployments, `SIDECAR_FIXED_TOKEN` can be set. It is an
operator setting, not a user-facing media setting. `SIDECAR_BOOTSTRAP_KEY`, when
set, protects session creation.

## Run Locally

```bash
cp .env.example .env
docker compose up --build
curl http://127.0.0.1:3169/health
```

The default local URL is `http://127.0.0.1:3169`. For a LAN or remote sidecar,
set `SIDECAR_PUBLIC_URL` to the URL visible from the playback device and put
the service behind HTTPS/authenticated network access.

## Publish The Image

Build and publish to Docker Hub:

```bash
docker login
docker build -t YOUR_DOCKERHUB_USER/toast-audio-sidecar:latest .
docker push YOUR_DOCKERHUB_USER/toast-audio-sidecar:latest
```

Build and publish to GitHub Container Registry:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
docker build -t ghcr.io/YOUR_GITHUB_USER/toast-audio-sidecar:latest .
docker push ghcr.io/YOUR_GITHUB_USER/toast-audio-sidecar:latest
```

## Run With Compose

To use a published image, replace `build: .` in `compose.yml` with:

```yaml
image: YOUR_DOCKERHUB_USER/toast-audio-sidecar:latest
```

Or, for GHCR:

```yaml
image: ghcr.io/YOUR_GITHUB_USER/toast-audio-sidecar:latest
```

Configure the public address in `.env`:

```env
SIDECAR_PUBLIC_URL=http://YOUR_SERVER_IP:3169
SIDECAR_PORT=3169
SIDECAR_AUDIO_PROXY=
```

Start it:

```bash
cp .env.example .env
docker login
docker compose pull
docker compose up -d
docker compose logs -f
```

Verify it:

```bash
curl http://YOUR_SERVER_IP:3169/health
```

The URL entered in ToastFlix's `Server audio DUAL` field must be reachable by
the playback device. For public use, prefer an HTTPS URL such as
`https://audio.example.com`.

## Run With Docker

```bash
docker run -d \
  --name toast-audio-sidecar \
  --restart unless-stopped \
  -p 3169:3107 \
  --env-file .env \
  -v toast-audio-data:/app/data \
  YOUR_DOCKERHUB_USER/toast-audio-sidecar:latest
```

For GHCR, replace the image name with:

```text
ghcr.io/YOUR_GITHUB_USER/toast-audio-sidecar:latest
```

## API Contract

```text
POST /session
POST /dual/aprep
POST /dual/acache
GET  /dual/aud/{hid}/audio.m3u8
GET  /dual/aud/{hid}/init.mp4
GET  /dual/aud/{hid}/s{idx}.m4s
POST /offset/lookup
POST /offset/report
POST /sync
GET  /health
```

`/dual/aprep` receives the audio playlist and AES key from the browser because
the vixsrc token is IP-bound. The sidecar validates public HTTPS URLs, stores
the key only in its local cache, and never receives video data.

## Offset Cache

The sidecar stores offsets locally in `data/offsets.db`. If `OFFSET_API_URL` is
configured, it first asks the central service for a matching fingerprint and
reports newly measured results back to it. Only metadata is reported:

- media key;
- resolution;
- video fingerprint;
- audio fingerprint;
- offset, rate, confidence, and measurements.

Audio segments, AES keys, and fMP4 fragments are never sent to the central
offset API.

## Security

- Session tokens expire automatically.
- Audio URLs must be HTTPS public URLs by default.
- Local/private/link-local hostnames and private IP addresses are rejected.
- Redirects are checked again before following.
- Arbitrary video URLs are not accepted by this service.

## Integration

The main addon accepts this service through `dualAudioHost`. Without that
setting it keeps the VPS audio path. The sidecar can be hosted on any machine
reachable by the playback device; its vixsrc input comes from the browser's
IP-bound playlist and key, not from a new server-side vixsrc login.
