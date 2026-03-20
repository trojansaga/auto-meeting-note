import logging
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from audio_extractor import find_ffmpeg

logger = logging.getLogger(__name__)


class Recorder:
    def __init__(self):
        self._screen_process: Optional[subprocess.Popen] = None  # screencapture
        self._sys_audio = None                                    # SystemAudioCapture (화면 녹화 + 녹음 공용)
        self._mode: Optional[str] = None  # "screen" | "audio"
        self._output_path: Optional[Path] = None
        self._audio_path: Optional[Path] = None   # 화면 녹화 시 시스템 오디오 WAV
        self._start_time: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def is_recording(self) -> bool:
        if self._mode == "screen":
            return self._screen_process is not None and self._screen_process.poll() is None
        if self._mode == "audio":
            return self._sys_audio is not None
        return False

    @property
    def mode(self) -> Optional[str]:
        return self._mode

    @property
    def elapsed_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    def start_screen_recording(self, output_dir: Path) -> Path:
        """화면 녹화 시작. SCStream으로 시스템 오디오 동시 캡처.
        SCStream 초기화가 블로킹이므로 반드시 백그라운드 스레드에서 호출해야 함."""
        with self._lock:
            from system_audio import SystemAudioCapture

            ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            mov_path = output_dir / f"{ts}_녹화.mov"
            audio_path = output_dir / f"{ts}_녹화_audio.wav"

            # 1) SCStream: 시스템 오디오 캡처 (블로킹, ~1초 소요)
            self._sys_audio = SystemAudioCapture()
            self._sys_audio.start(audio_path)
            logger.info("시스템 오디오 캡처 시작: %s", audio_path.name)

            # 2) screencapture: 영상 캡처 (stdin=PIPE 필수, DEVNULL이면 즉시 종료)
            sc_cmd = ["/usr/sbin/screencapture", "-v", str(mov_path)]
            logger.info("화면 녹화 시작: %s", mov_path.name)
            self._screen_process = subprocess.Popen(
                sc_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            self._mode = "screen"
            self._output_path = mov_path
            self._audio_path = audio_path
            self._start_time = time.time()
            return mov_path

    def start_audio_recording(self, output_dir: Path, **kwargs) -> Path:
        with self._lock:
            from system_audio import SystemAudioCapture

            filename = datetime.now().strftime("%Y-%m-%d %H-%M-%S") + "_녹음.wav"
            output_path = output_dir / filename

            self._sys_audio = SystemAudioCapture()
            self._sys_audio.start(output_path)

            self._mode = "audio"
            self._output_path = output_path
            self._start_time = time.time()
            return output_path

    def stop(self) -> tuple:
        """녹화/녹음 중지. (mode, output_path) 반환."""
        with self._lock:
            mode = self._mode
            output_path = self._output_path
            audio_path = self._audio_path

            if mode == "screen":
                # screencapture 중지: stdin에 개행 전송
                if self._screen_process is not None:
                    try:
                        if self._screen_process.poll() is None:
                            try:
                                self._screen_process.stdin.write(b"\n")
                                self._screen_process.stdin.flush()
                            except OSError:
                                os.kill(self._screen_process.pid, signal.SIGINT)
                        self._screen_process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        self._screen_process.kill()
                        self._screen_process.wait()
                    except Exception as e:
                        logger.error("screencapture 중지 오류: %s", e)
                    self._screen_process = None

                # 시스템 오디오 중지
                if self._sys_audio is not None:
                    try:
                        self._sys_audio.stop()
                    except Exception as e:
                        logger.error("시스템 오디오 중지 오류: %s", e)
                    self._sys_audio = None

            elif mode == "audio":
                if self._sys_audio is not None:
                    try:
                        self._sys_audio.stop()
                    except Exception as e:
                        logger.error("시스템 오디오 중지 오류: %s", e)
                    self._sys_audio = None

            self._mode = None
            self._output_path = None
            self._audio_path = None
            self._start_time = None

            logger.info("녹화/녹음 중지 완료")
            # screen 모드: (mode, mov_path, audio_path) / audio 모드: (mode, wav_path, None)
            return mode, output_path, audio_path

    def compress_and_merge(
        self,
        mov_path: Path,
        audio_path: Optional[Path],
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """MOV + 오디오 WAV → H.264 MP4로 병합 압축. 원본 파일 삭제."""
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise EnvironmentError("ffmpeg가 설치되어 있지 않습니다.")

        mp4_path = mov_path.with_suffix(".mp4")

        has_audio_file = audio_path and audio_path.exists() and audio_path.stat().st_size > 1000

        if has_audio_file:
            # 영상 + 별도 오디오 병합
            cmd = [
                ffmpeg_bin,
                "-i", str(mov_path),
                "-i", str(audio_path),
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                "-c:a", "aac",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                "-y", str(mp4_path),
            ]
            logger.info("영상+오디오 병합 압축: %s + %s → %s", mov_path.name, audio_path.name, mp4_path.name)
        else:
            # 오디오 없음 → 무음 트랙으로 압축
            cmd = [
                ffmpeg_bin,
                "-i", str(mov_path),
                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                "-c:a", "aac",
                "-map", "0:v", "-map", "1:a",
                "-shortest",
                "-y", str(mp4_path),
            ]
            logger.info("오디오 없음 — 무음 트랙으로 압축: %s", mov_path.name)

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        total_duration: Optional[float] = None
        dur_pat = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
        time_pat = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")

        for line in process.stderr:
            line = line.strip()
            if not line:
                continue
            if total_duration is None:
                m = dur_pat.search(line)
                if m:
                    total_duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if progress_callback and total_duration:
                m = time_pat.search(line)
                if m:
                    t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                    pct = min(t / total_duration * 100, 99)
                    progress_callback(f"녹화 파일 압축 중... {pct:.0f}%")

        process.wait()

        if process.returncode != 0:
            raise RuntimeError(f"압축 실패 (exit code {process.returncode})")

        if progress_callback:
            progress_callback("녹화 파일 압축 완료 (100%)")

        # 원본 파일 삭제
        for f in [mov_path, audio_path]:
            if f and f.exists():
                try:
                    os.remove(str(f))
                except OSError as e:
                    logger.warning("임시 파일 삭제 실패: %s", e)

        logger.info("압축 완료: %s", mp4_path.name)
        return mp4_path
