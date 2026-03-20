import logging
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
FRAME_MS = 30        # VAD 프레임 크기 (ms)
MIN_SPEECH_MS = 250  # 유지할 최소 음성 구간 길이
MERGE_GAP_MS = 400   # 이 이하 간격의 구간은 합침
PAD_MS = 150         # 음성 구간 앞뒤 여유
TARGET_RMS = 0.15    # 정규화 목표 RMS
MAX_GAIN = 12.0      # 최대 증폭 배율 (과도한 증폭 방지)


def _energy_vad(
    audio: np.ndarray,
    sr: int,
) -> List[Tuple[int, int]]:
    """에너지 기반 VAD. 음성 구간의 (시작, 종료) 샘플 인덱스 목록 반환."""
    frame_size = int(sr * FRAME_MS / 1000)

    rms_list = []
    for i in range(0, len(audio), frame_size):
        frame = audio[i : i + frame_size]
        rms_list.append(float(np.sqrt(np.mean(frame**2) + 1e-12)))
    rms_arr = np.array(rms_list)

    # 하위 20% 구간을 노이즈 플로어로 추정, 3배를 임계값으로 사용
    sorted_rms = np.sort(rms_arr)
    noise_floor = float(np.mean(sorted_rms[: max(1, len(sorted_rms) // 5)]))
    threshold = noise_floor * 3.0

    speech_mask = rms_arr > threshold

    segments: List[List[int]] = []
    in_speech = False
    start = 0
    for idx, is_speech in enumerate(speech_mask):
        pos = idx * frame_size
        if is_speech and not in_speech:
            start = pos
            in_speech = True
        elif not is_speech and in_speech:
            segments.append([start, pos])
            in_speech = False
    if in_speech:
        segments.append([start, len(audio)])

    # 인접 구간 합치기
    merge_samples = int(sr * MERGE_GAP_MS / 1000)
    merged: List[List[int]] = []
    for seg in segments:
        if merged and seg[0] - merged[-1][1] < merge_samples:
            merged[-1][1] = seg[1]
        else:
            merged.append(list(seg))

    # 너무 짧은 구간 제거
    min_samples = int(sr * MIN_SPEECH_MS / 1000)
    merged = [s for s in merged if s[1] - s[0] >= min_samples]

    # 앞뒤 여유 추가
    pad_samples = int(sr * PAD_MS / 1000)
    n = len(audio)
    return [(max(0, s - pad_samples), min(n, e + pad_samples)) for s, e in merged]


def _normalize_segments(
    audio: np.ndarray,
    segments: List[Tuple[int, int]],
) -> np.ndarray:
    """각 음성 구간의 RMS를 TARGET_RMS로 독립적으로 정규화."""
    result = audio.copy()
    for s, e in segments:
        seg = audio[s:e]
        rms = float(np.sqrt(np.mean(seg**2) + 1e-12))
        if rms > 1e-6:
            gain = min(TARGET_RMS / rms, MAX_GAIN)
            result[s:e] = np.clip(seg * gain, -1.0, 1.0)
    return result


def preprocess_audio(
    wav_path: str,
    output_path: str,
    progress_callback: Optional[Callable[[str], None]] = None,
    noise_reduce: bool = True,
    vad: bool = True,
    normalize: bool = True,
) -> str:
    """
    오디오 전처리 파이프라인 (각 단계 개별 활성화 가능):
    1. 배경 노이즈 제거 (noisereduce)
    2. 침묵 구간 제거 (에너지 기반 VAD)
    3. 화자 음량 정규화 (구간별 RMS 정규화)
    """
    def _notify(msg: str):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    steps = []
    if noise_reduce:
        steps.append("노이즈 제거")
    if vad:
        steps.append("침묵 제거")
    if normalize:
        steps.append("음량 정규화")

    if not steps:
        logger.info("전처리 비활성화 — 원본 파일 그대로 사용")
        import shutil
        shutil.copy2(wav_path, output_path)
        return output_path

    _notify(f"[3/6] 오디오 로드 중... ({', '.join(steps)})")
    audio, sr = sf.read(wav_path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    original_duration = len(audio) / sr

    # 1. 배경 노이즈 제거
    if noise_reduce:
        import noisereduce as nr
        _notify("[3/6] 배경 노이즈 제거 중...")
        audio = nr.reduce_noise(y=audio, sr=sr, stationary=False, prop_decrease=0.75)

    # 2. 침묵 구간 제거 (VAD)
    if vad:
        _notify("[3/6] 침묵 구간 감지 중...")
        segments = _energy_vad(audio, sr)
        logger.info("음성 구간 감지: %d개", len(segments))

        if not segments:
            logger.warning("음성 구간을 감지하지 못했습니다 — 원본 오디오를 그대로 사용합니다.")
            sf.write(output_path, audio, sr)
            import gc
            del audio
            gc.collect()
            _notify("[3/6] 전처리 완료 (음성 구간 미감지, 원본 사용)")
            return output_path

        # 3. 음량 정규화
        if normalize:
            _notify("[3/6] 화자 음량 정규화 중...")
            audio = _normalize_segments(audio, segments)

        # 음성 구간만 이어 붙이기 (구간 사이 0.3초 침묵 삽입 — Whisper 구간 인식 보조)
        silence_gap = np.zeros(int(sr * 0.3), dtype=np.float32)
        parts = []
        for s, e in segments:
            if parts:
                parts.append(silence_gap)
            parts.append(audio[s:e])
        output_audio = np.concatenate(parts)

        output_duration = len(output_audio) / sr
        removed_pct = (1.0 - output_duration / original_duration) * 100.0
        _notify(
            f"[3/6] 전처리 완료: {original_duration:.0f}초 → {output_duration:.0f}초 "
            f"(침묵 {removed_pct:.0f}% 제거, {len(segments)}개 구간)"
        )
        sf.write(output_path, output_audio, sr)

        import gc
        del audio, output_audio, parts
        gc.collect()
    else:
        # VAD 없이 음량 정규화만
        if normalize:
            _notify("[3/6] 화자 음량 정규화 중...")
            full_seg = [(0, len(audio))]
            audio = _normalize_segments(audio, full_seg)

        _notify(f"[3/6] 전처리 완료: {original_duration:.0f}초")
        sf.write(output_path, audio, sr)

        import gc
        del audio
        gc.collect()

    logger.info("전처리 완료 → %s", Path(output_path).name)
    return output_path
