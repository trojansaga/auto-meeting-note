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
        self._mic_process: Optional[subprocess.Popen] = None     # ffmpeg 마이크
        self._sys_audio = None                                    # SystemAudioCapture (화면 녹화 + 녹음 공용)
        self._mode: Optional[str] = None  # "screen" | "audio"
        self._output_path: Optional[Path] = None
        self._audio_path: Optional[Path] = None   # 시스템 오디오 WAV
        self._mic_path: Optional[Path] = None     # 마이크 오디오 WAV
        self._audio_offset: float = 0.0           # 화면 녹화 시 오디오 선행 시간(초)
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

    def _start_mic(self, mic_path: Path, mic_device_index: str) -> None:
        """ffmpeg avfoundation으로 마이크 녹음 시작."""
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            logger.warning("ffmpeg 없음 — 마이크 녹음 건너뜀")
            return
        cmd = [
            ffmpeg_bin,
            "-f", "avfoundation",
            "-i", f":{mic_device_index}",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            "-y", str(mic_path),
        ]
        self._mic_process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("마이크 녹음 시작: %s", mic_path.name)

    def _stop_mic(self) -> None:
        """ffmpeg 마이크 녹음 중지."""
        if self._mic_process is None:
            return
        try:
            if self._mic_process.poll() is None:
                try:
                    self._mic_process.stdin.write(b"q\n")
                    self._mic_process.stdin.flush()
                except OSError:
                    self._mic_process.terminate()
            self._mic_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._mic_process.kill()
            self._mic_process.wait()
        except Exception as e:
            logger.error("마이크 중지 오류: %s", e)
        self._mic_process = None

    def start_screen_recording(
        self, output_dir: Path, mic_enabled: bool = True, mic_device_index: str = "0"
    ) -> Path:
        """화면 녹화 시작. SCStream 시스템 오디오 + 선택적 마이크 동시 캡처.
        SCStream 초기화가 블로킹이므로 반드시 백그라운드 스레드에서 호출해야 함."""
        with self._lock:
            from system_audio import SystemAudioCapture

            ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            mov_path = output_dir / f"{ts}_녹화.mov"
            audio_path = output_dir / f"{ts}_녹화_sys.wav"

            # 1) SCStream: 시스템 오디오 캡처 (블로킹, ~1초 소요)
            self._sys_audio = SystemAudioCapture()
            self._sys_audio.start(audio_path)
            sys_audio_ready_time = time.time()  # 이 시점부터 오디오 기록 시작
            logger.info("시스템 오디오 캡처 시작: %s", audio_path.name)

            # 2) ffmpeg: 마이크 동시 캡처 (선택)
            if mic_enabled:
                mic_path = output_dir / f"{ts}_녹화_mic.wav"
                self._start_mic(mic_path, mic_device_index)
                self._mic_path = mic_path if self._mic_process else None
            else:
                self._mic_path = None

            # 3) screencapture: 영상 캡처 (stdin=PIPE 필수, DEVNULL이면 즉시 종료)
            sc_cmd = ["/usr/sbin/screencapture", "-v", str(mov_path)]
            screen_start_time = time.time()
            logger.info("화면 녹화 시작: %s", mov_path.name)
            self._screen_process = subprocess.Popen(
                sc_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # 오디오(sys_audio_ready_time)와 영상(screen_start_time)의 시작 차이만큼 trim
            # t_before_sys 기준이 아닌 실제 오디오 기록 시작 시점 기준
            self._audio_offset = screen_start_time - sys_audio_ready_time

            self._mode = "screen"
            self._output_path = mov_path
            self._audio_path = audio_path
            self._start_time = screen_start_time
            return mov_path

    def start_audio_recording(
        self, output_dir: Path, mic_enabled: bool = True, mic_device_index: str = "0"
    ) -> Path:
        """녹음 시작. SCStream 시스템 오디오 + 선택적 마이크 동시 캡처.
        SCStream 초기화가 블로킹이므로 반드시 백그라운드 스레드에서 호출해야 함."""
        with self._lock:
            from system_audio import SystemAudioCapture

            ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            sys_path = output_dir / f"{ts}_녹음_sys.wav"

            self._sys_audio = SystemAudioCapture()
            self._sys_audio.start(sys_path)

            if mic_enabled:
                mic_path = output_dir / f"{ts}_녹음_mic.wav"
                self._start_mic(mic_path, mic_device_index)
                self._mic_path = mic_path if self._mic_process else None
            else:
                self._mic_path = None

            self._mode = "audio"
            self._output_path = sys_path
            self._start_time = time.time()
            return sys_path

    def stop(self) -> tuple:
        """녹화/녹음 중지. (mode, output_path, audio_path, mic_path, audio_offset) 반환."""
        with self._lock:
            mode = self._mode
            output_path = self._output_path
            audio_path = self._audio_path
            mic_path = self._mic_path
            audio_offset = self._audio_offset

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

                # 마이크 중지
                self._stop_mic()

            elif mode == "audio":
                if self._sys_audio is not None:
                    try:
                        self._sys_audio.stop()
                    except Exception as e:
                        logger.error("시스템 오디오 중지 오류: %s", e)
                    self._sys_audio = None

                # 마이크 중지
                self._stop_mic()

            self._mode = None
            self._output_path = None
            self._audio_path = None
            self._mic_path = None
            self._audio_offset = 0.0
            self._start_time = None

            logger.info("녹화/녹음 중지 완료")
            return mode, output_path, audio_path, mic_path, audio_offset

    def mix_wav(self, sys_path: Path, mic_path: Path) -> Path:
        """시스템 오디오 + 마이크 WAV를 amix로 믹싱 → 단일 WAV 반환. 원본 삭제."""
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise EnvironmentError("ffmpeg가 설치되어 있지 않습니다.")

        out_path = sys_path.with_name(sys_path.stem.replace("_sys", "") + "_mixed.wav")
        has_sys = sys_path.exists() and sys_path.stat().st_size > 44
        has_mic = mic_path.exists() and mic_path.stat().st_size > 44

        if has_sys and has_mic:
            cmd = [
                ffmpeg_bin,
                "-i", str(sys_path),
                "-i", str(mic_path),
                "-filter_complex", "amix=inputs=2:duration=longest:normalize=0",
                "-y", str(out_path),
            ]
            logger.info("오디오 믹싱: %s + %s → %s", sys_path.name, mic_path.name, out_path.name)
        elif has_sys:
            out_path = sys_path
            logger.info("마이크 없음 — 시스템 오디오만 사용: %s", sys_path.name)
            return out_path
        elif has_mic:
            out_path = mic_path
            logger.info("시스템 오디오 없음 — 마이크만 사용: %s", mic_path.name)
            return out_path
        else:
            raise RuntimeError("시스템 오디오와 마이크 파일 모두 없음")

        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            raise RuntimeError("오디오 믹싱 실패")

        for f in [sys_path, mic_path]:
            if f != out_path and f.exists():
                try:
                    os.remove(str(f))
                except OSError:
                    pass

        return out_path

    def compress_and_merge(
        self,
        mov_path: Path,
        audio_path: Optional[Path],
        mic_path: Optional[Path] = None,
        audio_offset: float = 0.0,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """MOV + 시스템 오디오 WAV [+ 마이크 WAV] → H.264 MP4로 병합 압축. 원본 파일 삭제.

        audio_offset: 오디오가 영상보다 먼저 시작된 시간(초). 양수면 오디오 앞부분 trim.
        """
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise EnvironmentError("ffmpeg가 설치되어 있지 않습니다.")

        mp4_path = mov_path.with_suffix(".mp4")

        has_sys = audio_path and audio_path.exists() and audio_path.stat().st_size > 44
        has_mic = mic_path and mic_path.exists() and mic_path.stat().st_size > 44

        # 오디오 싱크 보정: 오디오가 영상보다 먼저 시작된 경우 앞부분 skip
        ss = ["-ss", f"{audio_offset:.3f}"] if audio_offset > 0.05 else []
        logger.info("오디오 싱크 오프셋: %.3fs", audio_offset)

        if has_sys and has_mic:
            # 영상 + 시스템 오디오 + 마이크 (amix)
            cmd = [
                ffmpeg_bin,
                "-i", str(mov_path),
                *ss, "-i", str(audio_path),
                *ss, "-i", str(mic_path),
                "-filter_complex", "amix=inputs=2:duration=longest:normalize=0[aout]",
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                "-c:a", "aac",
                "-map", "0:v:0", "-map", "[aout]",
                "-shortest",
                "-y", str(mp4_path),
            ]
            logger.info("영상+시스템오디오+마이크 병합: %s", mp4_path.name)
        elif has_sys:
            # 영상 + 시스템 오디오
            cmd = [
                ffmpeg_bin,
                "-i", str(mov_path),
                *ss, "-i", str(audio_path),
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                "-c:a", "aac",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                "-y", str(mp4_path),
            ]
            logger.info("영상+시스템오디오 병합: %s", mp4_path.name)
        elif has_mic:
            # 영상 + 마이크만
            cmd = [
                ffmpeg_bin,
                "-i", str(mov_path),
                *ss, "-i", str(mic_path),
                "-c:v", "libx264", "-crf", "23", "-preset", "medium",
                "-c:a", "aac",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                "-y", str(mp4_path),
            ]
            logger.info("영상+마이크 병합: %s", mp4_path.name)
        else:
            # 오디오 없음 → 무음 트랙
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
        for f in [mov_path, audio_path, mic_path]:
            if f and f.exists():
                try:
                    os.remove(str(f))
                except OSError as e:
                    logger.warning("임시 파일 삭제 실패: %s", e)

        logger.info("압축 완료: %s", mp4_path.name)
        return mp4_path
