import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

MODEL_REPOS = {
    "tiny":     {"base": "mlx-community/whisper-tiny-mlx",     "4bit": "mlx-community/whisper-tiny-mlx-4bit",     "8bit": "mlx-community/whisper-tiny-mlx-8bit"},
    "small":    {"base": "mlx-community/whisper-small-mlx",    "4bit": "mlx-community/whisper-small-mlx-4bit",    "8bit": "mlx-community/whisper-small-mlx-8bit"},
    "medium":   {"base": "mlx-community/whisper-medium-mlx",   "4bit": "mlx-community/whisper-medium-mlx-4bit",   "8bit": "mlx-community/whisper-medium-mlx-8bit"},
    "large-v3": {"base": "mlx-community/whisper-large-v3-mlx", "4bit": "mlx-community/whisper-large-v3-mlx-4bit", "8bit": "mlx-community/whisper-large-v3-mlx-8bit"},
}

_model_cache: dict = {}


def _get_repo(model_name: str, quant: Optional[str]) -> str:
    if model_name not in MODEL_REPOS:
        raise ValueError(f"지원하지 않는 모델: {model_name}")
    variants = MODEL_REPOS[model_name]
    key = quant if quant in variants else "base"
    return variants[key]


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def transcribe(
    wav_path: str,
    output_path: str,
    original_filename: str,
    model_name: str = "small",
    quant: Optional[str] = "4bit",
    batch_size: int = 4,
    language: str = "ko",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    import mlx_whisper
    import whisper as _whisper

    wav = Path(wav_path)
    if not wav.exists():
        raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {wav_path}")

    repo = _get_repo(model_name, quant)
    logger.info("STT 처리 시작: %s (repo: %s)", wav.name, repo)

    audio = _whisper.load_audio(str(wav))
    audio_duration = len(audio) / SAMPLE_RATE
    # mlx-whisper는 small 기준 실시간 10배 이상
    expected_secs = audio_duration / 10.0

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
        result = mlx_whisper.transcribe(
            str(wav),
            path_or_hf_repo=repo,
            language=language,
            temperature=0,
        )
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
        start_sec = seg.get("start", 0)
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"{_format_timestamp(start_sec)} {text}")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")

    logger.info("STT 처리 완료 → %s", output.name)
    return str(output)
