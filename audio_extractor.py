import logging
import os
import re
import shutil
import subprocess
from collections import deque
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

# 실패 시 원인 파악용으로 보관할 ffmpeg stderr 마지막 줄 수
_STDERR_TAIL_LINES = 12

# 입력에 오디오가 없을 때 ffmpeg 가 내는 문구.
# 실측: "[out#0/wav @ ...] Output file does not contain any stream"
_NO_STREAM_RE = re.compile(r"Output file .*does not contain any stream", re.IGNORECASE)


class NoAudioTrackError(RuntimeError):
    """입력 영상에 오디오 트랙이 없어 음성을 추출할 수 없음.

    녹화 종료 직후의 오디오 병합이 실패하면 영상만 담긴 mp4 가 남는데,
    그 파일을 파이프라인에 넣으면 ffmpeg 가 출력 스트림 없음으로 실패한다.
    """


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
    # ffmpeg 는 실패 이유를 stderr 로만 알린다. 진행률 파싱하며 흘려보내면 종료 코드만 남아
    # "exit code 234" 같은 무의미한 메시지가 되므로 마지막 줄들을 보관한다.
    stderr_tail: deque = deque(maxlen=_STDERR_TAIL_LINES)
    no_output_stream = False

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

        stderr_tail.append(line)
        if _NO_STREAM_RE.search(line):
            no_output_stream = True

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
        if no_output_stream:
            raise NoAudioTrackError(
                f"'{mp4.name}' 에 오디오 트랙이 없습니다. "
                "녹화 종료 직후의 오디오 병합이 실패해 영상만 저장된 파일입니다. "
                "같은 이름의 _sys.wav 가 남아 있다면 그것으로 병합을 다시 시도할 수 있고, "
                "없다면 이 영상의 음성은 복구할 수 없습니다."
            )
        detail = " / ".join(stderr_tail) or "stderr 출력 없음"
        raise RuntimeError(f"ffmpeg 실행 실패 (exit code {process.returncode}): {detail}")

    if progress_callback:
        progress_callback("[2/5] 음성 추출 완료 (100%)")

    logger.info("음성 추출 완료: %s", output.name)
    return str(output)
