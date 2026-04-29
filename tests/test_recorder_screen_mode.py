import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from recorder import Recorder


class _FixedNow:
    def strftime(self, _fmt):
        return "2026-04-02 12-00-00"


class _FixedDateTime:
    @staticmethod
    def now():
        return _FixedNow()


class _FakeContinuousScreenRecorder:
    def __init__(self, output_dir: Path, basename: str):
        self.output_dir = output_dir
        self.basename = basename
        self.is_running = False
        self.is_paused = False
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0

    def start(self) -> Path:
        self.is_running = True
        return self.output_dir / f"{self.basename}_seg0.mp4"

    def pause(self) -> None:
        self.pause_calls += 1
        self.is_paused = True

    def resume(self) -> Path:
        self.resume_calls += 1
        self.is_paused = False
        return self.output_dir / f"{self.basename}_seg1.mp4"

    def stop(self) -> Path:
        self.stop_calls += 1
        self.is_running = False
        return self.output_dir / f"{self.basename}.mp4"


def _make_recorder_with_fake_screen(output_dir: Path):
    """패치된 ContinuousScreenRecorder를 사용하는 Recorder와 fake 인스턴스를 반환."""
    created = []

    def _factory(od, bn):
        r = _FakeContinuousScreenRecorder(od, bn)
        created.append(r)
        return r

    patcher = patch("recorder.ContinuousScreenRecorder", side_effect=_factory)
    return patcher, created


class RecorderScreenModeTests(unittest.TestCase):
    def test_screen_pause_resume_uses_continuous_recorder_and_returns_mp4(self):
        created = []

        def _factory(output_dir, basename):
            recorder = _FakeContinuousScreenRecorder(output_dir, basename)
            created.append(recorder)
            return recorder

        with patch("recorder.datetime", _FixedDateTime), patch(
            "recorder.ContinuousScreenRecorder", side_effect=_factory
        ):
            recorder = Recorder()
            first_segment = recorder.start_screen_recording(Path("/tmp"), mic_enabled=False)
            recorder.pause()
            recorder.resume()
            mode, output_path, audio_path, mic_path, audio_offset, mic_audio_offset = recorder.stop()

        fake = created[0]
        self.assertEqual(first_segment, Path("/tmp/2026-04-02 12-00-00_녹화_seg0.mp4"))
        self.assertEqual(fake.pause_calls, 1)
        self.assertEqual(fake.resume_calls, 1)
        self.assertEqual(fake.stop_calls, 1)
        self.assertEqual(mode, "screen")
        self.assertEqual(output_path, Path("/tmp/2026-04-02 12-00-00_녹화.mp4"))
        self.assertIsNone(audio_path)
        self.assertIsNone(mic_path)
        self.assertEqual(audio_offset, 0.0)
        self.assertEqual(mic_audio_offset, 0.0)

    def test_screen_stop_without_pause_returns_mp4(self):
        """일시정지 없이 곧바로 stop해도 mp4 경로가 반환되어야 한다."""
        patcher, created = _make_recorder_with_fake_screen(Path("/tmp"))
        with patch("recorder.datetime", _FixedDateTime), patcher:
            recorder = Recorder()
            recorder.start_screen_recording(Path("/tmp"), mic_enabled=False)
            mode, output_path, audio_path, mic_path, *_ = recorder.stop()

        fake = created[0]
        self.assertEqual(fake.pause_calls, 0)
        self.assertEqual(fake.stop_calls, 1)
        self.assertEqual(mode, "screen")
        self.assertEqual(output_path, Path("/tmp/2026-04-02 12-00-00_녹화.mp4"))
        self.assertIsNone(audio_path)

    def test_screen_stop_propagates_recorder_error_as_none_output(self):
        """ContinuousScreenRecorder.stop()이 예외를 던져도 Recorder.stop()은 정상 반환한다."""
        patcher, created = _make_recorder_with_fake_screen(Path("/tmp"))

        def _failing_stop():
            raise RuntimeError("SCStream 종료 오류")

        with patch("recorder.datetime", _FixedDateTime), patcher:
            recorder = Recorder()
            recorder.start_screen_recording(Path("/tmp"), mic_enabled=False)
            created[0].stop = _failing_stop
            mode, output_path, *_ = recorder.stop()

        self.assertEqual(mode, "screen")
        self.assertIsNone(output_path)


class ConcatFilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._dir = Path(self._tmp)

    def _write(self, name: str, content: bytes = b"x" * 100) -> Path:
        p = self._dir / name
        p.write_bytes(content)
        return p

    def test_concat_files_uses_tmp_then_renames(self):
        """output이 input 중 하나와 같은 경로여도 tmp → rename으로 안전하게 처리된다."""
        seg0 = self._write("seg0.wav")
        seg1 = self._write("seg1.wav")
        out = self._dir / "seg0.wav"  # 첫 세그먼트와 동일 경로

        call_log = []

        def _fake_run(cmd, **kwargs):
            call_log.append(cmd)
            tmp_path = Path(cmd[-1])
            tmp_path.write_bytes(b"merged")
            r = MagicMock()
            r.returncode = 0
            return r

        recorder = Recorder()
        with patch("subprocess.run", side_effect=_fake_run):
            recorder._concat_files("ffmpeg", [seg0, seg1], out)

        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes(), b"merged")
        # 임시 파일이 정리되어 있어야 함
        tmp = self._dir / f"~concat_{out.name}"
        self.assertFalse(tmp.exists())

    def test_concat_files_video_fallback_on_copy_fail(self):
        """-c copy 실패 시 h264_videotoolbox → libx264 순으로 fallback한다."""
        seg0 = self._write("seg0.mp4")
        seg1 = self._write("seg1.mp4")
        out = self._dir / "final.mp4"
        attempt = [0]

        def _fake_run(cmd, **kwargs):
            attempt[0] += 1
            r = MagicMock()
            r.stderr = b""
            if attempt[0] == 1:
                r.returncode = 1  # -c copy 실패
            elif attempt[0] == 2:
                r.returncode = 1  # h264_videotoolbox 실패
            else:
                tmp_path = Path(cmd[-1])
                tmp_path.write_bytes(b"reencoded")
                r.returncode = 0  # libx264 성공
            return r

        recorder = Recorder()
        with patch("subprocess.run", side_effect=_fake_run):
            recorder._concat_files("ffmpeg", [seg0, seg1], out, is_video=True)

        self.assertEqual(attempt[0], 3)
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes(), b"reencoded")

    def test_concat_files_audio_fallback_to_resample(self):
        """-c copy 실패 시 pcm_s16le 재샘플로 fallback한다."""
        seg0 = self._write("seg0.wav")
        seg1 = self._write("seg1.wav")
        out = self._dir / "final.wav"
        attempt = [0]

        def _fake_run(cmd, **kwargs):
            attempt[0] += 1
            r = MagicMock()
            r.stderr = b""
            if attempt[0] == 1:
                r.returncode = 1
            else:
                Path(cmd[-1]).write_bytes(b"resampled")
                r.returncode = 0
            return r

        recorder = Recorder()
        with patch("subprocess.run", side_effect=_fake_run):
            recorder._concat_files("ffmpeg", [seg0, seg1], out, is_video=False)

        self.assertEqual(attempt[0], 2)
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes(), b"resampled")


if __name__ == "__main__":
    unittest.main()
