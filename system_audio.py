"""
SCStream 기반 시스템 오디오 캡처 (macOS 13+)
BlackHole 없이 Mac 내부에서 재생되는 모든 소리를 녹음.
"""
import ctypes
import logging
import os
import struct
import threading
import time
from pathlib import Path
from typing import Optional

import objc

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48000
_CHANNELS = 2
_SAMPLE_WIDTH = 4  # float32
_MIC_DEFAULT_CHANNELS = 1
_MIC_DEFAULT_SAMPLE_WIDTH = 2

# 앱 번들 내에서 stdout이 없으므로 파일로 디버그 로그 기록
_LOG_PATH = os.path.join(os.path.expanduser("~"), "Library", "Logs", "AutoMeetingNote_audio.log")

_AUDIO_FORMAT_LINEAR_PCM = 0x6C70636D  # 'lpcm'
_AUDIO_FORMAT_FLAG_IS_FLOAT = 1 << 0
_AUDIO_FORMAT_FLAG_IS_SIGNED_INTEGER = 1 << 2
_AUDIO_FORMAT_FLAG_IS_NON_INTERLEAVED = 1 << 5

_AUTO_MIC_DEVICE_SPECS = {"", "0", "auto", "default"}
_MACBOOK_MIC_DEVICE_SPECS = {"builtin", "macbook", "current", "local"}
_IPHONE_MIC_DEVICE_SPECS = {"iphone", "ipad", "ios", "continuity"}
_IPHONE_MIC_HINTS = ("iphone", "ipad", "continuity")


def _flog(msg: str):
    """파일 기반 디버그 로그 (앱 번들에서도 볼 수 있음)."""
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            import datetime
            f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')}] {msg}\n")
    except Exception:
        pass


def _write_wav_header(
    f,
    data_bytes: int,
    *,
    sample_rate: int,
    channels: int,
    sample_width: int,
    is_float: bool,
):
    f.seek(0)
    f.write(b'RIFF')
    f.write(struct.pack('<I', 36 + data_bytes))
    f.write(b'WAVE')
    f.write(b'fmt ')
    f.write(struct.pack('<I', 16))
    f.write(struct.pack('<H', 3 if is_float else 1))
    f.write(struct.pack('<H', channels))
    f.write(struct.pack('<I', sample_rate))
    f.write(struct.pack('<I', sample_rate * channels * sample_width))
    f.write(struct.pack('<H', channels * sample_width))
    f.write(struct.pack('<H', sample_width * 8))
    f.write(b'data')
    f.write(struct.pack('<I', data_bytes))


class _AudioBuffer(ctypes.Structure):
    _fields_ = [
        ('mNumberChannels', ctypes.c_uint32),
        ('mDataByteSize',   ctypes.c_uint32),
        ('mData',           ctypes.c_void_p),
    ]


class _AudioBufferListStereo(ctypes.Structure):
    _fields_ = [
        ('mNumberBuffers', ctypes.c_uint32),
        ('mBuffers',       _AudioBuffer * _CHANNELS),
    ]


class _AudioStreamBasicDescription(ctypes.Structure):
    _fields_ = [
        ("mSampleRate", ctypes.c_double),
        ("mFormatID", ctypes.c_uint32),
        ("mFormatFlags", ctypes.c_uint32),
        ("mBytesPerPacket", ctypes.c_uint32),
        ("mFramesPerPacket", ctypes.c_uint32),
        ("mBytesPerFrame", ctypes.c_uint32),
        ("mChannelsPerFrame", ctypes.c_uint32),
        ("mBitsPerChannel", ctypes.c_uint32),
        ("mReserved", ctypes.c_uint32),
    ]


import ScreenCaptureKit as _SCK  # 프레임워크 미리 로드 (프로토콜 참조에 필요)

try:
    _SCStreamOutputProtocol = objc.protocolNamed('SCStreamOutput')
    _DELEGATE_PROTOCOLS = [_SCStreamOutputProtocol]
except Exception:
    _DELEGATE_PROTOCOLS = []

# CoreMedia를 ctypes로 직접 로드 — PyObjC 타입 검사 우회 (AudioBufferList* 전달 시 필요)
_cm_lib = ctypes.CDLL('/System/Library/Frameworks/CoreMedia.framework/CoreMedia')
_cm_lib.CMSampleBufferCopyPCMDataIntoAudioBufferList.restype = ctypes.c_int32
_cm_lib.CMSampleBufferCopyPCMDataIntoAudioBufferList.argtypes = [
    ctypes.c_void_p,   # CMSampleBufferRef
    ctypes.c_int32,    # frameOffset
    ctypes.c_int32,    # numFrames
    ctypes.c_void_p,   # AudioBufferList*
]
_cm_lib.CMSampleBufferGetNumSamples.restype = ctypes.c_long
_cm_lib.CMSampleBufferGetNumSamples.argtypes = [ctypes.c_void_p]
_cm_lib.CMSampleBufferGetFormatDescription.restype = ctypes.c_void_p
_cm_lib.CMSampleBufferGetFormatDescription.argtypes = [ctypes.c_void_p]
_cm_lib.CMAudioFormatDescriptionGetStreamBasicDescription.restype = ctypes.POINTER(_AudioStreamBasicDescription)
_cm_lib.CMAudioFormatDescriptionGetStreamBasicDescription.argtypes = [ctypes.c_void_p]


def _sample_buffer_pointer(sample_buffer) -> int:
    """PyObjC가 넘기는 CMSampleBuffer 표현을 raw 포인터로 정규화."""
    ptr = getattr(sample_buffer, 'pointerAsInteger', None)
    if ptr is not None:
        return int(ptr() if callable(ptr) else ptr)
    return int(objc.pyobjc_id(sample_buffer))


def _normalize_audio_device_spec(spec: Optional[str]) -> str:
    return " ".join((spec or "").strip().lstrip(":").casefold().split())


def _is_iphone_mic(device_name: str) -> bool:
    normalized = _normalize_audio_device_spec(device_name)
    return any(token in normalized for token in _IPHONE_MIC_HINTS)


def _list_capture_audio_devices() -> list[tuple[str, str]]:
    try:
        import AVFoundation
    except Exception:
        return []

    devices = []
    try:
        raw_devices = list(AVFoundation.AVCaptureDevice.devicesWithMediaType_(AVFoundation.AVMediaTypeAudio) or [])
    except Exception:
        raw_devices = []

    if not raw_devices and hasattr(AVFoundation, "AVCaptureDeviceDiscoverySession"):
        try:
            discovery = AVFoundation.AVCaptureDeviceDiscoverySession.discoverySessionWithDeviceTypes_mediaType_position_(
                [
                    getattr(AVFoundation, "AVCaptureDeviceTypeBuiltInMicrophone", None),
                    getattr(AVFoundation, "AVCaptureDeviceTypeMicrophone", None),
                    getattr(AVFoundation, "AVCaptureDeviceTypeExternal", None),
                    getattr(AVFoundation, "AVCaptureDeviceTypeExternalUnknown", None),
                ],
                AVFoundation.AVMediaTypeAudio,
                getattr(AVFoundation, "AVCaptureDevicePositionUnspecified", 0),
            )
            raw_devices = list(discovery.devices() or [])
        except Exception:
            raw_devices = []

    for device in raw_devices:
        try:
            devices.append((str(device.localizedName()), str(device.uniqueID())))
        except Exception:
            continue
    return devices


def resolve_microphone_capture_device_id(requested_spec: Optional[str]) -> Optional[str]:
    normalized = _normalize_audio_device_spec(requested_spec)
    if normalized in _AUTO_MIC_DEVICE_SPECS or normalized in _MACBOOK_MIC_DEVICE_SPECS:
        return None

    devices = _list_capture_audio_devices()
    if not devices:
        return None

    if normalized in _IPHONE_MIC_DEVICE_SPECS:
        for name, unique_id in devices:
            if _is_iphone_mic(name):
                return unique_id
        return None

    for name, unique_id in devices:
        if normalized == _normalize_audio_device_spec(name) or normalized == _normalize_audio_device_spec(unique_id):
            return unique_id
    return None


def _audio_buffer_list_type(buffer_count: int):
    class _AudioBufferList(ctypes.Structure):
        _fields_ = [
            ('mNumberBuffers', ctypes.c_uint32),
            ('mBuffers', _AudioBuffer * max(1, buffer_count)),
        ]

    return _AudioBufferList


def _audio_format_from_sample_buffer(sample_buffer_ptr: int) -> Optional[dict]:
    fmt_desc = _cm_lib.CMSampleBufferGetFormatDescription(sample_buffer_ptr)
    if not fmt_desc:
        return None
    asbd_ptr = _cm_lib.CMAudioFormatDescriptionGetStreamBasicDescription(fmt_desc)
    if not asbd_ptr:
        return None
    asbd = asbd_ptr.contents
    channels = max(1, int(asbd.mChannelsPerFrame or 1))
    sample_rate = int(round(asbd.mSampleRate or _SAMPLE_RATE))
    sample_width = int(asbd.mBitsPerChannel // 8) if asbd.mBitsPerChannel else 0
    if sample_width <= 0:
        if asbd.mBytesPerFrame:
            sample_width = int(asbd.mBytesPerFrame // (channels if asbd.mBytesPerFrame >= channels else 1))
        else:
            sample_width = _SAMPLE_WIDTH
    interleaved = not bool(asbd.mFormatFlags & _AUDIO_FORMAT_FLAG_IS_NON_INTERLEAVED)
    is_float = bool(
        asbd.mFormatID == _AUDIO_FORMAT_LINEAR_PCM and (asbd.mFormatFlags & _AUDIO_FORMAT_FLAG_IS_FLOAT)
    )
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "interleaved": interleaved,
        "is_float": is_float,
    }


def _copy_pcm_bytes(sample_buffer_ptr: int, num_samples: int, fmt: dict) -> bytes:
    channels = int(fmt["channels"])
    sample_width = int(fmt["sample_width"])
    interleaved = bool(fmt["interleaved"])
    total_bytes = num_samples * channels * sample_width

    if interleaved:
        buf = (ctypes.c_ubyte * total_bytes)()
        abl_type = _audio_buffer_list_type(1)
        abl = abl_type()
        abl.mNumberBuffers = 1
        abl.mBuffers[0].mNumberChannels = channels
        abl.mBuffers[0].mDataByteSize = total_bytes
        abl.mBuffers[0].mData = ctypes.cast(buf, ctypes.c_void_p).value
        err = _cm_lib.CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sample_buffer_ptr, 0, num_samples, ctypes.addressof(abl)
        )
        if err != 0:
            raise RuntimeError(f"CMSampleBufferCopyPCMDataIntoAudioBufferList error: {err:#010x}")
        return bytes(buf)

    channel_bytes = num_samples * sample_width
    buffers = [(ctypes.c_ubyte * channel_bytes)() for _ in range(channels)]
    abl_type = _audio_buffer_list_type(channels)
    abl = abl_type()
    abl.mNumberBuffers = channels
    for idx, buf in enumerate(buffers):
        abl.mBuffers[idx].mNumberChannels = 1
        abl.mBuffers[idx].mDataByteSize = channel_bytes
        abl.mBuffers[idx].mData = ctypes.cast(buf, ctypes.c_void_p).value

    err = _cm_lib.CMSampleBufferCopyPCMDataIntoAudioBufferList(
        sample_buffer_ptr, 0, num_samples, ctypes.addressof(abl)
    )
    if err != 0:
        raise RuntimeError(f"CMSampleBufferCopyPCMDataIntoAudioBufferList error: {err:#010x}")

    interleaved_bytes = bytearray(total_bytes)
    for sample_idx in range(num_samples):
        sample_offset = sample_idx * sample_width
        frame_offset = sample_idx * channels * sample_width
        for channel_idx, buf in enumerate(buffers):
            start = frame_offset + channel_idx * sample_width
            interleaved_bytes[start:start + sample_width] = buf[sample_offset:sample_offset + sample_width]
    return bytes(interleaved_bytes)


class _PCMFileWriter:
    def __init__(self, *, default_sample_rate: int, default_channels: int, default_sample_width: int, default_is_float: bool):
        self._default_sample_rate = default_sample_rate
        self._default_channels = default_channels
        self._default_sample_width = default_sample_width
        self._default_is_float = default_is_float
        self._file = None
        self._data_bytes = 0
        self._format = None

    @property
    def is_open(self) -> bool:
        return self._file is not None

    def open(self, path: Path) -> None:
        self._file = open(str(path), 'wb')
        self._data_bytes = 0
        self._format = None
        _write_wav_header(
            self._file,
            0,
            sample_rate=self._default_sample_rate,
            channels=self._default_channels,
            sample_width=self._default_sample_width,
            is_float=self._default_is_float,
        )

    def close(self) -> None:
        if self._file is None:
            return
        fmt = self._format or {
            "sample_rate": self._default_sample_rate,
            "channels": self._default_channels,
            "sample_width": self._default_sample_width,
            "is_float": self._default_is_float,
        }
        _write_wav_header(
            self._file,
            self._data_bytes,
            sample_rate=int(fmt["sample_rate"]),
            channels=int(fmt["channels"]),
            sample_width=int(fmt["sample_width"]),
            is_float=bool(fmt["is_float"]),
        )
        self._file.close()
        self._file = None

    def write_sample_buffer(self, sample_buffer_ptr: int, num_samples: int) -> None:
        if self._file is None:
            return
        if self._format is None:
            self._format = _audio_format_from_sample_buffer(sample_buffer_ptr)
        if self._format is None:
            raise RuntimeError("오디오 포맷 정보를 읽지 못했습니다.")
        pcm_bytes = _copy_pcm_bytes(sample_buffer_ptr, num_samples, self._format)
        self._file.write(pcm_bytes)
        self._data_bytes += len(pcm_bytes)


class _AudioDelegate(objc.lookUpClass('NSObject')):
    __protocols__ = _DELEGATE_PROTOCOLS
    def init(self):
        self = objc.super(_AudioDelegate, self).init()
        if self is None:
            return None
        self._system_writer = _PCMFileWriter(
            default_sample_rate=_SAMPLE_RATE,
            default_channels=_CHANNELS,
            default_sample_width=_SAMPLE_WIDTH,
            default_is_float=True,
        )
        self._mic_writer = _PCMFileWriter(
            default_sample_rate=_SAMPLE_RATE,
            default_channels=_MIC_DEFAULT_CHANNELS,
            default_sample_width=_MIC_DEFAULT_SAMPLE_WIDTH,
            default_is_float=False,
        )
        self._lock = threading.Lock()
        self._audio_call_count = 0
        self._mic_call_count = 0
        self._owner = None
        return self

    def setOwner_(self, owner):
        self._owner = owner

    def openFiles_(self, path, mic_path=None):
        self._system_writer.open(Path(str(path)))
        self._audio_call_count = 0
        self._mic_call_count = 0
        _flog(f"openSystemFile: {path}")
        if mic_path:
            self._mic_writer.open(Path(str(mic_path)))
            _flog(f"openMicFile: {mic_path}")

    def closeFiles(self):
        with self._lock:
            self._system_writer.close()
            self._mic_writer.close()
            _flog("closeFiles")

    def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
        # sample_buffer: CMSampleBufferRef를 raw 포인터(정수)로 받음
        # (selector signature에서 ^{opaqueCMSampleBuffer=}로 지정)
        _SCStreamOutputTypeAudio = 1  # SCStreamOutputTypeAudio 상수
        _SCStreamOutputTypeMicrophone = int(getattr(_SCK, "SCStreamOutputTypeMicrophone", 2))

        if output_type == _SCStreamOutputTypeAudio:
            writer = self._system_writer
            timestamp_attr = "first_sample_at"
            self._audio_call_count += 1
            call_count = self._audio_call_count
        elif output_type == _SCStreamOutputTypeMicrophone:
            writer = self._mic_writer
            timestamp_attr = "mic_first_sample_at"
            self._mic_call_count += 1
            call_count = self._mic_call_count
        else:
            return
        if not writer.is_open:
            return

        try:
            sb_ptr = _sample_buffer_pointer(sample_buffer)

            num_samples = _cm_lib.CMSampleBufferGetNumSamples(sb_ptr)
            if call_count <= 3:
                _flog(f"audio callback #{call_count}: output_type={output_type}, num_samples={num_samples}, sb_ptr={sb_ptr:#x}")
            if num_samples == 0:
                return
            if self._owner is not None and getattr(self._owner, timestamp_attr, None) is None:
                setattr(self._owner, timestamp_attr, time.time())
                _flog(f"{timestamp_attr} set to {getattr(self._owner, timestamp_attr):.6f}")
            with self._lock:
                writer.write_sample_buffer(sb_ptr, num_samples)

        except Exception as e:
            _flog(f"오디오 버퍼 처리 오류: {e} (sample_buffer_type={type(sample_buffer).__name__})")
            logger.debug("오디오 버퍼 처리 오류: %s", e)

    stream_didOutputSampleBuffer_ofType_ = objc.selector(
        stream_didOutputSampleBuffer_ofType_,
        # CMSampleBufferRef를 ObjC id(@)가 아닌 raw 포인터로 받아야
        # ctypes 직접 호출이 가능 — ^{opaqueCMSampleBuffer=} 사용
        signature=b'v@:@^{opaqueCMSampleBuffer=}q',
    )


class SystemAudioCapture:
    """SCStream으로 Mac 시스템 오디오를 float32 WAV로 저장."""

    def __init__(self):
        self._stream = None
        self._delegate = _AudioDelegate.alloc().init()
        self._delegate.setOwner_(self)
        self._output_path: Optional[Path] = None
        self._ready = threading.Event()
        self._error: Optional[Exception] = None
        self.started_at: Optional[float] = None
        self.first_sample_at: Optional[float] = None
        self.mic_started_at: Optional[float] = None
        self.mic_first_sample_at: Optional[float] = None
        self.mic_capture_active: bool = False

    @staticmethod
    def supports_microphone_capture() -> bool:
        return hasattr(_SCK, "SCStreamOutputTypeMicrophone")

    def start(
        self,
        output_path: Path,
        mic_output_path: Optional[Path] = None,
        mic_device_spec: Optional[str] = None,
    ) -> None:
        import ScreenCaptureKit as SCK

        self._output_path = output_path
        normalized_mic_spec = _normalize_audio_device_spec(mic_device_spec)
        mic_requested = mic_output_path is not None
        mic_device_id = resolve_microphone_capture_device_id(mic_device_spec) if mic_requested else None
        self.mic_capture_active = bool(
            mic_requested
            and self.supports_microphone_capture()
            and (
                normalized_mic_spec not in _IPHONE_MIC_DEVICE_SPECS
                or mic_device_id is not None
            )
        )
        self._delegate.openFiles_(output_path, mic_output_path if self.mic_capture_active else None)
        self._ready.clear()
        self._error = None
        self.started_at = None
        self.first_sample_at = None
        self.mic_started_at = None
        self.mic_first_sample_at = None

        _flog(
            f"start() called: {output_path}, mic_output_path={mic_output_path}, "
            f"mic_capture_active={self.mic_capture_active}, mic_device_id={mic_device_id}"
        )

        def _on_content(content, error):
            if error:
                msg = f"콘텐츠 조회 실패: {error}"
                _flog(f"ERROR _on_content: {msg}")
                self._error = RuntimeError(msg)
                self._ready.set()
                return
            try:
                displays = content.displays()
                _flog(f"displays found: {len(displays)}")
                if not displays:
                    raise RuntimeError("디스플레이를 찾을 수 없습니다.")

                content_filter = SCK.SCContentFilter.alloc()\
                    .initWithDisplay_excludingWindows_(displays[0], [])

                config = SCK.SCStreamConfiguration.alloc().init()
                config.setCapturesAudio_(True)
                config.setExcludesCurrentProcessAudio_(False)
                config.setSampleRate_(_SAMPLE_RATE)
                config.setChannelCount_(_CHANNELS)
                if self.mic_capture_active:
                    if hasattr(config, "setCaptureMicrophone_"):
                        config.setCaptureMicrophone_(True)
                    if mic_device_id and hasattr(config, "setMicrophoneCaptureDeviceID_"):
                        config.setMicrophoneCaptureDeviceID_(mic_device_id)
                # 영상은 필요 없으므로 최소 크기로 설정
                config.setWidth_(2)
                config.setHeight_(2)

                self._stream = SCK.SCStream.alloc()\
                    .initWithFilter_configuration_delegate_(content_filter, config, None)
                _flog(f"SCStream created: {self._stream}")

                err_ptr = objc.nil
                add_result = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    self._delegate,
                    SCK.SCStreamOutputTypeAudio,
                    None,
                    None,
                )
                if isinstance(add_result, tuple):
                    added, add_error = add_result
                else:
                    added, add_error = add_result, None
                _flog(f"addStreamOutput result: {added}, error={add_error}")
                if not added:
                    raise RuntimeError(f"오디오 출력 추가 실패: {add_error}")
                if self.mic_capture_active:
                    mic_output_type = getattr(SCK, "SCStreamOutputTypeMicrophone", None)
                    mic_add_result = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
                        self._delegate,
                        mic_output_type,
                        None,
                        None,
                    )
                    if isinstance(mic_add_result, tuple):
                        mic_added, mic_add_error = mic_add_result
                    else:
                        mic_added, mic_add_error = mic_add_result, None
                    _flog(f"addMicStreamOutput result: {mic_added}, error={mic_add_error}")
                    if not mic_added:
                        raise RuntimeError(f"마이크 출력 추가 실패: {mic_add_error}")

                def _on_start(err2):
                    _flog(f"startCapture callback: err={err2}")
                    if err2:
                        self._error = RuntimeError(f"스트림 시작 실패: {err2}")
                    else:
                        self.started_at = time.time()
                        _flog(f"capture started_at set to {self.started_at:.6f}")
                        if self.mic_capture_active:
                            self.mic_started_at = self.started_at
                            _flog(f"mic capture started_at set to {self.mic_started_at:.6f}")
                    self._ready.set()

                self._stream.startCaptureWithCompletionHandler_(_on_start)

            except Exception as e:
                _flog(f"ERROR in _on_content setup: {e}")
                self._error = e
                self._ready.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(_on_content)
        _flog("waiting for ready...")
        self._ready.wait(timeout=8)
        _flog(f"ready! error={self._error}")

        if self._error:
            raise self._error
        logger.info("시스템 오디오 캡처 시작: %s", output_path.name)
        if self.mic_capture_active and mic_output_path is not None:
            logger.info("ScreenCaptureKit 마이크 캡처 시작: %s", mic_output_path.name)
        _flog("SystemAudioCapture started successfully")

    def stop(self) -> None:
        if self._stream:
            try:
                self._stream.stopCaptureWithCompletionHandler_(None)
            except Exception as e:
                _flog(f"stopCapture callback registration failed: {e}")
            self._stream = None
        self._delegate.closeFiles()
        logger.info("시스템 오디오 캡처 중지")
        _flog("SystemAudioCapture stopped")
