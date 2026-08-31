import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from recorder import Recorder


class _FakeFailedMicProcess:
    def __init__(self):
        self.stdin = None
        self._stderr = "avfoundation input device not found"

    def poll(self):
        return 1

    def communicate(self, timeout=None):
        return ("", self._stderr)


class _FakeRunningMicProcess:
    def __init__(self, events):
        self.stdin = None
        self._events = events

    def poll(self):
        self._events.append("poll")
        return None


class _FakeWritingMicProcess:
    """Popen mock that simulates ffmpeg writing WAV header + samples after a delay."""

    def __init__(self, mic_path: Path, events):
        self.stdin = None
        self._events = events
        self._mic_path = mic_path
        self._poll_count = 0

    def poll(self):
        self._events.append("poll")
        self._poll_count += 1
        # 두 번째 polling 호출에서 WAV 헤더 + 샘플을 기록한 것으로 시뮬레이션
        if self._poll_count >= 2 and not self._mic_path.exists():
            self._mic_path.write_bytes(b"RIFF" + b"\x00" * 40 + b"\x00" * 100)
        return None


class _FakeWritingMicProcessWithStderr(_FakeWritingMicProcess):
    """`_FakeWritingMicProcess` + stderr stream (drain 스레드 검증용)."""

    def __init__(self, mic_path: Path, events, stderr_payload: bytes):
        super().__init__(mic_path, events)
        # readline()이 b""를 반환할 때까지 drain 스레드가 소비
        self.stderr = io.BytesIO(stderr_payload)


class _FakeSystemAudioCapture:
    def __init__(self):
        self.started = False
        self.stopped = False
        # 실제 캡처에서는 첫 샘플 버퍼 PTS 가 앵커로 쓰인다 (_AUDIO_ANCHOR_ATTRS)
        self.started_at = 100.0
        self.first_sample_at = 100.0
        self.first_sample_host_at = 100.0
        self.mic_capture_active = False
        self.mic_started_at = None

    def start(self, output_path: Path, mic_output_path: Path | None = None, mic_device_spec: str | None = None) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class RecorderMicFailureTests(unittest.TestCase):
    def test_start_mic_returns_timestamp_when_first_samples_written(self):
        """ffmpeg가 WAV 헤더+샘플을 기록한 시점을 started_at으로 사용한다."""
        recorder = Recorder()
        events = []

        with tempfile.TemporaryDirectory() as tmpdir:
            mic_path = Path(tmpdir) / "demo.wav"

            def _fake_popen(*_args, **_kwargs):
                events.append("popen")
                return _FakeWritingMicProcess(mic_path, events)

            with patch(
                "recorder.find_ffmpeg", return_value="/usr/bin/ffmpeg"
            ), patch(
                "recorder.subprocess.Popen", side_effect=_fake_popen
            ):
                started_at = recorder._start_mic(mic_path, "Brio 300")

        self.assertIsInstance(started_at, float)
        self.assertEqual(events[0], "popen")
        self.assertIn("poll", events)
        self.assertIsNotNone(recorder._mic_process)

    def test_start_mic_raises_when_ffmpeg_exits_immediately(self):
        recorder = Recorder()

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "recorder.find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "recorder.subprocess.Popen", return_value=_FakeFailedMicProcess()
        ):
            with self.assertRaisesRegex(RuntimeError, "마이크 녹음 시작 실패"):
                recorder._start_mic(Path(tmpdir) / "demo.wav", "Brio 300")

        self.assertIsNone(recorder._mic_process)

    def test_start_audio_recording_stops_system_audio_if_mic_start_fails(self):
        sys_audio = _FakeSystemAudioCapture()

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "system_audio.SystemAudioCapture", return_value=sys_audio
        ), patch.object(
            Recorder, "_resolve_mic_device_spec", return_value="Brio 300"
        ), patch.object(
            Recorder, "_start_mic", side_effect=RuntimeError("마이크 녹음 시작 실패: test")
        ):
            recorder = Recorder()
            with self.assertRaisesRegex(RuntimeError, "마이크 녹음 시작 실패"):
                recorder.start_audio_recording(Path(tmpdir), mic_enabled=True, mic_device_index="builtin")

        self.assertTrue(sys_audio.started)
        self.assertTrue(sys_audio.stopped)

    def test_start_mic_spawns_stderr_drain_thread_when_stderr_pipe_open(self):
        """stderr=PIPE 인 경우 drain 스레드를 띄워 장기 녹화 시 PIPE 64KB 버퍼 풀림을 차단한다."""
        recorder = Recorder()
        # 약 100KB 분량 stderr — drain 없으면 macOS PIPE 버퍼(64KB)에서 ffmpeg 측 write 블로킹
        stderr_payload = b"\n".join(b"x" * 1024 for _ in range(100)) + b"\n"

        with tempfile.TemporaryDirectory() as tmpdir:
            mic_path = Path(tmpdir) / "demo.wav"

            def _fake_popen(*_args, **_kwargs):
                return _FakeWritingMicProcessWithStderr(mic_path, [], stderr_payload)

            with patch(
                "recorder.find_ffmpeg", return_value="/usr/bin/ffmpeg"
            ), patch(
                "recorder.subprocess.Popen", side_effect=_fake_popen
            ):
                recorder._start_mic(mic_path, "Brio 300")

            self.assertIsNotNone(recorder._mic_stderr_thread)
            self.assertTrue(recorder._mic_stderr_thread.daemon)
            # drain 스레드가 stderr 를 EOF 까지 모두 소비했는지 확인 (최대 1초 대기)
            recorder._mic_stderr_thread.join(timeout=1.0)
            self.assertFalse(recorder._mic_stderr_thread.is_alive())

    def test_start_mic_skips_drain_thread_when_no_stderr_attribute(self):
        """stderr가 없는 mock(테스트 환경)에서는 drain 스레드를 띄우지 않는다."""
        recorder = Recorder()

        with tempfile.TemporaryDirectory() as tmpdir:
            mic_path = Path(tmpdir) / "demo.wav"

            def _fake_popen(*_args, **_kwargs):
                return _FakeWritingMicProcess(mic_path, [])

            with patch(
                "recorder.find_ffmpeg", return_value="/usr/bin/ffmpeg"
            ), patch(
                "recorder.subprocess.Popen", side_effect=_fake_popen
            ):
                recorder._start_mic(mic_path, "Brio 300")

            self.assertIsNone(recorder._mic_stderr_thread)

    def test_start_mic_started_at_reflects_first_sample_write_time(self):
        """첫 샘플 기록 시점이 polling으로 감지되어 started_at이 그 시점에 가깝게 기록된다."""
        recorder = Recorder()

        with tempfile.TemporaryDirectory() as tmpdir:
            mic_path = Path(tmpdir) / "demo.wav"
            t0 = [None]

            def _fake_popen(*_args, **_kwargs):
                t0[0] = time.time()
                return _FakeWritingMicProcess(mic_path, [])

            with patch(
                "recorder.find_ffmpeg", return_value="/usr/bin/ffmpeg"
            ), patch(
                "recorder.subprocess.Popen", side_effect=_fake_popen
            ):
                started_at = recorder._start_mic(mic_path, "Brio 300")

        # started_at은 popen 직후가 아니라 폴링이 끝난 시점(파일 등장 시점)이어야 함
        self.assertGreater(started_at, t0[0])


if __name__ == "__main__":
    unittest.main()
