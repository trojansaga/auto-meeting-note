import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

# 앱 번들은 시스템 PATH를 상속하지 않으므로 Homebrew 경로를 명시적으로 추가
for _p in ["/opt/homebrew/bin", "/usr/local/bin"]:
    if _p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

# HuggingFace 모델 저장 경로를 쓰기 가능한 위치로 지정
_HF_HOME = str(Path.home() / "Library" / "Application Support" / "AutoMeetingNote" / "huggingface")
os.environ.setdefault("HF_HOME", _HF_HOME)
Path(_HF_HOME).mkdir(parents=True, exist_ok=True)

# mlx, lightning_whisper_mlx 등 네이티브 패키지는 번들링 불가 → venv site-packages 참조
# 번들 경로: .app/Contents/Resources/app.py → 5단계 위가 프로젝트 루트
_APP_FILE = Path(__file__).resolve()
_PROJECT_ROOT = _APP_FILE.parent.parent.parent.parent.parent  # dist/../ = 프로젝트 루트
_PY_VER = f"python{sys.version_info.major}.{sys.version_info.minor}"
_VENV_SITE = _PROJECT_ROOT / ".venv" / "lib" / _PY_VER / "site-packages"
if not _VENV_SITE.exists():
    _VENV_SITE = _APP_FILE.parent / ".venv" / "lib" / _PY_VER / "site-packages"
if _VENV_SITE.exists() and str(_VENV_SITE) not in sys.path:
    sys.path.insert(0, str(_VENV_SITE))

import rumps
import yaml
from dotenv import load_dotenv

from pipeline import run_pipeline
from watcher import FolderWatcher

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "AutoMeetingNote"
APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = Path.home() / "Library" / "Logs" / "AutoMeetingNote"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def _resource_path() -> Path:
    """py2app 번들 또는 개발 환경에서의 리소스 경로"""
    if getattr(sys, "frozen", False):
        return Path(os.environ.get("RESOURCEPATH", Path(__file__).parent))
    return Path(__file__).parent


def _ensure_user_config() -> Path:
    """Application Support에 config.yaml이 없으면 번들 기본값 복사"""
    user_config = APP_SUPPORT_DIR / "config.yaml"
    if not user_config.exists():
        bundled = _resource_path() / "config.yaml"
        if bundled.exists():
            shutil.copy2(str(bundled), str(user_config))
            logger.info("기본 config.yaml 복사: %s", user_config)
        else:
            default = {
                "watch_dir": "~/Desktop",
                "file_prefix": "회의_",
                "whisper_model": "large-v3",
                "language": "ko",
                "openai_model": "gpt-5.3",
            }
            user_config.write_text(yaml.dump(default, allow_unicode=True), encoding="utf-8")
            logger.info("기본 config.yaml 생성: %s", user_config)
    return user_config


CONFIG_PATH = _ensure_user_config()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.warning("config.yaml 없음, 기본값 사용")
        return {
            "watch_dir": "~/Desktop",
            "file_prefix": "회의_",
            "whisper_model": "large-v3",
            "language": "ko",
            "openai_model": "gpt-5.3",
        }
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class AutoMeetingNoteApp(rumps.App):
    def __init__(self):
        super().__init__("MN", quit_button=None)

        env_path = APP_SUPPORT_DIR / ".env"
        if not env_path.exists():
            bundled_env = _resource_path() / ".env"
            if bundled_env.exists():
                shutil.copy2(str(bundled_env), str(env_path))
        load_dotenv(env_path)

        self._config = load_config()
        self._watcher = FolderWatcher(
            config=self._config,
            status_callback=self._on_status,
            done_callback=self._on_done,
        )

        self._status_log: deque = deque(maxlen=50)
        self._pending_status_title = None
        self._pending_app_title = None
        rumps.Timer(self._flush_ui, 0.3).start()

        self._toggle_item = rumps.MenuItem("감시 시작", callback=self._toggle_watch)
        self._process_pending_item = rumps.MenuItem("미처리 파일 즉시 처리", callback=self._process_pending)
        self._status_item = rumps.MenuItem("처리 현황: 대기 중", callback=self._show_status_detail)
        self._open_folder_item = rumps.MenuItem("감시 폴더 열기", callback=self._open_watch_dir)
        self._open_config_item = rumps.MenuItem("설정 파일 열기", callback=self._open_config)
        self._quit_item = rumps.MenuItem("종료", callback=self._quit)

        self._check_dependencies()

        self.menu = [
            self._toggle_item,
            self._process_pending_item,
            None,
            self._status_item,
            None,
            self._open_folder_item,
            self._open_config_item,
            None,
            self._quit_item,
        ]

    def _check_dependencies(self):
        import shutil
        errors = []
        warnings = []

        if not shutil.which("ffmpeg"):
            errors.append("ffmpeg가 없습니다.\n터미널에서 'brew install ffmpeg' 실행 후 앱을 재시작하세요.")

        if not os.environ.get("OPENAI_API_KEY"):
            errors.append("OPENAI_API_KEY가 설정되지 않았습니다.\n설정 파일 열기 메뉴에서 .env 파일을 확인하세요.")

        try:
            import mlx_whisper  # noqa: F401
        except ImportError:
            errors.append("mlx_whisper 패키지가 없습니다.\n터미널에서 'pip install mlx-whisper' 실행 후 앱을 재시작하세요.")

        if errors:
            rumps.alert(title="설정 오류", message="\n\n".join(errors))
            return

        model_name = self._config.get("whisper_model", "small")
        quant = self._config.get("whisper_quant", "4bit")
        from transcriber import MODEL_REPOS
        variants = MODEL_REPOS.get(model_name, {})
        repo = variants.get(quant if quant in variants else "base", "")
        if repo:
            cache_dir_name = "models--" + repo.replace("/", "--")
            hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
            model_cache = hf_home / "hub" / cache_dir_name
            if not model_cache.exists():
                threading.Thread(
                    target=self._download_model,
                    args=(repo, model_cache, hf_home),
                    daemon=True,
                ).start()

    def _download_model(self, repo: str, model_cache: Path, hf_home: Path):
        import time
        import huggingface_hub

        self._on_status(f"모델 다운로드 준비 중: {repo}")

        total_files = None
        try:
            total_files = sum(1 for _ in huggingface_hub.list_repo_files(repo))
            logger.info("다운로드 대상: %s (%d 파일)", repo, total_files)
        except Exception as e:
            logger.warning("파일 목록 조회 실패: %s", e)

        stop_monitor = threading.Event()

        def _monitor():
            start_time = time.time()
            blobs_dir = model_cache / "blobs"
            while not stop_monitor.is_set():
                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                time_str = f"{mins}분 {secs}초" if mins else f"{secs}초"
                downloaded = 0
                if blobs_dir.exists():
                    downloaded = sum(
                        1 for f in blobs_dir.iterdir()
                        if not f.name.endswith(".incomplete")
                    )
                if total_files:
                    pct = min(downloaded / total_files * 100, 99)
                    self._on_status(f"모델 다운로드 중... {pct:.0f}% ({downloaded}/{total_files}개) [{time_str}]")
                else:
                    self._on_status(f"모델 다운로드 중... ({downloaded}개 완료) [{time_str}]")
                time.sleep(1)

        monitor_thread = threading.Thread(target=_monitor, daemon=True)
        monitor_thread.start()

        try:
            huggingface_hub.snapshot_download(repo_id=repo)
            stop_monitor.set()
            monitor_thread.join(timeout=2)
            self._on_status(f"✅ 모델 다운로드 완료: {repo}")
            rumps.notification(
                title="모델 다운로드 완료",
                subtitle=repo,
                message="Whisper 모델 준비가 완료되었습니다.",
            )
            logger.info("모델 다운로드 완료: %s", repo)
        except Exception as e:
            stop_monitor.set()
            monitor_thread.join(timeout=2)
            logger.error("모델 다운로드 실패: %s — %s", repo, e)
            self._on_status(f"❌ 모델 다운로드 실패: {repo}")
            rumps.notification(
                title="모델 다운로드 실패",
                subtitle=repo,
                message=str(e),
            )

    def _flush_ui(self, _):
        if self._pending_app_title is not None:
            self.title = self._pending_app_title
            self._pending_app_title = None
        if self._pending_status_title is not None:
            self._status_item.title = self._pending_status_title
            self._pending_status_title = None

    def _on_status(self, message: str):
        logger.info("상태: %s", message)
        self._status_log.append(message)
        self._pending_app_title = "MN ⏳"
        self._pending_status_title = f"처리 현황: {message}"

    def _on_done(self, filename: str):
        done_msg = f"✅ 완료: {filename}"
        self._status_log.append(done_msg)
        self._pending_app_title = "MN ✅"
        self._pending_status_title = f"처리 현황: {done_msg}"
        rumps.notification(
            title="회의록 자동 생성 완료",
            subtitle=filename,
            message="회의록이 성공적으로 생성되었습니다.",
        )
        rumps.Timer(self._reset_title, 5).start()

    def _show_status_detail(self, _):
        if not self._status_log:
            rumps.alert(title="처리 현황", message="진행 중인 작업이 없습니다.")
        else:
            rumps.alert(title="처리 현황", message="\n".join(self._status_log))

    def _reset_title(self, _timer):
        if self._watcher.is_running:
            self.title = "MN 👁"
        else:
            self.title = "MN"
        _timer.stop()

    def _toggle_watch(self, sender):
        if self._watcher.is_running:
            self._watcher.stop()
            sender.title = "감시 시작"
            self.title = "MN"
            logger.info("사용자가 감시를 중지했습니다")
        else:
            self._config = load_config()
            self._watcher = FolderWatcher(
                config=self._config,
                status_callback=self._on_status,
                done_callback=self._on_done,
            )
            self._watcher.start()
            sender.title = "감시 중지"
            self.title = "MN 👁"
            logger.info("사용자가 감시를 시작했습니다")

    def _find_unprocessed_files(self) -> list:
        import re
        watch_dir = Path(self._config.get("watch_dir", "~/Desktop")).expanduser()
        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}")
        if not watch_dir.exists():
            return []
        return sorted(
            f for f in watch_dir.iterdir()
            if f.suffix.lower() in {".mp4", ".mov"} and date_pattern.match(f.name)
        )

    def _process_pending(self, _):
        self._config = load_config()
        files = self._find_unprocessed_files()
        if not files:
            rumps.alert(title="미처리 파일 없음", message="처리할 MP4 파일이 없습니다.")
            return
        for f in files:
            response = rumps.alert(
                title="파일 처리 확인",
                message=f"다음 파일을 처리하시겠습니까?\n\n{f.name}",
                ok="OK",
                cancel="Cancel",
            )
            if response == 1:
                threading.Thread(target=self._run_single_file, args=(str(f),), daemon=True).start()

    def _confirm_on_main(self, message: str) -> bool:
        """백그라운드 스레드에서 호출해도 메인 스레드에서 안전하게 dialog를 표시."""
        result = [False]
        done = threading.Event()

        def _ask(_timer):
            result[0] = rumps.alert(title="확인", message=message, ok="예", cancel="아니오") == 1
            done.set()
            _timer.stop()

        rumps.Timer(_ask, 0.0).start()
        done.wait()
        return result[0]

    def _run_single_file(self, path: str):
        filename = Path(path).name
        try:
            run_pipeline(
                path,
                self._config,
                status_callback=self._on_status,
                confirm_callback=self._confirm_on_main,
            )
            self._on_done(filename)
        except Exception as e:
            logger.error("수동 처리 실패: %s — %s", filename, e)
            err_msg = f"❌ 오류: {filename}"
            self._status_log.append(err_msg)
            self._pending_status_title = f"처리 현황: {err_msg}"
            rumps.notification(
                title="처리 실패",
                subtitle=filename,
                message=str(e),
            )

    def _open_watch_dir(self, _):
        watch_dir = Path(self._config.get("watch_dir", "~/Desktop")).expanduser()
        subprocess.Popen(["open", str(watch_dir)])

    def _open_config(self, _):
        subprocess.Popen(["open", str(CONFIG_PATH)])

    def _quit(self, _):
        if self._watcher.is_running:
            self._watcher.stop()
        rumps.quit_application()


def main():
    logger.info("AutoMeetingNote 앱 시작")
    app = AutoMeetingNoteApp()
    app.run()


if __name__ == "__main__":
    main()
