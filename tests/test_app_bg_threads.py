import os
import tempfile
import threading
import time
import unittest


os.environ["HOME"] = tempfile.mkdtemp(prefix="automeetingnote-test-home-")

import app


class BackgroundThreadTrackingTests(unittest.TestCase):
    def _make_app(self):
        instance = app.AutoMeetingNoteApp.__new__(app.AutoMeetingNoteApp)
        instance._bg_threads = []
        instance._bg_threads_lock = threading.Lock()
        return instance

    def test_spawn_bg_thread_registers_started_thread(self):
        instance = self._make_app()
        finished = threading.Event()

        def _work():
            finished.wait(timeout=1.0)

        thread = instance._spawn_bg_thread(_work, name="t-spawn")
        try:
            self.assertIn(thread, instance._bg_threads)
            self.assertTrue(thread.daemon)
            self.assertEqual(thread.name, "t-spawn")
            self.assertTrue(thread.is_alive())
        finally:
            finished.set()
            thread.join(timeout=1.0)

    def test_spawn_bg_thread_prunes_dead_threads(self):
        instance = self._make_app()

        def _short():
            return None

        first = instance._spawn_bg_thread(_short, name="dead")
        first.join(timeout=1.0)
        self.assertFalse(first.is_alive())

        live_evt = threading.Event()
        second = instance._spawn_bg_thread(lambda: live_evt.wait(timeout=1.0), name="live")
        try:
            self.assertNotIn(first, instance._bg_threads)
            self.assertIn(second, instance._bg_threads)
        finally:
            live_evt.set()
            second.join(timeout=1.0)

    def test_join_bg_threads_waits_until_done_within_timeout(self):
        instance = self._make_app()
        evt = threading.Event()

        def _work():
            evt.wait(timeout=1.0)

        thread = instance._spawn_bg_thread(_work, name="join-target")
        # 다른 스레드에서 200ms 후 종료 신호
        threading.Timer(0.2, evt.set).start()

        alive = instance._join_bg_threads(timeout=2.0)

        self.assertEqual(alive, 0)
        self.assertFalse(thread.is_alive())

    def test_join_bg_threads_reports_alive_count_when_timeout_exceeded(self):
        instance = self._make_app()
        block = threading.Event()

        def _stuck():
            block.wait(timeout=5.0)

        thread = instance._spawn_bg_thread(_stuck, name="stuck")
        try:
            alive = instance._join_bg_threads(timeout=0.05)
            self.assertEqual(alive, 1)
            self.assertTrue(thread.is_alive())
        finally:
            block.set()
            thread.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
