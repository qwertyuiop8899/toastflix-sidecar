import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from audio import AudioStore
from sync import SyncEngine


class FakeAudio:
    def __init__(self, root: Path):
        self.root = root
        self.hid = "a" * 16
        self.directory = root / self.hid
        self.directory.mkdir()
        (self.directory / "enc.key").write_bytes(b"0" * 16)

    def metadata(self, hid):
        self.assert_hid(hid)
        durs = [4.0, 4.5, 3.75]
        return {
            "segs": [f"https://cdn.example.test/{index}.ts" for index in range(len(durs))],
            "durs": durs,
            "starts": [0.0, 4.0, 8.5],
            "iv": None,
            "headers": {},
        }

    def _dir(self, hid):
        self.assert_hid(hid)
        return self.directory

    def assert_hid(self, hid):
        if hid != self.hid:
            raise AssertionError("unexpected audio id")


class FakeOffsets:
    def __init__(self):
        self.lookup_payload = None

    @staticmethod
    def key(media_key, resolution, video_fp, audio_fp):
        return "cache-key"

    async def lookup(self, payload):
        self.lookup_payload = payload
        return {"details": {"offset": 0.0, "rate": 1.0, "confidence": 1.0}}


class SyncPlaylistTests(unittest.IsolatedAsyncioTestCase):
    def test_cached_audio_requires_fresh_signed_segments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = AudioStore(str(root))
            fresh = int(time.time()) + 3600
            stale = int(time.time()) - 1
            base = root / ("a" * 16)
            base.mkdir()
            self.assertTrue(audio._cache_is_fresh({
                "segs": [f"https://cdn.example.test/0.ts?expires={fresh}"],
            }))
            self.assertFalse(audio._cache_is_fresh({
                "segs": [f"https://cdn.example.test/0.ts?expires={stale}"],
            }))
            self.assertFalse(audio._cache_is_fresh({
                "segs": ["https://cdn.example.test/0.ts"],
            }))

    async def test_measure_accepts_reference_audio_url(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = FakeAudio(Path(directory))
            offsets = FakeOffsets()
            offsets.lookup = AsyncMock(return_value=None)
            engine = SyncEngine(audio, offsets)
            audio.metadata = lambda hid: {
                "durs": [100.0],
                "starts": [0.0],
            }
            engine._video_entries = AsyncMock(return_value=(
                [{"url": "https://cdn.example.test/video.m4s", "duration": 100.0, "start": 0.0}],
                None,
            ))
            engine._decode_reference_audio = AsyncMock(return_value=(Path(directory) / "reference.m3u8", 0.0, 100.0))
            engine._media_start_time = AsyncMock(return_value=0.0)
            engine._decode_audio = AsyncMock(return_value=(Path(directory) / "audio.m3u8", 0.0, 100.0))
            engine._pcm = AsyncMock()
            engine._envelope = lambda path: [0.0] * 500
            payload = {
                "media_key": "movie:reference:0:0",
                "resolution": 1080,
                "video_url": "https://video.example.test/video.m3u8",
                "reference_audio_url": "https://audio.example.test/reference.m3u8",
                "video_fingerprint": "video-fp",
                "audio_fingerprint": "audio-fp",
                "audio_hid": audio.hid,
            }

            result = await engine.measure(payload)

            self.assertEqual(result["status"], "incompatible")
            engine._decode_reference_audio.assert_awaited()
            engine._decode_video = AsyncMock(side_effect=AssertionError("video stream should not be decoded"))

    async def test_reference_audio_offset_includes_video_start_time(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = FakeAudio(Path(directory))
            offsets = FakeOffsets()
            offsets.lookup = AsyncMock(return_value=None)
            engine = SyncEngine(audio, offsets)
            audio.metadata = lambda hid: {"durs": [100.0], "starts": [0.0]}
            engine._video_entries = AsyncMock(return_value=(
                [{"url": "https://video.example.test/0.m4s", "duration": 100.0, "start": 0.0}],
                None,
            ))
            engine._media_start_time = AsyncMock(return_value=10.125)
            engine._decode_reference_audio = AsyncMock(return_value=(Path(directory) / "reference.m3u8", 0.0, 100.0))
            engine._decode_audio = AsyncMock(return_value=(Path(directory) / "audio.m3u8", 0.0, 100.0))
            engine._pcm = AsyncMock()
            engine._envelope = lambda path: [0.0] * 500
            engine._lag = lambda reference, candidate: (0.0, 0.9)
            payload = {
                "media_key": "movie:video-start:0:0",
                "resolution": 2160,
                "video_url": "https://video.example.test/video.m3u8",
                "reference_audio_url": "https://audio.example.test/reference.m3u8",
                "video_fingerprint": "video-fp",
                "audio_fingerprint": "audio-fp",
                "audio_hid": audio.hid,
            }

            result = await engine.measure(payload)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["offset"], 10.125)
            self.assertEqual(result["video_start_time"], 10.125)
            engine._media_start_time.assert_awaited_once()

    async def test_fragment_input_playlist_declares_target_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            hid = "b" * 16
            audio = AudioStore(str(root))
            track = root / hid
            track.mkdir()
            (track / "enc.key").write_bytes(b"0" * 16)
            (track / "meta.json").write_text("{" +
                "\"segs\":[\"https://cdn.example.test/0.ts\"]," +
                "\"durs\":[8.0],\"starts\":[0.0],\"iv\":null," +
                "\"headers\":{},\"media_key\":\"test\",\"language\":\"ita\"}")

            async def download(url, headers):
                return b"segment"

            async def process(self):
                work = Path(self._workdir)
                captured["playlist"] = (work / "input.m3u8").read_text()
                (work / "f0.m4s").write_bytes(b"fragment")
                (work / "init.mp4").write_bytes(b"init")
                self.returncode = 0
                return b"", b""

            class Process:
                returncode = 0
                _workdir = ""

                async def communicate(self):
                    return await process(self)

            captured = {}

            async def spawn(*command, cwd, **kwargs):
                process_instance = Process()
                process_instance._workdir = cwd
                captured["command"] = list(command)
                return process_instance

            audio._download = download
            with patch("audio.asyncio.create_subprocess_exec", new=spawn) as start:
                with patch.object(audio, "_patch_time"):
                    with patch.object(audio, "_find_box", return_value=None):
                        await audio.fragment(hid, 0, -1.0, 1.0)

            command = captured["command"]
            self.assertIn("#EXT-X-TARGETDURATION:9", captured["playlist"])
            self.assertLess(command.index("-i"), command.index("-ss"))

    async def test_measure_exposes_cache_key_for_offset_report(self):
        with tempfile.TemporaryDirectory() as directory:
            audio = FakeAudio(Path(directory))
            offsets = FakeOffsets()
            payload = {
                "media_key": "movie:test:0:0",
                "resolution": 2160,
                "video_fingerprint": "video-fp",
                "audio_fingerprint": "audio-fp",
                "audio_hid": audio.hid,
            }

            result = await SyncEngine(audio, offsets).measure(payload)

            self.assertEqual(payload["cache_key"], "cache-key")
            self.assertEqual(offsets.lookup_payload["cache_key"], "cache-key")
            self.assertEqual(result["cache_key"], "cache-key")

    async def test_audio_sample_playlist_declares_target_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = FakeAudio(root)
            engine = SyncEngine(audio, None)
            sample_dir = root / "sample"
            sample_dir.mkdir()

            async def download(url, path, headers):
                path.write_bytes(b"segment")

            engine._download = download
            playlist, _, _ = await engine._decode_audio(audio.hid, 0.0, sample_dir)

            self.assertIn("#EXT-X-TARGETDURATION:5", playlist.read_text())

    async def test_pcm_seeks_after_input_and_disables_video(self):
        with tempfile.TemporaryDirectory() as directory:
            playlist = Path(directory) / "sample.m3u8"
            output = Path(directory) / "sample.pcm"
            playlist.write_text("#EXTM3U\n")

            class Process:
                returncode = 0

                async def communicate(self):
                    output.write_bytes(b"0" * 160000)
                    return b"", b""

            with patch("sync.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=Process()) as spawn:
                await SyncEngine._pcm(playlist, 12.5, output)

            command = list(spawn.call_args.args)
            self.assertLess(command.index("-i"), command.index("-ss"))
            self.assertIn("-vn", command)


if __name__ == "__main__":
    unittest.main()
