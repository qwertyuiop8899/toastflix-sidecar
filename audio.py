import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from security import valid_public_url


class AudioStore:
    def __init__(self, root: str, proxy: str = "", max_bytes: int = 10 * 1024**3):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.proxy = proxy.strip()
        self.max_bytes = max_bytes
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, hid: str):
        return self._locks.setdefault(hid, asyncio.Lock())

    def _dir(self, hid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{16}", hid):
            raise ValueError("invalid audio id")
        return self.root / hid

    @staticmethod
    def _parse_playlist(playlist: str, base_url: str = ""):
        segments, durations = [], []
        pending = None
        key_line = ""
        for raw in playlist.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-KEY:"):
                key_line = line
            elif line.startswith("#EXTINF:"):
                pending = float(line.split(":", 1)[1].split(",", 1)[0])
            elif pending is not None and line and not line.startswith("#"):
                segments.append(urljoin(base_url, line))
                durations.append(pending)
                pending = None
        return segments, durations, key_line

    @staticmethod
    def _validate_segments(segments):
        if not segments or len(segments) > 10000:
            raise ValueError("invalid audio segment count")
        if not all(valid_public_url(url) for url in segments):
            raise ValueError("audio segment URL is not public HTTPS")

    async def register(self, playlist: str, key_b64: str, media_key: str,
                       language: str, base_url: str = "", headers: dict | None = None):
        if len(playlist) > 2 * 1024 * 1024 or "#EXTINF" not in playlist:
            raise ValueError("invalid audio playlist")
        language = str(language or "").lower().strip()
        if language not in {"ita", "eng", "it", "en", "italian", "english"}:
            raise ValueError("language must be ita or eng")
        segments, durations, key_line = self._parse_playlist(playlist, base_url)
        self._validate_segments(segments)
        if len(durations) < len(segments):
            raise ValueError("audio durations do not match segments")
        key = base64.b64decode(key_b64, validate=True)
        if len(key) != 16:
            raise ValueError("AES key must be 16 bytes")
        safe_headers = {
            str(name): str(value).strip()
            for name, value in (headers or {}).items()
            if re.fullmatch(r"[A-Za-z0-9-]+", str(name))
            and str(value).strip()
            and len(str(value).strip()) <= 1024
            and "\r" not in str(value)
            and "\n" not in str(value)
        }
        stable = [urlparse(url).path for url in segments[:3]]
        hid = hashlib.sha1(("|".join(stable) + str(len(segments)) + language).encode()).hexdigest()[:16]
        directory = self._dir(hid)
        async with self._lock(hid):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "enc.key").write_bytes(key)
            starts, total = [], 0.0
            for duration in durations[:len(segments)]:
                starts.append(total)
                total += duration
            metadata = {
                "segs": segments,
                "durs": durations[:len(segments)],
                "starts": starts,
                "iv": (re.search(r"IV=(0x[0-9A-Fa-f]+)", key_line) or [None, None])[1],
                "media_key": str(media_key or ""),
                "language": language,
                "headers": safe_headers,
                "source_fingerprint": hashlib.sha1(
                    ("|".join(stable) + str(len(segments))).encode()
                ).hexdigest()[:20],
            }
            (directory / "meta.json").write_text(json.dumps(metadata), encoding="utf-8")
        return hid

    def metadata(self, hid: str):
        path = self._dir(hid) / "meta.json"
        if not path.exists():
            raise FileNotFoundError("audio track not found")
        return json.loads(path.read_text(encoding="utf-8"))

    def find_cached(self, media_key: str, language: str):
        candidates = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or not re.fullmatch(r"[0-9a-f]{16}", directory.name):
                continue
            try:
                metadata = self.metadata(directory.name)
                if metadata.get("media_key") == media_key and metadata.get("language") in {language, "it" if language == "ita" else "en"}:
                    candidates.append((directory.stat().st_mtime, directory.name))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return max(candidates)[1] if candidates else None

    @staticmethod
    def timeline(metadata: dict, offset: float = 0.0, rate: float = 1.0):
        if not 0.998 <= rate <= 1.002:
            raise ValueError("audio rate outside supported range")
        entries = []
        for index, (start, duration) in enumerate(zip(metadata["starts"], metadata["durs"])):
            shifted = float(start) + float(offset)
            trim = max(0.0, -shifted)
            if trim >= float(duration) - 0.02:
                continue
            entries.append({"idx": index, "start": max(0.0, shifted / rate), "trim": trim, "duration": (float(duration) - trim) / rate})
        return entries

    async def _download(self, url: str, headers: dict):
        if not valid_public_url(url):
            raise ValueError("audio URL is not public HTTPS")
        kwargs = {"timeout": 30, "follow_redirects": True}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            if len(response.content) > 20 * 1024 * 1024:
                raise ValueError("audio segment too large")
            return response.content

    @staticmethod
    def _boxes(data: bytes, start=0, end=None):
        end = len(data) if end is None else end
        result, position = [], start
        while position + 8 <= end:
            size = struct.unpack(">I", data[position:position + 4])[0]
            kind = data[position + 4:position + 8]
            header = 8
            if size == 1:
                size = struct.unpack(">Q", data[position + 8:position + 16])[0]
                header = 16
            elif size == 0:
                size = end - position
            if size < header or position + size > end:
                break
            result.append((kind, position, size, header))
            position += size
        return result

    @classmethod
    def _find_box(cls, data, path, start=0, end=None):
        for kind, position, size, header in cls._boxes(data, start, end):
            if kind != path[0]:
                continue
            if len(path) == 1:
                return position, size, header
            found = cls._find_box(data, path[1:], position + header, position + size)
            if found:
                return found
        return None

    @classmethod
    def _patch_time(cls, fragment: bytearray, timescale: int, start_seconds: float):
        sidx = cls._find_box(bytes(fragment), [b"sidx"])
        if sidx:
            position, _, header = sidx
            version = fragment[position + header]
            sidx_timescale = struct.unpack(
                ">I", fragment[position + header + 8:position + header + 12]
            )[0]
            value = int(round(start_seconds * sidx_timescale))
            value_position = position + header + 12
            if version == 1:
                struct.pack_into(">Q", fragment, value_position, value)
            else:
                struct.pack_into(">I", fragment, value_position, value)
        found = cls._find_box(bytes(fragment), [b"moof", b"traf", b"tfdt"])
        if not found:
            raise ValueError("tfdt box missing")
        position, _, header = found
        version = fragment[position + header]
        value = int(round(start_seconds * timescale))
        if version == 1:
            struct.pack_into(">Q", fragment, position + header + 4, value)
        else:
            struct.pack_into(">I", fragment, position + header + 4, value)

    async def fragment(self, hid: str, index: int, offset: float, rate: float):
        metadata = self.metadata(hid)
        timeline = self.timeline(metadata, offset, rate)
        item = next((entry for entry in timeline if entry["idx"] == index), None)
        if not item:
            raise ValueError("audio segment outside timeline")
        directory = self._dir(hid)
        suffix = f"{int(round(offset * 1000)):+d}_r{int(round(rate * 1e9))}"
        init_path = directory / f"init_{suffix}.mp4"
        fragment_path = directory / f"s{index}_{suffix}.m4s"
        if init_path.exists() and fragment_path.exists():
            return init_path, fragment_path
        async with self._lock(hid):
            if init_path.exists() and fragment_path.exists():
                return init_path, fragment_path
            work = Path(tempfile.mkdtemp(prefix=f"work-{index}-", dir=directory))
            try:
                source = await self._download(metadata["segs"][index], metadata.get("headers") or {})
                (work / "src.ts").write_bytes(source)
                (work / "enc.key").write_bytes((directory / "enc.key").read_bytes())
                iv = f",IV={metadata['iv']}" if metadata.get("iv") else ""
                (work / "input.m3u8").write_text(
                    "#EXTM3U\n#EXT-X-VERSION:3\n"
                    f"#EXT-X-TARGETDURATION:{int(metadata['durs'][index]) + 1}\n"
                    f"#EXT-X-KEY:METHOD=AES-128,URI=\"enc.key\"{iv}\n"
                    f"#EXTINF:{metadata['durs'][index]:.6f},\nsrc.ts\n#EXT-X-ENDLIST\n"
                )
                command = ["ffmpeg", "-v", "error", "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto", "-i", "input.m3u8"]
                if item["trim"] > 0.001:
                    command += ["-ss", f"{item['trim']:.6f}"]
                command += ["-c:a", "copy", "-bsf:a", "aac_adtstoasc", "-f", "hls", "-hls_time", "99999", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4", "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "f%d.m4s", "-hls_list_size", "0", "-y", "output.m3u8"]
                process = await asyncio.create_subprocess_exec(*command, cwd=work, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, error = await asyncio.wait_for(process.communicate(), timeout=60)
                made = work / "f0.m4s"
                if process.returncode or not made.exists():
                    raise RuntimeError((error.decode(errors="replace") or "ffmpeg failed")[:300])
                if not init_path.exists():
                    shutil.copy2(work / "init.mp4", init_path)
                fragment = bytearray(made.read_bytes())
                timescale = 48000
                found = self._find_box(init_path.read_bytes(), [b"moov", b"trak", b"mdia", b"mdhd"])
                if found:
                    data = init_path.read_bytes()
                    position, _, header = found
                    version = data[position + header]
                    offset_pos = position + header + (20 if version == 1 else 12)
                    timescale = struct.unpack(">I", data[offset_pos:offset_pos + 4])[0] or 48000
                self._patch_time(fragment, timescale, item["start"])
                fragment_path.write_bytes(fragment)
                return init_path, fragment_path
            finally:
                shutil.rmtree(work, ignore_errors=True)
