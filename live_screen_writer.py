import logging
import threading
import time
from pathlib import Path

import CoreMedia
import ScreenCaptureKit as SCK
import objc
from Foundation import NSURL

logger = logging.getLogger(__name__)

_MPEG4_FILE_TYPE = "public.mpeg-4"
_H264_CODEC = "avc1"


def _objc_value(obj, name: str):
    value = getattr(obj, name)
    return value() if callable(value) else value


class _RecordingOutputDelegate(objc.lookUpClass("NSObject")):
    def initWithOwner_(self, owner):
        self = objc.super(_RecordingOutputDelegate, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    def recordingOutputDidStartRecording_(self, recording_output):
        self._owner._handle_recording_started(recording_output)

    def recordingOutputDidFinishRecording_(self, recording_output):
        self._owner._handle_recording_finished(recording_output)

    def recordingOutput_didFailWithError_(self, recording_output, error):
        self._owner._handle_recording_failed(recording_output, error)


class LiveScreenWriter:
    def __init__(self):
        self._stream = None
        self._recording_output = None
        self._recording_delegate = _RecordingOutputDelegate.alloc().initWithOwner_(self)
        self._ready = threading.Event()
        self._started = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._error = None
        self._running = False
        self._output_path: Path | None = None
        self._display_width = 0
        self._display_height = 0
        self._capture_started_at: float | None = None
        self._started_at: float | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def started_at(self) -> float | None:
        return self._started_at

    @property
    def capture_started_at(self) -> float | None:
        return self._capture_started_at

    def _recording_info(self) -> str:
        if self._recording_output is None:
            return "output=None"
        try:
            duration = self._recording_output.recordedDuration()
        except Exception:
            duration = None
        try:
            file_size = self._recording_output.recordedFileSize()
        except Exception:
            file_size = None
        return f"duration={duration}, size={file_size}"

    def _handle_recording_started(self, recording_output) -> None:
        self._started_at = time.time()
        logger.info(
            "SCRecordingOutput 시작: path=%s, %s",
            self._output_path,
            self._recording_info(),
        )
        self._started.set()

    def _handle_recording_finished(self, recording_output) -> None:
        logger.info(
            "SCRecordingOutput 종료: path=%s, %s",
            self._output_path,
            self._recording_info(),
        )
        self._finished.set()

    def _handle_recording_failed(self, recording_output, error) -> None:
        self._error = RuntimeError(f"SCRecordingOutput 실패: {error}")
        logger.error(
            "SCRecordingOutput 실패: path=%s, error=%s, %s",
            self._output_path,
            error,
            self._recording_info(),
        )
        self._finished.set()

    def start(self, output_path: Path) -> None:
        self._output_path = output_path
        self._ready.clear()
        self._started.clear()
        self._finished.clear()
        self._error = None
        self._running = False
        self._capture_started_at = None
        self._started_at = None

        if output_path.exists():
            output_path.unlink(missing_ok=True)

        def _on_content(content, error):
            if error:
                self._error = RuntimeError(f"화면 콘텐츠 조회 실패: {error}")
                self._ready.set()
                return

            try:
                displays = content.displays()
                if not displays:
                    raise RuntimeError("디스플레이를 찾을 수 없습니다.")

                display = displays[0]
                width = max(2, int(_objc_value(display, "width")))
                height = max(2, int(_objc_value(display, "height")))
                self._display_width = width
                self._display_height = height

                content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(display, [])
                config = SCK.SCStreamConfiguration.alloc().init()
                config.setCapturesAudio_(False)
                config.setWidth_(width)
                config.setHeight_(height)
                if hasattr(config, "setShowsCursor_"):
                    config.setShowsCursor_(True)
                if hasattr(config, "setQueueDepth_"):
                    config.setQueueDepth_(8)
                config.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, 30))

                recording_config = SCK.SCRecordingOutputConfiguration.alloc().init()
                recording_config.setOutputURL_(NSURL.fileURLWithPath_(str(output_path)))
                if _MPEG4_FILE_TYPE in list(recording_config.availableOutputFileTypes()):
                    recording_config.setOutputFileType_(_MPEG4_FILE_TYPE)
                if _H264_CODEC in list(recording_config.availableVideoCodecTypes()):
                    recording_config.setVideoCodecType_(_H264_CODEC)

                recording_output = SCK.SCRecordingOutput.alloc().initWithConfiguration_delegate_(
                    recording_config,
                    self._recording_delegate,
                )

                stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(content_filter, config, None)
                add_result = stream.addRecordingOutput_error_(recording_output, None)
                if isinstance(add_result, tuple):
                    added, add_error = add_result
                else:
                    added, add_error = add_result, None
                if not added:
                    raise RuntimeError(f"화면 recording output 추가 실패: {add_error}")

                self._recording_output = recording_output
                self._stream = stream

                def _on_start(err):
                    if err:
                        self._error = RuntimeError(f"화면 스트림 시작 실패: {err}")
                    else:
                        self._capture_started_at = time.time()
                        self._running = True
                    self._ready.set()

                stream.startCaptureWithCompletionHandler_(_on_start)
            except Exception as exc:
                self._error = exc
                self._ready.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(_on_content)
        self._ready.wait(timeout=10)
        if self._error:
            raise self._error

        self._started.wait(timeout=5)
        logger.info(
            "실시간 화면 writer 시작: %s, size=%sx%s",
            output_path.name,
            self._display_width,
            self._display_height,
        )

    def stop(self) -> None:
        self._running = False

        with self._lock:
            if self._stream is not None and self._recording_output is not None:
                remove_result = self._stream.removeRecordingOutput_error_(self._recording_output, None)
                if isinstance(remove_result, tuple):
                    removed, remove_error = remove_result
                else:
                    removed, remove_error = remove_result, None
                if not removed:
                    raise RuntimeError(f"화면 recording output 제거 실패: {remove_error}")

            self._finished.wait(timeout=10)
            if self._error:
                raise self._error

            if self._output_path and (not self._output_path.exists() or self._output_path.stat().st_size == 0):
                raise RuntimeError(f"화면 녹화 파일이 비어 있습니다: {self._output_path.name}")

            self._recording_output = None
            self._stream = None

        logger.info(
            "실시간 화면 writer 중지: %s, %s",
            self._output_path.name if self._output_path else "-",
            self._recording_info(),
        )
