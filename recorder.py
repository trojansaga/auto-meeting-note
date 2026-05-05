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
from continuous_screen_recorder import ContinuousScreenRecorder

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
        self._screen_recorder: Optional[ContinuousScreenRecorder] = None
        self._screen_mic_segments: list = []  # screen mode: [(mic_path, mic_offset), ...]
        self._mic_process: Optional[subprocess.Popen] = None     # ffmpeg 마이크
        self._mic_stderr_thread: Optional[threading.Thread] = None  # ffmpeg stderr drain (PIPE 버퍼 풀림 방지)
        self._sys_audio = None                                    # SystemAudioCapture (녹음 모드 전용)
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
        self._paused_duration: float = 0.0
        self._pause_start: Optional[float] = None
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
            return self._screen_recorder is not None and self._screen_recorder.is_running
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
        paused = self._paused_duration
        if self._pause_start is not None:
            paused += time.time() - self._pause_start
        return time.time() - self._start_time - paused

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
                encoding="utf-8",
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
        """ffmpeg avfoundation으로 마이크 녹음 시작.

        실제 첫 샘플이 캡처되는 시점 ≈ WAV 파일 크기가 헤더(44B) 초과로 늘어나는 시점.
        Popen 직후 시각이 아닌 이 시점을 `started_at`으로 사용해 ffmpeg avfoundation의
        초기화 지연(보통 100~300ms)을 보정한다.
        """
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg가 없어 마이크 녹음을 시작할 수 없습니다.")
        mic_device_spec = (mic_device_index or "").strip().lstrip(":") or "0"
        cmd = [
            ffmpeg_bin,
            "-f", "avfoundation",
            "-thread_queue_size", "512",
            "-i", f":{mic_device_spec}",
            "-acodec", "pcm_s16le",
            "-ar", "48000",
            "-ac", "1",
            "-flush_packets", "1",  # 첫 샘플 즉시 flush → 파일 크기로 캡처 시작 감지 가능
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

        # WAV 파일 크기 폴링으로 실제 첫 샘플 기록 시점 감지 (max 2초)
        started_at: Optional[float] = None
        poll_start = time.time()
        while time.time() - poll_start < 2.0:
            if process.poll() is not None:
                break
            try:
                if mic_path.exists() and mic_path.stat().st_size > 44:
                    started_at = time.time()
                    break
            except OSError:
                pass
            time.sleep(0.005)

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

        if started_at is None:
            # 폴링 타임아웃: 폴백으로 현재 시각 사용 (ffmpeg가 늦게 flush하는 경우)
            started_at = time.time()
            logger.warning("마이크 첫 샘플 감지 타임아웃 — 폴백 timestamp 사용")

        self._mic_process = process

        # ffmpeg stderr 를 백그라운드에서 지속 소비 (장기 녹화 시 PIPE 64KB 버퍼 풀림 차단)
        stderr_stream = getattr(process, "stderr", None)
        if stderr_stream is not None:
            def _drain_stderr(stream):
                try:
                    for _ in iter(stream.readline, b""):
                        pass
                except Exception:
                    pass

            self._mic_stderr_thread = threading.Thread(
                target=_drain_stderr,
                args=(stderr_stream,),
                daemon=True,
                name="mic-stderr-drain",
            )
            self._mic_stderr_thread.start()

        logger.info(
            "마이크 녹음 시작: %s (입력=%s, 초기화 지연=%.3fs)",
            mic_path.name,
            mic_device_spec,
            started_at - poll_start,
        )
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
        # 프로세스 종료 후 stderr 가 EOF 되면서 drain 스레드 자연 종료
        if self._mic_stderr_thread is not None:
            self._mic_stderr_thread.join(timeout=2)
            self._mic_stderr_thread = None

    def start_screen_recording(
        self, output_dir: Path, mic_enabled: bool = True, mic_device_index: str = "builtin"
    ) -> Path:
        """화면 녹화 시작. ContinuousScreenRecorder는 영상만, 시스템 오디오는 SystemAudioCapture로
        별도 WAV에 캡처한다 (SCRecordingOutput 오디오 인코딩의 popping/sync 이슈 회피, 1.1.13 fix 복원).
        SCStream 초기화가 블로킹이므로 반드시 백그라운드 스레드에서 호출해야 함."""
        with self._lock:
            from system_audio import SystemAudioCapture

            ts = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
            self._segments = []
            self._seg_index = 0
            self._screen_mic_segments = []
            self._is_paused = False
            self._output_dir = output_dir
            self._mic_enabled = mic_enabled
            self._mic_device_index = self._resolve_mic_device_spec(mic_device_index) if mic_enabled else "0"
            self._base_ts = ts
            self._using_stream_microphone = False

            # 1) 시스템 오디오 + (지원 시) 마이크를 SystemAudioCapture로 캡처
            #    SCStream raw sample → WAV. macOS 15+에서는 마이크도 SCStream으로 동시 캡처 가능.
            sys_path = output_dir / f"{ts}_녹화_sys.wav"
            mic_path = output_dir / f"{ts}_녹화_mic.wav" if mic_enabled else None
            sys_audio = SystemAudioCapture()
            sys_audio.start(
                sys_path,
                mic_output_path=mic_path if mic_enabled else None,
                mic_device_spec=self._mic_device_index if mic_enabled else None,
            )
            sys_audio_ready_time, sys_audio_source = self._capture_started_info(
                sys_audio,
                time.time(),
                "first_sample_at",
                "started_at",
            )
            self._sys_audio = sys_audio
            self._audio_path = sys_path
            logger.info("시스템 오디오 캡처 시작: %s", sys_path.name)

            # 2) ContinuousScreenRecorder: 영상만 캡처 (capture_audio=False)
            try:
                screen_recorder = ContinuousScreenRecorder(output_dir, f"{ts}_녹화", capture_audio=False)
                first_segment = screen_recorder.start()
            except Exception:
                try:
                    sys_audio.stop()
                except Exception as e:
                    logger.warning("화면 녹화 시작 실패 후 시스템 오디오 중지 오류: %s", e)
                self._sys_audio = None
                self._audio_path = None
                raise

            screen_start_time, screen_source = self._capture_started_info(
                screen_recorder,
                time.time(),
                "stream_capture_started_at",
                "active_segment_started_at",
            )
            self._screen_recorder = screen_recorder
            logger.info("화면 녹화 시작: %s", first_segment.name)

            # 3) 마이크 처리: SCStream에서 잡혔으면 그대로, 아니면 ffmpeg fallback
            self._using_stream_microphone = False
            if mic_enabled and getattr(sys_audio, "mic_capture_active", False):
                self._mic_started_at = (
                    getattr(sys_audio, "mic_first_sample_at", None)
                    or getattr(sys_audio, "mic_started_at", None)
                    or sys_audio_ready_time
                )
                self._mic_path = mic_path
                self._using_stream_microphone = True
                logger.info("마이크: SCStream에서 캡처됨 (stream microphone)")
            elif mic_enabled:
                try:
                    self._mic_started_at = self._start_mic(mic_path, self._mic_device_index)
                    self._mic_path = mic_path if self._mic_process else None
                except Exception:
                    try:
                        screen_recorder.stop()
                    except Exception as e:
                        logger.warning("마이크 실패 후 화면 녹화 중지 오류: %s", e)
                    try:
                        sys_audio.stop()
                    except Exception as e:
                        logger.warning("마이크 실패 후 시스템 오디오 중지 오류: %s", e)
                    self._screen_recorder = None
                    self._sys_audio = None
                    self._audio_path = None
                    raise
            else:
                self._mic_path = None
                self._mic_started_at = None

            # 4) 싱크 anchor: screen_start_time (SCStream 캡처 시작 콜백 시각, mp4 video time 0에 가까움)
            #    sys_audio_offset = screen_start_time - sys_audio_ready_time
            #    mic_offset = screen_start_time - mic_started_at - latency_correction
            self._audio_offset = self._offset_from_anchor(screen_start_time, sys_audio_ready_time)
            self._mic_audio_offset = self._mic_offset_from_anchor(screen_start_time)
            logger.info(
                "화면 녹화 시작 완료: screen_start=%.3f(%s) sys_started=%.3f(%s) mic_started=%.3f "
                "audio_offset=%.3f mic_offset=%.3f",
                screen_start_time, screen_source,
                sys_audio_ready_time, sys_audio_source,
                self._mic_started_at or 0.0,
                self._audio_offset, self._mic_audio_offset,
            )

            self._mode = "screen"
            self._output_path = first_segment
            self._start_time = screen_start_time
            return first_segment

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
        """현재 실행 중인 프로세스 중지. _lock 보유 상태에서 호출.
        screen 모드: 마이크만 중지 (ContinuousScreenRecorder는 pause/stop에서 직접 제어).
        audio 모드: SystemAudioCapture + 마이크 중지.
        """
        if self._mode == "screen":
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
        """녹화/녹음 일시 정지."""
        with self._lock:
            if self._mode is None or self._is_paused:
                return
            if self._mode == "screen":
                # 시스템 오디오 + 마이크 중지 → 세그먼트 저장, ContinuousScreenRecorder pause
                if self._sys_audio is not None:
                    try:
                        self._sys_audio.stop()
                    except Exception as e:
                        logger.warning("일시정지 중 시스템 오디오 중지 오류: %s", e)
                    self._sys_audio = None
                self._stop_mic()
                self._mic_started_at = None
                # 세그먼트 저장: (video_path, sys_path, mic_path, sys_offset, mic_offset)
                self._segments.append(
                    (self._output_path, self._audio_path, self._mic_path, self._audio_offset, self._mic_audio_offset)
                )
                if self._mic_path is not None:
                    self._screen_mic_segments.append((self._mic_path, self._mic_audio_offset))
                self._output_path = None
                self._audio_path = None
                self._mic_path = None
                self._audio_offset = 0.0
                self._mic_audio_offset = 0.0
                self._screen_recorder.pause()
            else:
                # audio 모드: 기존 세그먼트 기반 처리
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
            self._pause_start = time.time()
            logger.info("일시 정지 (세그먼트 %d 저장)", self._seg_index)

    def resume(self) -> None:
        """일시 정지 재개. 반드시 백그라운드 스레드에서 호출 (SCStream 초기화 블로킹)."""
        with self._lock:
            if not self._is_paused:
                return
            if self._pause_start is not None:
                self._paused_duration += time.time() - self._pause_start
                self._pause_start = None
            from system_audio import SystemAudioCapture

            self._seg_index += 1
            seg = self._seg_index
            ts = self._base_ts

            if self._mode == "screen":
                # 1) 시스템 오디오 + (지원 시) 마이크를 SystemAudioCapture로 새 WAV에 캡처 시작
                sys_path = self._output_dir / f"{ts}_녹화_sys_seg{seg}.wav"
                mic_path = self._output_dir / f"{ts}_녹화_mic_seg{seg}.wav" if self._mic_enabled else None
                sys_audio = SystemAudioCapture()
                sys_audio.start(
                    sys_path,
                    mic_output_path=mic_path if self._mic_enabled else None,
                    mic_device_spec=self._mic_device_index if self._mic_enabled else None,
                )
                sys_audio_ready_time, _ = self._capture_started_info(
                    sys_audio, time.time(), "first_sample_at", "started_at",
                )
                self._sys_audio = sys_audio
                logger.info("재개: 시스템 오디오 시작: %s", sys_path.name)

                # 2) ContinuousScreenRecorder가 SCStream 위에서 새 RecordingOutput 시작
                new_segment = self._screen_recorder.resume()
                resume_time = (
                    self._screen_recorder.active_segment_started_at or time.time()
                )
                logger.info("재개: 화면 녹화 세그먼트 시작: %s", new_segment.name)

                # 3) 마이크: SCStream에서 잡혔으면 그대로, 아니면 ffmpeg fallback
                self._using_stream_microphone = False
                if self._mic_enabled and getattr(sys_audio, "mic_capture_active", False):
                    self._mic_started_at = (
                        getattr(sys_audio, "mic_first_sample_at", None)
                        or getattr(sys_audio, "mic_started_at", None)
                        or sys_audio_ready_time
                    )
                    self._mic_path = mic_path
                    self._using_stream_microphone = True
                elif self._mic_enabled:
                    try:
                        self._mic_started_at = self._start_mic(mic_path, self._mic_device_index)
                        self._mic_path = mic_path if self._mic_process else None
                    except Exception:
                        try:
                            self._screen_recorder.pause()
                        except Exception as e:
                            logger.warning("재개 중 마이크 실패 후 화면 녹화 일시정지 오류: %s", e)
                        try:
                            sys_audio.stop()
                        except Exception as e:
                            logger.warning("재개 중 마이크 실패 후 시스템 오디오 중지 오류: %s", e)
                        self._sys_audio = None
                        raise
                else:
                    self._mic_path = None
                    self._mic_started_at = None

                self._audio_offset = self._offset_from_anchor(resume_time, sys_audio_ready_time)
                self._mic_audio_offset = self._mic_offset_from_anchor(resume_time)
                self._output_path = new_segment
                self._audio_path = sys_path

            elif self._mode == "audio":
                from system_audio import SystemAudioCapture
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

            try:
                if mode == "screen":
                    # 마지막 활성 세그먼트 저장 (sys + mic + 영상 세그먼트의 (출력, 오디오, 마이크) 메타)
                    if not self._is_paused:
                        if self._sys_audio is not None:
                            try:
                                self._sys_audio.stop()
                            except Exception as e:
                                logger.warning("중지 중 시스템 오디오 중지 오류: %s", e)
                            self._sys_audio = None
                        self._stop_mic()
                        self._mic_started_at = None
                        # 마지막 세그먼트 메타 기록
                        self._segments.append(
                            (self._output_path, self._audio_path, self._mic_path, self._audio_offset, self._mic_audio_offset)
                        )
                        if self._mic_path is not None:
                            self._screen_mic_segments.append((self._mic_path, self._mic_audio_offset))

                    # ContinuousScreenRecorder가 모든 영상 세그먼트를 하나의 mp4로 통합
                    try:
                        mp4_path = self._screen_recorder.stop() if self._screen_recorder else None
                    except Exception as exc:
                        logger.error("화면 녹화 중지 오류: %s", exc)
                        mp4_path = None

                    # 시스템 오디오 세그먼트 통합 (필요 시)
                    sys_segments = [(seg[1], seg[3]) for seg in self._segments if seg[1] is not None]
                    if not sys_segments:
                        final_sys = None
                        first_sys_offset = 0.0
                    elif len(sys_segments) == 1:
                        final_sys, first_sys_offset = sys_segments[0]
                    else:
                        try:
                            final_sys = self._concat_screen_sys_segments(sys_segments)
                            first_sys_offset = sys_segments[0][1]
                        except Exception as exc:
                            logger.error("시스템 오디오 세그먼트 통합 실패: %s", exc)
                            final_sys, first_sys_offset = sys_segments[0]

                    # 마이크 세그먼트 통합
                    try:
                        final_mic = self._concat_screen_mic_segments()
                    except Exception as exc:
                        logger.error("마이크 세그먼트 통합 실패: %s", exc)
                        final_mic = self._screen_mic_segments[0][0] if self._screen_mic_segments else None

                    first_mic_offset = self._screen_mic_segments[0][1] if self._screen_mic_segments else 0.0
                    output_path = mp4_path
                    audio_path = final_sys
                    mic_path = final_mic
                    audio_offset = first_sys_offset
                    mic_audio_offset = first_mic_offset

                else:
                    # audio 모드: 기존 세그먼트 기반 처리
                    if not self._is_paused:
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

                    segments = list(self._segments)
                    if not segments:
                        output_path = audio_path = mic_path = None
                        audio_offset = mic_audio_offset = 0.0
                    elif len(segments) == 1:
                        output_path, audio_path, mic_path, audio_offset, mic_audio_offset = segments[0]
                    else:
                        try:
                            output_path, audio_path, mic_path, audio_offset, mic_audio_offset = self._concat_segments(mode, segments)
                        except Exception as exc:
                            logger.error(
                                "세그먼트 통합 실패 — 첫 세그먼트만 사용합니다: %s\n미통합 파일: %s",
                                exc,
                                [str(s[0]) for s in segments],
                            )
                            output_path, audio_path, mic_path, audio_offset, mic_audio_offset = segments[0]
            finally:
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
                self._paused_duration = 0.0
                self._pause_start = None
                self._mic_started_at = None
                self._using_stream_microphone = False
                self._screen_recorder = None
                self._screen_mic_segments = []

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

    def _concat_files(
        self, ffmpeg_bin: str, paths: list, out_path: Path, is_video: bool = False
    ) -> None:
        """ffmpeg concat demuxer로 파일 목록을 하나로 합침.

        input 중 하나가 out_path와 경로가 같아도 안전하도록 임시 파일에 먼저 쓴 뒤 rename.
        영상(-c copy 실패 시): hevc_videotoolbox → libx265 순으로 fallback 재인코드.
        """
        tmp_path = out_path.with_name(f"~concat_{out_path.name}")
        list_file = out_path.with_suffix(".concat.txt")
        renamed = False
        try:
            with open(list_file, "w", encoding="utf-8") as f:
                for p in paths:
                    f.write(f"file '{p}'\n")

            def _run(extra: list) -> subprocess.CompletedProcess:
                return subprocess.run(
                    [ffmpeg_bin, "-f", "concat", "-safe", "0", "-i", str(list_file),
                     *extra, "-y", str(tmp_path)],
                    capture_output=True,
                )

            result = _run(["-c", "copy"])
            if result.returncode != 0:
                if not is_video:
                    logger.warning(
                        "오디오 concat -c copy 실패, pcm_s16le 재샘플로 재시도: %s\n%s",
                        out_path.name, result.stderr.decode(errors="replace"),
                    )
                    result = _run(["-c:a", "pcm_s16le", "-ar", "44100", "-ac", "2"])
                    if result.returncode != 0:
                        logger.error(
                            "오디오 세그먼트 합치기 최종 실패: %s\n%s",
                            out_path.name, result.stderr.decode(errors="replace"),
                        )
                        raise RuntimeError(f"세그먼트 합치기 실패: {out_path.name}")
                else:
                    logger.warning(
                        "영상 concat -c copy 실패, hevc_videotoolbox로 재시도: %s\n%s",
                        out_path.name, result.stderr.decode(errors="replace"),
                    )
                    result = _run([*self._hardware_video_codec_args(), "-c:a", "aac", "-b:a", "256k"])
                    if result.returncode != 0:
                        logger.warning(
                            "hevc_videotoolbox 실패, libx265로 재시도: %s\n%s",
                            out_path.name, result.stderr.decode(errors="replace"),
                        )
                        result = _run([*self._software_video_codec_args(), "-c:a", "aac", "-b:a", "256k"])
                        if result.returncode != 0:
                            logger.error(
                                "영상 세그먼트 합치기 최종 실패: %s\n%s",
                                out_path.name, result.stderr.decode(errors="replace"),
                            )
                            raise RuntimeError(f"세그먼트 합치기 실패: {out_path.name}")

            out_path.unlink(missing_ok=True)
            tmp_path.rename(out_path)
            renamed = True
        finally:
            list_file.unlink(missing_ok=True)
            if not renamed:
                tmp_path.unlink(missing_ok=True)

    def _concat_videos_normalized(self, ffmpeg_bin: str, paths: list, out_path: Path) -> None:
        """비디오 세그먼트의 PTS를 0부터 다시 매기며 재인코딩으로 합친다.

        SCRecordingOutput은 Mach time 기반 PTS를 기록해, -c copy 단순 결합 시
        세그먼트 사이에 일시정지 시간만큼 frozen 구간이 생긴다.
        setpts=PTS-STARTPTS 로 각 세그먼트 PTS를 0부터 재계산한 뒤 libx265 로 재인코딩한다.

        out_path 가 입력 segments[0] 와 동일 경로일 수 있어, 임시 파일에 쓴 뒤
        atomic replace 로 최종 위치에 옮긴다 (ffmpeg는 in-place 편집을 거부함).
        """
        tmp_out = out_path.parent / f"._normalize_tmp_{out_path.name}"
        cmd = [ffmpeg_bin]
        for p in paths:
            cmd.extend(["-i", str(p)])

        n = len(paths)
        reset = "".join(f"[{i}:v]setpts=PTS-STARTPTS[v{i}];" for i in range(n))
        concat_inputs = "".join(f"[v{i}]" for i in range(n))
        filter_complex = f"{reset}{concat_inputs}concat=n={n}:v=1:a=0[v]"

        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-c:v", "libx265", "-preset", "medium", "-crf", "28",
            "-tag:v", "hvc1",
            "-pix_fmt", "yuv420p",
            "-y", str(tmp_out),
        ])

        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode != 0:
            tmp_out.unlink(missing_ok=True)
            err = (result.stderr or b"").decode(errors="replace")[-500:]
            raise RuntimeError(f"PTS 정규화 concat 실패: {err}")

        tmp_out.replace(out_path)

    # 비디오 인코더 인자 단일 진실 소스 (hw/sw). 한쪽을 변경하면
    # _with_software_video_encoder 의 패턴 매처가 자동으로 따라간다.
    _HW_VIDEO_ENCODER_ARGS: tuple[str, ...] = (
        "-c:v", "hevc_videotoolbox",
        "-q:v", "40",
        "-tag:v", "hvc1",
        "-fps_mode", "passthrough",
    )

    _SW_VIDEO_ENCODER_ARGS: tuple[str, ...] = (
        "-c:v", "libx265",
        "-preset", "medium",
        "-crf", "28",
        "-tag:v", "hvc1",
        "-fps_mode", "passthrough",
    )

    @classmethod
    def _hardware_video_codec_args(cls) -> list[str]:
        return list(cls._HW_VIDEO_ENCODER_ARGS)

    @classmethod
    def _software_video_codec_args(cls) -> list[str]:
        return list(cls._SW_VIDEO_ENCODER_ARGS)

    @classmethod
    def _with_software_video_encoder(cls, cmd: list[str]) -> list[str]:
        """ffmpeg cmd 안의 hardware 인코더 블록을 software 인자로 치환.

        매칭은 `-c:v <hardware codec>` 위치만 sentinel 로 잡고, 그 뒤로
        다음 `-` prefix 옵션 그룹(예: `-c:a`, `-map`, `-shortest`, ...)을
        만나기 직전까지의 모든 인코더 옵션을 통째로 교체한다.
        이렇게 하면 hw/sw 옵션에 인자가 추가/변경돼도 매처가 깨지지 않는다.
        """
        hw_codec = cls._HW_VIDEO_ENCODER_ARGS[1]  # "hevc_videotoolbox"
        try:
            idx = next(
                i for i in range(len(cmd) - 1)
                if cmd[i] == "-c:v" and cmd[i + 1] == hw_codec
            )
        except StopIteration:
            return cmd

        # idx 부터 다음 옵션 그룹 시작 전까지를 교체 범위로 잡는다.
        # 인코더 옵션은 `-key value` 페어이므로 idx+2 부터 짝수 단위로 검사.
        end = idx + 2  # `-c:v hw_codec` 다음 위치
        while end + 1 < len(cmd) and cls._is_video_encoder_option(cmd[end]):
            end += 2  # `-key value` 한 쌍 건너뛰기
        return [*cmd[:idx], *cls._software_video_codec_args(), *cmd[end:]]

    # _HW_VIDEO_ENCODER_ARGS / _SW_VIDEO_ENCODER_ARGS 에 등장하는 비디오 인코더용 옵션 키.
    # 다른 옵션(`-c:a`, `-map`, `-shortest`, `-y`, …)은 여기 포함되지 않는다.
    _VIDEO_ENCODER_OPTION_KEYS: frozenset[str] = frozenset({
        "-q:v", "-tag:v", "-fps_mode",
        "-preset", "-crf", "-pix_fmt", "-b:v",
    })

    @classmethod
    def _is_video_encoder_option(cls, token: str) -> bool:
        return token in cls._VIDEO_ENCODER_OPTION_KEYS

    def _concat_screen_sys_segments(self, sys_segments: list[tuple[Path, float]]) -> Optional[Path]:
        """screen 모드 시스템 오디오 세그먼트들을 단일 WAV로 통합."""
        if not sys_segments:
            return None
        if len(sys_segments) == 1:
            return sys_segments[0][0]

        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            logger.warning("ffmpeg 없음 — 첫 시스템 오디오 세그먼트만 사용")
            return sys_segments[0][0]

        ts = self._base_ts
        parent = sys_segments[0][0].parent
        final_sys = parent / f"{ts}_녹화_sys.wav"
        trimmed: list[Path] = []
        try:
            for i, (sys_path, sys_offset) in enumerate(sys_segments):
                if sys_path and sys_path.exists() and sys_path.stat().st_size > 44:
                    trimmed_path = parent / f"_trim_screen_sys_{i}.wav"
                    self._trim_wav(ffmpeg_bin, sys_path, sys_offset, trimmed_path)
                    trimmed.append(trimmed_path)
            if trimmed:
                self._concat_files(ffmpeg_bin, trimmed, final_sys)
        finally:
            for f in trimmed:
                f.unlink(missing_ok=True)

        for sys_path, _ in sys_segments:
            if sys_path and sys_path.exists() and sys_path != final_sys:
                try:
                    sys_path.unlink()
                except OSError:
                    pass
        return final_sys if final_sys.exists() else None

    def _concat_screen_mic_segments(self) -> Optional[Path]:
        """screen 모드 마이크 세그먼트들을 단일 WAV로 통합. 세그먼트 없으면 None 반환."""
        segments = self._screen_mic_segments
        if not segments:
            return None
        if len(segments) == 1:
            return segments[0][0]

        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            logger.warning("ffmpeg 없음 — 첫 마이크 세그먼트만 사용")
            return segments[0][0]

        ts = self._base_ts
        parent = segments[0][0].parent
        final_mic = parent / f"{ts}_녹화_mic.wav"
        trimmed: list[Path] = []
        try:
            for i, (mic_path, mic_offset) in enumerate(segments):
                if mic_path and mic_path.exists() and mic_path.stat().st_size > 44:
                    trimmed_path = parent / f"_trim_screen_mic_{i}.wav"
                    self._trim_wav(ffmpeg_bin, mic_path, mic_offset, trimmed_path)
                    trimmed.append(trimmed_path)
            if trimmed:
                self._concat_files(ffmpeg_bin, trimmed, final_mic)
        finally:
            for f in trimmed:
                f.unlink(missing_ok=True)

        for mic_path, _ in segments:
            if mic_path and mic_path.exists() and mic_path != final_mic:
                try:
                    mic_path.unlink()
                except OSError:
                    pass

        return final_mic if trimmed else None

    def _concat_segments(self, mode: str, segments: list) -> tuple:
        """여러 세그먼트 파일을 하나로 합쳐 (output_path, audio_path, mic_path, sys_offset, mic_offset) 반환."""
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            logger.warning("ffmpeg 없음 — 첫 세그먼트만 사용")
            return segments[0]

        ts = self._base_ts
        parent = segments[0][0].parent

        if mode == "screen":
            video_ext = segments[0][0].suffix if segments[0][0] else ".mp4"
            final_mov = parent / f"{ts}_녹화{video_ext}"
            final_sys = parent / f"{ts}_녹화_sys.wav"
            final_mic = parent / f"{ts}_녹화_mic.wav"

            mov_paths, trimmed_sys, trimmed_mic = [], [], []
            try:
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
                    try:
                        self._concat_videos_normalized(ffmpeg_bin, mov_paths, final_mov)
                    except Exception as norm_err:
                        logger.warning("정규화 concat 실패, 단순 concat으로 폴백: %s", norm_err)
                        self._concat_files(ffmpeg_bin, mov_paths, final_mov)
                if trimmed_sys:
                    self._concat_files(ffmpeg_bin, trimmed_sys, final_sys)
                if trimmed_mic:
                    self._concat_files(ffmpeg_bin, trimmed_mic, final_mic)
            finally:
                for f in trimmed_sys + trimmed_mic:
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
            try:
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
                if trimmed_mic:
                    self._concat_files(ffmpeg_bin, trimmed_mic, final_mic)
            finally:
                for f in trimmed_sys + trimmed_mic:
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
        mic_echo_cancel: bool = False,
    ) -> Path:
        """MOV + 시스템 오디오 WAV [+ 마이크 WAV] → H.264 MP4로 병합 압축. 원본 파일 삭제.

        audio_offset: 오디오가 영상보다 먼저 시작된 시간(초). 양수면 오디오 앞부분 trim.
        mic_echo_cancel: True 면 amix 직전 마이크 트랙에서 시스템 오디오 echo 를 차감.
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

        # 마이크 에코 제거 (sys 와 mic 둘 다 있고 옵션 켜진 경우만)
        if mic_echo_cancel and has_sys and has_mic:
            try:
                from acoustic_echo_cancel import cancel_echo
                aec_path = mic_path.with_name(mic_path.stem + "_aec.wav")
                # mic_audio_offset = mic 가 anchor 보다 먼저 시작된 시간(초)
                # audio_offset    = sys 가 anchor 보다 먼저 시작된 시간(초)
                # mic 시작 시점 - sys 시작 시점 = audio_offset - mic_audio_offset
                mic_minus_sys_offset = audio_offset - mic_audio_offset
                if progress_callback:
                    progress_callback("마이크 에코 제거 중...")
                cancel_echo(
                    mic_path=mic_path,
                    sys_path=audio_path,
                    output_path=aec_path,
                    mic_sys_offset_seconds=mic_minus_sys_offset,
                )
                logger.info("마이크 에코 제거 적용: %s → %s", mic_path.name, aec_path.name)
                mic_path = aec_path
                # AEC 출력은 mic 와 같은 시간축이므로 mic_audio_offset 그대로 사용
            except Exception as e:
                logger.warning(
                    "마이크 에코 제거 실패 — 원본 마이크 트랙으로 진행: %s",
                    e, exc_info=True,
                )

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
                *self._hardware_video_codec_args(),
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
                *self._hardware_video_codec_args(),
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
                *self._hardware_video_codec_args(),
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
                *self._hardware_video_codec_args(),
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

        if returncode != 0 and "hevc_videotoolbox" in cmd:
            logger.warning(
                "videotoolbox 인코더 실패, libx265로 재시도합니다 (exit=%d)",
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
            # mp4(0): video only, sys(1): WAV, mic(2): WAV → amix sys+mic
            cmd = [
                ffmpeg_bin,
                "-i", str(mp4_path),
                *sys_args,
                *mic_args,
                "-filter_complex", f"[1:a:0][2:a:0]{self._amix_filter()}[aout]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-map", "0:v:0", "-map", "[aout]",
                "-shortest",
                "-y", str(temp_path),
            ]
        elif has_mic and not has_sys:
            # mp4에 시스템 오디오가 이미 내장된 경우 — mic을 기존 오디오와 amix
            # normalize=1로 합산 시 클리핑/튐 방지 (오디오 모드와 동일 정책)
            cmd = [
                ffmpeg_bin,
                "-i", str(mp4_path),
                *mic_args,
                "-filter_complex", "[0:a:0][1:a:0]amix=inputs=2:duration=first:dropout_transition=0:normalize=1[aout]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-map", "0:v:0", "-map", "[aout]",
                "-shortest",
                "-y", str(temp_path),
            ]
        else:
            cmd = [
                ffmpeg_bin,
                "-i", str(mp4_path),
                *sys_args,
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
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
