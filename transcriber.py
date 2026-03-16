import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import openai

logger = logging.getLogger(__name__)

MAX_DURATION_SECS = 1200  # 20분 (API 한도 1400초에 여유)
CHUNK_SECS = 1200


def _get_duration(audio_path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def _split_audio(audio_path: Path) -> tuple[list[Path], Path]:
    tmp_dir = Path(tempfile.mkdtemp())
    pattern = str(tmp_dir / "chunk_%03d.mp3")
    cmd = [
        "ffmpeg", "-i", str(audio_path),
        "-f", "segment", "-segment_time", str(CHUNK_SECS),
        "-acodec", "copy", "-y", pattern,
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return sorted(tmp_dir.glob("chunk_*.mp3")), tmp_dir


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def transcribe(
    audio_path: str,
    output_path: str,
    original_filename: str,
    model_name: str = "gpt-4o-transcribe",
    language: str = "ko",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    audio = Path(audio_path)
    if not audio.exists():
        raise FileNotFoundError(f"오디오 파일을 찾을 수 없습니다: {audio_path}")

    logger.info("STT 처리 시작: %s (모델: %s)", audio.name, model_name)

    client = openai.OpenAI()
    duration = _get_duration(audio)

    tmp_dir = None
    try:
        if duration > MAX_DURATION_SECS:
            chunks, tmp_dir = _split_audio(audio)
            logger.info("재생 길이 초과 (%.0f초), %d개 청크로 분할", duration, len(chunks))
        else:
            chunks = [audio]

        all_segments: list[tuple[float, str]] = []
        time_offset = 0.0

        for i, chunk in enumerate(chunks):
            if progress_callback:
                pct = int(i / len(chunks) * 100)
                progress_callback(f"[3/5] STT 처리 중... {pct}%")

            with open(chunk, "rb") as f:
                text = client.audio.transcriptions.create(
                    model=model_name,
                    file=f,
                    language=language,
                    response_format="text",
                )

            offset_sec = i * CHUNK_SECS
            all_segments.append((float(offset_sec), str(text).strip()))

    finally:
        if tmp_dir and tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if progress_callback:
        progress_callback("[3/5] STT 처리 완료 (100%)")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# 회의 대본\n",
        f"- 파일명: {original_filename}",
        f"- 생성일시: {now}\n",
        "## Transcript\n",
    ]
    for start_sec, text in all_segments:
        if text:
            lines.append(f"{_format_timestamp(start_sec)} {text}")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")

    logger.info("STT 처리 완료 → %s", output.name)
    return str(output)
