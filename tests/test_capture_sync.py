import tempfile
import unittest
from pathlib import Path
from typing import Optional
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


class _FakeContinuousScreenRecorder:
    def __init__(self, output_dir: Path, basename: str, capture_audio: bool = True):
        self.output_dir = output_dir
        self.basename = basename
        self.is_running = False
        self.active_segment_started_at: Optional[float] = None
        self.stream_capture_started_at: Optional[float] = None
        self.capture_audio = capture_audio

    def start(self) -> Path:
        self.is_running = True
        return self.output_dir / f"{self.basename}_seg0.mp4"

    def pause(self) -> None:
        pass

    def resume(self) -> Path:
        return self.output_dir / f"{self.basename}_seg1.mp4"

    def stop(self) -> Path:
        self.is_running = False
        return self.output_dir / f"{self.basename}.mp4"


def _make_screen_mode_patches(tmpdir, sys_started_at, screen_started_at):
    """screen mode 테스트용 표준 mock 세트: SystemAudioCapture + ContinuousScreenRecorder + datetime."""
    fake_sys = _FakeSystemAudioCapture(started_at=sys_started_at)
    fake_csr = _FakeContinuousScreenRecorder(Path(tmpdir), "2026-04-16 16-00-00_녹화")
    fake_csr.stream_capture_started_at = screen_started_at
    return fake_sys, fake_csr


class CaptureSyncTests(unittest.TestCase):
    def test_screen_mode_audio_offset_uses_sys_and_screen_start(self):
        """audio_offset = screen_start - sys_started (별도 sys WAV 캡처 후 mp4와 정렬)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_sys, fake_csr = _make_screen_mode_patches(tmpdir, sys_started_at=100.25, screen_started_at=100.34)

            with patch("recorder.datetime", _FixedDateTime), patch(
                "recorder.ContinuousScreenRecorder", return_value=fake_csr
            ), patch("system_audio.SystemAudioCapture", return_value=fake_sys):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=False)

            self.assertAlmostEqual(recorder._audio_offset, 0.09, places=3)
            self.assertEqual(recorder._mic_audio_offset, 0.0)

    def test_screen_mode_mic_latency_correction_applies(self):
        """화면 녹화 모드에서 mic_latency_correction이 mic_audio_offset에 반영된다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_sys, fake_csr = _make_screen_mode_patches(tmpdir, sys_started_at=100.25, screen_started_at=100.60)

            def _fake_start_mic(self_r, _mic_path, _device_index):
                self_r._mic_started_at = 100.34
                return 100.34

            with patch("recorder.datetime", _FixedDateTime), patch(
                "recorder.ContinuousScreenRecorder", return_value=fake_csr
            ), patch("system_audio.SystemAudioCapture", return_value=fake_sys), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=_fake_start_mic
            ), patch.object(
                Recorder, "_resolve_mic_device_spec", return_value="Brio 300"
            ):
                recorder = Recorder()
                recorder.set_mic_latency_correction(0.10)
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)

            # mic_offset = screen_start(100.60) - mic_start(100.34) - correction(0.10) = 0.16
            self.assertAlmostEqual(recorder._mic_audio_offset, 0.16, places=3)

    def test_screen_mode_mic_offset_from_screen_and_mic_start(self):
        """mic_offset = screen_start_time - mic_started_at."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_sys, fake_csr = _make_screen_mode_patches(tmpdir, sys_started_at=100.25, screen_started_at=100.60)

            def _fake_start_mic(self_r, _mic_path, _device_index):
                self_r._mic_started_at = 100.30
                return 100.30

            with patch("recorder.datetime", _FixedDateTime), patch(
                "recorder.ContinuousScreenRecorder", return_value=fake_csr
            ), patch("system_audio.SystemAudioCapture", return_value=fake_sys), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=_fake_start_mic
            ), patch.object(
                Recorder, "_resolve_mic_device_spec", return_value="Brio 300"
            ):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)

            # audio_offset = 100.60 - 100.25 = 0.35
            self.assertAlmostEqual(recorder._audio_offset, 0.35, places=3)
            # mic_offset = 100.60 - 100.30 = 0.30
            self.assertAlmostEqual(recorder._mic_audio_offset, 0.30, places=3)

    def test_screen_mode_no_latency_correction_when_zero(self):
        """latency_correction=0일 때 mic_offset = screen_start - mic_start."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_sys, fake_csr = _make_screen_mode_patches(tmpdir, sys_started_at=100.25, screen_started_at=100.60)

            def _fake_start_mic(self_r, _mic_path, _device_index):
                self_r._mic_started_at = 100.30
                return 100.30

            with patch("recorder.datetime", _FixedDateTime), patch(
                "recorder.ContinuousScreenRecorder", return_value=fake_csr
            ), patch("system_audio.SystemAudioCapture", return_value=fake_sys), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=_fake_start_mic
            ), patch.object(
                Recorder, "_resolve_mic_device_spec", return_value="Brio 300"
            ):
                recorder = Recorder()
                recorder.set_mic_latency_correction(0.0)
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)

            self.assertAlmostEqual(recorder._mic_audio_offset, 0.30, places=3)

    def test_screen_recording_logs_start_info(self):
        """화면 녹화 시작 완료 로그에 screen_start, sys_started, mic 타이밍 정보가 포함된다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_sys, fake_csr = _make_screen_mode_patches(tmpdir, sys_started_at=100.25, screen_started_at=100.60)

            with patch("recorder.datetime", _FixedDateTime), patch(
                "recorder.ContinuousScreenRecorder", return_value=fake_csr
            ), patch("system_audio.SystemAudioCapture", return_value=fake_sys), self.assertLogs(
                "recorder", level="INFO"
            ) as captured:
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=False)

            joined = "\n".join(captured.output)
            self.assertIn("화면 녹화 시작 완료", joined)
            self.assertIn("screen_start=100.600", joined)
            self.assertIn("sys_started=100.250", joined)

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

    def test_screen_mode_mic_path_set_when_enabled(self):
        """mic_enabled=True면 _mic_path가 설정된다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from unittest.mock import MagicMock
            fake_sys, fake_csr = _make_screen_mode_patches(tmpdir, sys_started_at=100.25, screen_started_at=100.60)
            expected_mic = Path(tmpdir) / "2026-04-16 16-00-00_녹화_mic.wav"

            def _fake_start_mic(self_r, mic_path, _device_index):
                self_r._mic_started_at = 100.50
                self_r._mic_process = MagicMock()
                return 100.50

            with patch("recorder.datetime", _FixedDateTime), patch(
                "recorder.ContinuousScreenRecorder", return_value=fake_csr
            ), patch("system_audio.SystemAudioCapture", return_value=fake_sys), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=_fake_start_mic
            ), patch.object(
                Recorder, "_resolve_mic_device_spec", return_value="Brio 300"
            ):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)

            self.assertEqual(recorder._mic_path, expected_mic)

    def test_screen_mode_no_mic_when_disabled(self):
        """mic_enabled=False면 _mic_path=None, _mic_audio_offset=0.0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_sys, fake_csr = _make_screen_mode_patches(tmpdir, sys_started_at=100.25, screen_started_at=100.60)

            with patch("recorder.datetime", _FixedDateTime), patch(
                "recorder.ContinuousScreenRecorder", return_value=fake_csr
            ), patch("system_audio.SystemAudioCapture", return_value=fake_sys):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=False)

            self.assertIsNone(recorder._mic_path)
            self.assertEqual(recorder._mic_audio_offset, 0.0)

    def test_resume_screen_mode_recalculates_mic_offset(self):
        """재개 후 mic_offset이 resume_time 기준으로 재계산된다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            from unittest.mock import MagicMock
            # 시작/재개 시 별도의 SystemAudioCapture 인스턴스가 생성됨
            sys_audio_instances = [
                _FakeSystemAudioCapture(started_at=100.25),
                _FakeSystemAudioCapture(started_at=200.05),
            ]
            fake_csr = _FakeContinuousScreenRecorder(Path(tmpdir), "2026-04-16 16-00-00_녹화")
            screen_times = iter([100.60, 200.10])

            def _csr_start():
                fake_csr.is_running = True
                fake_csr.stream_capture_started_at = next(screen_times)
                return fake_csr.output_dir / f"{fake_csr.basename}_seg0.mp4"

            def _csr_resume():
                fake_csr.active_segment_started_at = next(screen_times)
                return fake_csr.output_dir / f"{fake_csr.basename}_seg1.mp4"

            fake_csr.start = _csr_start
            fake_csr.resume = _csr_resume

            mic_start_times = iter([100.50, 200.07])

            def _fake_start_mic(self_r, mic_path, _device_index):
                t = next(mic_start_times)
                self_r._mic_started_at = t
                self_r._mic_process = MagicMock()
                return t

            with patch("recorder.datetime", _FixedDateTime), patch(
                "recorder.ContinuousScreenRecorder", return_value=fake_csr
            ), patch("system_audio.SystemAudioCapture", side_effect=sys_audio_instances), patch.object(
                Recorder, "_start_mic", autospec=True, side_effect=_fake_start_mic
            ), patch.object(
                Recorder, "_resolve_mic_device_spec", return_value="Brio 300"
            ):
                recorder = Recorder()
                recorder.start_screen_recording(Path(tmpdir), mic_enabled=True)
                recorder.pause()
                recorder.resume()

            # resume 시:
            #   audio_offset = resume_screen_time(200.10) - resume_sys_started(200.05) = 0.05
            #   mic_offset = resume_screen_time(200.10) - resume_mic_started(200.07) = 0.03
            self.assertAlmostEqual(recorder._audio_offset, 0.05, places=3)
            self.assertAlmostEqual(recorder._mic_audio_offset, 0.03, places=3)

    def test_amix_filter_enables_normalization(self):
        self.assertEqual(
            Recorder._amix_filter(),
            "amix=inputs=2:duration=longest:dropout_transition=0:normalize=1",
        )


if __name__ == "__main__":
    unittest.main()
