import builtins
import struct
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import transcriber


def _write_wav(path: Path, seconds: int = 1, sample_rate: int = 16000) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frame = struct.pack("<h", 0)
        wav_file.writeframes(frame * sample_rate * seconds)


class TranscriberPerformanceTests(unittest.TestCase):
    def test_transcribe_does_not_import_whisper_for_duration_lookup(self):
        calls = []

        fake_mlx_core = types.ModuleType("mlx.core")

        class FakeMetal:
            def clear_cache(self):
                calls.append(("clear_cache", None))

        fake_mlx_core.metal = FakeMetal()

        fake_mlx = types.ModuleType("mlx")
        fake_mlx.__path__ = []
        fake_mlx.core = fake_mlx_core

        fake_mlx_whisper = types.ModuleType("mlx_whisper")
        fake_mlx_whisper.__path__ = []

        def fake_transcribe(audio_path, **kwargs):
            calls.append((audio_path, kwargs))
            return {"segments": [{"start": 0, "text": "테스트 문장"}]}

        fake_mlx_whisper.transcribe = fake_transcribe

        fake_mlx_whisper_transcribe = types.ModuleType("mlx_whisper.transcribe")

        class FakeModelHolder:
            model = "model"
            model_path = "repo"

        fake_mlx_whisper_transcribe.ModelHolder = FakeModelHolder

        original_import = builtins.__import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "whisper":
                raise AssertionError("transcriber must not import whisper for duration lookup")
            return original_import(name, globals, locals, fromlist, level)

        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "input.wav"
            output_path = Path(tmpdir) / "script.md"
            _write_wav(wav_path, seconds=1)

            with patch.dict(
                sys.modules,
                {
                    "mlx": fake_mlx,
                    "mlx.core": fake_mlx_core,
                    "mlx_whisper": fake_mlx_whisper,
                    "mlx_whisper.transcribe": fake_mlx_whisper_transcribe,
                },
            ):
                with patch("builtins.__import__", side_effect=guarded_import):
                    result_path = transcriber.transcribe(
                        str(wav_path),
                        str(output_path),
                        original_filename="input.wav",
                    )

            self.assertEqual(result_path, str(output_path))
            self.assertTrue(output_path.exists())
            self.assertIn("테스트 문장", output_path.read_text(encoding="utf-8"))
            self.assertEqual(calls[0][0], str(wav_path))
            self.assertEqual(calls[0][1]["language"], "ko")


if __name__ == "__main__":
    unittest.main()
