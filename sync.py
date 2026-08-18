import asyncio
import math
import os
import statistics
import tempfile
from array import array
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from audio import AudioStore
from offsets import OffsetStore
from security import resolves_publicly, valid_public_url


class SyncEngine:
    def __init__(self, audio: AudioStore, offsets: OffsetStore, proxy: str = ""):
        self.audio = audio
        self.offsets = offsets
        self.proxy = proxy
        self.sample_seconds = 20

    async def _get(self, url: str, headers: dict):
        if not valid_public_url(url) or not await resolves_publicly(url):
            raise ValueError("media URL is not public HTTPS")
        kwargs = {"timeout": 30, "follow_redirects": False}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(url, headers=headers)
        if response.status_code in (301, 302, 307, 308):
            location = response.headers.get("location", "")
            if not await resolves_publicly(urljoin(url, location)):
                raise ValueError("media redirect is not public HTTPS")
            return await self._get(urljoin(url, location), headers)
        response.raise_for_status()
        return response

    @staticmethod
    def _playlist(text: str, master_url: str):
        entries, pending, elapsed = [], None, 0.0
        map_url = None
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("#EXT-X-MAP:"):
                import re
                match = re.search(r'URI="([^"]+)"', line)
                map_url = urljoin(master_url, match.group(1)) if match else None
            elif line.startswith("#EXTINF:"):
                pending = float(line.split(":", 1)[1].split(",", 1)[0])
            elif pending is not None and line and not line.startswith("#"):
                entries.append({"url": urljoin(master_url, line), "duration": pending, "start": elapsed})
                elapsed += pending
                pending = None
        if not entries:
            raise ValueError("empty media playlist")
        return entries, map_url

    async def _video_entries(self, url: str, headers: dict):
        response = await self._get(url, headers)
        return self._playlist(response.text, url)

    async def _download(self, url: str, path: Path, headers: dict):
        response = await self._get(url, headers)
        path.write_bytes(response.content)

    async def _decode_video(self, url: str, headers: dict, position: float, directory: Path):
        _, entries, map_url = (url, *await self._video_entries(url, headers))
        target = next((i for i, item in enumerate(entries) if item["start"] <= position < item["start"] + item["duration"]), len(entries) - 1)
        first = max(0, target - 1)
        local_seek = max(0.0, position - entries[first]["start"])
        selected, available = [], 0.0
        for item in entries[first:]:
            selected.append(item)
            available += item["duration"]
            if available >= local_seek + self.sample_seconds + 5:
                break
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD", f"#EXT-X-TARGETDURATION:{int(max(x['duration'] for x in selected)) + 1}"]
        if map_url:
            await self._download(map_url, directory / "video-init.mp4", headers)
            lines.append('#EXT-X-MAP:URI="video-init.mp4"')
        for number, item in enumerate(selected):
            name = f"video-{number}.m4s"
            await self._download(item["url"], directory / name, headers)
            lines += [f"#EXTINF:{item['duration']:.6f},", name]
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "video.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, sum(item["duration"] for item in entries)

    async def _decode_audio(self, hid: str, position: float, directory: Path):
        metadata = self.audio.metadata(hid)
        index = next((i for i, start in enumerate(metadata["starts"]) if start <= position < start + metadata["durs"][i]), len(metadata["segs"]) - 1)
        first = max(0, index - 1)
        local_seek = max(0.0, position - metadata["starts"][first])
        selected = range(first, min(len(metadata["segs"]), index + 5))
        iv = f",IV={metadata['iv']}" if metadata.get("iv") else ""
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-PLAYLIST-TYPE:VOD",
                 f"#EXT-X-TARGETDURATION:{int(max(metadata['durs'][item] for item in selected)) + 1}",
                 f'#EXT-X-KEY:METHOD=AES-128,URI="audio.key"{iv}']
        (directory / "audio.key").write_bytes((self.audio._dir(hid) / "enc.key").read_bytes())
        for number, item in enumerate(selected):
            name = f"audio-{number}.ts"
            await self._download(metadata["segs"][item], directory / name, metadata.get("headers") or {})
            lines += [f"#EXTINF:{metadata['durs'][item]:.6f},", name]
        lines.append("#EXT-X-ENDLIST")
        playlist = directory / "audio.m3u8"
        playlist.write_text("\n".join(lines) + "\n")
        return playlist, local_seek, sum(metadata["durs"])

    @staticmethod
    async def _pcm(playlist: Path, seek: float, output: Path, audio_map: bool = True):
        command = ["ffmpeg", "-v", "error", "-allowed_extensions", "ALL", "-protocol_whitelist", "file,crypto", "-i", str(playlist), "-ss", f"{max(0.0, seek):.3f}", "-t", "20"]
        if audio_map:
            command += ["-map", "0:a:0", "-vn"]
        command += ["-ac", "1", "-ar", "8000", "-f", "s16le", "-y", str(output)]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, error = await asyncio.wait_for(process.communicate(), timeout=60)
        if process.returncode or not output.exists() or output.stat().st_size < 160000:
            raise RuntimeError((error.decode(errors="replace") or "sample decode failed")[:300])

    @staticmethod
    def _envelope(path: Path):
        values = array("h")
        values.frombytes(path.read_bytes())
        if not values:
            return []
        if os.sys.byteorder != "little":
            values.byteswap()
        step, window = 80, 160
        prefix = [0.0]
        for value in values:
            prefix.append(prefix[-1] + abs(value))
        envelope = []
        for center in range(0, len(values), step):
            lo, hi = max(0, center - window // 2), min(len(values), center + window // 2)
            envelope.append((prefix[hi] - prefix[lo]) / max(1, hi - lo))
        mean = sum(envelope) / len(envelope)
        std = math.sqrt(sum((value - mean) ** 2 for value in envelope) / len(envelope)) or 1.0
        return [(value - mean) / std for value in envelope]

    @staticmethod
    def _lag(reference, candidate, max_seconds=5):
        best = (-2.0, 0)
        for lag in range(-max_seconds * 100, max_seconds * 100 + 1):
            if lag >= 0:
                size = min(len(reference), len(candidate) - lag)
                left, right = reference[:size], candidate[lag:lag + size]
            else:
                size = min(len(candidate), len(reference) + lag)
                left, right = reference[-lag:-lag + size], candidate[:size]
            if size < 500:
                continue
            lm, rm = sum(left) / size, sum(right) / size
            lv = sum((value - lm) ** 2 for value in left)
            rv = sum((value - rm) ** 2 for value in right)
            denominator = math.sqrt(lv * rv)
            if denominator:
                correlation = sum((left[i] - lm) * (right[i] - rm) for i in range(size)) / denominator
                if correlation > best[0]:
                    best = correlation, lag
        return best[1] / 100.0, best[0]

    async def measure(self, payload: dict):
        media_key = str(payload.get("media_key") or "")
        resolution = int(payload.get("resolution") or 0)
        video_url = str(payload.get("video_url") or "")
        video_headers = payload.get("video_headers") if isinstance(payload.get("video_headers"), dict) else {}
        audio_hid = str(payload.get("audio_hid") or "")
        video_fp = str(payload.get("video_fingerprint") or "")
        metadata = self.audio.metadata(audio_hid)
        audio_fp = str(payload.get("audio_fingerprint") or metadata.get("source_fingerprint") or "")
        cache_key = self.offsets.key(media_key, resolution, video_fp, audio_fp)
        payload["cache_key"] = cache_key
        lookup = await self.offsets.lookup({"cache_key": cache_key, "media_key": media_key, "resolution": resolution, "video_fingerprint": video_fp, "audio_fingerprint": audio_fp, "vpsAccess": payload.get("vpsAccess", "")})
        if lookup:
            result = {"status": "ok", "cached": True, **(lookup.get("details") or lookup)}
            result["cache_key"] = cache_key
            return result
        video_entries, _ = await self._video_entries(video_url, video_headers)
        video_duration = sum(item["duration"] for item in video_entries)
        audio_duration = sum(metadata["durs"])
        common = min(video_duration, audio_duration)
        if common < 90:
            raise ValueError("media too short")
        positions = sorted({min(60.0, common * .1), common * .5, max(30.0, common - 90.0)})
        measurements = []
        with tempfile.TemporaryDirectory(prefix="sidecar-sync-") as directory:
            root = Path(directory)
            for index, position in enumerate(positions):
                video_dir, audio_dir = root / f"video-{index}", root / f"audio-{index}"
                video_dir.mkdir(), audio_dir.mkdir()
                video_playlist, video_seek, _ = await self._decode_video(video_url, video_headers, position, video_dir)
                audio_playlist, audio_seek, _ = await self._decode_audio(audio_hid, position, audio_dir)
                video_pcm, audio_pcm = root / f"video-{index}.pcm", root / f"audio-{index}.pcm"
                await asyncio.gather(self._pcm(video_playlist, video_seek, video_pcm), self._pcm(audio_playlist, audio_seek, audio_pcm))
                lag, correlation = self._lag(self._envelope(video_pcm), self._envelope(audio_pcm))
                measurements.append({"position": position, "lag": lag, "offset": lag, "correlation": correlation})
        valid = [item for item in measurements if item["correlation"] >= .75]
        if len(valid) < 2:
            result = {"status": "incompatible", "video_duration": video_duration, "audio_duration": audio_duration, "measurements": measurements}
        else:
            measured = statistics.median(item["offset"] for item in valid)
            deviation = max(abs(item["offset"] - measured) for item in valid)
            result = {"status": "ok" if deviation <= .25 else "incompatible", "offset": round(-measured, 3), "rate": 1.0, "confidence": min(item["correlation"] for item in valid), "deviation": deviation, "sync_mode": "constant", "video_duration": video_duration, "audio_duration": audio_duration, "measurements": measurements}
        result["cache_key"] = cache_key
        return result
