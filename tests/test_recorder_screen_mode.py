import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
