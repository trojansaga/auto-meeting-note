import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import whisper  # 오디오 길이 측정용

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
LIGHTNING_SPEED_ESTIMATE = 6.0  # x realtime (medium batch=12 실측 기반)

_model_cache: dict = {}


def _get_model(model_name: str, batch_size: int = 12):
    cache_key = f"{model_name}_b{batch_size}"
    if cache_key not in _model_cache:
        from lightning_whisper_mlx import LightningWhisperMLX
        logger.info("LightningWhisperMLX 모델 로딩 중: %s (batch_size=%d)", model_name, batch_size)
        _model_cache[cache_key] = LightningWhisperMLX(
            model=model_name, batch_size=batch_size, quant=None
        )
        logger.info("모델 로딩 완료: %s", model_name)
    return _model_cache[cache_key]


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def transcribe(
    wav_path: str,
    output_path: str,
    original_filename: str,
    model_name: str = "medium",
    language: str = "ko",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    wav = Path(wav_path)
    if not wav.exists():
        raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {wav_path}")

    logger.info("STT 처리 시작: %s (모델: %s)", wav.name, model_name)

    # 오디오 길이 측정
    audio = whisper.load_audio(str(wav))
    audio_duration = len(audio) / SAMPLE_RATE
    expected_secs = audio_duration / LIGHTNING_SPEED_ESTIMATE

    model = _get_model(model_name)

    # 진행률 타이머 스레드 (예상 처리속도 기반)
    stop_event = threading.Event()

    def _progress_loop():
        start = time.time()
        while not stop_event.is_set():
            elapsed = time.time() - start
            pct = min(elapsed / expected_secs * 100, 99)
            if progress_callback:
                progress_callback(f"[3/5] STT 처리 중... {pct:.0f}%")
            time.sleep(1)

    if progress_callback:
        t = threading.Thread(target=_progress_loop, daemon=True)
        t.start()

    try:
        result = model.transcribe(audio_path=str(wav), language=language)
    finally:
        stop_event.set()

    if progress_callback:
        progress_callback("[3/5] STT 처리 완료 (100%)")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# 회의 대본\n",
        f"- 파일명: {original_filename}",
        f"- 생성일시: {now}\n",
        "## Transcript\n",
    ]

    for seg in result.get("segments", []):
        # lightning-whisper-mlx 포맷: [start_ms, end_ms, text]
        if isinstance(seg, (list, tuple)) and len(seg) >= 3:
            start_sec = seg[0] / 1000.0
            text = seg[2].strip()
        elif isinstance(seg, dict):
            start_sec = seg.get("start", 0)
            text = seg.get("text", "").strip()
        else:
            continue
        if text:
            lines.append(f"{_format_timestamp(start_sec)} {text}")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")

    logger.info("STT 처리 완료 → %s", output.name)
    return str(output)
