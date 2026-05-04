import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

import audio_preprocessor
from cancellation import OperationCancelledError


def _write_synthetic_wav(path: Path, sr: int = 16000, duration: float = 4.0,
                         speech_segments: list[tuple[float, float]] | None = None) -> None:
    """합성 WAV 생성. speech_segments(초) 구간만 sine 사인파, 나머지는 무음."""
    n = int(sr * duration)
    audio = np.zeros(n, dtype=np.float32)
    if speech_segments:
        t = np.arange(n) / sr
        wave = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        for start_s, end_s in speech_segments:
            i0 = int(start_s * sr)
            i1 = int(end_s * sr)
            audio[i0:i1] = wave[i0:i1]
    sf.write(str(path), audio, sr)


class EnergyVadTests(unittest.TestCase):
    def test_returns_empty_for_pure_silence(self):
        audio = np.zeros(16000 * 2, dtype=np.float32)
        segments = audio_preprocessor._energy_vad(audio, 16000)
        self.assertEqual(segments, [])

    def test_detects_single_loud_segment(self):
        sr = 16000
        n = sr * 4
        t = np.arange(n) / sr
        audio = np.zeros(n, dtype=np.float32)
        # 1.0~2.5초 구간만 sine
        i0, i1 = int(1.0 * sr), int(2.5 * sr)
        audio[i0:i1] = (0.3 * np.sin(2 * np.pi * 440 * t[i0:i1])).astype(np.float32)

        segments = audio_preprocessor._energy_vad(audio, sr)
        self.assertEqual(len(segments), 1)
        seg_start, seg_end = segments[0]
        # PAD_MS 만큼 앞뒤 여유 있어 정확히 일치하지는 않지만 구간 중심은 잡혀야 함
        self.assertLess(seg_start, i0)
        self.assertGreater(seg_end, i1)

    def test_merges_adjacent_segments_within_gap(self):
        sr = 16000
        n = sr * 5
        t = np.arange(n) / sr
        audio = np.zeros(n, dtype=np.float32)
        # 두 음성 구간이 200ms 간격(MERGE_GAP_MS=400ms 보다 작음) — 합쳐져야 함
        for i0_s, i1_s in [(1.0, 1.5), (1.7, 2.2)]:
            i0, i1 = int(i0_s * sr), int(i1_s * sr)
            audio[i0:i1] = (0.3 * np.sin(2 * np.pi * 440 * t[i0:i1])).astype(np.float32)

        segments = audio_preprocessor._energy_vad(audio, sr)
        self.assertEqual(len(segments), 1)


class NormalizeSegmentsTests(unittest.TestCase):
    def test_low_volume_segment_is_amplified_toward_target_rms(self):
        sr = 16000
        n = sr * 2
        audio = np.zeros(n, dtype=np.float32)
        # 매우 낮은 RMS
        audio[: sr] = 0.01 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)

        normalized = audio_preprocessor._normalize_segments(audio, [(0, sr)])
        rms_before = float(np.sqrt(np.mean(audio[:sr] ** 2)))
        rms_after = float(np.sqrt(np.mean(normalized[:sr] ** 2)))
        self.assertGreater(rms_after, rms_before)

    def test_already_normalized_segment_is_not_clipped(self):
        sr = 16000
        # TARGET_RMS 근처 신호 — 증폭 거의 없어야 함
        audio = (audio_preprocessor.TARGET_RMS * np.sqrt(2) * np.sin(
            2 * np.pi * 440 * np.arange(sr) / sr
        )).astype(np.float32)

        normalized = audio_preprocessor._normalize_segments(audio, [(0, len(audio))])
        # 클리핑 (-1.0 / 1.0) 발생하지 않아야 함
        self.assertLess(float(np.max(np.abs(normalized))), 0.99)

    def test_max_gain_caps_amplification(self):
        sr = 16000
        n = sr
        # 거의 0에 가까운 신호 — 증폭이 MAX_GAIN 으로 제한돼야 함
        audio = (1e-5 * np.sin(2 * np.pi * 440 * np.arange(n) / sr)).astype(np.float32)
        normalized = audio_preprocessor._normalize_segments(audio, [(0, n)])
        gain_observed = float(np.max(np.abs(normalized)) / max(np.max(np.abs(audio)), 1e-12))
        self.assertLessEqual(gain_observed, audio_preprocessor.MAX_GAIN + 1e-3)


class PreprocessAudioTests(unittest.TestCase):
    def _make_dirs(self) -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp(prefix="ap-test-"))
        return tmp / "in.wav", tmp / "out.wav"

    def test_all_steps_disabled_just_copies_file(self):
        in_path, out_path = self._make_dirs()
        _write_synthetic_wav(in_path, speech_segments=[(0.5, 1.5)])

        result = audio_preprocessor.preprocess_audio(
            str(in_path),
            str(out_path),
            noise_reduce=False,
            vad=False,
            normalize=False,
        )

        self.assertEqual(result, str(out_path))
        self.assertEqual(in_path.read_bytes(), out_path.read_bytes())

    def test_vad_only_removes_silence_and_writes_shorter_output(self):
        in_path, out_path = self._make_dirs()
        # 4초 중 1.0~2.5초만 음성 → VAD 제거 후 길이 짧아져야 함
        _write_synthetic_wav(in_path, duration=4.0, speech_segments=[(1.0, 2.5)])

        audio_preprocessor.preprocess_audio(
            str(in_path),
            str(out_path),
            noise_reduce=False,
            vad=True,
            normalize=False,
        )

        in_audio, _sr = sf.read(str(in_path))
        out_audio, _sr = sf.read(str(out_path))
        self.assertLess(len(out_audio), len(in_audio))

    def test_pure_silence_input_falls_back_to_original_audio(self):
        in_path, out_path = self._make_dirs()
        _write_synthetic_wav(in_path, duration=3.0, speech_segments=None)

        audio_preprocessor.preprocess_audio(
            str(in_path),
            str(out_path),
            noise_reduce=False,
            vad=True,
            normalize=False,
        )

        in_audio, _sr = sf.read(str(in_path))
        out_audio, _sr = sf.read(str(out_path))
        # 음성 구간 미감지 → 원본 그대로 작성됨
        self.assertEqual(len(in_audio), len(out_audio))

    def test_stop_event_aborts_with_cancelled_error(self):
        in_path, out_path = self._make_dirs()
        _write_synthetic_wav(in_path, speech_segments=[(0.5, 1.5)])

        stop_event = threading.Event()
        stop_event.set()

        with self.assertRaises(OperationCancelledError):
            audio_preprocessor.preprocess_audio(
                str(in_path),
                str(out_path),
                noise_reduce=False,
                vad=True,
                normalize=False,
                stop_event=stop_event,
            )

    def test_progress_callback_receives_step_messages(self):
        in_path, out_path = self._make_dirs()
        _write_synthetic_wav(in_path, speech_segments=[(0.5, 1.5)])

        statuses: list[str] = []
        audio_preprocessor.preprocess_audio(
            str(in_path),
            str(out_path),
            noise_reduce=False,
            vad=True,
            normalize=True,
            progress_callback=statuses.append,
        )

        joined = "\n".join(statuses)
        self.assertIn("오디오 로드", joined)
        self.assertIn("침묵 구간 감지", joined)

    def test_noise_reduce_invokes_noisereduce_module(self):
        in_path, out_path = self._make_dirs()
        _write_synthetic_wav(in_path, speech_segments=[(0.5, 1.5)])

        # noisereduce.reduce_noise 가 실제로 호출되는지만 검증 (CPU 비용 회피)
        import noisereduce as nr

        captured = {}

        def _fake_reduce_noise(y, sr, stationary, prop_decrease):
            captured["called"] = True
            captured["sr"] = sr
            captured["stationary"] = stationary
            captured["prop_decrease"] = prop_decrease
            return y

        with patch.object(nr, "reduce_noise", side_effect=_fake_reduce_noise):
            audio_preprocessor.preprocess_audio(
                str(in_path),
                str(out_path),
                noise_reduce=True,
                vad=False,
                normalize=False,
            )

        self.assertTrue(captured.get("called"))
        self.assertFalse(captured["stationary"])
        self.assertEqual(captured["prop_decrease"], 0.75)


if __name__ == "__main__":
    unittest.main()
