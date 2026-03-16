import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

FFMPEG_SEARCH_PATHS = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
]


def _find_ffmpeg() -> str | None:
    for p in FFMPEG_SEARCH_PATHS:
        if Path(p).exists():
            return p
    return shutil.which("ffmpeg")


def check_ffmpeg() -> bool:
    return _find_ffmpeg() is not None


def _parse_duration(line: str) -> Optional[float]:
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", line)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return None


def _parse_time(line: str) -> Optional[float]:
    m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return None


def extract_audio(
    mp4_path: str,
    output_path: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    ffmpeg_bin = _find_ffmpeg()
    if not ffmpeg_bin:
        raise EnvironmentError(
            "ffmpeg가 설치되어 있지 않습니다. 'brew install ffmpeg'로 설치하세요."
        )

    mp4 = Path(mp4_path)
    if not mp4.exists():
        raise FileNotFoundError(f"입력 파일을 찾을 수 없습니다: {mp4_path}")

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        ffmpeg_bin,
        "-i", str(mp4),
        "-vn",
        "-acodec", "libmp3lame",
        "-ar", "16000",
        "-ac", "1",
        "-b:a", "32k",
        "-y",
        str(output),
    ]

    logger.info("음성 추출 시작: %s → %s", mp4.name, output.name)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    total_duration: Optional[float] = None

    for line in process.stderr:
        line = line.strip()
        if not line:
            continue

        if total_duration is None:
            d = _parse_duration(line)
            if d is not None:
                total_duration = d

        if progress_callback and total_duration:
            t = _parse_time(line)
            if t is not None:
                pct = min(t / total_duration * 100, 99)
                progress_callback(f"[2/5] 음성 추출 중... {pct:.0f}%")

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg 실행 실패 (exit code {process.returncode})")

    if progress_callback:
        progress_callback("[2/5] 음성 추출 완료 (100%)")

    logger.info("음성 추출 완료: %s", output.name)
    return str(output)
