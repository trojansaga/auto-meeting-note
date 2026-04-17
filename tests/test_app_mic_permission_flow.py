import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


os.environ["HOME"] = tempfile.mkdtemp(prefix="automeetingnote-test-home-")

import app


class MicPermissionFlowTests(unittest.TestCase):
    def _make_app(self):
        instance = app.AutoMeetingNoteApp.__new__(app.AutoMeetingNoteApp)
        instance._is_recording = False
        instance._config = {"mic_enabled": True}
        instance._recorder = Mock()
        return instance

    def test_ensure_mic_permission_requests_access_and_opens_settings_when_denied(self):
        instance = self._make_app()

        with patch.object(instance, "_check_mic_permission", side_effect=[False, False]), patch.object(
            instance, "_request_mic_permission", return_value=False
        ) as request_mock, patch.object(
            instance, "_open_microphone_settings", return_value=True
        ) as open_mock, patch.object(
            app.rumps, "alert"
        ) as alert_mock:
            allowed = instance._ensure_mic_permission("마이크 녹음")

        self.assertFalse(allowed)
        request_mock.assert_called_once_with()
        open_mock.assert_called_once_with()
        alert_mock.assert_called_once()
        self.assertIn("설정 화면을 열었습니다", alert_mock.call_args.kwargs["message"])

    def test_toggle_audio_record_requires_mic_permission_when_enabled(self):
        instance = self._make_app()
        sender = SimpleNamespace(title="녹음 시작")

        with patch.object(instance, "_ensure_screen_permission", return_value=True) as screen_mock, patch.object(
            instance, "_ensure_mic_permission", return_value=False
        ) as mic_mock, patch.object(app.threading, "Thread") as thread_mock:
            instance._toggle_audio_rec(sender)

        screen_mock.assert_called_once_with("시스템 오디오 녹음")
        mic_mock.assert_called_once_with("마이크 녹음")
        thread_mock.assert_not_called()
        self.assertFalse(instance._is_recording)


if __name__ == "__main__":
    unittest.main()
