"""
SCStream 기반 시스템 오디오 캡처 (macOS 13+)
BlackHole 없이 Mac 내부에서 재생되는 모든 소리를 녹음.
"""
import ctypes
import logging
import os
import struct
import threading
from pathlib import Path
from typing import Optional

import objc

logger = logging.getLogger(__name__)

_SAMPLE_RATE = 48000
_CHANNELS = 2
_SAMPLE_WIDTH = 4  # float32

# 앱 번들 내에서 stdout이 없으므로 파일로 디버그 로그 기록
_LOG_PATH = os.path.join(os.path.expanduser("~"), "Library", "Logs", "AutoMeetingNote_audio.log")


def _flog(msg: str):
    """파일 기반 디버그 로그 (앱 번들에서도 볼 수 있음)."""
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            import datetime
            f.write(f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')}] {msg}\n")
    except Exception:
        pass


def _write_wav_header(f, data_bytes: int):
    f.seek(0)
    f.write(b'RIFF')
    f.write(struct.pack('<I', 36 + data_bytes))
    f.write(b'WAVE')
    f.write(b'fmt ')
    f.write(struct.pack('<I', 16))
    f.write(struct.pack('<H', 3))  # IEEE float PCM
    f.write(struct.pack('<H', _CHANNELS))
    f.write(struct.pack('<I', _SAMPLE_RATE))
    f.write(struct.pack('<I', _SAMPLE_RATE * _CHANNELS * _SAMPLE_WIDTH))
    f.write(struct.pack('<H', _CHANNELS * _SAMPLE_WIDTH))
    f.write(struct.pack('<H', _SAMPLE_WIDTH * 8))
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


class _AudioDelegate(objc.lookUpClass('NSObject')):
    __protocols__ = _DELEGATE_PROTOCOLS
    def init(self):
        self = objc.super(_AudioDelegate, self).init()
        if self is None:
            return None
        self._file = None
        self._lock = threading.Lock()
        self._bytes_written = 0
        self._call_count = 0
        return self

    def openFile_(self, path):
        self._file = open(str(path), 'wb')
        self._bytes_written = 0
        self._call_count = 0
        _write_wav_header(self._file, 0)
        _flog(f"openFile: {path}")

    def closeFile(self):
        with self._lock:
            if self._file:
                _write_wav_header(self._file, self._bytes_written)
                self._file.close()
                self._file = None
                _flog(f"closeFile: bytes_written={self._bytes_written}, calls={self._call_count}")

    def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
        # sample_buffer: CMSampleBufferRef를 raw 포인터(정수)로 받음
        # (selector signature에서 ^{opaqueCMSampleBuffer=}로 지정)
        _SCStreamOutputTypeAudio = 1  # SCStreamOutputTypeAudio 상수

        self._call_count += 1
        if self._call_count == 1:
            _flog(f"delegate called! output_type={output_type}")

        if output_type != _SCStreamOutputTypeAudio:
            return
        if self._file is None:
            return

        try:
            # sample_buffer는 objc.PyObjCPointer → .pointerAsInteger로 raw 주소 획득
            sb_ptr = sample_buffer.pointerAsInteger

            num_samples = _cm_lib.CMSampleBufferGetNumSamples(sb_ptr)
            if self._call_count <= 3:
                _flog(f"audio callback #{self._call_count}: num_samples={num_samples}, sb_ptr={sb_ptr:#x}")
            if num_samples == 0:
                return

            bytes_per_ch = num_samples * _SAMPLE_WIDTH

            # AudioBufferList 준비 (non-interleaved: 채널당 1버퍼)
            ch0_buf = (ctypes.c_float * num_samples)()
            ch1_buf = (ctypes.c_float * num_samples)()

            abl = _AudioBufferListStereo()
            abl.mNumberBuffers = _CHANNELS
            abl.mBuffers[0].mNumberChannels = 1
            abl.mBuffers[0].mDataByteSize = bytes_per_ch
            abl.mBuffers[0].mData = ctypes.cast(ch0_buf, ctypes.c_void_p).value
            abl.mBuffers[1].mNumberChannels = 1
            abl.mBuffers[1].mDataByteSize = bytes_per_ch
            abl.mBuffers[1].mData = ctypes.cast(ch1_buf, ctypes.c_void_p).value

            err = _cm_lib.CMSampleBufferCopyPCMDataIntoAudioBufferList(
                sb_ptr, 0, num_samples, ctypes.addressof(abl)
            )
            if err != 0:
                _flog(f"CMSampleBufferCopyPCMDataIntoAudioBufferList error: {err:#010x}")
                return

            # non-interleaved → interleaved: L R L R …
            interleaved = bytearray(num_samples * _CHANNELS * _SAMPLE_WIDTH)
            for i in range(num_samples):
                struct.pack_into('<f', interleaved, i * 8,     ch0_buf[i])
                struct.pack_into('<f', interleaved, i * 8 + 4, ch1_buf[i])

            with self._lock:
                if self._file:
                    self._file.write(interleaved)
                    self._bytes_written += len(interleaved)

        except Exception as e:
            _flog(f"오디오 버퍼 처리 오류: {e}")
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
        self._output_path: Optional[Path] = None
        self._ready = threading.Event()
        self._error: Optional[Exception] = None

    def start(self, output_path: Path) -> None:
        import ScreenCaptureKit as SCK

        self._output_path = output_path
        self._delegate.openFile_(output_path)
        self._ready.clear()
        self._error = None

        _flog(f"start() called: {output_path}")

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
                # 영상은 필요 없으므로 최소 크기로 설정
                config.setWidth_(2)
                config.setHeight_(2)

                self._stream = SCK.SCStream.alloc()\
                    .initWithFilter_configuration_delegate_(content_filter, config, None)
                _flog(f"SCStream created: {self._stream}")

                err_ptr = objc.nil
                added = self._stream.addStreamOutput_type_sampleHandlerQueue_error_(
                    self._delegate,
                    SCK.SCStreamOutputTypeAudio,
                    None,
                    None,
                )
                _flog(f"addStreamOutput result: {added}")
                if not added:
                    raise RuntimeError("오디오 출력 추가 실패")

                def _on_start(err2):
                    _flog(f"startCapture callback: err={err2}")
                    if err2:
                        self._error = RuntimeError(f"스트림 시작 실패: {err2}")
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
        _flog("SystemAudioCapture started successfully")

    def stop(self) -> None:
        self._delegate.closeFile()
        if self._stream:
            self._stream.stopCaptureWithCompletionHandler_(lambda e: None)
            self._stream = None
        logger.info("시스템 오디오 캡처 중지")
        _flog("SystemAudioCapture stopped")
