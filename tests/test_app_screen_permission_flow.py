import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


os.environ["HOME"] = tempfile.mkdtemp(prefix="automeetingnote-test-home-")

import app


class ScreenPermissionFlowTests(unittest.TestCase):
    def _make_app(self):
        instance = app.AutoMeetingNoteApp.__new__(app.AutoMeetingNoteApp)
        instance._is_recording = False
        instance._config = {}
        instance._recorder = Mock()
        return instance

    def test_ensure_screen_permission_requests_access_and_opens_settings_when_denied(self):
        instance = self._make_app()

        with patch.object(instance, "_check_screen_permission", side_effect=[False, False]), patch.object(
            instance, "_request_screen_permission", return_value=False
        ) as request_mock, patch.object(
            instance, "_open_screen_recording_settings", return_value=True
        ) as open_mock, patch.object(
            app.rumps, "alert"
        ) as alert_mock:
            allowed = instance._ensure_screen_permission("화면 녹화")

        self.assertFalse(allowed)
        request_mock.assert_called_once_with()
        open_mock.assert_called_once_with()
        alert_mock.assert_called_once()
        self.assertIn("설정 화면을 열었습니다", alert_mock.call_args.kwargs["message"])

    def test_toggle_screen_record_uses_screen_permission_helper(self):
        instance = self._make_app()
        sender = SimpleNamespace(title="화면 녹화 시작")

        with patch.object(instance, "_ensure_screen_permission", return_value=False) as ensure_mock, patch.object(
            app.threading, "Thread"
        ) as thread_mock:
            instance._toggle_screen_rec(sender)

        ensure_mock.assert_called_once_with("화면 녹화")
        thread_mock.assert_not_called()
        self.assertFalse(instance._is_recording)

    def test_toggle_audio_record_uses_screen_permission_helper(self):
        instance = self._make_app()
        sender = SimpleNamespace(title="녹음 시작")

        with patch.object(instance, "_ensure_screen_permission", return_value=False) as ensure_mock, patch.object(
            app.threading, "Thread"
        ) as thread_mock:
            instance._toggle_audio_rec(sender)

        ensure_mock.assert_called_once_with("시스템 오디오 녹음")
        thread_mock.assert_not_called()
        self.assertFalse(instance._is_recording)


if __name__ == "__main__":
    unittest.main()
