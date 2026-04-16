import logging
import os
import plistlib
import json
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
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
# venv 경로: Resources/.venv_path 파일 우선, 없으면 5단계 상위(dist 빌드 구조) 시도
_APP_FILE = Path(__file__).resolve()
_PY_VER = f"python{sys.version_info.major}.{sys.version_info.minor}"

_venv_root = None
_venv_path_file = _APP_FILE.parent / ".venv_path"
if _venv_path_file.exists():
    _venv_root = Path(_venv_path_file.read_text(encoding="utf-8").strip())
if not _venv_root or not _venv_root.exists():
    _venv_root = _APP_FILE.parent.parent.parent.parent.parent / ".venv"  # dist/../.venv

_VENV_SITE = _venv_root / "lib" / _PY_VER / "site-packages"
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
from pipeline import run_pipeline, PipelineCancelledError
from recorder import Recorder
from transcriber import (
    APPLE_SPEECH_MODELS,
    DEFAULT_APPLE_SPEECH_MODEL,
    DEFAULT_QWEN_MODEL,
    DEFAULT_STT_BACKEND,
    DEFAULT_WHISPER_MODEL,
    DEFAULT_WHISPER_QUANT,
    QWEN_MODEL_REPOS,
    WHISPER_MODEL_REPOS,
    get_apple_speech_dependency_error,
    get_backend_label,
    get_model_display_name,
    get_model_download_repo,
)

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
    force=True,
)
logger = logging.getLogger(__name__)

APP_BUNDLE_IDENTIFIER = "com.automeetingnote.app"
APP_DISPLAY_NAME = "AutoMeetingNote"
VERSION_FILE = Path(__file__).resolve().parent / "VERSION"
APP_VERSION = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "1.1.11"

MIC_DEVICE_CHOICES = {
    "macbook": "맥북/현재 디바이스",
    "iphone": "아이폰 마이크",
}
MIC_DEVICE_ALIASES = {
    "": "macbook",
    "0": "macbook",
    "auto": "macbook",
    "default": "macbook",
    "builtin": "macbook",
    "macbook": "macbook",
    "current": "macbook",
    "local": "macbook",
    "iphone": "iphone",
    "ipad": "iphone",
    "ios": "iphone",
    "continuity": "iphone",
}

DEFAULT_CONFIG = {
    "watch_dir": "~/Desktop",
    "stt_backend": DEFAULT_STT_BACKEND,
    "whisper_model": DEFAULT_WHISPER_MODEL,
    "whisper_quant": DEFAULT_WHISPER_QUANT,
    "whisper_batch_size": 4,
    "qwen_model": DEFAULT_QWEN_MODEL,
    "apple_speech_model": DEFAULT_APPLE_SPEECH_MODEL,
    "qwen_dtype": None,
    "qwen_device_map": None,
    "qwen_attn_implementation": None,
    "qwen_forced_aligner": None,
    "qwen_return_timestamps": False,
    "qwen_max_new_tokens": 4096,
    "qwen_max_batch_size": 1,
    "qwen_chunk_seconds": 600,
    "language": "ko",
    "openai_model": "gpt-5.4",
    "export_dir": "~/Downloads",
    "mic_enabled": False,
    "mic_device_index": "macbook",
    "stt_skip": False,
}


def _ensure_notification_runtime_plist():
    """rumps가 찾는 python 실행 디렉터리의 Info.plist를 보정한다."""
    plist_path = Path(sys.executable).parent / "Info.plist"
    desired = {
        "CFBundleIdentifier": APP_BUNDLE_IDENTIFIER,
        "CFBundleName": APP_DISPLAY_NAME,
        "CFBundleDisplayName": APP_DISPLAY_NAME,
    }

    try:
        current = {}
        if plist_path.exists():
            with plist_path.open("rb") as f:
                loaded = plistlib.load(f)
                if isinstance(loaded, dict):
                    current = loaded

        changed = False
        for key, value in desired.items():
            if current.get(key) != value:
                current[key] = value
                changed = True

        if changed:
            with plist_path.open("wb") as f:
                plistlib.dump(current, f, sort_keys=True)
            logger.info("알림 런타임 plist 준비: %s", plist_path)
    except Exception as e:
        logger.warning("알림 런타임 plist 준비 실패: %s", e)


_ensure_notification_runtime_plist()


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
            user_config.write_text(yaml.dump(DEFAULT_CONFIG, allow_unicode=True), encoding="utf-8")
            logger.info("기본 config.yaml 생성: %s", user_config)
    return user_config


CONFIG_PATH = _ensure_user_config()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.warning("config.yaml 없음, 기본값 사용")
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        loaded = yaml.safe_load(f) or {}
    config = dict(DEFAULT_CONFIG)
    config.update(loaded)
    return config


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
        self._reset_title_at: Optional[float] = None
        rumps.Timer(self._flush_ui, 0.3).start()

        self._select_file_item = rumps.MenuItem("파일 선택하여 처리...", callback=self._select_and_process)
        self._screen_rec_item = rumps.MenuItem("화면 녹화 시작", callback=self._toggle_screen_rec)
        self._audio_rec_item = rumps.MenuItem("녹음 시작", callback=self._toggle_audio_rec)
        self._pause_item = rumps.MenuItem("일시 정지", callback=None)
        self._mic_item = rumps.MenuItem("마이크 녹음 포함", callback=self._toggle_mic)
        self._mic_item.state = 1 if self._config.get("mic_enabled", False) else 0
        self._mic_device_items: dict = {}
        self._mic_device_menu = self._build_mic_device_menu()
        self._stt_skip_item = rumps.MenuItem("녹화/녹음만 (STT 건너뛰기)", callback=self._toggle_stt_skip)
        self._stt_skip_item.state = 1 if self._config.get("stt_skip", False) else 0
        self._flags_menu = rumps.MenuItem("녹화/녹음 옵션")
        self._flags_menu.add(self._mic_item)
        self._flags_menu.add(self._mic_device_menu)
        self._flags_menu.add(self._stt_skip_item)
        self._status_item = rumps.MenuItem("처리 현황: 대기 중", callback=self._show_status_detail)
        self._open_config_item = rumps.MenuItem("설정 파일 열기", callback=self._open_config)
        self._open_prompt_item = rumps.MenuItem("STT 용어 사전 열기", callback=self._open_prompt)
        self._open_log_item = rumps.MenuItem("로그 파일 열기", callback=self._open_log)
        self._release_notes_item = rumps.MenuItem(f"릴리즈 노트 (v{APP_VERSION})", callback=self._open_release_notes)
        self._quit_item = rumps.MenuItem("종료", callback=self._quit)

        self._model_menu_items: dict = {}
        self._model_menu = self._build_model_menu()
        self._preprocess_menu = self._build_preprocess_menu()
        self._hotkey_menu = self._build_hotkey_menu()

        self._download_stop_event: threading.Event = threading.Event()
        self._download_stop_event.set()  # 초기값: 다운로드 없음

        self._pipeline_stop_event: threading.Event = threading.Event()
        self._pipeline_pause_event: threading.Event = threading.Event()
        self._pipeline_pause_event.set()  # set = 실행 중, clear = 일시중단
        self._pipeline_start_time: Optional[float] = None
        self._pipeline_step: tuple = (0, 0)
        self._pipeline_base_msg: str = ""
        self._pipeline_running: bool = False

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
            self._release_notes_item,
            None,
            self._quit_item,
        ]

        self._check_dependencies()
        self._setup_hotkeys()

    def _check_dependencies(self):
        import shutil
        errors = []

        if not shutil.which("ffmpeg"):
            errors.append("ffmpeg가 없습니다.\n터미널에서 'brew install ffmpeg' 실행 후 앱을 재시작하세요.")

        if not os.environ.get("OPENAI_API_KEY"):
            errors.append("OPENAI_API_KEY가 설정되지 않았습니다.\n설정 파일 열기 메뉴에서 .env 파일을 확인하세요.")

        backend = self._config.get("stt_backend", DEFAULT_STT_BACKEND)
        stt_dependency_error = self._get_stt_dependency_error(backend)
        if stt_dependency_error:
            errors.append(stt_dependency_error)

        if errors:
            rumps.alert(title="설정 오류", message="\n\n".join(errors))
            return

        threading.Thread(target=self._validate_openai_model, daemon=True).start()

        backend, model_name, quant = self._get_current_stt_selection()
        self._check_and_download_model(backend, model_name, quant)

    def _apple_speech_probe_path(self) -> Optional[Path]:
        candidates = [
            _resource_path().parent / "MacOS" / "AutoMeetingNoteSpeechProbe",
            Path(__file__).resolve().parent.parent / "MacOS" / "AutoMeetingNoteSpeechProbe",
            Path(__file__).resolve().parent / "dist" / "AutoMeetingNote.app" / "Contents" / "MacOS" / "AutoMeetingNoteSpeechProbe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def _run_apple_speech_probe(self, request_auth: bool = False) -> dict:
        probe = self._apple_speech_probe_path()
        if probe is None:
            raise RuntimeError("Apple Speech 권한 확인 helper를 찾을 수 없습니다. 앱을 다시 빌드하세요.")

        cmd = [str(probe)]
        if request_auth:
            cmd.append("--request-auth")

        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
        )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Apple Speech 권한 상태 파싱 실패:\n{completed.stdout}") from exc

    def _ensure_apple_speech_authorization(self, request_if_needed: bool = True) -> tuple[bool, str]:
        try:
            payload = self._run_apple_speech_probe(request_auth=False)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip() or str(exc)
            return False, f"Apple Speech 권한 상태 확인 실패:\n{detail}"
        except Exception as exc:
            return False, str(exc)

        recognizer_payload = payload.get("sfspeechRecognizer", {})
        status = recognizer_payload.get("authorizationStatus", "unknown")

        if status == "authorized":
            return True, ""

        if request_if_needed and status == "notDetermined":
            try:
                payload = self._run_apple_speech_probe(request_auth=True)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip() or str(exc)
                return False, f"Apple Speech 권한 요청 실패:\n{detail}"
            except Exception as exc:
                return False, str(exc)
            recognizer_payload = payload.get("sfspeechRecognizer", {})
            status = recognizer_payload.get("authorizationStatus", "unknown")
            if status == "authorized":
                return True, ""

        if status == "denied":
            return False, (
                "음성 인식 권한이 거부되었습니다.\n\n"
                "시스템 설정 → 개인 정보 보호 및 보안 → 음성 인식에서 "
                "AutoMeetingNote를 허용한 뒤 다시 시도하세요."
            )
        if status == "restricted":
            return False, "이 Mac에서는 음성 인식 권한이 제한되어 Apple Speech를 사용할 수 없습니다."
        if status == "notDetermined":
            return False, "음성 인식 권한 요청이 완료되지 않았습니다. 다시 시도하세요."
        return False, f"Apple Speech 권한 상태를 확인할 수 없습니다: {status}"

    def _get_stt_dependency_error(self, backend: str) -> Optional[str]:
        if backend == "apple_speech":
            return get_apple_speech_dependency_error()
        if backend == "qwen3_asr":
            try:
                import qwen_asr  # noqa: F401
                return None
            except ImportError:
                return (
                    "qwen-asr 패키지가 없습니다.\n"
                    "터미널에서 'pip install qwen-asr' 실행 후 앱을 재시작하세요."
                )

        try:
            import mlx_whisper  # noqa: F401
            return None
        except ImportError:
            return (
                "mlx_whisper 패키지가 없습니다.\n"
                "터미널에서 'pip install mlx-whisper' 실행 후 앱을 재시작하세요."
            )

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

    def _save_config(self):
        CONFIG_PATH.write_text(yaml.dump(self._config, allow_unicode=True), encoding="utf-8")

    def _get_current_stt_selection(self) -> tuple[str, str, Optional[str]]:
        backend = self._config.get("stt_backend", DEFAULT_STT_BACKEND)
        if backend == "apple_speech":
            return backend, self._config.get("apple_speech_model", DEFAULT_APPLE_SPEECH_MODEL), None
        if backend == "qwen3_asr":
            return backend, self._config.get("qwen_model", DEFAULT_QWEN_MODEL), None
        return (
            "whisper",
            self._config.get("whisper_model", DEFAULT_WHISPER_MODEL),
            self._config.get("whisper_quant", DEFAULT_WHISPER_QUANT),
        )

    def _get_model_menu_title(self) -> str:
        backend, model_name, quant = self._get_current_stt_selection()
        backend_label = get_backend_label(backend)
        model_label = get_model_display_name(backend, model_name, quant)
        return f"STT 모델: {backend_label} / {model_label}"

    def _check_and_download_model(self, backend: str, model_name: str, quant: Optional[str]):
        repo = get_model_download_repo(backend, model_name, quant)
        if not repo:
            return
        cache_dir_name = "models--" + repo.replace("/", "--")
        hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
        model_cache = hf_home / "hub" / cache_dir_name
        if not model_cache.exists():
            backend_label = get_backend_label(backend)
            model_label = get_model_display_name(backend, model_name, quant)
            resp = rumps.alert(
                title="모델 다운로드",
                message=f"{backend_label} 모델 {model_label}을 다운로드하시겠습니까?\n(수백 MB ~ 수 GB, 시간이 걸릴 수 있습니다)",
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

    def _notify(self, title: str, subtitle: str = "", message: str = ""):
        """가능하면 AutoMeetingNote 이름으로 네이티브 알림을 보낸다."""
        title = (title or APP_DISPLAY_NAME).strip() or APP_DISPLAY_NAME
        subtitle = (subtitle or "").strip()
        message = (message or "").strip() or title

        try:
            rumps.notification(title=title, subtitle=subtitle, message=message)
            return
        except Exception as e:
            logger.warning("rumps 알림 실패, AppKit 알림으로 재시도: %s", e)

        try:
            from AppKit import NSUserNotification, NSUserNotificationCenter

            notification = NSUserNotification.alloc().init()
            notification.setTitle_(title)
            if subtitle:
                notification.setSubtitle_(subtitle)
            notification.setInformativeText_(message)
            NSUserNotificationCenter.defaultUserNotificationCenter().deliverNotification_(notification)
            return
        except Exception as e:
            logger.warning("AppKit 알림 실패, osascript 사용: %s", e)

        script = """
        on run argv
            set notificationTitle to item 1 of argv
            set notificationSubtitle to item 2 of argv
            set notificationMessage to item 3 of argv
            if notificationSubtitle is "" then
                display notification notificationMessage with title notificationTitle
            else
                display notification notificationMessage with title notificationTitle subtitle notificationSubtitle
            end if
        end run
        """
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script, title, subtitle, message],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            if result.stderr and result.stderr.strip():
                logger.warning("osascript 알림 경고: %s", result.stderr.strip())
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or "").strip() or str(e)
            logger.error("알림 전송 실패: %s", detail)
        except Exception as e:
            logger.error("알림 전송 실패: %s", e)

    def _cancel_download(self, _):
        self._download_stop_event.set()
        logger.info("사용자가 다운로드를 중단했습니다")

    def _pause_pipeline(self, _):
        self._pipeline_pause_event.clear()
        self._pipeline_base_msg = "⏸ 일시중단됨 (STT 처리 중이면 완료 후 중단)"
        logger.info("파이프라인 일시중단")

    def _resume_pipeline(self, _):
        self._pipeline_pause_event.set()
        logger.info("파이프라인 재개")

    def _cancel_pipeline(self, _):
        self._pipeline_pause_event.set()  # 일시중단 상태면 해제하여 stop 체크 도달 가능하게
        self._pipeline_stop_event.set()
        self._pipeline_base_msg = "⏹ 처리 중단 요청됨..."
        logger.info("사용자가 처리를 중단했습니다")

    def _build_model_menu(self) -> rumps.MenuItem:
        current_backend, current_model, current_quant = self._get_current_stt_selection()

        model_menu = rumps.MenuItem(self._get_model_menu_title())

        whisper_menu = rumps.MenuItem("Whisper (MLX)")
        for model_name in WHISPER_MODEL_REPOS:
            sub = rumps.MenuItem(model_name)
            for quant in ["4bit", "base", "8bit"]:
                item = rumps.MenuItem(quant, callback=self._make_model_callback("whisper", model_name, quant))
                item.state = 1 if (
                    current_backend == "whisper" and model_name == current_model and quant == current_quant
                ) else 0
                self._model_menu_items[("whisper", model_name, quant)] = item
                sub.add(item)
            whisper_menu.add(sub)
        model_menu.add(whisper_menu)

        qwen_menu = rumps.MenuItem("Qwen3-ASR")
        for label, repo in QWEN_MODEL_REPOS.items():
            item = rumps.MenuItem(label, callback=self._make_model_callback("qwen3_asr", repo, None))
            item.state = 1 if current_backend == "qwen3_asr" and current_model == repo else 0
            self._model_menu_items[("qwen3_asr", repo, None)] = item
            qwen_menu.add(item)
        model_menu.add(qwen_menu)

        apple_menu = rumps.MenuItem("Apple Speech")
        for model_key, label in APPLE_SPEECH_MODELS.items():
            item = rumps.MenuItem(label, callback=self._make_model_callback("apple_speech", model_key, None))
            item.state = 1 if current_backend == "apple_speech" and current_model == model_key else 0
            self._model_menu_items[("apple_speech", model_key, None)] = item
            apple_menu.add(item)
        model_menu.add(apple_menu)

        return model_menu

    def _toggle_mic(self, sender):
        sender.state = not sender.state
        self._config["mic_enabled"] = bool(sender.state)
        self._save_config()

    def _normalize_mic_device_choice(self, value: Optional[str]) -> str:
        normalized = str(value or "").strip().lstrip(":").casefold()
        return MIC_DEVICE_ALIASES.get(normalized, "macbook")

    def _get_mic_device_menu_title(self) -> str:
        choice = self._normalize_mic_device_choice(self._config.get("mic_device_index"))
        return f"마이크 입력: {MIC_DEVICE_CHOICES.get(choice, MIC_DEVICE_CHOICES['macbook'])}"

    def _build_mic_device_menu(self) -> rumps.MenuItem:
        menu = rumps.MenuItem(self._get_mic_device_menu_title())
        current_choice = self._normalize_mic_device_choice(self._config.get("mic_device_index"))
        for choice, label in MIC_DEVICE_CHOICES.items():
            item = rumps.MenuItem(label, callback=self._make_mic_device_callback(choice))
            item.state = 1 if choice == current_choice else 0
            self._mic_device_items[choice] = item
            menu.add(item)
        return menu

    def _make_mic_device_callback(self, choice: str):
        def _select(_sender):
            for item in self._mic_device_items.values():
                item.state = 0
            self._mic_device_items[choice].state = 1
            self._config["mic_device_index"] = choice
            self._mic_device_menu.title = self._get_mic_device_menu_title()
            self._save_config()
            logger.info("마이크 입력 변경: %s", choice)
        return _select

    def _toggle_stt_skip(self, sender):
        sender.state = not sender.state
        self._config["stt_skip"] = bool(sender.state)
        self._save_config()

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
            self._save_config()
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
                self._save_config()

                self._hotkey_manager.update_binding(action, mod, keycode)
                self._hotkey_items[action].title = f"{label}: {display}"
                self._pending_app_title = "MN"

                logger.info("단축키 변경: %s → %s", action, display)
                self._notify("AutoMeetingNote", "단축키 변경", f"{label}: {display}")

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
        self._save_config()

        for action, hk in DEFAULT_HOTKEYS.items():
            self._hotkey_manager.update_binding(action, hk["mod"], hk["key"])
            label = HOTKEY_LABELS[action]
            self._hotkey_items[action].title = f"{label}: {format_hotkey(hk['mod'], hk['key'])}"

        logger.info("단축키 초기화")
        rumps.alert(title="단축키 초기화", message="모든 단축키가 기본값으로 초기화되었습니다.")

    def _make_model_callback(self, backend: str, model_name: str, quant: Optional[str]):
        def _select(_sender):
            for item in self._model_menu_items.values():
                item.state = 0
            self._model_menu_items[(backend, model_name, quant)].state = 1
            self._config["stt_backend"] = backend
            if backend == "apple_speech":
                self._config["apple_speech_model"] = model_name
            elif backend == "qwen3_asr":
                self._config["qwen_model"] = model_name
            else:
                self._config["whisper_model"] = model_name
                self._config["whisper_quant"] = quant or DEFAULT_WHISPER_QUANT
            self._model_menu.title = self._get_model_menu_title()
            self._save_config()
            logger.info("STT 모델 변경: backend=%s, model=%s, quant=%s", backend, model_name, quant)
            stt_dependency_error = self._get_stt_dependency_error(backend)
            if stt_dependency_error:
                rumps.alert(title="설정 오류", message=stt_dependency_error)
                return
            if backend == "apple_speech":
                authorized, detail = self._ensure_apple_speech_authorization(request_if_needed=True)
                if not authorized:
                    rumps.alert(title="Apple Speech 권한 필요", message=detail)
            self._check_and_download_model(backend, model_name, quant)
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
            self._notify("모델 다운로드 완료", repo, "STT 모델 준비가 완료되었습니다.")
        except Exception as e:
            logger.error("모델 다운로드 실패: %s — %s", repo, e)
            self._on_status(f"❌ 모델 다운로드 실패: {repo}")
            self._notify("모델 다운로드 실패", repo, str(e))
        finally:
            self._hide_cancel_item()

    def _flush_ui(self, _):
        if self._pending_app_title is not None:
            self.title = self._pending_app_title
            self._pending_app_title = None
        if self._reset_title_at is not None and time.time() >= self._reset_title_at:
            self.title = "MN"
            self._reset_title_at = None
        if self._pipeline_running:
            if "⏹ 처리 중단" not in self.menu:
                self.menu.add(rumps.MenuItem("⏹ 처리 중단", callback=self._cancel_pipeline))
            is_paused = not self._pipeline_pause_event.is_set()
            if is_paused:
                if "⏸ 처리 일시중단" in self.menu:
                    del self.menu["⏸ 처리 일시중단"]
                if "▶ 처리 재개" not in self.menu:
                    self.menu.add(rumps.MenuItem("▶ 처리 재개", callback=self._resume_pipeline))
            else:
                if "▶ 처리 재개" in self.menu:
                    del self.menu["▶ 처리 재개"]
                if "⏸ 처리 일시중단" not in self.menu:
                    self.menu.add(rumps.MenuItem("⏸ 처리 일시중단", callback=self._pause_pipeline))
        else:
            for label in ["⏹ 처리 중단", "⏸ 처리 일시중단", "▶ 처리 재개"]:
                if label in self.menu:
                    del self.menu[label]
        if self._pipeline_start_time is not None:
            self._status_item.title = self._build_pipeline_status()
        elif self._pending_status_title is not None:
            self._status_item.title = self._pending_status_title
            self._pending_status_title = None

    def _on_status(self, message: str):
        logger.info("상태: %s", message)
        self._status_log.append(message)
        if not self._is_recording:
            self._pending_app_title = "MN ⏳"
        if self._pipeline_start_time is not None:
            self._pipeline_base_msg = message
            m = re.match(r'\[(\d+)/(\d+)\]', message)
            if m:
                self._pipeline_step = (int(m.group(1)), int(m.group(2)))
        else:
            self._pending_status_title = f"처리 현황: {message}"

    def _on_done(self, filename: str):
        done_msg = f"✅ 완료: {filename}"
        self._status_log.append(done_msg)
        if not self._is_recording:
            self._pending_app_title = "MN ✅"
        self._pending_status_title = f"처리 현황: {done_msg}"
        self._notify("회의록 자동 생성 완료", filename, "회의록이 성공적으로 생성되었습니다.")
        self._schedule_title_reset(5)

    def _schedule_title_reset(self, delay: float = 5):
        self._reset_title_at = time.time() + delay

    def _build_pipeline_status(self) -> str:
        elapsed = time.time() - self._pipeline_start_time
        elapsed_str = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        start_str = datetime.fromtimestamp(self._pipeline_start_time).strftime("%H:%M:%S")
        base = self._pipeline_base_msg or "처리 중..."
        k, n = self._pipeline_step
        is_paused = not self._pipeline_pause_event.is_set()
        if is_paused:
            time_info = f"시작 {start_str} | 경과 {elapsed_str}"
            return f"처리 현황: ⏸ {base} | {time_info}"
        if k > 0 and n > 0 and k < n:
            total_est = elapsed * n / k
            remaining = total_est - elapsed
            rem_str = f"{int(remaining // 60):02d}:{int(remaining % 60):02d}"
            time_info = f"시작 {start_str} | 경과 {elapsed_str} | 예상잔여 {rem_str}"
        else:
            time_info = f"시작 {start_str} | 경과 {elapsed_str}"
        return f"처리 현황: {base} | {time_info}"

    def _show_status_detail(self, _):
        if not self._status_log:
            rumps.alert(title="처리 현황", message="진행 중인 작업이 없습니다.")
        else:
            rumps.alert(title="처리 현황", message="\n".join(self._status_log))

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
                mic_index = str(self._config.get("mic_device_index") or "builtin")
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
                    self._notify("AutoMeetingNote", "화면 녹화 오류", str(e))

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
                mic_index = str(self._config.get("mic_device_index") or "builtin")
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
                if Path(output_path).suffix.lower() == ".mp4":
                    if audio_path or mic_path:
                        self._on_status("녹화 오디오 병합 중...")
                        mp4_path = self._recorder.merge_audio_into_mp4(
                            Path(output_path),
                            audio_path,
                            mic_path=mic_path,
                            audio_offset=audio_offset,
                        )
                    else:
                        mp4_path = Path(output_path)
                else:
                    self._on_status("녹화 파일 압축 중...")
                    mp4_path = self._recorder.compress_and_merge(
                        output_path, audio_path,
                        mic_path=mic_path,
                        audio_offset=audio_offset,
                        progress_callback=self._on_status,
                    )
                self._notify("AutoMeetingNote", "녹화 완료", mp4_path.name)
                if stt_skip:
                    self._on_status(f"✅ 녹화 완료 (STT 건너뜀): {mp4_path.name}")
                    self._schedule_title_reset(5)
                    return
                if self._pipeline_running:
                    self._finish_recording_while_processing()
                    return
                if not self._confirm_on_main(f"녹화 파일이 준비되었습니다.\n({mp4_path.name})\n\n회의록 생성을 시작할까요?"):
                    self._on_status(f"회의록 생성 취소됨: {mp4_path.name}")
                    self._schedule_title_reset(5)
                    return
                self._run_single_file(str(mp4_path))
            elif mode == "audio":
                # 시스템 오디오 + 마이크 믹싱
                if mic_path:
                    self._on_status("오디오 믹싱 중...")
                    final_path = self._recorder.mix_wav(output_path, mic_path)
                else:
                    final_path = output_path
                self._notify("AutoMeetingNote", "녹음 완료", final_path.name)
                if stt_skip:
                    self._on_status(f"✅ 녹음 완료 (STT 건너뜀): {final_path.name}")
                    self._schedule_title_reset(5)
                    return
                if self._pipeline_running:
                    self._finish_recording_while_processing()
                    return
                if not self._confirm_on_main(f"녹음 파일이 준비되었습니다.\n({final_path.name})\n\n회의록 생성을 시작할까요?"):
                    self._on_status(f"회의록 생성 취소됨: {final_path.name}")
                    self._schedule_title_reset(5)
                    return
                self._run_single_file(str(final_path))
        except Exception as e:
            logger.error("녹화 후처리 실패: %s", e)
            err_msg = f"❌ 녹화 후처리 오류: {e}"
            self._status_log.append(err_msg)
            self._pending_status_title = f"처리 현황: {err_msg}"
            self._schedule_title_reset(5)

    def _finish_recording_while_processing(self):
        message = "처리중이므로 회의록 생성이 불가능하여 여기서 종료합니다."
        self._on_status(message)
        self._alert_on_main("회의록 생성 불가", message)
        self._schedule_title_reset(5)

    def _alert_on_main(self, title: str, message: str):
        def _alert(timer):
            timer.stop()
            rumps.alert(title=title, message=message)

        rumps.Timer(_alert, 0.0).start()

    def _confirm_on_main(self, message: str) -> bool:
        """회의록 생성 여부 확인. 메뉴바 앱에서는 osascript가 더 안정적이다."""
        script = (
            "on run argv\n"
            "set dialogMessage to item 1 of argv\n"
            "tell application \"System Events\"\n"
            "activate\n"
            "set dialogResult to display dialog dialogMessage with title \"확인\" "
            "buttons {\"아니오\", \"예\"} default button \"예\" cancel button \"아니오\"\n"
            "return button returned of dialogResult\n"
            "end tell\n"
            "end run"
        )
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-e", script, message],
                capture_output=True,
                text=True,
            )
            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()
            if result.returncode == 0:
                return stdout == "예"
            if stderr:
                logger.warning("confirm dialog osascript 경고: %s", stderr)
            else:
                logger.warning("confirm dialog osascript 비정상 종료: rc=%s", result.returncode)
            return False
        except Exception as e:
            logger.error("confirm dialog 오류: %s", e)
            return False

    def _run_files_sequentially(self, paths: list):
        for path in paths:
            self._run_single_file(path)
            if self._pipeline_stop_event.is_set():
                break

    def _run_single_file(self, path: str):
        filename = Path(path).name
        self._pipeline_stop_event.clear()
        self._pipeline_pause_event.set()  # 항상 실행 상태로 시작
        self._pipeline_start_time = time.time()
        self._pipeline_step = (0, 0)
        self._pipeline_base_msg = ""
        self._pipeline_running = True
        self._config = load_config()
        try:
            if self._config.get("stt_backend", DEFAULT_STT_BACKEND) == "apple_speech":
                authorized, detail = self._ensure_apple_speech_authorization(request_if_needed=True)
                if not authorized:
                    raise RuntimeError(detail)
            run_pipeline(
                path,
                self._config,
                status_callback=self._on_status,
                confirm_callback=self._confirm_on_main,
                stop_event=self._pipeline_stop_event,
                pause_event=self._pipeline_pause_event,
            )
            self._on_done(filename)
        except PipelineCancelledError:
            msg = "⏹ 처리 중단됨"
            self._status_log.append(msg)
            self._pending_status_title = f"처리 현황: {msg}"
            self._schedule_title_reset(5)
        except Exception as e:
            logger.error("수동 처리 실패: %s — %s", filename, e)
            err_msg = f"❌ 오류: {filename}"
            self._status_log.append(err_msg)
            self._pending_status_title = f"처리 현황: {err_msg}"
            self._notify("처리 실패", filename, str(e))
        finally:
            self._pipeline_pause_event.set()  # 잔류 일시중단 상태 해제
            self._pipeline_start_time = None
            self._pipeline_running = False

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

    def _open_release_notes(self, _):
        release_notes_path = _resource_path() / "RELEASE_NOTES.md"
        if not release_notes_path.exists():
            rumps.alert(title="릴리즈 노트", message="RELEASE_NOTES.md 파일이 없습니다.")
            return
        subprocess.Popen(["open", str(release_notes_path)])

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
