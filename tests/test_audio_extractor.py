import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import audio_extractor
from cancellation import OperationCancelledError


class _FakeProcess:
    def __init__(self, stderr_lines: list[str], returncode: int = 0, output_path: Path | None = None):
        self.stderr = io.StringIO("".join(line + "\n" for line in stderr_lines))
        self.returncode = returncode
        self._output_path = output_path
        self._terminated = False
        self._killed = False

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._killed = True

    def wait(self, timeout: float | None = None):
        # output 이 있으면 ffmpeg 가 정상 종료된 것으로 간주하여 결과 파일 생성
        if self._output_path is not None and self.returncode == 0:
            self._output_path.write_bytes(b"FAKE-WAV")


class ParseHelperTests(unittest.TestCase):
    def test_parse_duration_extracts_seconds(self):
        self.assertEqual(
            audio_extractor._parse_duration("  Duration: 01:02:03.45, start: 0.000"),
            3723.45,
        )

    def test_parse_duration_returns_none_on_no_match(self):
        self.assertIsNone(audio_extractor._parse_duration("Stream #0:0 ..."))

    def test_parse_time_extracts_progress(self):
        self.assertAlmostEqual(
            audio_extractor._parse_time("frame=1234 fps=30 q=-1.0 size= time=00:01:30.50 bitrate="),
            90.5,
        )


class FindFfmpegTests(unittest.TestCase):
    def test_find_ffmpeg_returns_first_existing_search_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "ffmpeg"
            fake.write_text("#!/bin/sh\nexit 0\n")
            with patch.object(audio_extractor, "FFMPEG_SEARCH_PATHS", [str(fake)]):
                self.assertEqual(audio_extractor.find_ffmpeg(), str(fake))

    def test_find_ffmpeg_falls_back_to_shutil_which(self):
        with patch.object(audio_extractor, "FFMPEG_SEARCH_PATHS", ["/nonexistent/path"]), \
             patch("audio_extractor.shutil.which", return_value="/opt/dummy/ffmpeg"):
            self.assertEqual(audio_extractor.find_ffmpeg(), "/opt/dummy/ffmpeg")

    def test_check_ffmpeg_reflects_find_result(self):
        with patch.object(audio_extractor, "find_ffmpeg", return_value=None):
            self.assertFalse(audio_extractor.check_ffmpeg())
        with patch.object(audio_extractor, "find_ffmpeg", return_value="/opt/ffmpeg"):
            self.assertTrue(audio_extractor.check_ffmpeg())


class ExtractAudioTests(unittest.TestCase):
    def _setup_input(self, tmp: str) -> tuple[Path, Path]:
        mp4 = Path(tmp) / "demo.mp4"
        mp4.write_bytes(b"FAKE-MP4")
        out = Path(tmp) / "out" / "audio.wav"
        return mp4, out

    def test_extract_audio_raises_when_ffmpeg_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4, out = self._setup_input(tmp)
            with patch.object(audio_extractor, "find_ffmpeg", return_value=None):
                with self.assertRaisesRegex(EnvironmentError, "ffmpeg"):
                    audio_extractor.extract_audio(str(mp4), str(out))

    def test_extract_audio_raises_when_input_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "no.mp4"
            out = Path(tmp) / "audio.wav"
            with patch.object(audio_extractor, "find_ffmpeg", return_value="/opt/ffmpeg"):
                with self.assertRaisesRegex(FileNotFoundError, "입력 파일을 찾을 수 없습니다"):
                    audio_extractor.extract_audio(str(missing), str(out))

    def test_extract_audio_invokes_progress_callback_with_percentage(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4, out = self._setup_input(tmp)
            stderr_lines = [
                "Duration: 00:00:10.00, start: 0.000",
                "frame=1 fps=30 time=00:00:05.00 bitrate=",
                "frame=2 fps=30 time=00:00:09.00 bitrate=",
            ]
            fake_process = _FakeProcess(stderr_lines, returncode=0, output_path=out)
            statuses: list[str] = []

            with patch.object(audio_extractor, "find_ffmpeg", return_value="/opt/ffmpeg"), \
                 patch.object(audio_extractor.subprocess, "Popen", return_value=fake_process):
                result = audio_extractor.extract_audio(
                    str(mp4),
                    str(out),
                    progress_callback=statuses.append,
                )

            self.assertEqual(result, str(out))
            # 진행률이 한 번 이상 보고되고, 마지막엔 100%
            joined = "\n".join(statuses)
            self.assertIn("음성 추출 중", joined)
            self.assertIn("100%", joined)
            self.assertTrue(out.exists())

    def test_extract_audio_propagates_nonzero_returncode(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4, out = self._setup_input(tmp)
            fake_process = _FakeProcess(["something failed"], returncode=1)

            with patch.object(audio_extractor, "find_ffmpeg", return_value="/opt/ffmpeg"), \
                 patch.object(audio_extractor.subprocess, "Popen", return_value=fake_process):
                with self.assertRaisesRegex(RuntimeError, "exit code 1"):
                    audio_extractor.extract_audio(str(mp4), str(out))

    def test_extract_audio_cancels_on_stop_event_and_removes_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            mp4, out = self._setup_input(tmp)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"PARTIAL")  # 부분 출력 시뮬레이션

            stop_event = threading.Event()
            stop_event.set()

            fake_process = _FakeProcess(
                ["Duration: 00:00:10.00, start: 0.000",
                 "frame=1 fps=30 time=00:00:01.00 bitrate="],
                returncode=0,
            )

            with patch.object(audio_extractor, "find_ffmpeg", return_value="/opt/ffmpeg"), \
                 patch.object(audio_extractor.subprocess, "Popen", return_value=fake_process):
                with self.assertRaises(OperationCancelledError):
                    audio_extractor.extract_audio(
                        str(mp4),
                        str(out),
                        stop_event=stop_event,
                    )

            # 부분 출력 파일은 제거됨
            self.assertFalse(out.exists())
            # terminate 호출
            self.assertTrue(fake_process._terminated)

    def test_extract_audio_command_omits_sample_rate_when_none(self):
        """sample_rate=None / channels=None (Apple Speech 모드) 시 -ar/-ac 인자 미포함."""
        with tempfile.TemporaryDirectory() as tmp:
            mp4, out = self._setup_input(tmp)
            captured: list[list[str]] = []

            def _capture_popen(cmd, *_args, **_kwargs):
                captured.append(cmd)
                return _FakeProcess([], returncode=0, output_path=out)

            with patch.object(audio_extractor, "find_ffmpeg", return_value="/opt/ffmpeg"), \
                 patch.object(audio_extractor.subprocess, "Popen", side_effect=_capture_popen):
                audio_extractor.extract_audio(
                    str(mp4),
                    str(out),
                    sample_rate=None,
                    channels=None,
                )

            self.assertEqual(len(captured), 1)
            cmd = captured[0]
            self.assertNotIn("-ar", cmd)
            self.assertNotIn("-ac", cmd)


if __name__ == "__main__":
    unittest.main()
