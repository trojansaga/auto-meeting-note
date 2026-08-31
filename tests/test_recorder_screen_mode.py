import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from recorder import Recorder


class _FixedNow:
    def strftime(self, _fmt):
        return "2026-04-02 12-00-00"


class _FixedDateTime:
    @staticmethod
    def now():
        return _FixedNow()


class _FakeContinuousScreenRecorder:
    def __init__(self, output_dir: Path, basename: str, capture_audio: bool = True):
        self.output_dir = output_dir
        self.basename = basename
        self.is_running = False
        self.is_paused = False
        self.pause_calls = 0
        self.resume_calls = 0
        self.stop_calls = 0
        self.active_segment_started_at = None
        self.stream_capture_started_at = None
        self.capture_audio = capture_audio

    def start(self) -> Path:
        self.is_running = True
        return self.output_dir / f"{self.basename}_seg0.mp4"

    def pause(self) -> None:
        self.pause_calls += 1
        self.is_paused = True

    def resume(self) -> Path:
        self.resume_calls += 1
        self.is_paused = False
        return self.output_dir / f"{self.basename}_seg1.mp4"

    def stop(self) -> Path:
        self.stop_calls += 1
        self.is_running = False
        return self.output_dir / f"{self.basename}.mp4"


class _FakeSystemAudioCapture:
    def __init__(self):
        self.started_at = 100.0
        self.first_sample_at = 100.0
        self.first_sample_host_at = 100.0
        self.mic_capture_active = False
        self.mic_started_at = None
        self.output_path = None
        self.stopped = False

    def start(self, output_path, mic_output_path=None, mic_device_spec=None):
        self.output_path = output_path

    def stop(self):
        self.stopped = True


def _make_recorder_with_fake_screen(output_dir: Path):
    """패치된 ContinuousScreenRecorder + SystemAudioCapture를 사용하는 Recorder와 fake 인스턴스 반환."""
    created = []
    sys_instances = []

    def _factory(od, bn, capture_audio=True):
        r = _FakeContinuousScreenRecorder(od, bn, capture_audio=capture_audio)
        created.append(r)
        return r

    def _sys_factory():
        s = _FakeSystemAudioCapture()
        sys_instances.append(s)
        return s

    csr_patcher = patch("recorder.ContinuousScreenRecorder", side_effect=_factory)
    sys_patcher = patch("system_audio.SystemAudioCapture", side_effect=_sys_factory)

    class _CombinedPatcher:
        def __enter__(self):
            csr_patcher.__enter__()
            sys_patcher.__enter__()
            return self

        def __exit__(self, *args):
            sys_patcher.__exit__(*args)
            csr_patcher.__exit__(*args)

    return _CombinedPatcher(), created


class RecorderScreenModeTests(unittest.TestCase):
    def test_screen_pause_resume_uses_continuous_recorder_and_returns_mp4(self):
        patcher, created = _make_recorder_with_fake_screen(Path("/tmp"))
        with patch("recorder.datetime", _FixedDateTime), patcher:
            recorder = Recorder()
            first_segment = recorder.start_screen_recording(Path("/tmp"), mic_enabled=False)
            recorder.pause()
            recorder.resume()
            mode, output_path, audio_path, mic_path, audio_offset, mic_audio_offset = recorder.stop()

        fake = created[0]
        self.assertEqual(first_segment, Path("/tmp/2026-04-02 12-00-00_녹화_seg0.mp4"))
        self.assertEqual(fake.pause_calls, 1)
        self.assertEqual(fake.resume_calls, 1)
        self.assertEqual(fake.stop_calls, 1)
        self.assertEqual(mode, "screen")
        self.assertEqual(output_path, Path("/tmp/2026-04-02 12-00-00_녹화.mp4"))
        # mic이 비활성화된 상태로 시작하지만 sys WAV는 별도 캡처되어 audio_path는 sys WAV 경로
        self.assertIsNone(mic_path)

    def test_screen_stop_without_pause_returns_mp4(self):
        """일시정지 없이 곧바로 stop해도 mp4 경로가 반환되어야 한다."""
        patcher, created = _make_recorder_with_fake_screen(Path("/tmp"))
        with patch("recorder.datetime", _FixedDateTime), patcher:
            recorder = Recorder()
            recorder.start_screen_recording(Path("/tmp"), mic_enabled=False)
            mode, output_path, audio_path, mic_path, *_ = recorder.stop()

        fake = created[0]
        self.assertEqual(fake.pause_calls, 0)
        self.assertEqual(fake.stop_calls, 1)
        self.assertEqual(mode, "screen")
        self.assertEqual(output_path, Path("/tmp/2026-04-02 12-00-00_녹화.mp4"))

    def test_screen_stop_propagates_recorder_error_as_none_output(self):
        """ContinuousScreenRecorder.stop()이 예외를 던져도 Recorder.stop()은 정상 반환한다."""
        patcher, created = _make_recorder_with_fake_screen(Path("/tmp"))

        def _failing_stop():
            raise RuntimeError("SCStream 종료 오류")

        with patch("recorder.datetime", _FixedDateTime), patcher:
            recorder = Recorder()
            recorder.start_screen_recording(Path("/tmp"), mic_enabled=False)
            created[0].stop = _failing_stop
            mode, output_path, *_ = recorder.stop()

        self.assertEqual(mode, "screen")
        self.assertIsNone(output_path)


class ConcatFilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._dir = Path(self._tmp)

    def _write(self, name: str, content: bytes = b"x" * 100) -> Path:
        p = self._dir / name
        p.write_bytes(content)
        return p

    def test_concat_files_uses_tmp_then_renames(self):
        """output이 input 중 하나와 같은 경로여도 tmp → rename으로 안전하게 처리된다."""
        seg0 = self._write("seg0.wav")
        seg1 = self._write("seg1.wav")
        out = self._dir / "seg0.wav"  # 첫 세그먼트와 동일 경로

        call_log = []

        def _fake_run(cmd, **kwargs):
            call_log.append(cmd)
            tmp_path = Path(cmd[-1])
            tmp_path.write_bytes(b"merged")
            r = MagicMock()
            r.returncode = 0
            return r

        recorder = Recorder()
        with patch("subprocess.run", side_effect=_fake_run):
            recorder._concat_files("ffmpeg", [seg0, seg1], out)

        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes(), b"merged")
        # 임시 파일이 정리되어 있어야 함
        tmp = self._dir / f"~concat_{out.name}"
        self.assertFalse(tmp.exists())

    def test_concat_files_video_fallback_on_copy_fail(self):
        """-c copy 실패 시 hevc_videotoolbox → libx265 순으로 fallback한다.

        호출 횟수 대신 각 시도의 cmd 토큰을 검사하여 폴백 체인이
        실제로 다른 인코더로 진행됐는지 행동 검증한다 (폴백 단계 추가/변경에 강함).
        """
        seg0 = self._write("seg0.mp4")
        seg1 = self._write("seg1.mp4")
        out = self._dir / "final.mp4"
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            r = MagicMock()
            r.stderr = b""
            # cmd 내용으로 결과를 결정 (호출 순서 의존 X)
            if "copy" in cmd:
                r.returncode = 1  # -c copy 실패
            elif "hevc_videotoolbox" in cmd:
                r.returncode = 1  # hardware 인코더 실패
            elif "libx265" in cmd:
                tmp_path = Path(cmd[-1])
                tmp_path.write_bytes(b"reencoded")
                r.returncode = 0  # software 인코더 성공
            else:
                r.returncode = 1
            return r

        recorder = Recorder()
        with patch("subprocess.run", side_effect=_fake_run):
            recorder._concat_files("ffmpeg", [seg0, seg1], out, is_video=True)

        # 결과 파일은 software 인코더가 작성한 내용으로 채워졌어야 한다
        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes(), b"reencoded")

        # 폴백 체인이 모두 시도되었는지 cmd 내용으로 검증
        flat = [tok for cmd in calls for tok in cmd]
        self.assertIn("copy", flat)
        self.assertIn("hevc_videotoolbox", flat)
        self.assertIn("libx265", flat)

    def test_concat_files_audio_fallback_to_resample(self):
        """-c copy 실패 시 pcm_s16le 재샘플로 fallback한다.

        호출 횟수 대신 cmd 내용으로 폴백 동작을 행동 검증.
        """
        seg0 = self._write("seg0.wav")
        seg1 = self._write("seg1.wav")
        out = self._dir / "final.wav"
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            r = MagicMock()
            r.stderr = b""
            if "copy" in cmd:
                r.returncode = 1
            elif "pcm_s16le" in cmd:
                Path(cmd[-1]).write_bytes(b"resampled")
                r.returncode = 0
            else:
                r.returncode = 1
            return r

        recorder = Recorder()
        with patch("subprocess.run", side_effect=_fake_run):
            recorder._concat_files("ffmpeg", [seg0, seg1], out, is_video=False)

        self.assertTrue(out.exists())
        self.assertEqual(out.read_bytes(), b"resampled")
        flat = [tok for cmd in calls for tok in cmd]
        self.assertIn("copy", flat)
        self.assertIn("pcm_s16le", flat)


class VideoEncoderArgsTests(unittest.TestCase):
    """[L1] hw/sw 비디오 인코더 인자 헬퍼와 패턴 매처 회귀 방지."""

    def test_hw_args_starts_with_c_v_flag(self):
        args = Recorder._hardware_video_codec_args()
        self.assertEqual(args[0], "-c:v")
        self.assertEqual(args[1], "hevc_videotoolbox")
        # 동일 객체 반환 시 list mutation 위험 방지 (매번 새 리스트)
        self.assertIsNot(args, Recorder._hardware_video_codec_args())

    def test_sw_args_uses_libx265_with_hvc1_tag(self):
        args = Recorder._software_video_codec_args()
        self.assertIn("libx265", args)
        # macOS 호환을 위한 hvc1 태그 보존
        self.assertEqual(args[args.index("-tag:v") + 1], "hvc1")

    def test_with_software_video_encoder_swaps_hw_block_only(self):
        """sentinel 매처가 -c:v 위치만 잡아 hw 옵션 그룹을 통째로 sw 인자로 교체한다."""
        cmd = [
            "ffmpeg", "-i", "input.mp4",
            *Recorder._hardware_video_codec_args(),
            "-c:a", "aac", "-b:a", "256k",
            "-y", "out.mp4",
        ]
        result = Recorder._with_software_video_encoder(cmd)

        # hw 인자는 사라지고 sw 인자가 대체됨
        self.assertNotIn("hevc_videotoolbox", result)
        self.assertIn("libx265", result)
        # 비디오 인코더 외 다른 옵션 (-c:a aac, -y, ...) 은 보존
        self.assertEqual(result[result.index("-c:a") + 1], "aac")
        self.assertIn("-y", result)
        self.assertEqual(result[-1], "out.mp4")

    def test_with_software_video_encoder_returns_unchanged_when_no_hw_block(self):
        cmd = ["ffmpeg", "-i", "input.mp4", "-c:v", "copy", "-y", "out.mp4"]
        self.assertEqual(Recorder._with_software_video_encoder(cmd), cmd)

    def test_with_software_video_encoder_handles_extra_hw_options_added_later(self):
        """미래에 hw 인자 그룹에 옵션이 추가돼도 매처가 깨지지 않는다.

        예: 누군가 _HW_VIDEO_ENCODER_ARGS 에 `-b:v 5M` 옵션을 추가했을 때.
        매처는 sentinel(-c:v) 위치만 잡고 그 뒤 비디오 인코더 옵션 그룹을
        통째로 교체하므로, 정확한 토큰 카운트 매칭에 의존하지 않는다.
        """
        cmd_with_extra = [
            "ffmpeg", "-i", "input.mp4",
            "-c:v", "hevc_videotoolbox",
            "-q:v", "40",
            "-tag:v", "hvc1",
            "-fps_mode", "passthrough",
            "-b:v", "5M",  # ← 미래 추가
            "-c:a", "aac", "-b:a", "256k",
            "-y", "out.mp4",
        ]
        result = Recorder._with_software_video_encoder(cmd_with_extra)

        self.assertNotIn("hevc_videotoolbox", result)
        # 미래 추가된 -b:v 5M 도 sw 블록과 함께 제거됨
        self.assertNotIn("5M", result)
        self.assertIn("libx265", result)
        # 오디오/출력 옵션은 보존
        self.assertEqual(result[result.index("-c:a") + 1], "aac")
        self.assertEqual(result[-1], "out.mp4")


if __name__ == "__main__":
    unittest.main()
