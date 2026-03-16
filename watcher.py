import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from pipeline import run_pipeline

logger = logging.getLogger(__name__)

FILE_STABLE_CHECK_INTERVAL = 3
FILE_STABLE_CHECK_COUNT = 2


class MeetingFileHandler(FileSystemEventHandler):
    def __init__(
        self,
        file_prefix: str,
        config: dict,
        status_callback: Optional[Callable[[str], None]] = None,
        done_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__()
        self._file_prefix = file_prefix
        self._config = config
        self._status_callback = status_callback
        self._done_callback = done_callback
        self._processed: set[str] = set()
        self._task_queue: queue.Queue[str] = queue.Queue()
        self._worker = threading.Thread(target=self._process_loop, daemon=True)
        self._worker.start()

    def _matches(self, path: str) -> bool:
        p = Path(path)
        return (
            p.suffix.lower() in {".mp4", ".mov"}
            and p.name.startswith(self._file_prefix)
        )

    def on_created(self, event: FileSystemEvent):
        if not event.is_directory and self._matches(event.src_path):
            self._enqueue(event.src_path)

    def on_moved(self, event: FileSystemEvent):
        if not event.is_directory and hasattr(event, "dest_path") and self._matches(event.dest_path):
            self._enqueue(event.dest_path)

    def _enqueue(self, path: str):
        real = os.path.realpath(path)
        if real in self._processed:
            logger.debug("이미 처리된 파일 무시: %s", path)
            return
        self._processed.add(real)
        self._task_queue.put(real)
        logger.info("처리 큐에 추가: %s", Path(real).name)

    def _wait_until_stable(self, path: str) -> bool:
        for i in range(FILE_STABLE_CHECK_COUNT):
            try:
                size = os.path.getsize(path)
            except OSError:
                return False
            time.sleep(FILE_STABLE_CHECK_INTERVAL)
            try:
                new_size = os.path.getsize(path)
            except OSError:
                return False
            if size != new_size:
                logger.info("파일 쓰기 진행 중, 대기: %s", Path(path).name)
                return False
        return True

    def _process_loop(self):
        while True:
            path = self._task_queue.get()
            try:
                if not self._wait_until_stable(path):
                    self._processed.discard(path)
                    continue

                logger.info("파이프라인 시작: %s", Path(path).name)
                work_dir = run_pipeline(
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


class FolderWatcher:
    def __init__(
        self,
        config: dict,
        status_callback: Optional[Callable[[str], None]] = None,
        done_callback: Optional[Callable[[str], None]] = None,
    ):
        self._config = config
        self._status_callback = status_callback
        self._done_callback = done_callback
        self._observer: Optional[Observer] = None

    @property
    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def start(self):
        if self.is_running:
            return

        watch_dir = Path(self._config.get("watch_dir", "~/Desktop")).expanduser()
        file_prefix = self._config.get("file_prefix", "회의_")

        if not watch_dir.exists():
            logger.warning("감시 폴더가 존재하지 않아 생성: %s", watch_dir)
            watch_dir.mkdir(parents=True, exist_ok=True)

        handler = MeetingFileHandler(
            file_prefix=file_prefix,
            config=self._config,
            status_callback=self._status_callback,
            done_callback=self._done_callback,
        )

        self._observer = Observer()
        self._observer.schedule(handler, str(watch_dir), recursive=False)
        self._observer.start()
        logger.info("폴더 감시 시작: %s (prefix: %s)", watch_dir, file_prefix)

    def stop(self):
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("폴더 감시 중지")
