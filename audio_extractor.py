import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from threading import Event
from typing import Callable, Optional

from cancellation import OperationCancelledError

logger = logging.getLogger(__name__)

FFMPEG_SEARCH_PATHS = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
]


def find_ffmpeg() -> str | None:
    for p in FFMPEG_SEARCH_PATHS:
        if Path(p).exists():
            return p
    return shutil.which("ffmpeg")


def check_ffmpeg() -> bool:
    return find_ffmpeg() is not None


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
    stop_event: Optional[Event] = None,
    sample_rate: Optional[int] = 16000,
    channels: Optional[int] = 1,
) -> str:
    ffmpeg_bin = find_ffmpeg()
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
        "-acodec", "pcm_s16le",
    ]
    if sample_rate is not None:
        cmd.extend(["-ar", str(sample_rate)])
    if channels is not None:
        cmd.extend(["-ac", str(channels)])
    cmd.extend([
        "-y",
        str(output),
    ])

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
        if stop_event is not None and stop_event.is_set():
            logger.info("음성 추출 중단 요청: %s", mp4.name)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if output.exists():
                output.unlink(missing_ok=True)
            raise OperationCancelledError("음성 추출이 중단되었습니다.")

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

    if stop_event is not None and stop_event.is_set():
        if output.exists():
            output.unlink(missing_ok=True)
        raise OperationCancelledError("음성 추출이 중단되었습니다.")

    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg 실행 실패 (exit code {process.returncode})")

    if progress_callback:
        progress_callback("[2/5] 음성 추출 완료 (100%)")

    logger.info("음성 추출 완료: %s", output.name)
    return str(output)
