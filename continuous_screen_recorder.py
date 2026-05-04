import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from audio_extractor import find_ffmpeg

logger = logging.getLogger(__name__)


@dataclass
class SegmentHandle:
    path: Path
    started: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    error: Optional[Exception] = None
    token: object = None
    started_at: Optional[float] = None


class RecordingDriver(Protocol):
    def start_stream(self) -> None:
        ...

    def stop_stream(self) -> None:
        ...

    def start_segment(self, path: Path) -> SegmentHandle:
        ...

    def stop_segment(self, handle: SegmentHandle) -> None:
        ...


class ContinuousCaptureController:
    def __init__(
        self,
        driver: RecordingDriver,
        output_dir: Path,
        basename: str,
        finalize_segments: Callable[[list[Path]], Path],
        segment_extension: str = ".mp4",
        start_timeout: float = 10.0,
        finish_timeout: float = 15.0,
    ):
        self._driver = driver
        self._output_dir = Path(output_dir)
        self._basename = basename
        self._finalize_segments = finalize_segments
        self._segment_extension = segment_extension
        self._start_timeout = start_timeout
        self._finish_timeout = finish_timeout

        self._stream_running = False
        self._paused = False
        self._segment_index = 0
        self._active_segment: Optional[SegmentHandle] = None
        self._segments: list[SegmentHandle] = []

    @property
    def is_running(self) -> bool:
        return self._stream_running

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def active_segment_started_at(self) -> Optional[float]:
        if self._active_segment is None:
            return None
        return self._active_segment.started_at

    @property
    def stream_capture_started_at(self) -> Optional[float]:
        """SCStream 캡처 시작 콜백 시각. RecordingOutput 콜백보다 이른 시점이라
        mp4 audio time 0의 실제 시각을 더 정확히 반영한다."""
        return getattr(self._driver, "stream_capture_started_at", None)

    def start(self) -> Path:
        if self._stream_running:
            raise RuntimeError("화면 녹화 스트림이 이미 실행 중입니다.")

        self._segments = []
        self._segment_index = 0
        self._active_segment = None
        self._paused = False
        self._driver.start_stream()
        self._stream_running = True
        try:
            return self._start_new_segment()
        except Exception:
            self._safe_stop_stream()
            self._stream_running = False
            raise

    def pause(self) -> None:
        if not self._stream_running or self._paused:
            return
        self._finish_active_segment()
        self._paused = True

    def resume(self) -> Path:
        if not self._stream_running:
            raise RuntimeError("화면 녹화 스트림이 시작되지 않았습니다.")
        if not self._paused:
            return self._active_segment.path if self._active_segment else self._segment_path(self._segment_index)
        path = self._start_new_segment()
        self._paused = False
        return path

    def stop(self) -> Path:
        if not self._stream_running:
            raise RuntimeError("화면 녹화 스트림이 시작되지 않았습니다.")

        try:
            if not self._paused and self._active_segment is not None:
                self._finish_active_segment()
            segment_paths = [segment.path for segment in self._segments]
            if not segment_paths:
                raise RuntimeError("완료된 화면 녹화 세그먼트가 없습니다.")
            return self._finalize_segments(segment_paths)
        finally:
            self._safe_stop_stream()
            self._stream_running = False
            self._paused = False
            self._active_segment = None
            self._segments = []
            self._segment_index = 0

    def _start_new_segment(self) -> Path:
        segment_path = self._segment_path(self._segment_index)
        handle = self._driver.start_segment(segment_path)
        self._wait_started(handle)
        self._active_segment = handle
        self._segment_index += 1
        return segment_path

    def _finish_active_segment(self) -> None:
        handle = self._active_segment
        if handle is None:
            return
        self._driver.stop_segment(handle)
        self._wait_finished(handle)
        self._segments.append(handle)
        self._active_segment = None

    def _segment_path(self, index: int) -> Path:
        return self._output_dir / f"{self._basename}_seg{index}{self._segment_extension}"

    def _wait_started(self, handle: SegmentHandle) -> None:
        if not handle.started.wait(timeout=self._start_timeout):
            raise TimeoutError(f"화면 녹화 세그먼트 시작 타임아웃: {handle.path.name}")
        if handle.error is not None:
            raise handle.error

    def _wait_finished(self, handle: SegmentHandle) -> None:
        if not handle.finished.wait(timeout=self._finish_timeout):
            raise TimeoutError(f"화면 녹화 세그먼트 종료 타임아웃: {handle.path.name}")
        if handle.error is not None:
            raise handle.error

    def _safe_stop_stream(self) -> None:
        try:
            self._driver.stop_stream()
        except Exception as exc:
            logger.warning("화면 녹화 스트림 종료 중 경고: %s", exc)


def finalize_mp4_segments(segment_paths: list[Path], final_path: Path) -> Path:
    if not segment_paths:
        raise RuntimeError("병합할 화면 녹화 세그먼트가 없습니다.")

    final_path.parent.mkdir(parents=True, exist_ok=True)

    if len(segment_paths) == 1:
        single = segment_paths[0]
        if single != final_path:
            final_path.unlink(missing_ok=True)
            shutil.move(str(single), str(final_path))
        return final_path

    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        raise EnvironmentError("ffmpeg가 설치되어 있지 않습니다.")

    list_file = final_path.with_suffix(".segments.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for path in segment_paths:
            f.write(f"file '{path}'\n")

    cmd = [
        ffmpeg_bin,
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        "-y",
        str(final_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    list_file.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"화면 녹화 세그먼트 병합 실패: {final_path.name}")

    for path in segment_paths:
        path.unlink(missing_ok=True)
    return final_path


_RECORDING_DELEGATE_CLASS = None


def _get_recording_delegate_class():
    global _RECORDING_DELEGATE_CLASS
    if _RECORDING_DELEGATE_CLASS is not None:
        return _RECORDING_DELEGATE_CLASS

    import objc

    try:
        protocols = [objc.protocolNamed("SCRecordingOutputDelegate")]
    except Exception:
        protocols = []

    class _RecordingOutputDelegate(objc.lookUpClass("NSObject")):
        __protocols__ = protocols

        def initWithOwner_(self, owner):
            self = objc.super(_RecordingOutputDelegate, self).init()
            if self is None:
                return None
            self._owner = owner
            return self

        def recordingOutputDidStartRecording_(self, recording_output):
            self._owner._on_recording_started(recording_output)

        def recordingOutputDidFinishRecording_(self, recording_output):
            self._owner._on_recording_finished(recording_output)

        def recordingOutputTimerDidUpdate_(self, recording_output):
            return None

        def recordingOutput_didFailWithError_(self, recording_output, error):
            self._owner._on_recording_failed(recording_output, error)

    _RECORDING_DELEGATE_CLASS = _RecordingOutputDelegate
    return _RECORDING_DELEGATE_CLASS


class ScreenCaptureKitRecordingDriver:
    def __init__(self, capture_audio: bool = True):
        self._stream = None
        self._delegate = _get_recording_delegate_class().alloc().initWithOwner_(self)
        self._lock = threading.Lock()
        self._handles: dict[int, SegmentHandle] = {}
        self._stream_capture_started_at: Optional[float] = None
        self._capture_audio = capture_audio

    @property
    def stream_capture_started_at(self) -> Optional[float]:
        return self._stream_capture_started_at

    def start_stream(self) -> None:
        import CoreMedia
        import Quartz
        import ScreenCaptureKit as SCK

        if self._stream is not None:
            return

        ready = threading.Event()
        state = {"error": None}

        def _on_content(content, error):
            if error is not None:
                state["error"] = RuntimeError(f"공유 가능한 화면 콘텐츠 조회 실패: {error}")
                ready.set()
                return

            try:
                displays = list(content.displays())
                if not displays:
                    raise RuntimeError("캡처 가능한 디스플레이를 찾을 수 없습니다.")

                display = self._pick_display(displays)
                native_w = Quartz.CGDisplayPixelsWide(Quartz.CGMainDisplayID())
                native_h = Quartz.CGDisplayPixelsHigh(Quartz.CGMainDisplayID())
                # 화면 픽셀의 75% 로 다운스케일 (4K UHD급, HEVC 매크로블록 정렬 위해 짝수로 정규화)
                scale = 0.75
                width = (int(native_w * scale) // 2) * 2
                height = (int(native_h * scale) // 2) * 2

                content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
                config = SCK.SCStreamConfiguration.alloc().init()
                config.setCapturesAudio_(self._capture_audio)
                if self._capture_audio:
                    config.setExcludesCurrentProcessAudio_(False)
                    # 오디오 안정성: 샘플레이트/채널 명시 (system_audio.py와 동일)
                    if hasattr(config, "setSampleRate_"):
                        config.setSampleRate_(48000)
                    if hasattr(config, "setChannelCount_"):
                        config.setChannelCount_(2)
                config.setShowsCursor_(True)
                config.setQueueDepth_(8)
                config.setWidth_(int(width))
                config.setHeight_(int(height))
                config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 30))

                self._stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
                    content_filter,
                    config,
                    None,
                )
                if self._stream is None:
                    raise RuntimeError("SCStream 초기화에 실패했습니다.")

                self._stream.startCaptureWithCompletionHandler_(
                    lambda start_error: self._on_stream_started(start_error, state, ready)
                )
            except Exception as exc:
                state["error"] = exc
                ready.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(_on_content)
        if not ready.wait(timeout=10):
            raise TimeoutError("SCStream 시작 준비 타임아웃")
        if state["error"] is not None:
            self._stream = None
            raise state["error"]

    def start_segment(self, path: Path) -> SegmentHandle:
        import ScreenCaptureKit as SCK
        from Foundation import NSURL

        if self._stream is None:
            raise RuntimeError("SCStream이 시작되지 않았습니다.")

        path.parent.mkdir(parents=True, exist_ok=True)
        handle = SegmentHandle(path=path)

        config = SCK.SCRecordingOutputConfiguration.alloc().init()
        config.setOutputURL_(NSURL.fileURLWithPath_(str(path)))
        config.setOutputFileType_("public.mpeg-4")
        config.setVideoCodecType_("avc1")

        recording_output = SCK.SCRecordingOutput.alloc().initWithConfiguration_delegate_(
            config,
            self._delegate,
        )
        handle.token = recording_output

        with self._lock:
            self._handles[id(recording_output)] = handle

        added = self._stream.addRecordingOutput_error_(recording_output, None)
        if not added:
            with self._lock:
                self._handles.pop(id(recording_output), None)
            raise RuntimeError(f"화면 녹화 세그먼트 추가 실패: {path.name}")
        return handle

    def stop_segment(self, handle: SegmentHandle) -> None:
        if self._stream is None:
            raise RuntimeError("SCStream이 시작되지 않았습니다.")
        if handle.token is None:
            raise RuntimeError(f"유효하지 않은 화면 녹화 세그먼트입니다: {handle.path.name}")

        removed = self._stream.removeRecordingOutput_error_(handle.token, None)
        if not removed:
            raise RuntimeError(f"화면 녹화 세그먼트 종료 실패: {handle.path.name}")

    def stop_stream(self) -> None:
        if self._stream is None:
            return

        ready = threading.Event()
        state = {"error": None}

        def _on_stop(error):
            if error is not None:
                state["error"] = RuntimeError(f"SCStream 종료 실패: {error}")
            ready.set()

        self._stream.stopCaptureWithCompletionHandler_(_on_stop)
        if not ready.wait(timeout=10):
            raise TimeoutError("SCStream 종료 타임아웃")
        self._stream = None
        self._stream_capture_started_at = None
        if state["error"] is not None:
            raise state["error"]

    def _pick_display(self, displays: list) -> object:
        import Quartz

        main_display_id = int(Quartz.CGMainDisplayID())
        for display in displays:
            display_id = getattr(display, "displayID", None)
            if callable(display_id):
                try:
                    display_id = int(display_id())
                except Exception:
                    display_id = None
            elif display_id is not None:
                display_id = int(display_id)
            if display_id == main_display_id:
                return display
        return displays[0]

    def _on_stream_started(self, error, state: dict, ready: threading.Event) -> None:
        if error is not None:
            state["error"] = RuntimeError(f"SCStream 시작 실패: {error}")
        else:
            # SCRecordingOutput 콜백보다 더 이른 실제 캡처 시작 시각 (mp4 audio time 0에 가까움)
            self._stream_capture_started_at = time.time()
        ready.set()

    def _on_recording_started(self, recording_output) -> None:
        handle = self._handle_for_output(recording_output)
        if handle is not None:
            handle.started_at = time.time()
            handle.started.set()

    def _on_recording_finished(self, recording_output) -> None:
        handle = self._detach_handle(recording_output)
        if handle is not None:
            handle.finished.set()

    def _on_recording_failed(self, recording_output, error) -> None:
        handle = self._detach_handle(recording_output)
        if handle is not None:
            handle.error = RuntimeError(f"화면 녹화 세그먼트 오류: {error}")
            handle.started.set()
            handle.finished.set()

    def _handle_for_output(self, recording_output) -> Optional[SegmentHandle]:
        with self._lock:
            return self._handles.get(id(recording_output))

    def _detach_handle(self, recording_output) -> Optional[SegmentHandle]:
        with self._lock:
            return self._handles.pop(id(recording_output), None)


class ContinuousScreenRecorder:
    def __init__(self, output_dir: Path, basename: str, capture_audio: bool = True):
        """capture_audio=False로 호출하면 SCStream에서 시스템 오디오를 캡처하지 않는다.
        이 경우 시스템 오디오는 별도 SystemAudioCapture로 잡아 audio quality(특히 popping 회피)를
        보존하는 것이 권장된다."""
        self._output_dir = Path(output_dir)
        self._basename = basename
        self._final_path = self._output_dir / f"{self._basename}.mp4"
        self._controller = ContinuousCaptureController(
            driver=ScreenCaptureKitRecordingDriver(capture_audio=capture_audio),
            output_dir=self._output_dir,
            basename=self._basename,
            finalize_segments=self._finalize_segments,
        )

    @property
    def is_running(self) -> bool:
        return self._controller.is_running

    @property
    def is_paused(self) -> bool:
        return self._controller.is_paused

    @property
    def active_segment_started_at(self) -> Optional[float]:
        return self._controller.active_segment_started_at

    @property
    def stream_capture_started_at(self) -> Optional[float]:
        return self._controller.stream_capture_started_at

    def start(self) -> Path:
        return self._controller.start()

    def pause(self) -> None:
        self._controller.pause()

    def resume(self) -> Path:
        return self._controller.resume()

    def stop(self) -> Path:
        return self._controller.stop()

    def _finalize_segments(self, segment_paths: list[Path]) -> Path:
        return finalize_mp4_segments(segment_paths, self._final_path)
