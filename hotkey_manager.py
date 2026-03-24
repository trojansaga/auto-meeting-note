import logging
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

MOD_CMD = 1
MOD_CTRL = 2
MOD_ALT = 4
MOD_SHIFT = 8

_NS_MOD_SHIFT = 1 << 17
_NS_MOD_CTRL = 1 << 18
_NS_MOD_ALT = 1 << 19
_NS_MOD_CMD = 1 << 20
_NS_KEY_DOWN_MASK = 1 << 10

KEYCODE_TO_NAME: Dict[int, str] = {
    0: 'A', 1: 'S', 2: 'D', 3: 'F', 4: 'H', 5: 'G', 6: 'Z', 7: 'X',
    8: 'C', 9: 'V', 11: 'B', 12: 'Q', 13: 'W', 14: 'E', 15: 'R',
    16: 'Y', 17: 'T', 18: '1', 19: '2', 20: '3', 21: '4', 22: '6',
    23: '5', 24: '=', 25: '9', 26: '7', 27: '-', 28: '8', 29: '0',
    30: ']', 31: 'O', 32: 'U', 33: '[', 34: 'I', 35: 'P', 36: 'Return',
    37: 'L', 38: 'J', 39: "'", 40: 'K', 41: ';', 42: '\\', 43: ',',
    44: '/', 45: 'N', 46: 'M', 47: '.', 48: 'Tab', 49: 'Space', 50: '`',
    51: 'Delete', 53: 'Esc',
    96: 'F5', 97: 'F6', 98: 'F7', 99: 'F3', 100: 'F8', 101: 'F9',
    103: 'F11', 105: 'F13', 106: 'F16', 107: 'F14', 109: 'F10',
    111: 'F12', 113: 'F15', 115: 'Home', 116: 'PageUp',
    117: 'FwdDel', 118: 'F4', 119: 'End', 120: 'F2', 121: 'PageDown',
    122: 'F1', 123: '←', 124: '→', 125: '↓', 126: '↑',
}

DEFAULT_HOTKEYS = {
    "screen_record": {"mod": MOD_CMD | MOD_CTRL, "key": 15},   # ⌃⌘R
    "audio_record": {"mod": MOD_CMD | MOD_CTRL, "key": 0},     # ⌃⌘A
    "pause_resume": {"mod": MOD_CMD | MOD_CTRL, "key": 35},    # ⌃⌘P
}

HOTKEY_LABELS = {
    "screen_record": "화면 녹화",
    "audio_record": "녹음",
    "pause_resume": "일시 정지/재개",
}


def _ns_flags_to_mod(flags: int) -> int:
    mod = 0
    if flags & _NS_MOD_CMD:
        mod |= MOD_CMD
    if flags & _NS_MOD_CTRL:
        mod |= MOD_CTRL
    if flags & _NS_MOD_ALT:
        mod |= MOD_ALT
    if flags & _NS_MOD_SHIFT:
        mod |= MOD_SHIFT
    return mod


def format_hotkey(mod: int, keycode: int) -> str:
    parts = []
    if mod & MOD_CTRL:
        parts.append("⌃")
    if mod & MOD_ALT:
        parts.append("⌥")
    if mod & MOD_SHIFT:
        parts.append("⇧")
    if mod & MOD_CMD:
        parts.append("⌘")
    parts.append(KEYCODE_TO_NAME.get(keycode, f"({keycode})"))
    return "".join(parts)


class HotkeyManager:
    def __init__(self):
        self._actions: Dict[str, Tuple[int, int, Callable]] = {}
        self._global_monitor = None
        self._local_monitor = None
        self._recording = False
        self._on_recorded: Optional[Callable] = None
        self._on_cancel: Optional[Callable] = None

    def register(self, action: str, mod: int, keycode: int, callback: Callable):
        self._actions[action] = (mod, keycode, callback)
        logger.info("단축키 등록: %s → %s", action, format_hotkey(mod, keycode))

    def update_binding(self, action: str, mod: int, keycode: int):
        if action in self._actions:
            _, _, cb = self._actions[action]
            self._actions[action] = (mod, keycode, cb)
            logger.info("단축키 변경: %s → %s", action, format_hotkey(mod, keycode))

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self):
        from AppKit import NSEvent

        def _on_global(event):
            self._handle(event)

        def _on_local(event):
            self._handle(event)
            return event

        self._global_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            _NS_KEY_DOWN_MASK, _on_global
        )
        self._local_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            _NS_KEY_DOWN_MASK, _on_local
        )
        logger.info("단축키 모니터 시작")

    def stop(self):
        from AppKit import NSEvent
        if self._global_monitor:
            NSEvent.removeMonitor_(self._global_monitor)
            self._global_monitor = None
        if self._local_monitor:
            NSEvent.removeMonitor_(self._local_monitor)
            self._local_monitor = None

    def start_recording(self, on_recorded: Callable, on_cancel: Optional[Callable] = None):
        self._recording = True
        self._on_recorded = on_recorded
        self._on_cancel = on_cancel

    def cancel_recording(self):
        was_recording = self._recording
        self._recording = False
        cb = self._on_cancel
        self._on_recorded = None
        self._on_cancel = None
        if was_recording and cb:
            cb()

    def _handle(self, event):
        keycode = event.keyCode()
        mod = _ns_flags_to_mod(event.modifierFlags())

        if self._recording:
            if keycode == 53:  # Escape → 취소
                self._recording = False
                cb = self._on_cancel
                self._on_recorded = None
                self._on_cancel = None
                if cb:
                    cb()
                return
            if mod:  # 수정자 키 필수
                self._recording = False
                cb = self._on_recorded
                self._on_recorded = None
                self._on_cancel = None
                if cb:
                    cb(mod, keycode)
                return
            return

        for _, (reg_mod, reg_key, callback) in self._actions.items():
            if mod == reg_mod and keycode == reg_key:
                try:
                    callback()
                except Exception as e:
                    logger.error("단축키 콜백 오류: %s", e)
                return
