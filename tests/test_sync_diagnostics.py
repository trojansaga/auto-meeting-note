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

    def test_recommend_sync_adjustments_falls_back_to_probe_timestamps_when_flash_missing(self):
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

        recommendations = recommend_sync_adjustments(report)

        self.assertAlmostEqual(recommendations["mic_latency_correction_seconds"], 0.303, places=3)


if __name__ == "__main__":
    unittest.main()
