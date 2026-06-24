"""녹화 중 시각적 표시기.

- 메뉴바 타이틀을 빨간색으로 점멸시켜 가독성을 높인다.
- 화면 전체 가장자리에 빨간 테두리를 점멸시켜 녹화 중임을 알린다.

테두리 창은 ``NSWindowSharingNone`` 으로 설정해 ScreenCaptureKit 녹화 결과물에는
포함되지 않는다(사용자에게만 보이는 표시).

모든 AppKit 객체 생성/조작은 메인 스레드에서 이뤄져야 하며, 이 모듈의 메서드는
rumps 메뉴/단축키 콜백(메인 스레드)에서 호출되는 것을 전제로 한다.
"""

import logging
import math
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 가장자리에서 안쪽으로 빨간색이 사라지는 글로우 폭(포인트)
_GLOW_WIDTH = 90.0
# 빨간색 최대 투명도(불투명도). 0.5 = 50%
_MAX_ALPHA = 0.5
# 펄스(밝아졌다 어두워지는) 한 주기 길이(초) — 자연스러운 호흡 느낌
_PULSE_PERIOD = 1.8
# 애니메이션 갱신 간격(초). 0.05 → 약 20fps 부드러운 펄스
_TICK_INTERVAL = 0.05
# 메뉴바 타이틀 빨강/기본색 전환 주기(초)
_TITLE_BLINK_PERIOD = 0.6


def _make_border_view_class():
    """AppKit 의존성을 지연 로드하기 위해 NSView 서브클래스를 함수 내부에서 정의.

    네 변(상/하/좌/우)에 각각 가장자리=빨강 → 안쪽=투명 그라데이션 밴드를 그려
    화면 테두리에 부드러운 빨간 글로우를 만든다. 펄스(깜빡임)는 창 전체의
    alpha 값을 시간에 따라 조절해 표현하므로 이 뷰는 한 번만 그리면 된다.
    """
    from AppKit import (
        NSBezierPath,
        NSColor,
        NSGradient,
        NSGraphicsContext,
        NSMakePoint,
        NSMakeRect,
        NSRectFill,
        NSView,
    )

    class _BorderView(NSView):
        def drawRect_(self, _rect):
            bounds = self.bounds()
            w = bounds.size.width
            h = bounds.size.height
            g = min(_GLOW_WIDTH, w / 2.0, h / 2.0)

            # 가운데는 완전 투명(클릭/시야 방해 없음)
            NSColor.clearColor().set()
            NSRectFill(bounds)

            edge = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.0, 0.0, 1.0)
            inner = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.0, 0.0, 0.0)
            gradient = NSGradient.alloc().initWithStartingColor_endingColor_(edge, inner)

            # (밴드 사각형, 가장자리 점, 안쪽 점) — 가장자리에서 안쪽으로 페이드
            bands = [
                (NSMakeRect(0, h - g, w, g), NSMakePoint(0, h), NSMakePoint(0, h - g)),  # 상
                (NSMakeRect(0, 0, w, g), NSMakePoint(0, 0), NSMakePoint(0, g)),          # 하
                (NSMakeRect(0, 0, g, h), NSMakePoint(0, 0), NSMakePoint(g, 0)),          # 좌
                (NSMakeRect(w - g, 0, g, h), NSMakePoint(w, 0), NSMakePoint(w - g, 0)),  # 우
            ]
            for rect, start_pt, end_pt in bands:
                ctx = NSGraphicsContext.currentContext()
                ctx.saveGraphicsState()
                NSBezierPath.bezierPathWithRect_(rect).addClip()
                gradient.drawFromPoint_toPoint_options_(start_pt, end_pt, 0)
                ctx.restoreGraphicsState()

    return _BorderView


class RecordingIndicator:
    """녹화 중 메뉴바 타이틀 + 화면 테두리 점멸 표시기."""

    def __init__(
        self,
        status_item_getter: Callable[[], object],
    ):
        """
        :param status_item_getter: 현재 NSStatusItem 을 반환하는 콜러블
            (앱 실행 후에야 생성되므로 지연 조회).
        """
        self._status_item_getter = status_item_getter
        self._timer = None
        self._windows: list = []
        self._tick = 0  # 애니메이션 프레임 카운터
        self._blink_on = False  # 메뉴바 타이틀 빨강 표시 여부
        self._active = False
        self._text = "● REC"
        self._border_view_class = None

    # ---- 외부 인터페이스 ----------------------------------------------------

    def start(self):
        if self._active:
            return
        self._active = True
        self._tick = 0
        self._blink_on = True
        try:
            self._build_border_windows()
        except Exception as exc:
            logger.warning("녹화 테두리 표시 생성 실패: %s", exc)
            self._windows = []
        try:
            import rumps

            self._timer = rumps.Timer(self._on_tick, _TICK_INTERVAL)
            self._timer.start()
        except Exception as exc:
            logger.warning("녹화 점멸 타이머 시작 실패: %s", exc)
        # 즉시 1회 반영(타이머 첫 발화 대기 없이 바로 빨간 상태 표시)
        self._render()

    def set_text(self, text: str):
        """메뉴바에 표시할 현재 텍스트(예: '● REC 00:12')를 갱신."""
        self._text = text
        if self._active:
            self._render()

    def stop(self):
        if not self._active:
            return
        self._active = False
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass
            self._timer = None
        for window in self._windows:
            try:
                window.orderOut_(None)
            except Exception:
                pass
        self._windows = []
        self._restore_title()

    # ---- 내부 구현 ---------------------------------------------------------

    def _build_border_windows(self):
        from AppKit import (
            NSBackingStoreBuffered,
            NSColor,
            NSScreen,
            NSWindow,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary,
            NSWindowStyleMaskBorderless,
        )

        if self._border_view_class is None:
            self._border_view_class = _make_border_view_class()

        # 메뉴바/풀스크린 위로 떠 있도록 매우 높은 윈도우 레벨 사용
        window_level = 2147483631  # CGShieldingWindowLevel 근처(최상위)
        share_none = 0  # NSWindowSharingNone
        collection = (
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )

        windows = []
        for screen in NSScreen.screens():
            frame = screen.frame()
            window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                frame,
                NSWindowStyleMaskBorderless,
                NSBackingStoreBuffered,
                False,
            )
            window.setOpaque_(False)
            window.setBackgroundColor_(NSColor.clearColor())
            window.setIgnoresMouseEvents_(True)
            window.setLevel_(window_level)
            window.setCollectionBehavior_(collection)
            # 녹화 결과물에서 제외(사용자에게만 보임)
            if window.respondsToSelector_("setSharingType:"):
                window.setSharingType_(share_none)

            view = self._border_view_class.alloc().initWithFrame_(
                NS_zero_origin_frame(frame)
            )
            window.setContentView_(view)
            window.orderFrontRegardless()
            windows.append(window)

        self._windows = windows

    def _on_tick(self, _timer):
        self._tick += 1
        # 메뉴바 타이틀은 일정 주기로 빨강/기본색 토글(가독성)
        title_period_ticks = max(1, int(round(_TITLE_BLINK_PERIOD / _TICK_INTERVAL)))
        self._blink_on = (self._tick // title_period_ticks) % 2 == 0
        self._render()

    def _render(self):
        self._render_border()
        self._render_title()

    def _current_pulse_alpha(self) -> float:
        """사인 곡선 기반 부드러운 펄스 alpha (0 ~ _MAX_ALPHA)."""
        period_ticks = max(1, _PULSE_PERIOD / _TICK_INTERVAL)
        phase = (self._tick % period_ticks) / period_ticks * 2.0 * math.pi
        # (1 - cos) / 2 → 0..1 을 부드럽게 오가며, 최저점에서 멈춤이 자연스러움
        return _MAX_ALPHA * (1.0 - math.cos(phase)) / 2.0

    def _render_border(self):
        alpha = self._current_pulse_alpha()
        for window in self._windows:
            try:
                window.setAlphaValue_(alpha)
            except Exception:
                pass

    def _render_title(self):
        try:
            from AppKit import (
                NSAttributedString,
                NSColor,
                NSForegroundColorAttributeName,
            )

            item = self._status_item_getter()
            if item is None:
                return
            button = item.button()
            if button is None:
                return
            color = NSColor.redColor() if self._blink_on else NSColor.labelColor()
            attrs = {NSForegroundColorAttributeName: color}
            astr = NSAttributedString.alloc().initWithString_attributes_(
                self._text, attrs
            )
            button.setAttributedTitle_(astr)
        except Exception as exc:
            logger.debug("녹화 타이틀 점멸 적용 실패: %s", exc)

    def _restore_title(self):
        """점멸 종료 후 기본(검정/라벨색) 타이틀로 복귀."""
        try:
            item = self._status_item_getter()
            if item is None:
                return
            button = item.button()
            if button is None:
                return
            # 빈 attributedTitle 로 초기화 → 이후 setTitle_ 가 기본 스타일로 표시
            from AppKit import NSAttributedString

            button.setAttributedTitle_(
                NSAttributedString.alloc().initWithString_("")
            )
        except Exception as exc:
            logger.debug("녹화 타이틀 복원 실패: %s", exc)


def NS_zero_origin_frame(frame):
    """화면 frame 을 (0,0) 원점 기준 크기로 변환(콘텐츠 뷰용)."""
    from AppKit import NSMakeRect

    return NSMakeRect(0, 0, frame.size.width, frame.size.height)
