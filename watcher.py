import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from pipeline import run_pipeline

logger = logging.getLogger(__name__)

POLL_INTERVAL = 60  # 감시 주기 (초)
FILE_STABLE_WAIT = 3  # 파일 안정 확인 대기 (초)

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _matches(path: str) -> bool:
    p = Path(path)
    return p.suffix.lower() in {".mp4", ".mov"} and _DATE_PREFIX.match(p.name) is not None


def _is_stable(path: str) -> bool:
    """FILE_STABLE_WAIT초 간격으로 파일 크기를 비교해 쓰기 완료 여부 확인."""
    try:
        before = os.path.getsize(path)
        time.sleep(FILE_STABLE_WAIT)
        after = os.path.getsize(path)
        return before == after
    except OSError:
        return False


class FolderWatcher:
    def __init__(
        self,
        config: dict,
        status_callback: Optional[Callable[[str], None]] = None,
        done_callback: Optional[Callable[[str], None]] = None,
        tick_callback: Optional[Callable[[int], None]] = None,
    ):
        self._config = config
        self._status_callback = status_callback
        self._done_callback = done_callback
        self._tick_callback = tick_callback

        self._stop_event = threading.Event()
        self._processed: set[str] = set()
        self._task_queue: queue.Queue[str] = queue.Queue()
        self._poll_thread: Optional[threading.Thread] = None
        self._worker_thread: Optional[threading.Thread] = None

    @property
    def is_running(self) -> bool:
        return self._poll_thread is not None and self._poll_thread.is_alive()

    def start(self):
        if self.is_running:
            return
        self._stop_event.clear()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._worker_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._poll_thread.start()
        self._worker_thread.start()
        watch_dir = Path(self._config.get("watch_dir", "~/Desktop")).expanduser()
        logger.info("폴더 감시 시작: %s (주기: %d초)", watch_dir, POLL_INTERVAL)

    def exclude(self, path: str):
        """외부에서 이미 처리 중인 파일을 watcher가 중복 픽업하지 않도록 등록."""
        self._processed.add(os.path.realpath(path))

    def stop(self):
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        self._poll_thread = None
        self._worker_thread = None
        logger.info("폴더 감시 중지")

    def _poll_loop(self):
        # 시작하자마자 첫 스캔 실행
        self._scan()
        while not self._stop_event.is_set():
            for remaining in range(POLL_INTERVAL, 0, -1):
                if self._stop_event.is_set():
                    return
                if self._tick_callback:
                    self._tick_callback(remaining)
                time.sleep(1)
            self._scan()

    def _scan(self):
        watch_dir = Path(self._config.get("watch_dir", "~/Desktop")).expanduser()
        if not watch_dir.exists():
            watch_dir.mkdir(parents=True, exist_ok=True)

        for f in watch_dir.iterdir():
            if not f.is_file():
                continue
            real = os.path.realpath(str(f))
            if real in self._processed:
                continue
            if not _matches(str(f)):
                continue
            self._processed.add(real)
            self._task_queue.put(real)
            logger.info("처리 큐에 추가: %s", f.name)

    def _process_loop(self):
        while True:
            path = self._task_queue.get()
            try:
                if not _is_stable(path):
                    logger.info("파일 쓰기 진행 중, 다음 스캔에서 재시도: %s", Path(path).name)
                    self._processed.discard(path)
                    continue
                logger.info("파이프라인 시작: %s", Path(path).name)
                run_pipeline(
                    path,
                    self._config,
                    status_callback=self._status_callback,
                )
                if self._done_callback:
                    self._done_callback(Path(path).name)
            except Exception as e:
                logger.error("파이프라인 실패: %s — %s", Path(path).name, e)
            finally:
                self._task_queue.task_done()
