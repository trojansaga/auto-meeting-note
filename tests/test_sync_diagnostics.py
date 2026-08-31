import json
import tempfile
import unittest
from pathlib import Path

from sync_diagnostics import (
    SyncDiagnosticSession,
    detect_audio_onset,
    infer_sync_cause,
    recommend_sync_adjustments,
)


class SyncDiagnosticSessionTests(unittest.TestCase):
    def test_session_preserves_artifacts_and_writes_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            raw_path = base_dir / "demo_sys.wav"
            final_path = base_dir / "demo.mp4"
            raw_path.write_bytes(b"sys-audio")
            final_path.write_bytes(b"video")

            session = SyncDiagnosticSession.create(
                output_dir=base_dir,
                mode="screen",
                app_version="1.1.13",
                mic_enabled=True,
            )
            session.record_sync_snapshot(
                "screen_start",
                {
                    "sys.started_at": 100.25,
                    "mic.started_at": 100.30,
                    "screen.capture_started_at": 100.34,
                    "sys_offset": 0.09,
                    "mic_offset": 0.04,
                },
            )
            session.record_merge_stage(
                "merge_audio",
                media_name="demo.mp4",
                sys_offset=0.09,
                mic_offset=0.04,
                sys_args=["-ss", "0.090", "-i", "demo_sys.wav"],
                mic_args=["-ss", "0.040", "-i", "demo_mic.wav"],
            )

            raw_copy = session.preserve_artifact("raw_system_audio", raw_path, group="raw")
            final_copy = session.preserve_artifact("final_video", final_path, group="final")
            session.finalize(status="completed")

            self.assertTrue(raw_copy.exists())
            self.assertTrue(final_copy.exists())

            metadata = json.loads(session.metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["mode"], "screen")
            self.assertEqual(metadata["status"], "completed")
            self.assertEqual(metadata["artifacts"]["raw_system_audio"]["group"], "raw")
            self.assertEqual(metadata["artifacts"]["final_video"]["group"], "final")
            self.assertEqual(metadata["merge_stages"][0]["media_name"], "demo.mp4")
            self.assertAlmostEqual(metadata["sync_snapshots"]["screen_start"]["mic_offset"], 0.04)

    def test_infer_sync_cause_detects_mic_path_issue(self):
        cause = infer_sync_cause(
            {
                "raw_video_flash": 2.000,
                "raw_system_click": 2.010,
                "raw_mic_click": 1.790,
                "final_video_flash": 2.000,
                "final_mixed_click": 1.790,
            }
        )

        self.assertEqual(cause["category"], "mic_capture_or_mic_offset")
        self.assertIn("마이크", cause["summary"])

    def test_infer_sync_cause_treats_large_raw_delta_as_normal_when_offset_matches(self):
        """raw 트랙이 화면과 0.5초 어긋난 것 자체는 정상이다 — 그만큼 offset 을 적용하면 된다."""
        cause = infer_sync_cause(
            {
                "raw_video_flash": 1.867,
                "raw_system_click": 2.370,
                "final_video_flash": 1.867,
                "final_mixed_click": 1.835,
            },
            screen_snapshot={"sys_offset": 0.537, "mic_offset": 0.0},
        )

        self.assertEqual(cause["category"], "in_sync")
        self.assertAlmostEqual(cause["raw_system_delta"], 0.503, places=3)
        self.assertAlmostEqual(cause["sys_anchor_error"], 0.034, places=3)

    def test_infer_sync_cause_flags_anchor_when_applied_offset_wrong(self):
        cause = infer_sync_cause(
            {
                "raw_video_flash": 1.033,
                "raw_system_click": 2.249,
                "final_video_flash": 1.033,
                "final_mixed_click": 2.013,
            },
            screen_snapshot={"sys_offset": 0.236, "mic_offset": 0.0},
        )

        self.assertEqual(cause["category"], "capture_anchor")
        self.assertAlmostEqual(cause["sys_anchor_error"], -0.980, places=3)

    def test_infer_sync_cause_flags_merge_when_offset_right_but_final_wrong(self):
        cause = infer_sync_cause(
            {
                "raw_video_flash": 1.000,
                "raw_system_click": 1.500,
                "final_video_flash": 1.000,
                "final_mixed_click": 1.400,
            },
            screen_snapshot={"sys_offset": 0.500, "mic_offset": 0.0},
        )

        self.assertEqual(cause["category"], "merge_or_mux")

    def test_session_records_probe_emission_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = SyncDiagnosticSession.create(
                output_dir=Path(tmpdir),
                mode="screen",
                app_version="1.1.13",
                mic_enabled=True,
            )

            session.record_probe_emission(
                include_flash=True,
                flash_started_at=12.5,
                click_started_at=12.8,
            )

            metadata = json.loads(session.metadata_path.read_text(encoding="utf-8"))
            self.assertTrue(metadata["probe"]["include_flash"])
            self.assertEqual(metadata["probe"]["flash_started_at"], 12.5)
            self.assertEqual(metadata["probe"]["click_started_at"], 12.8)

    def test_detect_audio_onset_supports_probe_click_starting_at_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            session = SyncDiagnosticSession.create(
                output_dir=Path(tmpdir),
                mode="screen",
                app_version="1.1.13",
                mic_enabled=True,
            )

            onset = detect_audio_onset(session.probe_audio_path)

            self.assertEqual(onset, 0.0)

    def _write_click_at(self, path: Path, click_at: float, total_seconds: float, amplitude: float) -> None:
        """무음 + 지정 시각의 프로브 클릭(3연타)으로 구성된 WAV 를 만든다."""
        import math
        import struct
        import wave

        from sync_diagnostics import PROBE_PULSE_DURATION, PROBE_PULSES, PROBE_SAMPLE_RATE

        sample_rate = PROBE_SAMPLE_RATE
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            frames = bytearray()
            for frame_idx in range(int(sample_rate * total_seconds)):
                t = frame_idx / sample_rate - click_at
                sample = 0.0
                for start, freq in PROBE_PULSES:
                    if start <= t < (start + PROBE_PULSE_DURATION):
                        sample += math.sin(2.0 * math.pi * freq * (t - start)) * amplitude
                frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767))
            wav_file.writeframes(bytes(frames))

    def test_detect_audio_onset_finds_click_after_leading_silence(self):
        """앞부분이 완전 무음이어도 onset 을 0.0 으로 오판하지 않는다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sys.wav"
            self._write_click_at(path, click_at=2.0, total_seconds=5.0, amplitude=0.8)

            self.assertAlmostEqual(detect_audio_onset(path), 2.0, places=2)

    def test_detect_audio_onset_ignores_noise_outside_expected_window(self):
        """expected_near 밖의 잡음은 클릭으로 잡지 않는다."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "mic.wav"
            self._write_click_at(path, click_at=4.0, total_seconds=6.0, amplitude=0.3)

            self.assertAlmostEqual(detect_audio_onset(path, expected_near=4.1), 4.0, places=2)
            self.assertIsNone(detect_audio_onset(path, expected_near=1.0, search_radius=0.5))

    def test_recommend_sync_adjustments_suggests_mic_latency_correction(self):
        report = {
            "session": {
                "probe": {"include_flash": True},
                "runtime": {
                    "mic_latency_correction_seconds": 0.487,
                },
                "sync_snapshots": {
                    "screen_start": {
                        "mic_offset": -0.201,
                    }
                },
            },
            "measurements": {
                "raw_video_flash": 1.058,
                "raw_mic_click": 1.010,
            },
        }

        recommendations = recommend_sync_adjustments(report)

        self.assertAlmostEqual(recommendations["mic_latency_correction_seconds"], 0.334, places=3)

    def test_recommend_sync_adjustments_uses_fallback_current_correction(self):
        report = {
            "session": {
                "probe": {"include_flash": True},
                "sync_snapshots": {
                    "screen_start": {
                        "mic_offset": -0.201,
                    }
                },
            },
            "measurements": {
                "raw_video_flash": 1.058,
                "raw_mic_click": 1.010,
            },
        }

        recommendations = recommend_sync_adjustments(
            report,
            fallback_current_mic_latency_correction=0.487,
        )

        self.assertAlmostEqual(recommendations["mic_latency_correction_seconds"], 0.334, places=3)

    def test_recommend_sync_adjustments_declines_when_flash_missing(self):
        """플래시 측정이 없으면 보정을 제안하지 않는다.

        예전에는 click_started_at - screen anchor 를 영상 기준점으로 대체했는데, 그 값은
        클릭 재생 지연(afplay spawn + 디바이스 워밍업, 실측 0.5~0.9초)을 그대로 물고 있어
        엉뚱한 mic_latency_correction 을 config 에 밀어 넣는 경로였다.
        """
        report = {
            "session": {
                "probe": {
                    "include_flash": True,
                    "click_started_at": 1776404276.9505532,
                },
                "runtime": {
                    "mic_latency_correction_seconds": 0.335,
                },
                "sync_snapshots": {
                    "screen_start": {
                        "mic_offset": 1.1144058589935302,
                        "screen.capture_started_at": 1776404275.857346,
                    }
                },
            },
            "measurements": {
                "raw_video_flash": None,
                "raw_mic_click": 2.24,
            },
        }

        self.assertEqual(recommend_sync_adjustments(report), {})

    def test_recommend_sync_adjustments_declines_when_click_timing_unreliable(self):
        report = {
            "session": {
                "probe": {
                    "include_flash": True,
                    "click_timing_reliable": False,
                    "flash_started_at": 1776404276.95,
                    "click_started_at": 1776404276.95,
                },
                "runtime": {"mic_latency_correction_seconds": 0.335},
                "sync_snapshots": {"screen_start": {"mic_offset": 1.114}},
            },
            "measurements": {
                "raw_video_flash": 1.0,
                "raw_mic_click": 2.24,
            },
        }

        self.assertEqual(recommend_sync_adjustments(report), {})


if __name__ == "__main__":
    unittest.main()
