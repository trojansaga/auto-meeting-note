import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from audio_extractor import find_ffmpeg
from live_screen_writer import LiveScreenWriter

logger = logging.getLogger(__name__)

_AUDIO_DEVICE_LINE_RE = re.compile(r"\[\s*(\d+)\s*\]\s+(.+?)\s*$")
_AUTO_MIC_DEVICE_SPECS = {"", "0", "auto", "default"}
_MACBOOK_MIC_DEVICE_SPECS = {"builtin", "macbook", "current", "local"}
_IPHONE_MIC_DEVICE_SPECS = {"iphone", "ipad", "ios", "continuity"}
_BUILTIN_MIC_HINTS = (
    "macbook",
    "built-in",
    "built in",
    "internal microphone",
    "internal mic",
    "내장",
)
_IPHONE_MIC_HINTS = ("iphone", "ipad", "continuity")


class Recorder:
    def __init__(self):
        self._screen_writer: Optional[LiveScreenWriter] = None
        self._mic_process: Optional[subprocess.Popen] = None     # ffmpeg 마이크
        self._sys_audio = None                                    # SystemAudioCapture (화면 녹화 + 녹음 공용)
        self._mode: Optional[str] = None  # "screen" | "audio"
        self._output_path: Optional[Path] = None
        self._audio_path: Optional[Path] = None   # 시스템 오디오 WAV
        self._mic_path: Optional[Path] = None     # 마이크 오디오 WAV
        self._audio_offset: float = 0.0           # 시스템 오디오 선행 시간(초)
        self._mic_audio_offset: float = 0.0       # 마이크 오디오 선행 시간(초)
        self._start_time: Optional[float] = None
        self._lock = threading.Lock()
        # pause/resume 세그먼트 지원
        self._segments: list = []          # (output_path, audio_path, mic_path, sys_offset, mic_offset) 목록
        self._is_paused: bool = False
        self._seg_index: int = 0
        self._output_dir: Optional[Path] = None
        self._mic_enabled: bool = True
        self._mic_device_index: str = "macbook"
        self._base_ts: Optional[str] = None
        self._mic_started_at: Optional[float] = None
        self._sync_diagnostic_session = None
        self._mic_latency_correction_seconds: float = 0.0
        self._using_stream_microphone: bool = False

    @property
    def is_recording(self) -> bool:
        if self._mode == "screen":
            return self._screen_writer is not None and self._screen_writer.is_running
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

    def attach_sync_diagnostic_session(self, session) -> None:
        self._sync_diagnostic_session = session

    def set_mic_latency_correction(self, seconds: float) -> None:
        self._mic_latency_correction_seconds = float(seconds)

    @staticmethod
    def _normalize_audio_device_spec(spec: Optional[str]) -> str:
        return re.sub(r"\s+", " ", (spec or "").strip().lstrip(":")).casefold()

    def _is_iphone_mic(self, device_name: str) -> bool:
        normalized = self._normalize_audio_device_spec(device_name)
        return any(token in normalized for token in _IPHONE_MIC_HINTS)

    def _is_builtin_mic(self, device_name: str) -> bool:
        normalized = self._normalize_audio_device_spec(device_name)
        return any(token in normalized for token in _BUILTIN_MIC_HINTS)

    def _list_audio_input_devices(self, ffmpeg_bin: str) -> list[tuple[str, str]]:
        cmd = [ffmpeg_bin, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except Exception as e:
            logger.warning("AVFoundation 오디오 장치 목록 조회 실패: %s", e)
            return []

        output = "\n".join(part for part in [result.stdout, result.stderr] if part)
        devices: list[tuple[str, str]] = []
        in_audio_section = False

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if "AVFoundation audio devices:" in line:
                in_audio_section = True
                continue
            if not in_audio_section:
                continue
            match = _AUDIO_DEVICE_LINE_RE.search(line)
            if match:
                devices.append((match.group(1), match.group(2).strip()))

        return devices

    def _resolve_mic_device_spec(self, requested_spec: Optional[str]) -> str:
        requested_spec = (requested_spec or "").strip().lstrip(":")
        normalized_request = self._normalize_audio_device_spec(requested_spec)
        auto_like = normalized_request in _AUTO_MIC_DEVICE_SPECS
        macbook_like = normalized_request in _MACBOOK_MIC_DEVICE_SPECS
        iphone_like = normalized_request in _IPHONE_MIC_DEVICE_SPECS
        fallback_spec = "0" if (auto_like or macbook_like or iphone_like) else (requested_spec or "0")
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            return fallback_spec

        devices = self._list_audio_input_devices(ffmpeg_bin)
        if not devices:
            logger.warning("오디오 입력 장치를 찾지 못해 마이크 설정값을 사용합니다: %s", fallback_spec)
            return fallback_spec

        if iphone_like:
            for _, device_name in devices:
                if self._is_iphone_mic(device_name):
                    logger.info("iPhone 마이크 선택: %s", device_name)
                    return device_name
            logger.warning("iPhone 마이크를 찾지 못해 내장/현재 마이크로 대체합니다.")

        if not (auto_like or macbook_like or iphone_like):
            if requested_spec.isdigit():
                matched_request_name = next((name for index, name in devices if index == requested_spec), None)
            else:
                matched_request_name = next(
                    (name for _, name in devices if self._normalize_audio_device_spec(name) == normalized_request),
                    None,
                )

            if matched_request_name:
                return requested_spec

            if not requested_spec.isdigit():
                return requested_spec

        for _, device_name in devices:
            if self._is_builtin_mic(device_name):
                logger.info("내장 마이크 선택: %s", device_name)
                return device_name

        for _, device_name in devices:
            if self._is_iphone_mic(device_name):
                continue
            logger.warning("내장 마이크를 찾지 못해 iPhone이 아닌 입력 장치를 사용합니다: %s", device_name)
            return device_name

        logger.warning("iPhone이 아닌 마이크를 찾지 못해 기존 설정을 유지합니다: %s", fallback_spec)
        return fallback_spec

    def _start_mic(self, mic_path: Path, mic_device_index: Optional[str]) -> float:
        """ffmpeg avfoundation으로 마이크 녹음 시작."""
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg가 없어 마이크 녹음을 시작할 수 없습니다.")
        mic_device_spec = (mic_device_index or "").strip().lstrip(":") or "0"
        cmd = [
            ffmpeg_bin,
            "-f", "avfoundation",
            "-i", f":{mic_device_spec}",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            "-y", str(mic_path),
        ]
        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            raise RuntimeError(f"마이크 녹음 시작 실패: {e}") from e

        started_at = time.time()
        time.sleep(0.2)
        if process.poll() is not None:
            stderr_text = ""
            try:
                _, stderr_output = process.communicate(timeout=1)
            except Exception:
                stderr_output = b""
            if isinstance(stderr_output, bytes):
                stderr_text = stderr_output.decode("utf-8", errors="ignore").strip()
            elif isinstance(stderr_output, str):
                stderr_text = stderr_output.strip()
            detail = stderr_text.splitlines()[-1] if stderr_text else f"입력 장치={mic_device_spec}"
            logger.error("마이크 녹음 시작 실패: %s", detail)
            self._mic_process = None
            raise RuntimeError(f"마이크 녹음 시작 실패: {detail}")

        self._mic_process = process
        logger.info("마이크 녹음 시작: %s (입력=%s)", mic_path.name, mic_device_spec)
        return started_at

    @staticmethod
    def _capture_started_info(capture, fallback: float, *attr_names: str) -> tuple[float, str]:
        names = attr_names or ("started_at",)
        for attr_name in names:
            started_at = getattr(capture, attr_name, None)
            if isinstance(started_at, (int, float)):
                return float(started_at), attr_name
        return fallback, "fallback"

    @classmethod
    def _capture_started_at(cls, capture, fallback: float, *attr_names: str) -> float:
        started_at, _ = cls._capture_started_info(capture, fallback, *attr_names)
        return started_at

    @staticmethod
    def _amix_filter() -> str:
        return "amix=inputs=2:duration=longest:dropout_transition=0:normalize=1"

    @staticmethod
    def _audio_input_args(path: Path, audio_offset: float) -> list[str]:
        if audio_offset > 0.05:
            return ["-ss", f"{audio_offset:.3f}", "-i", str(path)]
        if audio_offset < -0.05:
            return ["-itsoffset", f"{abs(audio_offset):.3f}", "-i", str(path)]
        return ["-i", str(path)]

    @staticmethod
    def _offset_from_anchor(anchor: float, started_at: Optional[float]) -> float:
        if isinstance(started_at, (int, float)):
            return anchor - float(started_at)
        return 0.0

    def _mic_offset_from_anchor(self, anchor: float) -> float:
        if self._using_stream_microphone:
            return self._offset_from_anchor(anchor, self._mic_started_at)
        return self._offset_from_anchor(anchor, self._mic_started_at) - self._mic_latency_correction_seconds

    @staticmethod
    def _format_debug_time(value: Optional[float]) -> str:
        if isinstance(value, (int, float)):
            return f"{float(value):.3f}"
        return "-"

    def _log_screen_sync_debug(
        self,
        sys_capture,
        screen_writer,
        sys_started_at: float,
        sys_source: str,
        mic_started_at: Optional[float],
        screen_started_at: float,
        screen_source: str,
        sys_offset: float,
        mic_offset: float,
    ) -> None:
        sys_first_sample = getattr(sys_capture, "first_sample_at", None)
        screen_capture_started = getattr(screen_writer, "capture_started_at", None)
        screen_recording_started = getattr(screen_writer, "started_at", None)
        alt_first_sample_offset = None
        if isinstance(sys_first_sample, (int, float)):
            alt_first_sample_offset = screen_started_at - float(sys_first_sample)

        logger.info(
            "화면 녹화 싱크 로그: "
            "sys.started_at=%s(%s), sys.first_sample_at=%s, "
            "mic.started_at=%s, screen.capture_started_at=%s, screen.started_at=%s, "
            "sys_offset=%.3f(screen:%s-sys:%s), mic_offset=%.3f, alt_first_sample_offset=%s",
            self._format_debug_time(sys_started_at),
            sys_source,
            self._format_debug_time(sys_first_sample),
            self._format_debug_time(mic_started_at),
            self._format_debug_time(screen_capture_started),
            self._format_debug_time(screen_recording_started),
            sys_offset,
            screen_source,
            sys_source,
            mic_offset,
            self._format_debug_time(alt_first_sample_offset),
        )
        if self._sync_diagnostic_session is not None:
            self._sync_diagnostic_session.record_sync_snapshot(
                "screen_start",
                {
                    "sys.started_at": sys_started_at,
                    "sys.source": sys_source,
                    "sys.first_sample_at": sys_first_sample,
                    "mic.started_at": mic_started_at,
                    "screen.capture_started_at": screen_capture_started,
                    "screen.started_at": screen_recording_started,
                    "screen.source": screen_source,
                    "sys_offset": sys_offset,
                    "mic_offset": mic_offset,
                    "alt_first_sample_offset": alt_first_sample_offset,
                },
            )

    def _log_audio_merge_debug(
        self,
        stage: str,
        media_path: Path,
        sys_offset: float,
        mic_offset: float,
        sys_args: Optional[list[str]],
        mic_args: Optional[list[str]],
    ) -> None:
        logger.info(
            "%s 싱크 로그: media=%s, sys_offset=%.3f, mic_offset=%.3f, sys_args=%s, mic_args=%s",
            stage,
            media_path.name,
            sys_offset,
            mic_offset,
            sys_args if sys_args is not None else "-",
            mic_args if mic_args is not None else "-",
        )
        if self._sync_diagnostic_session is not None:
            self._sync_diagnostic_session.record_merge_stage(
                stage,
                media_name=media_path.name,
                sys_offset=sys_offset,
                mic_offset=mic_offset,
                sys_args=sys_args,
                mic_args=mic_args,
            )

    def _log_audio_recording_sync_debug(
        self,
        sys_started_at: float,
        mic_started_at: Optional[float],
        sys_offset: float,
        mic_offset: float,
    ) -> None:
        logger.info(
            "오디오 녹음 싱크 로그: sys.started_at=%s, mic.started_at=%s, sys_offset=%.3f, mic_offset=%.3f",
            self._format_debug_time(sys_started_at),
            self._format_debug_time(mic_started_at),
            sys_offset,
            mic_offset,
        )
        if self._sync_diagnostic_session is not None:
            self._sync_diagnostic_session.record_sync_snapshot(
                "audio_start",
                {
                    "sys.started_at": sys_started_at,
                    "mic.started_at": mic_started_at,
                    "sys_offset": sys_offset,
                    "mic_offset": mic_offset,
                },
            )

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
        self, output_dir: Path, mic_enabled: bool = True, mic_device_index: str = "builtin"
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
            self._mic_device_index = self._resolve_mic_device_spec(mic_device_index) if mic_enabled else "0"
            self._base_ts = ts

            mp4_path = output_dir / f"{ts}_녹화.mp4"
            audio_path = output_dir / f"{ts}_녹화_sys.wav"

            # 1) SCStream: 시스템 오디오 캡처 (블로킹, ~1초 소요)
            self._sys_audio = SystemAudioCapture()
            mic_path = output_dir / f"{ts}_녹화_mic.wav" if mic_enabled else None
            self._sys_audio.start(
                audio_path,
                mic_output_path=mic_path if mic_enabled else None,
                mic_device_spec=self._mic_device_index if mic_enabled else None,
            )
            sys_audio_ready_time, sys_audio_source = self._capture_started_info(
                self._sys_audio,
                time.time(),
                "started_at",
            )
            logger.info("시스템 오디오 캡처 시작: %s", audio_path.name)

            # 2) ScreenCaptureKit 마이크 또는 ffmpeg 마이크 동시 캡처 (선택)
            self._using_stream_microphone = False
            if mic_enabled and getattr(self._sys_audio, "mic_capture_active", False):
                self._mic_started_at = getattr(self._sys_audio, "mic_started_at", sys_audio_ready_time)
                self._mic_path = mic_path
                self._using_stream_microphone = True
            elif mic_enabled:
                try:
                    self._mic_started_at = self._start_mic(mic_path, self._mic_device_index)
                    self._mic_path = mic_path if self._mic_process else None
                except Exception:
                    try:
                        self._sys_audio.stop()
                    except Exception as e:
                        logger.warning("마이크 실패 후 시스템 오디오 중지 오류: %s", e)
                    self._sys_audio = None
                    raise
            else:
                self._mic_path = None

            # 3) SCStream + AVAssetWriter: 실시간 화면 인코딩
            self._screen_writer = LiveScreenWriter()
            logger.info("화면 녹화 시작: %s", mp4_path.name)
            self._screen_writer.start(mp4_path)
            screen_start_time, screen_source = self._capture_started_info(
                self._screen_writer,
                time.time(),
                "capture_started_at",
                "started_at",
            )

            # 오디오(sys_audio_ready_time)와 영상(screen_start_time)의 시작 차이만큼 trim
            # t_before_sys 기준이 아닌 실제 오디오 기록 시작 시점 기준
            self._audio_offset = self._offset_from_anchor(screen_start_time, sys_audio_ready_time)
            self._mic_audio_offset = self._mic_offset_from_anchor(screen_start_time)
            self._log_screen_sync_debug(
                self._sys_audio,
                self._screen_writer,
                sys_audio_ready_time,
                sys_audio_source,
                self._mic_started_at,
                screen_start_time,
                screen_source,
                self._audio_offset,
                self._mic_audio_offset,
            )

            self._mode = "screen"
            self._output_path = mp4_path
            self._audio_path = audio_path
            self._start_time = screen_start_time
            return mp4_path

    def start_audio_recording(
        self, output_dir: Path, mic_enabled: bool = True, mic_device_index: str = "builtin"
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
            self._mic_device_index = self._resolve_mic_device_spec(mic_device_index) if mic_enabled else "0"
            self._base_ts = ts

            sys_path = output_dir / f"{ts}_녹음_sys.wav"

            self._sys_audio = SystemAudioCapture()
            mic_path = output_dir / f"{ts}_녹음_mic.wav" if mic_enabled else None
            self._sys_audio.start(
                sys_path,
                mic_output_path=mic_path if mic_enabled else None,
                mic_device_spec=self._mic_device_index if mic_enabled else None,
            )
            sys_audio_ready_time, _ = self._capture_started_info(
                self._sys_audio,
                time.time(),
                "started_at",
            )

            self._using_stream_microphone = False
            if mic_enabled and getattr(self._sys_audio, "mic_capture_active", False):
                self._mic_started_at = getattr(self._sys_audio, "mic_started_at", sys_audio_ready_time)
                self._mic_path = mic_path
                self._using_stream_microphone = True
            elif mic_enabled:
                try:
                    self._mic_started_at = self._start_mic(mic_path, self._mic_device_index)
                    self._mic_path = mic_path if self._mic_process else None
                except Exception:
                    try:
                        self._sys_audio.stop()
                    except Exception as e:
                        logger.warning("마이크 실패 후 시스템 오디오 중지 오류: %s", e)
                    self._sys_audio = None
                    raise
            else:
                self._mic_path = None

            anchor_time = max(sys_audio_ready_time, self._mic_started_at or sys_audio_ready_time)
            self._audio_offset = self._offset_from_anchor(anchor_time, sys_audio_ready_time)
            self._mic_audio_offset = self._mic_offset_from_anchor(anchor_time)
            self._log_audio_recording_sync_debug(
                sys_audio_ready_time,
                self._mic_started_at,
                self._audio_offset,
                self._mic_audio_offset,
            )
            self._mode = "audio"
            self._output_path = sys_path
            self._start_time = time.time()
            return sys_path

    def _stop_current_processes(self) -> None:
        """현재 실행 중인 프로세스 중지. _lock 보유 상태에서 호출."""
        if self._mode == "screen":
            if self._screen_writer is not None:
                try:
                    self._screen_writer.stop()
                except Exception as e:
                    logger.error("화면 writer 중지 오류: %s", e)
                self._screen_writer = None

            if self._sys_audio is not None:
                try:
                    self._sys_audio.stop()
                except Exception as e:
                    logger.error("시스템 오디오 중지 오류: %s", e)
                self._sys_audio = None

            if not self._using_stream_microphone:
                self._stop_mic()
            self._mic_started_at = None

        elif self._mode == "audio":
            if self._sys_audio is not None:
                try:
                    self._sys_audio.stop()
                except Exception as e:
                    logger.error("시스템 오디오 중지 오류: %s", e)
                self._sys_audio = None

            if not self._using_stream_microphone:
                self._stop_mic()
            self._mic_started_at = None

    def pause(self) -> None:
        """녹화/녹음 일시 정지. 현재 세그먼트를 저장하고 프로세스 중지."""
        with self._lock:
            if self._mode is None or self._is_paused:
                return
            self._stop_current_processes()
            self._segments.append(
                (self._output_path, self._audio_path, self._mic_path, self._audio_offset, self._mic_audio_offset)
            )
            self._output_path = None
            self._audio_path = None
            self._mic_path = None
            self._audio_offset = 0.0
            self._mic_audio_offset = 0.0
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
                mp4_path = self._output_dir / f"{ts}_녹화_seg{seg}.mp4"
                audio_path = self._output_dir / f"{ts}_녹화_sys_seg{seg}.wav"

                self._sys_audio = SystemAudioCapture()
                mic_path = self._output_dir / f"{ts}_녹화_mic_seg{seg}.wav" if self._mic_enabled else None
                self._sys_audio.start(
                    audio_path,
                    mic_output_path=mic_path if self._mic_enabled else None,
                    mic_device_spec=self._mic_device_index if self._mic_enabled else None,
                )
                sys_audio_ready_time, sys_audio_source = self._capture_started_info(
                    self._sys_audio,
                    time.time(),
                    "started_at",
                )
                logger.info("재개: 시스템 오디오 시작: %s", audio_path.name)

                self._using_stream_microphone = False
                if self._mic_enabled and getattr(self._sys_audio, "mic_capture_active", False):
                    self._mic_started_at = getattr(self._sys_audio, "mic_started_at", sys_audio_ready_time)
                    self._mic_path = mic_path
                    self._using_stream_microphone = True
                elif self._mic_enabled:
                    try:
                        self._mic_started_at = self._start_mic(mic_path, self._mic_device_index)
                        self._mic_path = mic_path if self._mic_process else None
                    except Exception:
                        try:
                            self._sys_audio.stop()
                        except Exception as e:
                            logger.warning("재개 중 마이크 실패 후 시스템 오디오 중지 오류: %s", e)
                        self._sys_audio = None
                        raise
                else:
                    self._mic_path = None

                self._screen_writer = LiveScreenWriter()
                logger.info("재개: 화면 녹화 시작: %s", mp4_path.name)
                self._screen_writer.start(mp4_path)
                screen_start_time, screen_source = self._capture_started_info(
                    self._screen_writer,
                    time.time(),
                    "capture_started_at",
                    "started_at",
                )
                self._audio_offset = self._offset_from_anchor(screen_start_time, sys_audio_ready_time)
                self._mic_audio_offset = self._mic_offset_from_anchor(screen_start_time)
                self._log_screen_sync_debug(
                    self._sys_audio,
                    self._screen_writer,
                    sys_audio_ready_time,
                    sys_audio_source,
                    self._mic_started_at,
                    screen_start_time,
                    screen_source,
                    self._audio_offset,
                    self._mic_audio_offset,
                )
                self._output_path = mp4_path
                self._audio_path = audio_path

            elif self._mode == "audio":
                sys_path = self._output_dir / f"{ts}_녹음_sys_seg{seg}.wav"

                self._sys_audio = SystemAudioCapture()
                mic_path = self._output_dir / f"{ts}_녹음_mic_seg{seg}.wav" if self._mic_enabled else None
                self._sys_audio.start(
                    sys_path,
                    mic_output_path=mic_path if self._mic_enabled else None,
                    mic_device_spec=self._mic_device_index if self._mic_enabled else None,
                )
                sys_audio_ready_time, _ = self._capture_started_info(
                    self._sys_audio,
                    time.time(),
                    "started_at",
                )
                logger.info("재개: 시스템 오디오 시작: %s", sys_path.name)

                self._using_stream_microphone = False
                if self._mic_enabled and getattr(self._sys_audio, "mic_capture_active", False):
                    self._mic_started_at = getattr(self._sys_audio, "mic_started_at", sys_audio_ready_time)
                    self._mic_path = mic_path
                    self._using_stream_microphone = True
                elif self._mic_enabled:
                    try:
                        self._mic_started_at = self._start_mic(mic_path, self._mic_device_index)
                        self._mic_path = mic_path if self._mic_process else None
                    except Exception:
                        try:
                            self._sys_audio.stop()
                        except Exception as e:
                            logger.warning("재개 중 마이크 실패 후 시스템 오디오 중지 오류: %s", e)
                        self._sys_audio = None
                        raise
                else:
                    self._mic_path = None

                anchor_time = max(sys_audio_ready_time, self._mic_started_at or sys_audio_ready_time)
                self._audio_offset = self._offset_from_anchor(anchor_time, sys_audio_ready_time)
                self._mic_audio_offset = self._mic_offset_from_anchor(anchor_time)
                self._log_audio_recording_sync_debug(
                    sys_audio_ready_time,
                    self._mic_started_at,
                    self._audio_offset,
                    self._mic_audio_offset,
                )
                self._output_path = sys_path

            self._is_paused = False
            logger.info("녹화/녹음 재개 (세그먼트 %d)", self._seg_index)

    def stop(self) -> tuple:
        """녹화/녹음 중지. (mode, output_path, audio_path, mic_path, sys_offset, mic_offset) 반환."""
        with self._lock:
            mode = self._mode

            if not self._is_paused:
                # 현재 녹화 중: 프로세스 중지 후 마지막 세그먼트 저장
                self._stop_current_processes()
                if self._output_path is not None:
                    self._segments.append(
                        (
                            self._output_path,
                            self._audio_path,
                            self._mic_path,
                            self._audio_offset,
                            self._mic_audio_offset,
                        )
                    )
            # 일시 정지 중이면 프로세스 없음, 세그먼트는 이미 pause()에서 저장됨

            segments = list(self._segments)

            if not segments:
                output_path = audio_path = mic_path = None
                audio_offset = mic_audio_offset = 0.0
            elif len(segments) == 1:
                output_path, audio_path, mic_path, audio_offset, mic_audio_offset = segments[0]
            else:
                output_path, audio_path, mic_path, audio_offset, mic_audio_offset = self._concat_segments(mode, segments)

            self._mode = None
            self._output_path = None
            self._audio_path = None
            self._mic_path = None
            self._audio_offset = 0.0
            self._mic_audio_offset = 0.0
            self._start_time = None
            self._segments = []
            self._seg_index = 0
            self._is_paused = False
            self._mic_started_at = None
            self._using_stream_microphone = False

            logger.info("녹화/녹음 중지 완료")
            return mode, output_path, audio_path, mic_path, audio_offset, mic_audio_offset

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

    @staticmethod
    def _software_video_codec_args() -> list[str]:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]

    def _with_software_video_encoder(self, cmd: list[str]) -> list[str]:
        pattern = ["-c:v", "h264_videotoolbox", "-q:v", "60"]
        for idx in range(len(cmd) - len(pattern) + 1):
            if cmd[idx:idx + len(pattern)] == pattern:
                return [
                    *cmd[:idx],
                    *self._software_video_codec_args(),
                    *cmd[idx + len(pattern):],
                ]
        return cmd

    def _concat_segments(self, mode: str, segments: list) -> tuple:
        """여러 세그먼트 파일을 하나로 합쳐 (output_path, audio_path, mic_path, sys_offset, mic_offset) 반환."""
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            logger.warning("ffmpeg 없음 — 첫 세그먼트만 사용")
            return segments[0]

        ts = self._base_ts
        parent = segments[0][0].parent

        if mode == "screen":
            video_ext = segments[0][0].suffix if segments[0][0] else ".mov"
            final_mov = parent / f"{ts}_녹화{video_ext}"
            final_sys = parent / f"{ts}_녹화_sys.wav"
            final_mic = parent / f"{ts}_녹화_mic.wav"

            mov_paths, trimmed_sys, trimmed_mic = [], [], []
            for i, (out_path, audio_path, mic_path, sys_offset, mic_offset) in enumerate(segments):
                if out_path and out_path.exists():
                    mov_paths.append(out_path)
                if audio_path and audio_path.exists() and audio_path.stat().st_size > 44:
                    trimmed = parent / f"_trim_sys_{i}.wav"
                    self._trim_wav(ffmpeg_bin, audio_path, sys_offset, trimmed)
                    trimmed_sys.append(trimmed)
                if mic_path and mic_path.exists() and mic_path.stat().st_size > 44:
                    trimmed = parent / f"_trim_mic_{i}.wav"
                    self._trim_wav(ffmpeg_bin, mic_path, mic_offset, trimmed)
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
            for out_path, audio_path, mic_path, _, _ in segments:
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
                0.0,
            )

        elif mode == "audio":
            parent = segments[0][0].parent
            final_sys = parent / f"{ts}_녹음_sys.wav"
            final_mic = parent / f"{ts}_녹음_mic.wav"

            trimmed_sys: list[Path] = []
            trimmed_mic: list[Path] = []
            for i, (out_path, _, mic_path, sys_offset, mic_offset) in enumerate(segments):
                if out_path and out_path.exists() and out_path.stat().st_size > 44:
                    trimmed = parent / f"_trim_audio_sys_{i}.wav"
                    self._trim_wav(ffmpeg_bin, out_path, sys_offset, trimmed)
                    trimmed_sys.append(trimmed)
                if mic_path and mic_path.exists() and mic_path.stat().st_size > 44:
                    trimmed = parent / f"_trim_audio_mic_{i}.wav"
                    self._trim_wav(ffmpeg_bin, mic_path, mic_offset, trimmed)
                    trimmed_mic.append(trimmed)

            if trimmed_sys:
                self._concat_files(ffmpeg_bin, trimmed_sys, final_sys)
                for f in trimmed_sys:
                    f.unlink(missing_ok=True)
            if trimmed_mic:
                self._concat_files(ffmpeg_bin, trimmed_mic, final_mic)
                for f in trimmed_mic:
                    f.unlink(missing_ok=True)

            for out_path, _, mic_path, _, _ in segments:
                for f in [out_path, mic_path]:
                    if f and f.exists() and f not in (final_sys, final_mic):
                        try:
                            f.unlink()
                        except OSError:
                            pass

            return (
                final_sys if trimmed_sys else None,
                None,
                final_mic if trimmed_mic else None,
                0.0,
                0.0,
            )

        return segments[0]

    def mix_wav(
        self,
        sys_path: Path,
        mic_path: Path,
        audio_offset: float = 0.0,
        mic_audio_offset: float = 0.0,
    ) -> Path:
        """시스템 오디오 + 마이크 WAV를 amix로 믹싱 → 단일 WAV 반환. 원본 삭제."""
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise EnvironmentError("ffmpeg가 설치되어 있지 않습니다.")

        out_path = sys_path.with_name(sys_path.stem.replace("_sys", "") + "_mixed.wav")
        has_sys = sys_path.exists() and sys_path.stat().st_size > 44
        has_mic = mic_path.exists() and mic_path.stat().st_size > 44
        sys_args = self._audio_input_args(sys_path, audio_offset) if has_sys else None
        mic_args = self._audio_input_args(mic_path, mic_audio_offset) if has_mic else None

        if has_sys and has_mic:
            cmd = [
                ffmpeg_bin,
                *sys_args,
                *mic_args,
                "-filter_complex", self._amix_filter(),
                "-y", str(out_path),
            ]
            self._log_audio_merge_debug("오디오 믹싱", sys_path, audio_offset, mic_audio_offset, sys_args, mic_args)
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
        mic_audio_offset: float = 0.0,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Path:
        """MOV + 시스템 오디오 WAV [+ 마이크 WAV] → H.264 MP4로 병합 압축. 원본 파일 삭제.

        audio_offset: 오디오가 영상보다 먼저 시작된 시간(초). 양수면 오디오 앞부분 trim.
        """
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise EnvironmentError("ffmpeg가 설치되어 있지 않습니다.")

        mp4_path = mov_path.with_suffix(".mp4")

        if not mov_path.exists():
            raise RuntimeError(f"화면 녹화 파일이 없습니다: {mov_path.name} (screencapture가 파일을 생성하지 못했습니다)")
        if mov_path.stat().st_size < 100:
            raise RuntimeError(f"화면 녹화 파일이 너무 작습니다: {mov_path.name} ({mov_path.stat().st_size} bytes)")

        has_sys = audio_path and audio_path.exists() and audio_path.stat().st_size > 44
        has_mic = mic_path and mic_path.exists() and mic_path.stat().st_size > 44

        # 오디오 싱크 보정: 오디오가 영상보다 먼저 시작된 경우 앞부분 skip
        logger.info("오디오 싱크 오프셋: sys=%.3fs, mic=%.3fs", audio_offset, mic_audio_offset)
        sys_args = self._audio_input_args(audio_path, audio_offset) if has_sys else None
        mic_args = self._audio_input_args(mic_path, mic_audio_offset) if has_mic else None
        self._log_audio_merge_debug("녹화 압축/병합", mov_path, audio_offset, mic_audio_offset, sys_args, mic_args)

        if has_sys and has_mic:
            # 영상 + 시스템 오디오 + 마이크 (amix)
            cmd = [
                ffmpeg_bin,
                "-i", str(mov_path),
                *sys_args,
                *mic_args,
                "-filter_complex", f"{self._amix_filter()}[aout]",
                "-c:v", "h264_videotoolbox", "-q:v", "60",
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
                *sys_args,
                "-c:v", "h264_videotoolbox", "-q:v", "60",
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
                *mic_args,
                "-c:v", "h264_videotoolbox", "-q:v", "60",
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
                "-c:v", "h264_videotoolbox", "-q:v", "60",
                "-c:a", "aac",
                "-map", "0:v", "-map", "1:a",
                "-shortest",
                "-y", str(mp4_path),
            ]
            logger.info("오디오 없음 — 무음 트랙으로 압축: %s", mov_path.name)

        def _run_ffmpeg(ffmpeg_cmd: list[str]) -> tuple[int, list[str]]:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            total_duration: Optional[float] = None
            dur_pat = re.compile(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)")
            time_pat = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")
            stderr_lines: list[str] = []

            for line in process.stderr:
                line = line.strip()
                if not line:
                    continue
                stderr_lines.append(line)
                if len(stderr_lines) > 100:
                    stderr_lines.pop(0)
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
            return process.returncode, stderr_lines

        returncode, stderr_lines = _run_ffmpeg(cmd)

        if returncode != 0 and "h264_videotoolbox" in cmd:
            logger.warning(
                "videotoolbox 인코더 실패, libx264로 재시도합니다 (exit=%d)",
                returncode,
            )
            fallback_cmd = self._with_software_video_encoder(cmd)
            returncode, stderr_lines = _run_ffmpeg(fallback_cmd)

        if returncode != 0:
            logger.error("ffmpeg 실패 (exit=%d) 마지막 로그:\n%s", returncode, "\n".join(stderr_lines[-20:]))
            raise RuntimeError(f"압축 실패 (exit code {returncode})")

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

    def merge_audio_into_mp4(
        self,
        mp4_path: Path,
        audio_path: Optional[Path],
        mic_path: Optional[Path] = None,
        audio_offset: float = 0.0,
        mic_audio_offset: float = 0.0,
    ) -> Path:
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise EnvironmentError("ffmpeg가 설치되어 있지 않습니다.")

        has_sys = audio_path and audio_path.exists() and audio_path.stat().st_size > 44
        has_mic = mic_path and mic_path.exists() and mic_path.stat().st_size > 44
        if not has_sys and not has_mic:
            return mp4_path
        if not mp4_path.exists() or mp4_path.stat().st_size == 0:
            raise RuntimeError(f"화면 녹화 파일이 비어 있습니다: {mp4_path.name}")

        temp_path = mp4_path.with_name(mp4_path.stem + "_mux.mp4")
        sys_args = self._audio_input_args(audio_path, audio_offset) if has_sys else None
        mic_args = self._audio_input_args(mic_path, mic_audio_offset) if has_mic else None
        self._log_audio_merge_debug("실시간 오디오 병합", mp4_path, audio_offset, mic_audio_offset, sys_args, mic_args)
        if has_sys and has_mic:
            cmd = [
                ffmpeg_bin,
                "-i", str(mp4_path),
                *sys_args,
                *mic_args,
                "-filter_complex", f"{self._amix_filter()}[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-map", "0:v:0", "-map", "[aout]",
                "-shortest",
                "-y", str(temp_path),
            ]
        else:
            source = audio_path if has_sys else mic_path
            cmd = [
                ffmpeg_bin,
                "-i", str(mp4_path),
                *(sys_args if has_sys else mic_args),
                "-c:v", "copy",
                "-c:a", "aac",
                "-map", "0:v:0", "-map", "1:a:0",
                "-shortest",
                "-y", str(temp_path),
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if stderr:
                logger.error("오디오 병합 ffmpeg stderr:\n%s", stderr)
            raise RuntimeError("오디오 병합 실패")

        mp4_path.unlink(missing_ok=True)
        temp_path.rename(mp4_path)
        for path in (audio_path, mic_path):
            if path and path.exists():
                path.unlink(missing_ok=True)
        logger.info("실시간 녹화 오디오 병합 완료: %s", mp4_path.name)
        return mp4_path
