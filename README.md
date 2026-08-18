# ToastFlix Audio Sidecar

Questo servizio gestisce solo l'audio del DUAL ToastFlix.

- Il video continua a passare dal proxy locale di Stremio.
- Il browser dell'utente recupera da vixsrc playlist, token e chiave audio.
- Il sidecar scarica i segmenti audio gia' autorizzati, usa `ffmpeg` e serve l'audio convertito.
- Se `dualAudioHost` non viene configurato in ToastFlix, il sidecar non viene usato.

## Pubblicare Su GHCR

GHCR significa GitHub Container Registry. L'immagine viene costruita da GitHub
Actions e pubblicata su:

```text
ghcr.io/qwertyuiop8899/toastflix-sidecar:latest
```

### 1. Creare il workflow GitHub

Nel repository GitHub `qwertyuiop8899/toastflix-sidecar`:

1. Apri `Actions`.
2. Premi `New workflow`.
3. Premi `set up a workflow yourself`.
4. Incolla questo contenuto.
5. Salva il file come `.github/workflows/publish-ghcr.yml` sul branch `main`.

```yaml
name: Pubblica immagine GHCR

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read
  packages: write

jobs:
  build:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Login GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build e pubblica
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ github.repository_owner }}/toastflix-sidecar:latest
            ghcr.io/${{ github.repository_owner }}/toastflix-sidecar:sha-${{ github.sha }}
```

6. Vai in `Actions` e aspetta che il workflow finisca con successo.
7. Controlla il pacchetto nella sezione `Packages` del profilo GitHub.

Alla prima pubblicazione il pacchetto puo' essere privato. Se vuoi usarlo da
un server senza login, apri le impostazioni del pacchetto GHCR e imposta
`visibility: Public`.

## Installare Su Una Macchina

Requisiti:

- Docker;
- Docker Compose;
- porta TCP `3169` raggiungibile dal dispositivo che usa Stremio.

Clona il repository:

```bash
git clone https://github.com/qwertyuiop8899/toastflix-sidecar.git
cd toastflix-sidecar
cp .env.example .env
```

Nel file `.env` imposta l'indirizzo pubblico della macchina:

```env
SIDECAR_PUBLIC_URL=http://IP_DELLA_MACCHINA:3169
SIDECAR_PORT=3169
SIDECAR_AUDIO_PROXY=
```

`SIDECAR_AUDIO_PROXY` deve restare vuoto. Il sidecar usa direttamente la
connessione Internet della macchina e non dipende da WARP o da ToastFlix.

Per una macchina esposta su Internet usa preferibilmente HTTPS:

```env
SIDECAR_PUBLIC_URL=https://audio.example.com
```

Avvia usando l'immagine pubblicata su GHCR. Nel `compose.yml`, sostituisci:

```yaml
build: .
```

con:

```yaml
image: ghcr.io/qwertyuiop8899/toastflix-sidecar:latest
```

Poi esegui:

```bash
docker compose pull
docker compose up -d
```

Controlla lo stato:

```bash
docker compose ps
docker compose logs -f
curl http://IP_DELLA_MACCHINA:3169/health
```

La risposta corretta e' simile a:

```json
{"status":"ok","service":"toast-audio-sidecar"}
```

## Collegarlo A ToastFlix

Apri la configurazione di ToastFlix e attiva `FHD / 4K Remuxed`.

Nel campo `Server audio DUAL` inserisci solo l'indirizzo del sidecar:

```text
http://IP_DELLA_MACCHINA:3169
```

Oppure, se usi HTTPS:

```text
https://audio.example.com
```

Il campo audio resta disabilitato quando `FHD / 4K Remuxed` e' spento.

Non devi inserire token nella configurazione: il sidecar crea automaticamente
un token temporaneo per ogni sessione.

## Avvio Diretto Senza Compose

Se preferisci usare Docker direttamente:

```bash
docker run -d \
  --name toast-audio-sidecar \
  --restart unless-stopped \
  -p 3169:3107 \
  --env-file .env \
  -v toast-audio-data:/app/data \
  ghcr.io/qwertyuiop8899/toastflix-sidecar:latest
```

## Offset Audio

Il sidecar mantiene una cache offset locale.

Se vuoi salvare e recuperare gli offset anche dalla VPS ToastFlix, imposta nel
`.env`:

```env
OFFSET_API_URL=https://noprox.stremio-italia.eu/dual/offset
```

Il sidecar invia alla VPS solo metadati:

- titolo e risoluzione;
- fingerprint video/audio;
- offset, rate e confidence.

Non invia alla VPS segmenti audio, chiavi AES o file convertiti.

## Aggiornare L'immagine

Quando aggiorni il codice su GitHub:

```bash
docker compose pull
docker compose up -d
```

Controlla poi:

```bash
docker compose logs --tail=100
curl http://IP_DELLA_MACCHINA:3169/health
```
