import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch


os.environ["HOME"] = tempfile.mkdtemp(prefix="automeetingnote-test-home-")

import app


class SyncDiagnosticModeTests(unittest.TestCase):
    def _make_app(self):
        instance = app.AutoMeetingNoteApp.__new__(app.AutoMeetingNoteApp)
        instance._config = {"sync_diagnostic_mode": False, "mic_enabled": True}
        instance._recorder = Mock()
        instance._sync_diagnostic_session = None
        instance._menu = {}
        instance._status_item = SimpleNamespace(title="")
        instance.title = "MN"
        instance._pipeline_running = False
        instance._pipeline_start_time = None
        instance._pending_status_title = None
        instance._pending_app_title = None
        instance._reset_title_at = None
        return instance

    def test_toggle_sync_diagnostic_mode_persists_config(self):
        instance = self._make_app()
        sender = SimpleNamespace(state=False)

        with patch.object(instance, "_save_config") as save_mock:
            instance._toggle_sync_diagnostic(sender)

        self.assertTrue(sender.state)
        self.assertTrue(instance._config["sync_diagnostic_mode"])
        save_mock.assert_called_once_with()

    def test_create_sync_diagnostic_session_returns_none_when_disabled(self):
        instance = self._make_app()
        instance._config["sync_diagnostic_mode"] = False

        session = instance._create_sync_diagnostic_session("screen", Path("/tmp"))

        self.assertIsNone(session)

    def test_create_sync_diagnostic_session_attaches_to_recorder(self):
        instance = self._make_app()
        instance._config.update(
            {
                "sync_diagnostic_mode": True,
                "mic_latency_correction_seconds": 0.487,
            }
        )

        fake_session = Mock()
        with patch("app.SyncDiagnosticSession.create", return_value=fake_session) as create_mock:
            session = instance._create_sync_diagnostic_session("screen", Path("/tmp/demo"))

        self.assertIs(session, fake_session)
        create_mock.assert_called_once()
        fake_session.record_runtime_context.assert_called_once_with(
            resource_path=str(app._resource_path()),
            app_file=str(app._APP_FILE),
            mic_latency_correction_seconds=0.487,
        )
        instance._recorder.attach_sync_diagnostic_session.assert_has_calls([call(None), call(fake_session)])

    def test_flush_ui_emits_pending_sync_probe_on_main_thread(self):
        instance = self._make_app()
        fake_session = Mock()
        instance._sync_diagnostic_session = fake_session
        instance._is_recording = True
        instance._sync_probe_session = fake_session
        instance._sync_probe_due_at = 10.0
        instance._sync_probe_include_flash = True

        with patch("app.time.time", return_value=11.0), patch(
            "app.emit_screen_flash", return_value=21.5
        ) as flash_mock, patch(
            "app.play_probe_click", return_value=21.7
        ) as click_mock:
            instance._flush_ui(None)

        flash_mock.assert_called_once_with()
        click_mock.assert_called_once_with(fake_session.probe_audio_path)
        fake_session.record_probe_emission.assert_called_once_with(
            include_flash=True,
            flash_started_at=21.5,
            click_started_at=21.7,
        )
        self.assertIsNone(instance._sync_probe_session)
        self.assertIsNone(instance._sync_probe_due_at)

    def test_apply_latest_sync_diagnostic_correction_updates_config(self):
        instance = self._make_app()
        instance._config.update(
            {
                "watch_dir": "/tmp/watch",
                "mic_latency_correction_seconds": 0.0,
                "mic_latency_correction_source_session": "",
            }
        )

        with patch.object(instance, "_latest_sync_diagnostic_dir", return_value=Path("/tmp/watch/_sync_diagnostics/20260417-141959_screen")), patch(
            "app.analyze_session",
            return_value={
                "recommendations": {
                    "mic_latency_correction_seconds": 0.487,
                }
            },
        ), patch.object(instance, "_save_config") as save_mock:
            applied = instance._apply_latest_sync_diagnostic_correction()

        self.assertTrue(applied)
        self.assertEqual(instance._config["mic_latency_correction_seconds"], 0.487)
        self.assertEqual(instance._config["mic_latency_correction_source_session"], "20260417-141959_screen")
        save_mock.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
