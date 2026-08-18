import json
import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from audio import AudioStore
from offsets import OffsetStore
from security import SessionManager, request_token, resolves_publicly
from sync import SyncEngine


APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = Path(os.getenv("SIDECAR_CACHE_DIR", APP_DIR / "data"))
PUBLIC_BASE_URL = os.getenv("SIDECAR_PUBLIC_URL", "").strip().rstrip("/")
SESSION_TTL = int(os.getenv("SIDECAR_SESSION_TTL", "21600"))
FIXED_TOKEN = os.getenv("SIDECAR_FIXED_TOKEN", "").strip()
AUDIO_PROXY = os.getenv("SIDECAR_AUDIO_PROXY", "").strip()
OFFSET_API_URL = os.getenv("OFFSET_API_URL", "").strip()
OFFSET_API_TOKEN = os.getenv("OFFSET_API_TOKEN", "").strip()
BOOTSTRAP_KEY = os.getenv("SIDECAR_BOOTSTRAP_KEY", "").strip()

audio = AudioStore(str(CACHE_DIR / "audio"), proxy=AUDIO_PROXY)
offsets = OffsetStore(str(CACHE_DIR / "offsets.db"), OFFSET_API_URL, OFFSET_API_TOKEN)
sessions = SessionManager(SESSION_TTL, FIXED_TOKEN)
sync_engine = SyncEngine(audio, offsets, AUDIO_PROXY)

app = FastAPI(title="Toast Audio Sidecar", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.getenv("CORS_ORIGINS", "*").split(",") if item.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def _require_session(request: Request, body: dict | None = None) -> str:
    token = request_token(request, body)
    if not sessions.valid(token):
        raise HTTPException(status_code=401, detail="sidecar session required")
    return token


def _base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or str(request.base_url).rstrip("/")


def _audio_url(request: Request, hid: str, token: str, offset: float = 0.0, rate: float = 1.0) -> str:
    query = urlencode({"o": int(round(offset * 1000)), "r": int(round(rate * 1_000_000_000)), "t": token})
    return f"{_base_url(request)}/dual/aud/{hid}/audio.m3u8?{query}"


@app.get("/health")
async def health():
    return {"status": "ok", "service": "toast-audio-sidecar", "public_url": PUBLIC_BASE_URL or None}


@app.post("/session")
async def create_session(request: Request):
    if BOOTSTRAP_KEY:
        supplied = request.headers.get("x-sidecar-bootstrap", "")
        if supplied != BOOTSTRAP_KEY:
            raise HTTPException(status_code=401, detail="bootstrap key required")
    sessions.cleanup()
    token, expires_at = sessions.issue()
    return {"token": token, "expires_at": expires_at, "ttl_seconds": sessions.ttl_seconds}


@app.post("/dual/aprep")
async def prepare_audio(request: Request):
    body = await request.json()
    token = _require_session(request, body)
    try:
        hid = await audio.register(
            playlist=str(body.get("playlist") or ""),
            key_b64=str(body.get("key") or ""),
            media_key=str(body.get("mediaKey") or ""),
            language=str(body.get("lang") or ""),
            base_url=str(body.get("baseUrl") or ""),
            headers=body.get("headers") if isinstance(body.get("headers"), dict) else {},
        )
        metadata = audio.metadata(hid)
        for url in (metadata["segs"][0], metadata["segs"][-1]):
            if not await resolves_publicly(url):
                raise ValueError("audio source does not resolve publicly")
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    language = str(body.get("lang") or "").lower()
    metadata = audio.metadata(hid)
    return JSONResponse({
        "hid": hid,
        "url": _audio_url(request, hid, token),
        "language": language,
        "audio_fingerprint": metadata.get("source_fingerprint", ""),
    })


@app.post("/dual/acache")
async def cached_audio(request: Request):
    body = await request.json()
    token = _require_session(request, body)
    hid = audio.find_cached(str(body.get("mediaKey") or ""), str(body.get("lang") or "").lower())
    if not hid:
        raise HTTPException(status_code=404, detail="valid cached audio track not found")
    metadata = audio.metadata(hid)
    return {
        "url": _audio_url(request, hid, token),
        "cached": True,
        "hid": hid,
        "audio_fingerprint": metadata.get("source_fingerprint", ""),
    }


def _audio_response(path: Path, media_type: str, cache_control: str = "no-cache"):
    return FileResponse(path, media_type=media_type, headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": cache_control,
        "Accept-Ranges": "bytes",
    })


@app.get("/dual/aud/{hid}/audio.m3u8")
async def audio_playlist(hid: str, request: Request, o: int = 0, r: int = 1_000_000_000):
    token = _require_session(request)
    try:
        metadata = audio.metadata(hid)
        offset, rate = o / 1000.0, r / 1_000_000_000
        timeline = audio.timeline(metadata, offset, rate)
        if not timeline:
            raise ValueError("empty audio timeline")
        base = _base_url(request)
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(item['duration'] for item in timeline)) + 1}",
                 "#EXT-X-MEDIA-SEQUENCE:0",
                 f'#EXT-X-MAP:URI="{base}/dual/aud/{hid}/init.mp4?{urlencode({"o": o, "r": r, "t": token})}"']
        for item in timeline:
            query = urlencode({"o": o, "r": r, "t": token})
            lines += [f"#EXTINF:{item['duration']:.6f},", f"{base}/dual/aud/{hid}/s{item['idx']}.m4s?{query}"]
        lines.append("#EXT-X-ENDLIST")
        return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/dual/aud/{hid}/init.mp4")
async def audio_init(hid: str, request: Request, o: int = 0, r: int = 1_000_000_000):
    _require_session(request)
    try:
        metadata = audio.metadata(hid)
        timeline = audio.timeline(metadata, o / 1000.0, r / 1_000_000_000)
        if not timeline:
            raise ValueError("empty audio timeline")
        init_path, _ = await audio.fragment(hid, timeline[0]["idx"], o / 1000.0, r / 1_000_000_000)
        return _audio_response(init_path, "video/mp4", "public, max-age=3600")
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/dual/aud/{hid}/s{idx}.m4s")
async def audio_segment(hid: str, idx: int, request: Request, o: int = 0, r: int = 1_000_000_000):
    _require_session(request)
    try:
        _, fragment_path = await audio.fragment(hid, idx, o / 1000.0, r / 1_000_000_000)
        return _audio_response(fragment_path, "video/iso.segment", "public, max-age=3600")
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/offset/lookup")
async def offset_lookup(request: Request):
    body = await request.json()
    _require_session(request, body)
    result = await offsets.lookup(body)
    return {"found": bool(result), "offset": result}


@app.post("/offset/report")
async def offset_report(request: Request):
    body = await request.json()
    _require_session(request, body)
    result = body.get("offset")
    if not isinstance(result, dict):
        raise HTTPException(status_code=400, detail="offset result required")
    await offsets.report(body, result)
    return {"ok": True}


@app.post("/sync")
async def sync_audio(request: Request):
    body = await request.json()
    _require_session(request, body)
    try:
        result = await sync_engine.measure(body)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    await offsets.report(body, result)
    return result
