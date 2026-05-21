import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ["HOME"] = tempfile.mkdtemp(prefix="automeetingnote-test-home-")

import app


class _ImmediateTimer:
    def __init__(self, callback, _interval):
        self._callback = callback

    def start(self):
        self._callback(self)

    def stop(self):
        return None


class RecordingBusyGuardTests(unittest.TestCase):
    def _make_app(self):
        instance = app.AutoMeetingNoteApp.__new__(app.AutoMeetingNoteApp)
        instance._config = {"stt_skip": False}
        instance._pipeline_running = True
        instance._status_log = []
        instance._pending_status_title = None
        instance._reset_title_at = None
        instance._is_recording = False
        instance._notify = Mock()
        instance._on_status = Mock()
        instance._schedule_title_reset = Mock()
        instance._run_single_file = Mock(side_effect=AssertionError("회의록 생성이 시작되면 안 됩니다."))
        instance._confirm_on_main = Mock(side_effect=AssertionError("확인 팝업을 띄우면 안 됩니다."))
        return instance

    def test_processing_busy_skips_note_generation_prompt_for_completed_recordings(self):
        cases = [
            ("screen", Path("/tmp/demo.mp4")),
            ("audio", Path("/tmp/demo.wav")),
        ]

        for mode, output_path in cases:
            instance = self._make_app()
            with self.subTest(mode=mode), patch.object(app.rumps, "Timer", _ImmediateTimer), patch.object(
                app.rumps, "alert"
            ) as alert_mock:
                instance._on_recording_stopped(mode, output_path)

            instance._confirm_on_main.assert_not_called()
            instance._run_single_file.assert_not_called()
            instance._schedule_title_reset.assert_called_once_with(5)
            instance._on_status.assert_any_call("처리중이므로 회의록 생성이 불가능하여 여기서 종료합니다.")
            alert_mock.assert_called_once_with(
                title="회의록 생성 불가",
                message="처리중이므로 회의록 생성이 불가능하여 여기서 종료합니다.",
            )


class RunSingleFileFailureCleanupTests(unittest.TestCase):
    """load_config 같은 초기 단계 예외에서도 파이프라인 상태가 잔류하지 않아야 한다."""

    def _make_app(self):
        instance = app.AutoMeetingNoteApp.__new__(app.AutoMeetingNoteApp)
        instance._config = {}
        instance._pipeline_running = False
        instance._pipeline_start_time = None
        instance._pipeline_step = (0, 0)
        instance._pipeline_base_msg = ""
        instance._pipeline_stop_event = Mock()
        instance._pipeline_pause_event = Mock()
        instance._status_log = []
        instance._pending_status_title = None
        instance._reset_title_at = None
        instance._notify = Mock()
        instance._schedule_title_reset = Mock()
        return instance

    def test_load_config_failure_clears_pipeline_running_state(self):
        instance = self._make_app()
        with patch.object(app, "load_config", side_effect=RuntimeError("yaml broken")):
            instance._run_single_file("/tmp/demo.mp4")

        self.assertFalse(instance._pipeline_running)
        self.assertIsNone(instance._pipeline_start_time)


if __name__ == "__main__":
    unittest.main()
