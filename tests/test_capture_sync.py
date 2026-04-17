import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from recorder import Recorder


class _FixedNow:
    def strftime(self, _fmt):
        return "2026-04-16 16-00-00"


class _FixedDateTime:
    @staticmethod
    def now():
        return _FixedNow()


class _FakeSystemAudioCapture:
    def __init__(self, started_at: float, mic_capture_active: bool = False):
        self.started_at = started_at
        self.mic_started_at = started_at if mic_capture_active else None
        self.mic_capture_active = mic_capture_active
        self.output_path = None
        self.mic_output_path = None
        self.mic_device_spec = None

    def start(self, output_path: Path, mic_output_path: Path | None = None, mic_device_spec: str | None = None) -> None:
        self.output_path = output_path
        self.mic_output_path = mic_output_path
        self.mic_device_spec = mic_device_spec

    def stop(self) -> None:
        return None


class _FakeLiveScreenWriter:
    def __init__(self, started_at: float, capture_started_at: float | None = None):
        self.started_at = started_at
        self.capture_started_at = capture_started_at
        self.output_path = None
        self.is_running = True

    def start(self, output_path: Path) -> None:
        self.output_path = output_path

    def stop(self) -> None:
        self.is_running = False


class CaptureSyncTests(unittest.TestCase):
    def test_screen_recording_uses_stream_microphone_capture_when_available(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_audio = _FakeSystemAudioCapture(started_at=100.25, mic_capture_active=True)
            screen_writer = _FakeLiveScreenWriter(started_at=100.60, capture_started_at=100.34)

            with patch("recorder.datetime", _FixedDateTime), patch(
                "system_audio.SystemAudioCapture", return_value=sys_audio
            ), patch(
                "recorder.LiveScreenWriter", return_value=screen_writer
            ), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=AssertionError("ffmpeg mic should not start")
            ):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)

            self.assertEqual(sys_audio.mic_output_path, Path(tmpdir) / "2026-04-16 16-00-00_녹화_mic.wav")
            self.assertEqual(recorder._mic_path, Path(tmpdir) / "2026-04-16 16-00-00_녹화_mic.wav")
            self.assertAlmostEqual(recorder._audio_offset, 0.09, places=3)
            self.assertAlmostEqual(recorder._mic_audio_offset, 0.09, places=3)

    def test_screen_recording_ignores_legacy_mic_latency_correction_when_stream_mic_active(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_audio = _FakeSystemAudioCapture(started_at=100.25, mic_capture_active=True)
            screen_writer = _FakeLiveScreenWriter(started_at=100.60, capture_started_at=100.34)

            with patch("recorder.datetime", _FixedDateTime), patch(
                "system_audio.SystemAudioCapture", return_value=sys_audio
            ), patch(
                "recorder.LiveScreenWriter", return_value=screen_writer
            ), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=AssertionError("ffmpeg mic should not start")
            ):
                recorder = Recorder()
                recorder.set_mic_latency_correction(0.303)
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)

            self.assertAlmostEqual(recorder._audio_offset, 0.09, places=3)
            self.assertAlmostEqual(recorder._mic_audio_offset, 0.09, places=3)

    def test_screen_recording_tracks_separate_mic_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_audio = _FakeSystemAudioCapture(started_at=100.25)
            screen_writer = _FakeLiveScreenWriter(started_at=100.60, capture_started_at=100.34)

            def _fake_start_mic(recorder, _mic_path, _device_index):
                recorder._mic_started_at = 100.30
                return 100.30

            with patch("recorder.datetime", _FixedDateTime), patch(
                "system_audio.SystemAudioCapture", return_value=sys_audio
            ), patch(
                "recorder.LiveScreenWriter", return_value=screen_writer
            ), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=_fake_start_mic
            ), patch.object(
                Recorder, "_resolve_mic_device_spec", return_value="Brio 300"
            ):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)

            self.assertAlmostEqual(recorder._audio_offset, 0.09, places=3)
            self.assertAlmostEqual(recorder._mic_audio_offset, 0.04, places=3)

    def test_screen_recording_applies_mic_latency_correction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_audio = _FakeSystemAudioCapture(started_at=100.25)
            screen_writer = _FakeLiveScreenWriter(started_at=100.60, capture_started_at=100.34)

            def _fake_start_mic(recorder, _mic_path, _device_index):
                recorder._mic_started_at = 100.30
                return 100.30

            with patch("recorder.datetime", _FixedDateTime), patch(
                "system_audio.SystemAudioCapture", return_value=sys_audio
            ), patch(
                "recorder.LiveScreenWriter", return_value=screen_writer
            ), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=_fake_start_mic
            ), patch.object(
                Recorder, "_resolve_mic_device_spec", return_value="Brio 300"
            ):
                recorder = Recorder()
                recorder.set_mic_latency_correction(0.02)
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)

            self.assertAlmostEqual(recorder._mic_audio_offset, 0.02, places=3)

    def test_screen_recording_logs_sync_debug_timestamps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_audio = _FakeSystemAudioCapture(started_at=100.25)
            sys_audio.first_sample_at = 100.21
            screen_writer = _FakeLiveScreenWriter(started_at=100.60, capture_started_at=100.34)

            with patch("recorder.datetime", _FixedDateTime), patch(
                "system_audio.SystemAudioCapture", return_value=sys_audio
            ), patch(
                "recorder.LiveScreenWriter", return_value=screen_writer
            ), self.assertLogs("recorder", level="INFO") as captured:
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=False)

            joined = "\n".join(captured.output)
            self.assertIn("화면 녹화 싱크 로그", joined)
            self.assertIn("sys.started_at=100.250", joined)
            self.assertIn("sys.first_sample_at=100.210", joined)
            self.assertIn("mic.started_at=-", joined)
            self.assertIn("screen.capture_started_at=100.340", joined)
            self.assertIn("screen.started_at=100.600", joined)
            self.assertIn("sys_offset=0.090", joined)
            self.assertIn("mic_offset=0.000", joined)

    def test_merge_audio_into_mp4_uses_distinct_offsets_for_system_and_mic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            mp4_path = tmpdir_path / "demo.mp4"
            sys_path = tmpdir_path / "demo_sys.wav"
            mic_path = tmpdir_path / "demo_mic.wav"
            temp_path = tmpdir_path / "demo_mux.mp4"
            mp4_path.write_bytes(b"video")
            sys_path.write_bytes(b"0" * 128)
            mic_path.write_bytes(b"1" * 128)

            captured = {}

            class _Result:
                returncode = 0
                stderr = ""

            def _fake_run(cmd, capture_output=False, text=False):
                captured["cmd"] = cmd
                temp_path.write_bytes(b"muxed")
                return _Result()

            with patch("recorder.find_ffmpeg", return_value="/usr/bin/ffmpeg"), patch(
                "recorder.subprocess.run", side_effect=_fake_run
            ):
                recorder = Recorder()
                recorder.merge_audio_into_mp4(
                    mp4_path,
                    sys_path,
                    mic_path=mic_path,
                    audio_offset=0.30,
                    mic_audio_offset=0.12,
                )

            cmd = captured["cmd"]
            sys_idx = cmd.index(str(sys_path))
            mic_idx = cmd.index(str(mic_path))
            self.assertEqual(cmd[sys_idx - 3:sys_idx + 1], ["-ss", "0.300", "-i", str(sys_path)])
            self.assertEqual(cmd[mic_idx - 3:mic_idx + 1], ["-ss", "0.120", "-i", str(mic_path)])

    def test_screen_recording_prefers_capture_session_start_time_for_audio_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_audio = _FakeSystemAudioCapture(started_at=100.25)
            screen_writer = _FakeLiveScreenWriter(started_at=100.60, capture_started_at=100.34)

            with patch("recorder.datetime", _FixedDateTime), patch(
                "system_audio.SystemAudioCapture", return_value=sys_audio
            ), patch(
                "recorder.LiveScreenWriter", return_value=screen_writer
            ):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=False)

            self.assertAlmostEqual(recorder._audio_offset, 0.09, places=3)

    def test_screen_recording_uses_actual_capture_start_times_for_audio_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_audio = _FakeSystemAudioCapture(started_at=100.25)
            screen_writer = _FakeLiveScreenWriter(started_at=100.60)

            with patch("recorder.datetime", _FixedDateTime), patch(
                "system_audio.SystemAudioCapture", return_value=sys_audio
            ), patch(
                "recorder.LiveScreenWriter", return_value=screen_writer
            ):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=False)

            self.assertAlmostEqual(recorder._audio_offset, 0.35, places=3)

    def test_resume_uses_actual_capture_start_times_for_audio_offset(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys_audio_instances = [
                _FakeSystemAudioCapture(started_at=10.0),
                _FakeSystemAudioCapture(started_at=20.1),
            ]
            screen_writer_instances = [
                _FakeLiveScreenWriter(started_at=10.2),
                _FakeLiveScreenWriter(started_at=20.55),
            ]

            with patch("recorder.datetime", _FixedDateTime), patch(
                "system_audio.SystemAudioCapture", side_effect=sys_audio_instances
            ), patch(
                "recorder.LiveScreenWriter", side_effect=screen_writer_instances
            ):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=False)
                recorder.pause()
                recorder.resume()

            self.assertAlmostEqual(recorder._audio_offset, 0.45, places=3)

    def test_amix_filter_enables_normalization(self):
        self.assertEqual(
            Recorder._amix_filter(),
            "amix=inputs=2:duration=longest:dropout_transition=0:normalize=1",
        )


if __name__ == "__main__":
    unittest.main()
