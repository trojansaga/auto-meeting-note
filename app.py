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
                "whisper_model": "small",
                "whisper_quant": "4bit",
                "language": "ko",
                "openai_model": "gpt-5.4",
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
            "whisper_model": "small",
            "whisper_quant": "4bit",
            "language": "ko",
            "openai_model": "gpt-5.4",
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
            tick_callback=self._on_tick,
        )

        self._status_log: deque = deque(maxlen=50)
        self._pending_status_title = None
        self._pending_app_title = None
        rumps.Timer(self._flush_ui, 0.3).start()

        self._toggle_item = rumps.MenuItem("감시 시작", callback=self._toggle_watch)
        self._process_pending_item = rumps.MenuItem("미처리 파일 즉시 처리", callback=self._process_pending)
        self._select_file_item = rumps.MenuItem("파일 선택하여 처리...", callback=self._select_and_process)
        self._status_item = rumps.MenuItem("처리 현황: 대기 중", callback=self._show_status_detail)
        self._open_folder_item = rumps.MenuItem("감시 폴더 열기", callback=self._open_watch_dir)
        self._open_config_item = rumps.MenuItem("설정 파일 열기", callback=self._open_config)
        self._open_prompt_item = rumps.MenuItem("STT 용어 사전 열기", callback=self._open_prompt)
        self._quit_item = rumps.MenuItem("종료", callback=self._quit)

        self._model_menu_items: dict = {}
        self._model_menu = self._build_model_menu()
        self._preprocess_menu = self._build_preprocess_menu()

        self._download_stop_event: threading.Event = threading.Event()
        self._download_stop_event.set()  # 초기값: 다운로드 없음

        self.menu = [
            self._toggle_item,
            self._process_pending_item,
            self._select_file_item,
            None,
            self._status_item,
            None,
            self._model_menu,
            self._preprocess_menu,
            None,
            self._open_folder_item,
            self._open_config_item,
            self._open_prompt_item,
            None,
            self._quit_item,
        ]

        self._check_dependencies()

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

        threading.Thread(target=self._validate_openai_model, daemon=True).start()

        model_name = self._config.get("whisper_model", "small")
        quant = self._config.get("whisper_quant", "4bit")
        self._check_and_download_model(model_name, quant)

    def _validate_openai_model(self):
        import openai
        model = self._config.get("openai_model", "gpt-5.4")
        try:
            client = openai.OpenAI()
            available = [m.id for m in client.models.list().data]
            if model not in available:
                logger.warning("OpenAI 모델 없음: %s", model)
                def _alert(_timer):
                    _timer.stop()
                    rumps.alert(
                        title="OpenAI 모델 오류",
                        message=f"설정된 모델 '{model}'을 찾을 수 없습니다.\n'설정 파일 열기'에서 openai_model 값을 확인해주세요.",
                    )
                rumps.Timer(_alert, 0.0).start()
            else:
                logger.info("OpenAI 모델 확인 완료: %s", model)
        except Exception as e:
            logger.warning("OpenAI 모델 검증 실패: %s", e)

    def _check_and_download_model(self, model_name: str, quant: str):
        from transcriber import MODEL_REPOS
        variants = MODEL_REPOS.get(model_name, {})
        repo = variants.get(quant if quant in variants else "base", "")
        if not repo:
            return
        cache_dir_name = "models--" + repo.replace("/", "--")
        hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
        model_cache = hf_home / "hub" / cache_dir_name
        if not model_cache.exists():
            resp = rumps.alert(
                title="모델 다운로드",
                message=f"Whisper 모델 {model_name} ({quant})을 다운로드하시겠습니까?\n(수백 MB ~ 수 GB, 시간이 걸릴 수 있습니다)",
                ok="다운로드",
                cancel="취소",
            )
            if resp != 1:
                return
            self._download_stop_event = threading.Event()
            self._show_cancel_item()
            threading.Thread(
                target=self._download_model,
                args=(repo, model_cache, hf_home, self._download_stop_event),
                daemon=True,
            ).start()

    def _show_cancel_item(self):
        """메인 스레드에서 호출"""
        self.menu.add(rumps.MenuItem("⏹ 다운로드 중단", callback=self._cancel_download))

    def _hide_cancel_item(self):
        """백그라운드 스레드에서 호출 가능"""
        def _remove(_timer):
            if "⏹ 다운로드 중단" in self.menu:
                del self.menu["⏹ 다운로드 중단"]
            _timer.stop()
        rumps.Timer(_remove, 0.0).start()

    def _cancel_download(self, _):
        self._download_stop_event.set()
        logger.info("사용자가 다운로드를 중단했습니다")

    def _build_model_menu(self) -> rumps.MenuItem:
        from transcriber import MODEL_REPOS
        current_model = self._config.get("whisper_model", "small")
        current_quant = self._config.get("whisper_quant", "4bit")

        model_menu = rumps.MenuItem(f"STT 모델: {current_model} ({current_quant})")
        for model_name in MODEL_REPOS:
            sub = rumps.MenuItem(model_name)
            for quant in ["4bit", "base", "8bit"]:
                item = rumps.MenuItem(quant, callback=self._make_model_callback(model_name, quant))
                item.state = 1 if (model_name == current_model and quant == current_quant) else 0
                self._model_menu_items[(model_name, quant)] = item
                sub.add(item)
            model_menu.add(sub)
        return model_menu

    def _build_preprocess_menu(self) -> rumps.MenuItem:
        menu = rumps.MenuItem("전처리 설정")
        steps = [
            ("노이즈 제거", "preprocess_noise_reduce"),
            ("침묵 구간 제거", "preprocess_vad"),
            ("음량 정규화", "preprocess_normalize"),
        ]
        for label, key in steps:
            item = rumps.MenuItem(label, callback=self._make_preprocess_callback(key))
            item.state = 1 if self._config.get(key, True) else 0
            menu.add(item)
        return menu

    def _make_preprocess_callback(self, key: str):
        def _toggle(sender):
            current = self._config.get(key, True)
            self._config[key] = not current
            sender.state = 1 if self._config[key] else 0
            CONFIG_PATH.write_text(yaml.dump(self._config, allow_unicode=True), encoding="utf-8")
            logger.info("전처리 설정 변경: %s = %s", key, self._config[key])
        return _toggle

    def _make_model_callback(self, model_name: str, quant: str):
        def _select(_sender):
            for item in self._model_menu_items.values():
                item.state = 0
            self._model_menu_items[(model_name, quant)].state = 1
            self._model_menu.title = f"STT 모델: {model_name} ({quant})"
            self._config["whisper_model"] = model_name
            self._config["whisper_quant"] = quant
            CONFIG_PATH.write_text(yaml.dump(self._config, allow_unicode=True), encoding="utf-8")
            logger.info("STT 모델 변경: %s (%s)", model_name, quant)
            self._check_and_download_model(model_name, quant)
        return _select

    def _download_model(self, repo: str, model_cache: Path, hf_home: Path, stop_event: threading.Event):
        import time
        import huggingface_hub

        self._on_status(f"모델 파일 목록 조회 중: {repo}")

        try:
            files = list(huggingface_hub.list_repo_files(repo))
            total = len(files)
            logger.info("다운로드 대상: %s (%d 파일)", repo, total)
        except Exception as e:
            logger.error("파일 목록 조회 실패: %s", e)
            self._on_status(f"❌ 모델 다운로드 실패: 파일 목록 조회 불가")
            self._hide_cancel_item()
            return

        start_time = time.time()
        try:
            for i, filename in enumerate(files):
                if stop_event.is_set():
                    self._on_status(f"⏹ 다운로드 중단됨: {repo}")
                    logger.info("다운로드 중단: %s", repo)
                    return

                elapsed = int(time.time() - start_time)
                mins, secs = divmod(elapsed, 60)
                time_str = f"{mins}분 {secs}초" if mins else f"{secs}초"
                pct = i / total * 100
                self._on_status(f"모델 다운로드 중... {pct:.0f}% ({i}/{total}개) [{time_str}]")

                huggingface_hub.hf_hub_download(repo_id=repo, filename=filename)

            logger.info("모델 다운로드 완료: %s", repo)
            self._on_status(f"✅ 모델 다운로드 완료: {repo}")
            try:
                rumps.notification(
                    title="모델 다운로드 완료",
                    subtitle=repo,
                    message="Whisper 모델 준비가 완료되었습니다.",
                )
            except Exception:
                pass
        except Exception as e:
            logger.error("모델 다운로드 실패: %s — %s", repo, e)
            self._on_status(f"❌ 모델 다운로드 실패: {repo}")
            try:
                rumps.notification(
                    title="모델 다운로드 실패",
                    subtitle=repo,
                    message=str(e),
                )
            except Exception:
                pass
        finally:
            self._hide_cancel_item()

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
        try:
            rumps.notification(
                title="회의록 자동 생성 완료",
                subtitle=filename,
                message="회의록이 성공적으로 생성되었습니다.",
            )
        except Exception:
            pass
        rumps.Timer(self._reset_title, 5).start()

    def _show_status_detail(self, _):
        if not self._status_log:
            rumps.alert(title="처리 현황", message="진행 중인 작업이 없습니다.")
        else:
            rumps.alert(title="처리 현황", message="\n".join(self._status_log))

    def _on_tick(self, remaining: int):
        self._pending_app_title = f"MN 👁 {remaining}s"

    def _reset_title(self, _timer):
        if self._watcher.is_running:
            pass  # 카운트다운이 계속 업데이트하므로 덮어쓰지 않음
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
                tick_callback=self._on_tick,
            )
            self._watcher.start()
            sender.title = "감시 중지"
            self.title = "MN 👁 60s"
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
        # 모든 확인을 먼저 받은 뒤 스레드 시작 (메인 스레드 점유 충돌 방지)
        to_process = []
        for f in files:
            response = rumps.alert(
                title="파일 처리 확인",
                message=f"다음 파일을 처리하시겠습니까?\n\n{f.name}",
                ok="OK",
                cancel="Cancel",
            )
            if response == 1:
                to_process.append(str(f))
        if to_process:
            threading.Thread(target=self._run_files_sequentially, args=(to_process,), daemon=True).start()

    def _select_and_process(self, _):
        from AppKit import NSOpenPanel, NSModalResponseOK
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(True)
        panel.setAllowedFileTypes_(["mp4", "mov", "MP4", "MOV"])
        panel.setTitle_("처리할 파일 선택")
        panel.setPrompt_("선택")

        if panel.runModal() != NSModalResponseOK:
            return

        paths = [str(url.path()) for url in panel.URLs()]
        if not paths:
            return

        threading.Thread(target=self._run_files_sequentially, args=(paths,), daemon=True).start()

    def _confirm_on_main(self, message: str) -> bool:
        """백그라운드 스레드에서 호출해도 메인 스레드에서 안전하게 dialog를 표시."""
        result = [False]
        done = threading.Event()

        def _ask(_timer):
            _timer.stop()  # 재진입 방지: alert 실행 전에 타이머 중단
            try:
                result[0] = rumps.alert(title="확인", message=message, ok="예", cancel="아니오") == 1
            except Exception as e:
                logger.error("confirm dialog 오류: %s", e)
            finally:
                done.set()

        rumps.Timer(_ask, 0.0).start()
        done.wait(timeout=60)  # 60초 타임아웃 (무한 블록 방지)
        return result[0]

    def _run_files_sequentially(self, paths: list):
        for path in paths:
            self._run_single_file(path)

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

    def _open_prompt(self, _):
        prompt_path = _resource_path() / "dictionary.txt"
        if not prompt_path.exists():
            rumps.alert(title="STT 용어 사전", message="dictionary.txt 파일이 없습니다.")
            return
        subprocess.Popen(["open", str(prompt_path)])

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
