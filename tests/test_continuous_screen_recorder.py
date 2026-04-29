import threading
import unittest
from pathlib import Path

from continuous_screen_recorder import ContinuousCaptureController, SegmentHandle


class _FakeDriver:
    def __init__(self):
        self.calls = []
        self._handles = []
        self.fail_on_stop = None

    def start_stream(self):
        self.calls.append("start_stream")

    def stop_stream(self):
        self.calls.append("stop_stream")

    def start_segment(self, path: Path) -> SegmentHandle:
        handle = SegmentHandle(path=path, started=threading.Event(), finished=threading.Event())
        handle.started.set()
        self._handles.append(handle)
        self.calls.append(("start_segment", path.name))
        return handle

    def stop_segment(self, handle: SegmentHandle) -> None:
        self.calls.append(("stop_segment", handle.path.name))
        if self.fail_on_stop is not None:
            handle.error = self.fail_on_stop
        handle.finished.set()


class ContinuousCaptureControllerTests(unittest.TestCase):
    def test_pause_resume_rotates_segments_without_restarting_stream(self):
        driver = _FakeDriver()
        finalized = []

        def finalize(paths):
            finalized.append([p.name for p in paths])
            return Path("/tmp/final.mp4")

        controller = ContinuousCaptureController(
            driver=driver,
            output_dir=Path("/tmp"),
            basename="demo",
            finalize_segments=finalize,
        )

        controller.start()
        controller.pause()
        controller.resume()
        result = controller.stop()

        self.assertEqual(result, Path("/tmp/final.mp4"))
        self.assertEqual(driver.calls[0], "start_stream")
        self.assertEqual(driver.calls.count("start_stream"), 1)
        self.assertEqual(driver.calls.count("stop_stream"), 1)
        self.assertEqual(
            [call for call in driver.calls if isinstance(call, tuple) and call[0] == "start_segment"],
            [("start_segment", "demo_seg0.mp4"), ("start_segment", "demo_seg1.mp4")],
        )
        self.assertEqual(
            [call for call in driver.calls if isinstance(call, tuple) and call[0] == "stop_segment"],
            [("stop_segment", "demo_seg0.mp4"), ("stop_segment", "demo_seg1.mp4")],
        )
        self.assertEqual(finalized, [["demo_seg0.mp4", "demo_seg1.mp4"]])

    def test_stop_with_single_segment_finalizes_once(self):
        driver = _FakeDriver()
        finalized = []

        controller = ContinuousCaptureController(
            driver=driver,
            output_dir=Path("/tmp"),
            basename="single",
            finalize_segments=lambda paths: finalized.append([p.name for p in paths]) or Path("/tmp/single.mp4"),
        )

        controller.start()
        result = controller.stop()

        self.assertEqual(result, Path("/tmp/single.mp4"))
        self.assertEqual(finalized, [["single_seg0.mp4"]])

    def test_segment_failure_surfaces_on_pause(self):
        driver = _FakeDriver()
        driver.fail_on_stop = RuntimeError("segment finalize failed")

        controller = ContinuousCaptureController(
            driver=driver,
            output_dir=Path("/tmp"),
            basename="broken",
            finalize_segments=lambda paths: Path("/tmp/unused.mp4"),
        )

        controller.start()

        with self.assertRaisesRegex(RuntimeError, "segment finalize failed"):
            controller.pause()


if __name__ == "__main__":
    unittest.main()
