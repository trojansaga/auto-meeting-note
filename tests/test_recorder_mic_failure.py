import tempfile
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


class _FakeSystemAudioCapture:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self, output_path: Path, mic_output_path: Path | None = None, mic_device_spec: str | None = None) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class RecorderMicFailureTests(unittest.TestCase):
    def test_start_mic_records_timestamp_before_readiness_sleep(self):
        recorder = Recorder()
        events = []

        def _fake_popen(*_args, **_kwargs):
            events.append("popen")
            return _FakeRunningMicProcess(events)

        def _fake_time():
            events.append("time")
            return 10.0

        def _fake_sleep(_seconds):
            events.append("sleep")

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "recorder.find_ffmpeg", return_value="/usr/bin/ffmpeg"
        ), patch(
            "recorder.subprocess.Popen", side_effect=_fake_popen
        ), patch(
            "recorder.time.time", side_effect=_fake_time
        ), patch(
            "recorder.time.sleep", side_effect=_fake_sleep
        ):
            started_at = recorder._start_mic(Path(tmpdir) / "demo.wav", "Brio 300")

        self.assertEqual(started_at, 10.0)
        self.assertEqual(events[:4], ["popen", "time", "sleep", "poll"])

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


if __name__ == "__main__":
    unittest.main()
