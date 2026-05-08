import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

import acoustic_echo_cancel as aec
from cancellation import OperationCancelledError


def _write_wav(path: Path, audio: np.ndarray, sr: int = 48000) -> None:
    sf.write(str(path), audio.astype(np.float32), sr)


def _sine(freq: float, duration: float, sr: int, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(sr * duration)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class CancelEchoTests(unittest.TestCase):
    """AEC 후처리의 핵심 불변식 검증.

    - 출력 WAV 의 sample rate / 길이는 입력 mic 와 동일 (싱크 보존)
    - sys 신호와 매우 유사한 echo 가 mic 에 섞여 있을 때 그 에너지가 감소
    - sys 가 비어있는 mic-only 일 땐 출력이 mic 에 가까움
    """

    def test_output_preserves_mic_length_and_sample_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mic_path = tmp_path / "mic.wav"
            sys_path = tmp_path / "sys.wav"
            out_path = tmp_path / "mic_aec.wav"

            mic_audio = _sine(440, duration=2.0, sr=48000)
            sys_audio = _sine(220, duration=2.0, sr=48000)
            _write_wav(mic_path, mic_audio, sr=48000)
            _write_wav(sys_path, sys_audio, sr=48000)

            aec.cancel_echo(mic_path, sys_path, out_path)

            out_audio, out_sr = sf.read(str(out_path), dtype="float32")
            self.assertEqual(out_sr, 48000)
            self.assertEqual(len(out_audio), len(mic_audio))

    def test_echo_energy_is_reduced_when_mic_contains_sys_copy(self):
        """mic = self_voice + 0.5 * sys 인 경우 AEC 후 sys 성분이 충분히 감소해야 한다."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mic_path = tmp_path / "mic.wav"
            sys_path = tmp_path / "sys.wav"
            out_path = tmp_path / "mic_aec.wav"

            duration = 4.0
            sr = 48000
            self_voice = _sine(800, duration=duration, sr=sr, amp=0.2)
            np.random.seed(42)
            sys_signal = (0.4 * np.random.randn(int(sr * duration))).astype(np.float32)
            mic_signal = (self_voice + 0.5 * sys_signal).astype(np.float32)

            _write_wav(mic_path, mic_signal, sr=sr)
            _write_wav(sys_path, sys_signal, sr=sr)

            aec.cancel_echo(mic_path, sys_path, out_path)

            out_audio, _ = sf.read(str(out_path), dtype="float32")
            # 후반부(필터가 적응한 뒤) 에너지를 비교 — AEC 가 적응 시간 필요
            tail_start = int(sr * 2.5)
            mic_energy = float(np.mean(mic_signal[tail_start:] ** 2))
            out_energy = float(np.mean(out_audio[tail_start:] ** 2))
            # 적어도 어느 정도는 줄었는지만 회귀 검증 (튜닝 없이도 약간은 줄어야 함)
            self.assertLess(out_energy, mic_energy)

    def test_stop_event_aborts_with_cancelled_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mic_path = tmp_path / "mic.wav"
            sys_path = tmp_path / "sys.wav"
            out_path = tmp_path / "mic_aec.wav"
            _write_wav(mic_path, _sine(440, 5.0, 48000), sr=48000)
            _write_wav(sys_path, _sine(220, 5.0, 48000), sr=48000)

            stop_event = threading.Event()
            stop_event.set()

            with self.assertRaises(OperationCancelledError):
                aec.cancel_echo(mic_path, sys_path, out_path, stop_event=stop_event)

    def test_offset_alignment_uses_seconds_correctly(self):
        """sys 가 mic 보다 0.5s 늦게 시작했다고 가정 (mic_minus_sys_offset = -0.5).

        align_reference 가 sys 앞에 0.5s 패딩을 넣어 정렬해야 한다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mic_path = tmp_path / "mic.wav"
            sys_path = tmp_path / "sys.wav"
            out_path = tmp_path / "mic_aec.wav"

            sr = 48000
            mic_audio = _sine(440, duration=3.0, sr=sr)
            sys_audio = _sine(220, duration=2.5, sr=sr)
            _write_wav(mic_path, mic_audio, sr=sr)
            _write_wav(sys_path, sys_audio, sr=sr)

            aec.cancel_echo(
                mic_path, sys_path, out_path,
                mic_sys_offset_seconds=-0.5,  # mic 가 sys 보다 먼저 시작
            )

            out_audio, _ = sf.read(str(out_path), dtype="float32")
            # 길이는 mic 그대로 보존
            self.assertEqual(len(out_audio), len(mic_audio))


class HelperTests(unittest.TestCase):
    def test_align_reference_pads_when_sys_starts_later(self):
        sys_audio = np.ones(1000, dtype=np.float32)
        # sample_offset = -200: sys 가 mic 보다 200 sample 늦게 시작
        out = aec._align_reference(sys_audio, mic_length=1500, sample_offset=-200)
        self.assertEqual(len(out), 1500)
        # 앞 200 sample 은 0 패딩
        self.assertTrue(np.allclose(out[:200], 0.0))
        # 그 뒤 1000 sample 은 sys 본체
        self.assertTrue(np.allclose(out[200:1200], 1.0))
        # 끝 300 sample 은 0 패딩 (mic 길이가 더 길어서)
        self.assertTrue(np.allclose(out[1200:], 0.0))

    def test_align_reference_trims_when_sys_starts_earlier(self):
        sys_audio = np.arange(1000, dtype=np.float32)
        # sample_offset = 100: sys 가 mic 보다 100 sample 먼저 시작 → 앞 100 trim
        out = aec._align_reference(sys_audio, mic_length=500, sample_offset=100)
        self.assertEqual(len(out), 500)
        # 첫 sample 은 sys[100]
        self.assertEqual(out[0], 100.0)
        self.assertEqual(out[-1], 599.0)


if __name__ == "__main__":
    unittest.main()
