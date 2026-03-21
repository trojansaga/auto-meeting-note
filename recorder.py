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
        # pause/resume 세그먼트 지원
        self._segments: list = []          # (output_path, audio_path, mic_path, audio_offset) 목록
        self._is_paused: bool = False
        self._seg_index: int = 0
        self._output_dir: Optional[Path] = None
        self._mic_enabled: bool = True
        self._mic_device_index: str = "0"
        self._base_ts: Optional[str] = None

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
    def is_paused(self) -> bool:
        return self._is_paused

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
            self._segments = []
            self._seg_index = 0
            self._is_paused = False
            self._output_dir = output_dir
            self._mic_enabled = mic_enabled
            self._mic_device_index = mic_device_index
            self._base_ts = ts

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
            self._segments = []
            self._seg_index = 0
            self._is_paused = False
            self._output_dir = output_dir
            self._mic_enabled = mic_enabled
            self._mic_device_index = mic_device_index
            self._base_ts = ts

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

    def _stop_current_processes(self) -> None:
        """현재 실행 중인 프로세스 중지. _lock 보유 상태에서 호출."""
        if self._mode == "screen":
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

            if self._sys_audio is not None:
                try:
                    self._sys_audio.stop()
                except Exception as e:
                    logger.error("시스템 오디오 중지 오류: %s", e)
                self._sys_audio = None

            self._stop_mic()

        elif self._mode == "audio":
            if self._sys_audio is not None:
                try:
                    self._sys_audio.stop()
                except Exception as e:
                    logger.error("시스템 오디오 중지 오류: %s", e)
                self._sys_audio = None

            self._stop_mic()

    def pause(self) -> None:
        """녹화/녹음 일시 정지. 현재 세그먼트를 저장하고 프로세스 중지."""
        with self._lock:
            if self._mode is None or self._is_paused:
                return
            self._stop_current_processes()
            self._segments.append((self._output_path, self._audio_path, self._mic_path, self._audio_offset))
            self._output_path = None
            self._audio_path = None
            self._mic_path = None
            self._audio_offset = 0.0
            self._is_paused = True
            logger.info("일시 정지 (세그먼트 %d 저장)", self._seg_index)

    def resume(self) -> None:
        """일시 정지 재개. 반드시 백그라운드 스레드에서 호출 (SCStream 초기화 블로킹)."""
        with self._lock:
            if not self._is_paused:
                return
            from system_audio import SystemAudioCapture

            self._seg_index += 1
            seg = self._seg_index
            ts = self._base_ts

            if self._mode == "screen":
                mov_path = self._output_dir / f"{ts}_녹화_seg{seg}.mov"
                audio_path = self._output_dir / f"{ts}_녹화_sys_seg{seg}.wav"

                self._sys_audio = SystemAudioCapture()
                self._sys_audio.start(audio_path)
                sys_audio_ready_time = time.time()
                logger.info("재개: 시스템 오디오 시작: %s", audio_path.name)

                if self._mic_enabled:
                    mic_path = self._output_dir / f"{ts}_녹화_mic_seg{seg}.wav"
                    self._start_mic(mic_path, self._mic_device_index)
                    self._mic_path = mic_path if self._mic_process else None
                else:
                    self._mic_path = None

                sc_cmd = ["/usr/sbin/screencapture", "-v", str(mov_path)]
                screen_start_time = time.time()
                logger.info("재개: 화면 녹화 시작: %s", mov_path.name)
                self._screen_process = subprocess.Popen(
                    sc_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self._audio_offset = screen_start_time - sys_audio_ready_time
                self._output_path = mov_path
                self._audio_path = audio_path

            elif self._mode == "audio":
                sys_path = self._output_dir / f"{ts}_녹음_sys_seg{seg}.wav"

                self._sys_audio = SystemAudioCapture()
                self._sys_audio.start(sys_path)
                logger.info("재개: 시스템 오디오 시작: %s", sys_path.name)

                if self._mic_enabled:
                    mic_path = self._output_dir / f"{ts}_녹음_mic_seg{seg}.wav"
                    self._start_mic(mic_path, self._mic_device_index)
                    self._mic_path = mic_path if self._mic_process else None
                else:
                    self._mic_path = None

                self._output_path = sys_path

            self._is_paused = False
            logger.info("녹화/녹음 재개 (세그먼트 %d)", self._seg_index)

    def stop(self) -> tuple:
        """녹화/녹음 중지. (mode, output_path, audio_path, mic_path, audio_offset) 반환."""
        with self._lock:
            mode = self._mode

            if not self._is_paused:
                # 현재 녹화 중: 프로세스 중지 후 마지막 세그먼트 저장
                self._stop_current_processes()
                if self._output_path is not None:
                    self._segments.append((self._output_path, self._audio_path, self._mic_path, self._audio_offset))
            # 일시 정지 중이면 프로세스 없음, 세그먼트는 이미 pause()에서 저장됨

            segments = list(self._segments)

            if not segments:
                output_path = audio_path = mic_path = None
                audio_offset = 0.0
            elif len(segments) == 1:
                output_path, audio_path, mic_path, audio_offset = segments[0]
            else:
                output_path, audio_path, mic_path, audio_offset = self._concat_segments(mode, segments)

            self._mode = None
            self._output_path = None
            self._audio_path = None
            self._mic_path = None
            self._audio_offset = 0.0
            self._start_time = None
            self._segments = []
            self._seg_index = 0
            self._is_paused = False

            logger.info("녹화/녹음 중지 완료")
            return mode, output_path, audio_path, mic_path, audio_offset

    def _trim_wav(self, ffmpeg_bin: str, in_path: Path, offset: float, out_path: Path) -> None:
        """오디오 앞부분을 offset초만큼 잘라 out_path에 저장."""
        if offset <= 0.05:
            import shutil
            shutil.copy2(str(in_path), str(out_path))
            return
        cmd = [ffmpeg_bin, "-ss", f"{offset:.3f}", "-i", str(in_path), "-c", "copy", "-y", str(out_path)]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _concat_files(self, ffmpeg_bin: str, paths: list, out_path: Path) -> None:
        """ffmpeg concat demuxer로 파일 목록을 하나로 합침."""
        list_file = out_path.with_suffix(".concat.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in paths:
                f.write(f"file '{p}'\n")
        cmd = [ffmpeg_bin, "-f", "concat", "-safe", "0", "-i", str(list_file), "-c", "copy", "-y", str(out_path)]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            list_file.unlink()
        except OSError:
            pass
        if result.returncode != 0:
            raise RuntimeError(f"세그먼트 합치기 실패: {out_path.name}")

    def _concat_segments(self, mode: str, segments: list) -> tuple:
        """여러 세그먼트 파일을 하나로 합쳐 (output_path, audio_path, mic_path, offset) 반환."""
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            logger.warning("ffmpeg 없음 — 첫 세그먼트만 사용")
            return segments[0]

        ts = self._base_ts
        parent = segments[0][0].parent

        if mode == "screen":
            final_mov = parent / f"{ts}_녹화.mov"
            final_sys = parent / f"{ts}_녹화_sys.wav"
            final_mic = parent / f"{ts}_녹화_mic.wav"

            mov_paths, trimmed_sys, trimmed_mic = [], [], []
            for i, (out_path, audio_path, mic_path, offset) in enumerate(segments):
                if out_path and out_path.exists():
                    mov_paths.append(out_path)
                if audio_path and audio_path.exists() and audio_path.stat().st_size > 44:
                    trimmed = parent / f"_trim_sys_{i}.wav"
                    self._trim_wav(ffmpeg_bin, audio_path, offset, trimmed)
                    trimmed_sys.append(trimmed)
                if mic_path and mic_path.exists() and mic_path.stat().st_size > 44:
                    trimmed = parent / f"_trim_mic_{i}.wav"
                    self._trim_wav(ffmpeg_bin, mic_path, offset, trimmed)
                    trimmed_mic.append(trimmed)

            if mov_paths:
                self._concat_files(ffmpeg_bin, mov_paths, final_mov)
            if trimmed_sys:
                self._concat_files(ffmpeg_bin, trimmed_sys, final_sys)
                for f in trimmed_sys:
                    f.unlink(missing_ok=True)
            if trimmed_mic:
                self._concat_files(ffmpeg_bin, trimmed_mic, final_mic)
                for f in trimmed_mic:
                    f.unlink(missing_ok=True)

            # 원본 세그먼트 파일 삭제
            for out_path, audio_path, mic_path, _ in segments:
                for f in [out_path, audio_path, mic_path]:
                    if f and f.exists() and f not in (final_mov, final_sys, final_mic):
                        try:
                            f.unlink()
                        except OSError:
                            pass

            return (
                final_mov if mov_paths else None,
                final_sys if trimmed_sys else None,
                final_mic if trimmed_mic else None,
                0.0,
            )

        elif mode == "audio":
            final_sys = parent / f"{ts}_녹음_sys.wav"
            final_mic = parent / f"{ts}_녹음_mic.wav"

            sys_paths = [seg[1] for seg in segments if seg[1] and seg[1].exists() and seg[1].stat().st_size > 44]
            mic_paths = [seg[2] for seg in segments if seg[2] and seg[2].exists() and seg[2].stat().st_size > 44]

            if sys_paths:
                self._concat_files(ffmpeg_bin, sys_paths, final_sys)
            if mic_paths:
                self._concat_files(ffmpeg_bin, mic_paths, final_mic)

            for _, audio_path, mic_path, _ in segments:
                for f in [audio_path, mic_path]:
                    if f and f.exists() and f not in (final_sys, final_mic):
                        try:
                            f.unlink()
                        except OSError:
                            pass

            return (
                None,
                final_sys if sys_paths else None,
                final_mic if mic_paths else None,
                0.0,
            )

        return segments[0]

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
