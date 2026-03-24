import logging
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Optional

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

try:
    import setproctitle
    setproctitle.setproctitle("AutoMeetingNote")
except ImportError:
    pass

import rumps
import yaml
from dotenv import load_dotenv

from hotkey_manager import HotkeyManager, format_hotkey, DEFAULT_HOTKEYS, HOTKEY_LABELS
from pipeline import run_pipeline
from recorder import Recorder

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "AutoMeetingNote"
APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)

LOG_DIR = Path.home() / "Library" / "Logs" / "AutoMeetingNote"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "app.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8"),
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

        self._recorder = Recorder()
        self._rec_timer: Optional[rumps.Timer] = None
        self._is_recording = False  # screencapture 프로세스 종료 여부와 무관한 앱 레벨 상태
        self._recording_mode = None  # 'screen' or 'audio'

        self._status_log: deque = deque(maxlen=50)
        self._pending_status_title = None
        self._pending_app_title = None
        rumps.Timer(self._flush_ui, 0.3).start()

        self._select_file_item = rumps.MenuItem("파일 선택하여 처리...", callback=self._select_and_process)
        self._screen_rec_item = rumps.MenuItem("화면 녹화 시작", callback=self._toggle_screen_rec)
        self._audio_rec_item = rumps.MenuItem("녹음 시작", callback=self._toggle_audio_rec)
        self._pause_item = rumps.MenuItem("일시 정지", callback=None)
        self._mic_item = rumps.MenuItem("마이크 녹음 포함", callback=self._toggle_mic)
        self._mic_item.state = 1 if self._config.get("mic_enabled", False) else 0
        self._stt_skip_item = rumps.MenuItem("녹화/녹음만 (STT 건너뛰기)", callback=self._toggle_stt_skip)
        self._stt_skip_item.state = 1 if self._config.get("stt_skip", False) else 0
        self._flags_menu = rumps.MenuItem("녹화/녹음 옵션")
        self._flags_menu.add(self._mic_item)
        self._flags_menu.add(self._stt_skip_item)
        self._status_item = rumps.MenuItem("처리 현황: 대기 중", callback=self._show_status_detail)
        self._open_config_item = rumps.MenuItem("설정 파일 열기", callback=self._open_config)
        self._open_prompt_item = rumps.MenuItem("STT 용어 사전 열기", callback=self._open_prompt)
        self._open_log_item = rumps.MenuItem("로그 파일 열기", callback=self._open_log)
        self._quit_item = rumps.MenuItem("종료", callback=self._quit)

        self._model_menu_items: dict = {}
        self._model_menu = self._build_model_menu()
        self._preprocess_menu = self._build_preprocess_menu()
        self._hotkey_menu = self._build_hotkey_menu()

        self._download_stop_event: threading.Event = threading.Event()
        self._download_stop_event.set()  # 초기값: 다운로드 없음

        self.menu = [
            self._select_file_item,
            self._screen_rec_item,
            self._audio_rec_item,
            self._pause_item,
            self._flags_menu,
            None,
            self._status_item,
            None,
            self._model_menu,
            self._preprocess_menu,
            self._hotkey_menu,
            None,
            self._open_config_item,
            self._open_prompt_item,
            self._open_log_item,
            None,
            self._quit_item,
        ]

        self._check_dependencies()
        self._setup_hotkeys()

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

    def _toggle_mic(self, sender):
        sender.state = not sender.state
        self._config["mic_enabled"] = bool(sender.state)
        CONFIG_PATH.write_text(yaml.dump(self._config, allow_unicode=True), encoding="utf-8")

    def _toggle_stt_skip(self, sender):
        sender.state = not sender.state
        self._config["stt_skip"] = bool(sender.state)
        CONFIG_PATH.write_text(yaml.dump(self._config, allow_unicode=True), encoding="utf-8")

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

    def _build_hotkey_menu(self) -> rumps.MenuItem:
        hotkeys_cfg = self._config.get("hotkeys", DEFAULT_HOTKEYS)
        menu = rumps.MenuItem("단축키 설정")

        self._hotkey_items: dict = {}
        for action, label in HOTKEY_LABELS.items():
            hk = hotkeys_cfg.get(action, DEFAULT_HOTKEYS.get(action, {}))
            mod, key = hk.get("mod", 0), hk.get("key", 0)
            display = format_hotkey(mod, key)
            item = rumps.MenuItem(
                f"{label}: {display}",
                callback=self._make_hotkey_setter(action, label),
            )
            self._hotkey_items[action] = item
            menu.add(item)

        menu.add(None)
        menu.add(rumps.MenuItem("단축키 초기화", callback=self._reset_hotkeys))
        return menu

    def _setup_hotkeys(self):
        self._hotkey_manager = HotkeyManager()
        hotkeys_cfg = self._config.get("hotkeys", DEFAULT_HOTKEYS)

        callbacks = {
            "screen_record": self._hotkey_screen_rec,
            "audio_record": self._hotkey_audio_rec,
            "pause_resume": self._hotkey_pause_resume,
        }
        for action, cb in callbacks.items():
            hk = hotkeys_cfg.get(action, DEFAULT_HOTKEYS.get(action, {}))
            self._hotkey_manager.register(action, hk["mod"], hk["key"], cb)

        self._hotkey_manager.start()

    def _hotkey_screen_rec(self):
        if self._is_recording and self._recording_mode != 'screen':
            return
        self._toggle_screen_rec(self._screen_rec_item)

    def _hotkey_audio_rec(self):
        if self._is_recording and self._recording_mode != 'audio':
            return
        self._toggle_audio_rec(self._audio_rec_item)

    def _hotkey_pause_resume(self):
        if not self._is_recording:
            return
        self._toggle_pause(self._pause_item)

    def _make_hotkey_setter(self, action: str, label: str):
        def _on_click(_sender):
            rumps.alert(
                title="단축키 변경",
                message=f"'{label}' 단축키를 변경합니다.\n\n"
                        "확인을 누른 후 새 단축키 조합을 눌러주세요.\n"
                        "(수정자 키 + 일반 키, Esc로 취소)",
            )

            self._pending_app_title = "MN ⌨️"

            def _on_recorded(mod, keycode):
                display = format_hotkey(mod, keycode)

                if "hotkeys" not in self._config:
                    from copy import deepcopy
                    self._config["hotkeys"] = deepcopy(DEFAULT_HOTKEYS)
                self._config["hotkeys"][action] = {"mod": mod, "key": keycode}
                CONFIG_PATH.write_text(
                    yaml.dump(self._config, allow_unicode=True), encoding="utf-8"
                )

                self._hotkey_manager.update_binding(action, mod, keycode)
                self._hotkey_items[action].title = f"{label}: {display}"
                self._pending_app_title = "MN"

                logger.info("단축키 변경: %s → %s", action, display)
                try:
                    rumps.notification("AutoMeetingNote", "단축키 변경", f"{label}: {display}")
                except Exception:
                    pass

            def _on_cancel():
                self._pending_app_title = "MN"

            self._hotkey_manager.start_recording(_on_recorded, _on_cancel)

            def _timeout(timer):
                timer.stop()
                if self._hotkey_manager.is_recording:
                    self._hotkey_manager.cancel_recording()

            rumps.Timer(_timeout, 10).start()

        return _on_click

    def _reset_hotkeys(self, _sender):
        from copy import deepcopy
        self._config["hotkeys"] = deepcopy(DEFAULT_HOTKEYS)
        CONFIG_PATH.write_text(
            yaml.dump(self._config, allow_unicode=True), encoding="utf-8"
        )

        for action, hk in DEFAULT_HOTKEYS.items():
            self._hotkey_manager.update_binding(action, hk["mod"], hk["key"])
            label = HOTKEY_LABELS[action]
            self._hotkey_items[action].title = f"{label}: {format_hotkey(hk['mod'], hk['key'])}"

        logger.info("단축키 초기화")
        rumps.alert(title="단축키 초기화", message="모든 단축키가 기본값으로 초기화되었습니다.")

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
        if not self._is_recording:
            self._pending_app_title = "MN ⏳"
        self._pending_status_title = f"처리 현황: {message}"

    def _on_done(self, filename: str):
        done_msg = f"✅ 완료: {filename}"
        self._status_log.append(done_msg)
        if not self._is_recording:
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

    def _reset_title(self, _timer):
        self.title = "MN"
        _timer.stop()

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

    @staticmethod
    def _check_screen_permission() -> bool:
        try:
            import Quartz
            return bool(Quartz.CGPreflightScreenCaptureAccess())
        except Exception:
            return True  # 확인 불가 시 허용으로 간주

    @staticmethod
    def _check_mic_permission() -> bool:
        try:
            import AVFoundation
            status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
                AVFoundation.AVMediaTypeAudio
            )
            return status == 3  # AVAuthorizationStatusAuthorized
        except Exception:
            return True

    def _toggle_screen_rec(self, sender):
        if self._is_recording:
            # 녹화 중지
            self._is_recording = False
            self._recording_mode = None
            sender.title = "화면 녹화 시작"
            self._audio_rec_item.set_callback(self._toggle_audio_rec)
            self._pause_item.title = "일시 정지"
            self._pause_item.set_callback(None)
            self._stop_rec_timer()
            mode, output_path, audio_path, mic_path, audio_offset = self._recorder.stop()
            threading.Thread(
                target=self._on_recording_stopped,
                args=(mode, output_path, audio_path, mic_path, audio_offset),
                daemon=True,
            ).start()
        else:
            # 권한 확인
            if not self._check_screen_permission():
                rumps.alert(
                    title="화면 녹화 권한 필요",
                    message="화면 녹화 권한이 없습니다.\n\n"
                            "시스템 설정 → 개인 정보 보호 및 보안 → 화면 및 시스템 오디오 녹음\n"
                            "에서 AutoMeetingNote를 허용한 후 앱을 재시작하세요.",
                )
                return
            # UI 먼저 업데이트 후 백그라운드에서 SCStream + screencapture 시작
            # (SCShareableContent 콜백이 메인 스레드 필요 → 메인 스레드 블록 시 데드락)
            self._is_recording = True
            self._recording_mode = 'screen'
            sender.title = "녹화 중지"
            self._audio_rec_item.set_callback(None)
            self._pause_item.set_callback(self._toggle_pause)
            self._start_rec_timer()

            watch_dir = Path(self._config.get("watch_dir", "~/Desktop")).expanduser()
            watch_dir.mkdir(parents=True, exist_ok=True)

            def _start_bg():
                mic_enabled = bool(self._config.get("mic_enabled", True))
                mic_index = str(self._config.get("mic_device_index", "0"))
                try:
                    self._recorder.start_screen_recording(watch_dir, mic_enabled=mic_enabled, mic_device_index=mic_index)
                except Exception as e:
                    logger.error("화면 녹화 시작 실패: %s", e)
                    self._is_recording = False
                    self._stop_rec_timer()
                    def _revert(t):
                        t.stop()
                        sender.title = "화면 녹화 시작"
                        self._audio_rec_item.set_callback(self._toggle_audio_rec)
                        self._pause_item.set_callback(None)
                    rumps.Timer(_revert, 0).start()
                    rumps.notification("AutoMeetingNote", "화면 녹화 오류", str(e))

            threading.Thread(target=_start_bg, daemon=True).start()

    def _toggle_audio_rec(self, sender):
        if self._is_recording:
            # 녹음 중지
            self._is_recording = False
            self._recording_mode = None
            sender.title = "녹음 시작"
            self._screen_rec_item.set_callback(self._toggle_screen_rec)
            self._pause_item.title = "일시 정지"
            self._pause_item.set_callback(None)
            self._stop_rec_timer()
            mode, output_path, audio_path, mic_path, audio_offset = self._recorder.stop()
            threading.Thread(
                target=self._on_recording_stopped,
                args=(mode, output_path, audio_path, mic_path, audio_offset),
                daemon=True,
            ).start()
        else:
            # 권한 확인 (시스템 오디오는 화면 녹화 권한 필요)
            if not self._check_screen_permission():
                rumps.alert(
                    title="화면 녹화 권한 필요",
                    message="시스템 오디오 녹음에는 화면 녹화 권한이 필요합니다.\n\n"
                            "시스템 설정 → 개인 정보 보호 및 보안 → 화면 및 시스템 오디오 녹음\n"
                            "에서 AutoMeetingNote를 허용한 후 앱을 재시작하세요.",
                )
                return
            # UI 먼저 업데이트 후 백그라운드에서 SCStream 시작
            # (SCShareableContent 콜백이 메인 스레드 필요 → 메인 스레드 블록 시 데드락)
            self._is_recording = True
            self._recording_mode = 'audio'
            sender.title = "녹음 중지"
            self._screen_rec_item.set_callback(None)
            self._pause_item.set_callback(self._toggle_pause)
            self._start_rec_timer()

            watch_dir = Path(self._config.get("watch_dir", "~/Desktop")).expanduser()
            watch_dir.mkdir(parents=True, exist_ok=True)

            def _start_bg():
                mic_enabled = bool(self._config.get("mic_enabled", True))
                mic_index = str(self._config.get("mic_device_index", "0"))
                try:
                    self._recorder.start_audio_recording(watch_dir, mic_enabled=mic_enabled, mic_device_index=mic_index)
                except Exception as e:
                    logger.error("녹음 시작 실패: %s", e)
                    self._is_recording = False
                    self._stop_rec_timer()
                    def _revert(t):
                        t.stop()
                        sender.title = "녹음 시작"
                        self._screen_rec_item.set_callback(self._toggle_screen_rec)
                        self._pause_item.set_callback(None)
                        rumps.alert(title="녹음 오류", message=str(e))
                    rumps.Timer(_revert, 0.0).start()

            threading.Thread(target=_start_bg, daemon=True).start()

    def _toggle_pause(self, sender):
        if self._recorder.is_paused:
            # 재개: 백그라운드에서 SCStream 초기화 (블로킹)
            sender.title = "일시 정지"
            threading.Thread(target=self._do_resume, daemon=True).start()
        else:
            # 일시 정지
            sender.title = "녹화 재개"
            self._recorder.pause()

    def _do_resume(self):
        try:
            self._recorder.resume()
        except Exception as e:
            logger.error("재개 실패: %s", e)
            def _revert(t):
                t.stop()
                self._pause_item.title = "녹화 재개"
            rumps.Timer(_revert, 0).start()

    def _start_rec_timer(self):
        self._rec_timer = rumps.Timer(self._update_rec_display, 1)
        self._rec_timer.start()

    def _update_rec_display(self, _timer):
        elapsed = int(self._recorder.elapsed_seconds)
        mins, secs = divmod(elapsed, 60)
        if self._recorder.is_paused:
            self._pending_app_title = f"⏸ REC {mins:02d}:{secs:02d}"
        else:
            self._pending_app_title = f"● REC {mins:02d}:{secs:02d}"

    def _stop_rec_timer(self):
        if self._rec_timer:
            self._rec_timer.stop()
            self._rec_timer = None
        self._pending_app_title = "MN"

    def _on_recording_stopped(self, mode: str, output_path, audio_path=None, mic_path=None, audio_offset=0.0):
        """녹화/녹음 종료 후 후처리 (백그라운드 스레드에서 실행)."""
        if output_path is None:
            return
        stt_skip = self._config.get("stt_skip", False)
        try:
            if mode == "screen":
                self._on_status("녹화 파일 압축 중...")
                mp4_path = self._recorder.compress_and_merge(
                    output_path, audio_path,
                    mic_path=mic_path,
                    audio_offset=audio_offset,
                    progress_callback=self._on_status,
                )
                if stt_skip:
                    self._on_status(f"✅ 녹화 완료 (STT 건너뜀): {mp4_path.name}")
                    return
                if not self._confirm_on_main(f"녹화 파일이 준비되었습니다.\n({mp4_path.name})\n\n회의록 생성을 시작할까요?"):
                    self._on_status(f"회의록 생성 취소됨: {mp4_path.name}")
                    return
                self._run_single_file(str(mp4_path))
            elif mode == "audio":
                # 시스템 오디오 + 마이크 믹싱
                if mic_path:
                    self._on_status("오디오 믹싱 중...")
                    final_path = self._recorder.mix_wav(output_path, mic_path)
                else:
                    final_path = output_path
                if stt_skip:
                    self._on_status(f"✅ 녹음 완료 (STT 건너뜀): {final_path.name}")
                    return
                if not self._confirm_on_main(f"녹음 파일이 준비되었습니다.\n({final_path.name})\n\n회의록 생성을 시작할까요?"):
                    self._on_status(f"회의록 생성 취소됨: {final_path.name}")
                    return
                self._run_single_file(str(final_path))
        except Exception as e:
            logger.error("녹화 후처리 실패: %s", e)
            err_msg = f"❌ 녹화 후처리 오류: {e}"
            self._status_log.append(err_msg)
            self._pending_status_title = f"처리 현황: {err_msg}"

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

    def _open_config(self, _):
        subprocess.Popen(["open", str(CONFIG_PATH)])

    def _open_prompt(self, _):
        prompt_path = _resource_path() / "dictionary.txt"
        if not prompt_path.exists():
            rumps.alert(title="STT 용어 사전", message="dictionary.txt 파일이 없습니다.")
            return
        subprocess.Popen(["open", str(prompt_path)])

    def _open_log(self, _):
        subprocess.Popen(["open", str(LOG_FILE)])

    def _quit(self, _):
        if self._is_recording:
            try:
                self._recorder.stop()
            except Exception:
                pass
        self._hotkey_manager.stop()
        rumps.quit_application()


def main():
    logger.info("=" * 60)
    logger.info("AutoMeetingNote 앱 시작")

    # exec으로 프로세스 교체 시 .app 번들 연결이 끊겨 LSUIElement가 적용 안 됨
    # rumps.App 생성 전에 먼저 Accessory 정책 설정 → 독 아이콘 숨김
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass

    app = AutoMeetingNoteApp()
    app.run()


if __name__ == "__main__":
    main()
