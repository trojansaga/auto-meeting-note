"""마이크 트랙에서 시스템 출력(스피커) 신호를 빼내는 후처리 AEC.

화상회의 시 상대방 음성이 스피커 → 마이크로 다시 들어가 echo 가 발생.
시스템 오디오가 별도로 깔끔하게 캡처돼 있으므로 이를 reference 로 어댑티브
필터(NLMS, frequency-domain block)를 학습시켜 마이크에서 차감한다.

전제:
- mic / sys 둘 다 mono 또는 stereo WAV (stereo 면 mono 평균)
- mic_offset (시스템 시작 - 마이크 시작) 으로 sys 를 미리 정렬
- sample rate 같음 (mic 48k, sys 48k 가정 — 다르면 sys 를 mic rate 로 resample)

싱크 영향:
- 출력 WAV 는 입력 mic 와 동일한 sample 수 / sample rate / 시작 시점
- 어댑티브 필터의 group delay 는 1ms 미만으로 영상 싱크에 무의미
"""
from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
from typing import Optional

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from cancellation import OperationCancelledError

logger = logging.getLogger(__name__)

# Frequency-domain block NLMS 파라미터
_FRAME_SIZE = 1024              # 64ms @ 16kHz, 21ms @ 48kHz
_PROCESSING_SR = 16000          # 다운샘플 후 처리 (속도/안정성 trade-off)
_MU = 0.3                       # NLMS step size (0.1~0.5, 클수록 빠른 적응 + 발산 위험)
_REGULARIZATION = 1e-3          # NLMS 분모 안정화
_LEAK = 0.999                   # 필터 weight leakage (정적 echo 적응 후 망각 방지)


def _load_mono(path: Path, target_sr: int) -> tuple[np.ndarray, int]:
    """WAV 로드 → mono 평균 → target_sr 로 resample."""
    audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1).astype(np.float32)
    if sr != target_sr:
        # rational resample. 정확한 ratio 보존 위해 gcd 기반 up/down
        from math import gcd
        g = gcd(sr, target_sr)
        up = target_sr // g
        down = sr // g
        audio = resample_poly(audio, up, down).astype(np.float32)
    return audio, target_sr


def _align_reference(
    sys_audio: np.ndarray,
    mic_length: int,
    sample_offset: int,
) -> np.ndarray:
    """sys 신호를 mic 시작 시점으로 정렬해 mic_length 길이로 자르거나 패딩.

    sample_offset > 0: sys 가 mic 보다 먼저 시작 → sys 앞부분 잘라냄
    sample_offset < 0: sys 가 mic 보다 늦게 시작 → sys 앞에 0 패딩
    """
    if sample_offset > 0:
        sys_audio = sys_audio[sample_offset:]
    elif sample_offset < 0:
        sys_audio = np.concatenate([
            np.zeros(-sample_offset, dtype=np.float32),
            sys_audio,
        ])

    if len(sys_audio) >= mic_length:
        return sys_audio[:mic_length]
    return np.concatenate([
        sys_audio,
        np.zeros(mic_length - len(sys_audio), dtype=np.float32),
    ])


def _fdaf(
    mic: np.ndarray,
    ref: np.ndarray,
    frame_size: int,
    mu: float,
    leak: float,
    regularization: float,
    stop_event: Optional[Event] = None,
) -> np.ndarray:
    """Frequency-domain block NLMS adaptive filter.

    mic, ref 는 동일 길이의 1D float32. ref 신호의 echo 성분을 mic 에서 차감.
    block-based 처리로 numpy FFT 만 사용 (1시간 분량도 수십 초).
    """
    n = len(mic)
    out = np.zeros_like(mic)
    fft_size = 2 * frame_size
    weights = np.zeros(fft_size // 2 + 1, dtype=np.complex64)
    prev_ref = np.zeros(frame_size, dtype=np.float32)
    eps = np.float32(regularization)

    n_blocks = n // frame_size
    for b in range(n_blocks):
        if stop_event is not None and stop_event.is_set():
            raise OperationCancelledError("에코 제거가 중단되었습니다.")

        i0 = b * frame_size
        i1 = i0 + frame_size

        ref_block = ref[i0:i1]
        x_block = np.concatenate([prev_ref, ref_block])  # overlap-save
        prev_ref = ref_block

        X = np.fft.rfft(x_block, fft_size)
        Y = X * weights
        y_full = np.fft.irfft(Y, fft_size)
        y = y_full[frame_size:]  # 후반부만 사용 (overlap-save)

        d = mic[i0:i1]
        e = (d - y).astype(np.float32)
        out[i0:i1] = e

        # NLMS update (frequency domain)
        E_padded = np.concatenate([np.zeros(frame_size, dtype=np.float32), e])
        E = np.fft.rfft(E_padded, fft_size)
        norm = np.abs(X) ** 2 + eps
        weights = leak * weights + mu * np.conj(X) * E / norm

    # 마지막 미정렬 블록은 학습된 필터로 처리
    rem = n - n_blocks * frame_size
    if rem > 0:
        i0 = n_blocks * frame_size
        ref_block = np.concatenate([
            ref[i0:i0 + rem],
            np.zeros(frame_size - rem, dtype=np.float32),
        ])
        x_block = np.concatenate([prev_ref, ref_block])
        X = np.fft.rfft(x_block, fft_size)
        Y = X * weights
        y_full = np.fft.irfft(Y, fft_size)
        y = y_full[frame_size:frame_size + rem]
        d = mic[i0:i0 + rem]
        out[i0:i0 + rem] = d - y

    return out.astype(np.float32)


def cancel_echo(
    mic_path: Path,
    sys_path: Path,
    output_path: Path,
    mic_sys_offset_seconds: float = 0.0,
    stop_event: Optional[Event] = None,
) -> Path:
    """마이크에서 시스템 오디오 echo 를 차감해 새 WAV 출력.

    mic_sys_offset_seconds: mic 시작 시점 - sys 시작 시점 (초).
        양수면 mic 가 sys 보다 늦게 시작 → sys 앞부분을 같은 크기만큼 건너뛰고 사용.
        amix 시 사용하던 audio_offset 부호 규칙과 정합.
    출력 WAV 는 입력 mic 의 sample rate / 길이 / 시작 시점을 보존.
    """
    if stop_event is not None and stop_event.is_set():
        raise OperationCancelledError("에코 제거가 중단되었습니다.")

    # 원본 mic 의 sample rate 와 길이를 보존하기 위해 두 단계로 처리:
    # 1) 16k 로 다운샘플한 mic/sys 로 echo 제거 → mic_aec_16k
    # 2) 결과를 다시 mic 원본 sr 로 업샘플 → 길이 맞춰 자름

    mic_orig, mic_sr = sf.read(str(mic_path), dtype="float32", always_2d=False)
    if mic_orig.ndim > 1:
        mic_orig = mic_orig.mean(axis=1).astype(np.float32)

    mic_16k, _ = _load_mono(mic_path, _PROCESSING_SR)
    sys_16k, _ = _load_mono(sys_path, _PROCESSING_SR)

    sample_offset = int(round(mic_sys_offset_seconds * _PROCESSING_SR))
    sys_aligned = _align_reference(sys_16k, len(mic_16k), sample_offset)

    logger.info(
        "AEC 처리 시작: mic_len=%.1fs, ref_len=%.1fs, offset=%.3fs",
        len(mic_16k) / _PROCESSING_SR,
        len(sys_16k) / _PROCESSING_SR,
        mic_sys_offset_seconds,
    )

    mic_aec_16k = _fdaf(
        mic_16k,
        sys_aligned,
        frame_size=_FRAME_SIZE,
        mu=_MU,
        leak=_LEAK,
        regularization=_REGULARIZATION,
        stop_event=stop_event,
    )

    # 원본 sr 로 복원 (길이 보존 위해 자르거나 패딩)
    if mic_sr != _PROCESSING_SR:
        from math import gcd
        g = gcd(mic_sr, _PROCESSING_SR)
        up = mic_sr // g
        down = _PROCESSING_SR // g
        mic_aec = resample_poly(mic_aec_16k, up, down).astype(np.float32)
    else:
        mic_aec = mic_aec_16k

    if len(mic_aec) >= len(mic_orig):
        mic_aec = mic_aec[:len(mic_orig)]
    else:
        mic_aec = np.concatenate([
            mic_aec,
            np.zeros(len(mic_orig) - len(mic_aec), dtype=np.float32),
        ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), mic_aec, mic_sr)
    logger.info("AEC 처리 완료 → %s", output_path.name)
    return output_path
