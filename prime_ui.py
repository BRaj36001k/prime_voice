"""
Prime UI (PySide6) - holographic HUD style.

  PrimeWindow  - desktop console: arc-core visualiser, system readout, conversation log, command bar.
  FloatingOrb  - small always-on-top arc core that floats on the desktop while Prime is closed.

The UI only emits signals and exposes setters; the "brain" plugs in from prime.py.
States for set_state(): "idle", "listening", "thinking", "replying", "speaking".
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# --- theme -------------------------------------------------------------------
BG = QColor(2, 7, 13)
CYAN = QColor(47, 216, 255)
STATE_COL = {
    "idle": QColor(47, 216, 255),
    "listening": QColor(60, 255, 214),
    "thinking": QColor(255, 176, 46),
    "replying": QColor(110, 232, 255),
    "speaking": QColor(150, 240, 255),
    "acting": QColor(255, 110, 80),
}
STATE_LABEL = {
    "idle": "STANDBY",
    "listening": "LISTENING",
    "thinking": "PROCESSING",
    "replying": "RESPONDING",
    "speaking": "SPEAKING",
    "acting": "EXECUTING",
}
TEXT = "#d4f3fb"
DIM = "#5f8d9c"
HUD_FONTS = ["Bahnschrift", "Orbitron", "Rajdhani", "Segoe UI Semibold", "Segoe UI", "Arial"]
BODY_FONTS = ["Segoe UI", "Inter", "Roboto", "Helvetica Neue", "Arial"]
MONO_FONTS = ["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"]


def A(c: QColor, a: float) -> QColor:
    q = QColor(c)
    q.setAlpha(max(0, min(255, int(a))))
    return q


def _font(families: list[str], px: int, weight=QFont.Weight.Normal, spacing: float = 0.0) -> QFont:
    f = QFont()
    f.setFamilies(families)
    f.setPixelSize(px)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return f


def hud_font(px: int, spacing: float = 2.0) -> QFont:
    return _font(HUD_FONTS, px, QFont.Weight.DemiBold, spacing)


def body_font(px: int) -> QFont:
    return _font(BODY_FONTS, px)


def mono_font(px: int, spacing: float = 1.0) -> QFont:
    return _font(MONO_FONTS, px, QFont.Weight.Normal, spacing)


# --- the arc core ----------------------------------------------------------------
def paint_core(p: QPainter, cx: float, cy: float, R: float, t: float, state: str,
               level: float, col: QColor, detail: bool = True) -> None:
    """Concentric rotating rings, audio bars and a segmented reactor ring around a bright core."""
    think = state in ("thinking", "acting")
    live = state in ("listening", "speaking", "replying")
    spin = 1.0 + (2.4 if think else 0.0) + (0.6 if live else 0.0)
    pulse = 0.5 + 0.5 * math.sin(t * (5.0 if live else 1.6))
    p.save()
    p.translate(cx, cy)

    g = QRadialGradient(0, 0, R * 1.08)
    g.setColorAt(0.0, A(col, 60 + 50 * pulse if live else 42))
    g.setColorAt(0.6, A(col, 18))
    g.setColorAt(1.0, A(col, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(g)
    p.drawEllipse(QPointF(0, 0), R * 1.08, R * 1.08)
    p.setBrush(Qt.BrushStyle.NoBrush)

    if detail:
        p.save()
        p.rotate(t * 5 * spin)
        for i in range(72):
            major = i % 6 == 0
            p.setPen(QPen(A(col, 170 if major else 80), 1.3 if major else 1.0))
            p.drawLine(QPointF(R * 0.99, 0), QPointF(R * (0.99 - (0.06 if major else 0.03)), 0))
            p.rotate(5)
        p.restore()

        rr = R * 0.84
        rect = QRectF(-rr, -rr, 2 * rr, 2 * rr)
        pen = QPen(A(col, 150), max(1.2, R * 0.012))
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        ang = -t * 14 * spin
        for k in range(3):
            p.drawArc(rect, int((ang + k * 120) * 16), 70 * 16)
        p.setPen(QPen(A(col, 45), 1))
        p.drawEllipse(QPointF(0, 0), R * 0.78, R * 0.78)

    acc = QColor(255, 176, 46) if state == "thinking" else col
    rr = R * 0.68
    rect = QRectF(-rr, -rr, 2 * rr, 2 * rr)
    pen = QPen(A(acc, 225), max(2.0, R * 0.03))
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    p.setPen(pen)
    ang = t * 38 * spin
    for k in range(2):
        p.drawArc(rect, int((ang + k * 180) * 16), 48 * 16)

    if detail:
        n = 56
        pen = QPen(A(col, 205), max(1.5, R * 0.014))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.save()
        for i in range(n):
            if state == "listening":
                a = level * (0.55 + 0.45 * math.sin(i * 0.7 + t * 9)) + 0.05
            elif state in ("speaking", "replying"):
                a = 0.32 + 0.28 * math.sin(i * 0.55 + t * 7) * math.sin(t * 2.3 + i * 0.2)
            elif think:
                a = 0.14 + 0.10 * math.sin(i * 0.9 - t * 8)
            else:
                a = 0.06 + 0.04 * math.sin(i * 0.5 + t * 1.5)
            ln = R * 0.03 + R * 0.13 * max(0.0, min(1.0, a))
            p.drawLine(QPointF(R * 0.50, 0), QPointF(R * 0.50 + ln, 0))
            p.rotate(360.0 / n)
        p.restore()

    rr = R * 0.36
    rect = QRectF(-rr, -rr, 2 * rr, 2 * rr)
    pen = QPen(A(col, 235), max(2.0, R * 0.06))
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    p.setPen(pen)
    rot = -t * 10 * spin
    for k in range(10):
        p.drawArc(rect, int((k * 36 + rot) * 16), 24 * 16)
    p.setPen(QPen(A(col, 120), 1))
    p.drawEllipse(QPointF(0, 0), R * 0.27, R * 0.27)

    cr = R * (0.24 + 0.02 * pulse)
    cg = QRadialGradient(0, 0, cr)
    cg.setColorAt(0.0, QColor(255, 255, 255, 255))
    cg.setColorAt(0.45, A(col.lighter(150), 230))
    cg.setColorAt(1.0, A(col, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(cg)
    p.drawEllipse(QPointF(0, 0), cr, cr)
    p.restore()


class ArcCore(QWidget):
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(240, 250)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.state = "idle"
        self.level = 0.0
        self._target = 0.0
        self._col = QColor(STATE_COL["idle"])
        self._t0 = time.monotonic()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(16)

    def set_state(self, state: str) -> None:
        self.state = state

    def set_level(self, v: float) -> None:
        self._target = max(0.0, min(1.0, v))

    def _tick(self) -> None:
        tgt = STATE_COL.get(self.state, CYAN)
        c = self._col
        self._col = QColor(
            int(c.red() + (tgt.red() - c.red()) * 0.1),
            int(c.green() + (tgt.green() - c.green()) * 0.1),
            int(c.blue() + (tgt.blue() - c.blue()) * 0.1),
        )
        self.level += (self._target - self.level) * 0.25
        if self.isVisible():
            self.update()

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, lab = float(self.width()), float(self.height()), 26.0
        R = min(w, h - lab) / 2 - 6
        t = time.monotonic() - self._t0
        paint_core(p, w / 2, (h - lab) / 2, R, t, self.state, self.level, self._col)
        p.setFont(mono_font(11, 4.0))
        p.setPen(A(self._col, 235))
        p.drawText(QRectF(0, h - lab, w, lab), Qt.AlignmentFlag.AlignCenter, STATE_LABEL.get(self.state, ""))


# --- HUD building blocks --------------------------------------------------------
class HudPanel(QWidget):
    """Translucent panel with corner brackets and a small title. Add content to .layout()."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 34, 14, 14)
        lay.setSpacing(8)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.fillRect(r, QColor(6, 28, 42, 95))
        p.setPen(QPen(A(CYAN, 55), 1))
        p.drawRect(r)
        p.setPen(QPen(A(CYAN, 40), 1))
        p.drawLine(QPointF(12, 28), QPointF(r.right() - 12, 28))
        p.setPen(QPen(A(CYAN, 210), 1.8))
        L = 13
        for x, y, dx, dy in (
            (r.left(), r.top(), 1, 1),
            (r.right(), r.top(), -1, 1),
            (r.left(), r.bottom(), 1, -1),
            (r.right(), r.bottom(), -1, -1),
        ):
            p.drawLine(QPointF(x, y), QPointF(x + dx * L, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * L))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(A(CYAN, 220))
        p.drawRect(QRectF(14, 11, 6, 6))
        p.setFont(mono_font(11, 3.0))
        p.setPen(A(CYAN, 200))
        p.drawText(QPointF(28, 18), self.title)


class HudButton(QAbstractButton):
    """Chamfered square button with a drawn icon."""

    def __init__(self, kind: str, size: int = 40, accent: QColor | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.accent = accent or CYAN
        self._hover = False
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.update()

    def enterEvent(self, e) -> None:
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, c = float(self.width()) - 1, float(self.height()) - 1, 8.0
        poly = QPolygonF([QPointF(0.5, 0.5), QPointF(w - c, 0.5), QPointF(w, c),
                          QPointF(w, h), QPointF(c, h), QPointF(0.5, h - c)])
        warn = self.kind in ("stop", "mic_off")
        col = QColor(255, 120, 90) if warn else self.accent
        p.setBrush(A(col, 70 if self.isDown() else (42 if self._hover else 16)))
        p.setPen(QPen(A(col, 200 if self._hover else 130), 1))
        p.drawPolygon(poly)
        s = self.width() / 40.0
        p.translate(self.width() / 2, self.height() / 2)
        p.scale(s, s)
        pen = QPen(A(col.lighter(125), 255), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        k = self.kind
        if k == "menu":
            for y in (-6, 0, 6):
                p.drawLine(QPointF(-8, y), QPointF(8, y))
        elif k == "min":
            p.drawLine(QPointF(-8, 6), QPointF(8, 6))
        elif k == "attach":
            p.drawLine(QPointF(-8, 0), QPointF(8, 0))
            p.drawLine(QPointF(0, -8), QPointF(0, 8))
        elif k == "send":
            p.drawLine(QPointF(-8, 0), QPointF(8, 0))
            p.drawLine(QPointF(1, -7), QPointF(8, 0))
            p.drawLine(QPointF(1, 7), QPointF(8, 0))
        elif k == "stop":
            p.setBrush(A(col.lighter(125), 255))
            p.drawRect(QRectF(-6, -6, 12, 12))
        elif k in ("mic", "mic_off"):
            p.drawRoundedRect(QRectF(-3.5, -10, 7, 12), 3.5, 3.5)
            p.drawArc(QRectF(-7, -6, 14, 14), 180 * 16, 180 * 16)
            p.drawLine(QPointF(0, 8), QPointF(0, 11))
            if k == "mic_off":
                p.drawLine(QPointF(-9, -10), QPointF(9, 11))


# --- chat ----------------------------------------------------------------------
class Message(QWidget):
    """user = outlined box on the right; assistant = plain text with a cyan rule on the left."""

    def __init__(self, role: str, text: str, name: str = "YOU", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self._max = 520
        user = role == "user"
        if role == "action":
            self.label = QLabel(text)
            self.label.setWordWrap(True)
            self.label.setFont(mono_font(12, 0.5))
            self._style_action()
            lay = QVBoxLayout(self)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self.label)
            return
        tag = QLabel(f"{name}  ·  {time.strftime('%H:%M')}" if user else f"PRIME  ·  {time.strftime('%H:%M')}")
        tag.setFont(mono_font(10, 2.0))
        tag.setStyleSheet(f"color:{'#7fb4c4' if user else '#2fd8ff'}; background:transparent;")
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.label.setFont(body_font(15))
        if user:
            self.label.setStyleSheet(
                "background:rgba(47,216,255,26); color:#e8fbff; border:1px solid rgba(47,216,255,105);"
                "border-radius:3px; padding:9px 13px;"
            )
        else:
            self.label.setStyleSheet(
                "background:transparent; color:#cdeaf3; border:none;"
                "border-left:2px solid #2fd8ff; padding:2px 0px 2px 12px;"
            )
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        for wdg in (tag, self.label):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            if user:
                row.addStretch(1)
                row.addWidget(wdg)
            else:
                row.addWidget(wdg)
                row.addStretch(1)
            col.addLayout(row)

    def _style_action(self) -> None:
        t = self.label.text()
        col = "#3cffd6" if t.startswith("✓") else ("#ff7a5c" if t.startswith(("✗", "⚠")) else "#6fc9dc")
        self.label.setStyleSheet(f"color:{col}; background:transparent; padding:0 0 0 14px;")

    def set_text(self, text: str) -> None:
        self.label.setText(text)
        if self.role == "action":
            self._style_action()
            return
        self._fit()

    def text(self) -> str:
        return self.label.text()

    def set_max_width(self, w: int) -> None:
        self._max = max(160, w)
        if self.role == "action":
            self.label.setMaximumWidth(self._max)
            return
        self._fit()

    def _fit(self) -> None:
        if self.role == "action":
            return
        pad = 28 if self.role == "user" else 14
        inner = max(80, self._max - pad)
        r = self.label.fontMetrics().boundingRect(
            0, 0, inner, 100000, int(Qt.TextFlag.TextWordWrap), self.label.text()
        )
        self.label.setFixedWidth(min(inner, r.width() + 6) + pad)


class Transcript(QScrollArea):
    def __init__(self, user_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.user_name = user_name.upper()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(
            "QScrollArea{background:transparent;} QWidget#inner{background:transparent;}"
            "QScrollBar:vertical{background:transparent;width:5px;margin:0;}"
            "QScrollBar::handle:vertical{background:rgba(47,216,255,90);border-radius:2px;min-height:30px;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        inner = QWidget()
        inner.setObjectName("inner")
        self._col = QVBoxLayout(inner)
        self._col.setContentsMargins(2, 4, 8, 4)
        self._col.setSpacing(16)
        self._col.addStretch(1)
        self.setWidget(inner)
        self.messages: list[Message] = []

    def add(self, role: str, text: str) -> Message:
        m = Message(role, text, self.user_name)
        m.set_max_width(int(self.width() * 0.86))
        self._col.insertWidget(self._col.count() - 1, m)
        self.messages.append(m)
        self.scroll_to_end()
        return m

    def clear(self) -> None:
        for m in self.messages:
            m.setParent(None)
            m.deleteLater()
        self.messages.clear()

    def scroll_to_end(self) -> None:
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(self.verticalScrollBar().maximum()))

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        for m in self.messages:
            m.set_max_width(int(self.width() * 0.86))


# --- main window -----------------------------------------------------------------
CUT = 22  # chamfer on the window's top-right and bottom-left corners


class PrimeWindow(QWidget):
    submitted = Signal(str)
    stop_requested = Signal()
    orb_pressed = Signal()
    mic_toggled = Signal(bool)  # True = muted
    attach_requested = Signal()
    new_chat_requested = Signal()
    hide_requested = Signal()
    quit_requested = Signal()
    confirm_answered = Signal(bool)

    def __init__(self, user_name: str = "Spidy") -> None:
        super().__init__()
        self.user_name = user_name
        self.state = "idle"
        self.muted = False
        self.link_ok = True
        self.voice_on = True
        self._t_start = time.monotonic()
        self._bg: QPixmap | None = None
        self.setWindowTitle("Prime")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(980, 640)
        self.setMinimumSize(780, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 20)
        root.setSpacing(14)

        # header
        head = QHBoxLayout()
        head.setSpacing(12)
        title = QLabel("PRIME")
        title.setFont(hud_font(26, 8.0))
        title.setStyleSheet("color:#e8fbff; background:transparent;")
        sub = QLabel("PERSONAL INTELLIGENCE SYSTEM")
        sub.setFont(mono_font(10, 3.0))
        sub.setStyleSheet("color:#2fd8ff; background:transparent;")
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        tcol.addWidget(title)
        tcol.addWidget(sub)
        head.addLayout(tcol)
        head.addStretch(1)
        self.clock = QLabel("")
        self.clock.setFont(mono_font(13, 2.0))
        self.clock.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.clock.setStyleSheet("color:#9fe9fb; background:transparent;")
        head.addWidget(self.clock)
        head.addSpacing(8)
        self.menu_btn = HudButton("menu", 36)
        self.menu_btn.clicked.connect(self._open_menu)
        self.min_btn = HudButton("min", 36)
        self.min_btn.clicked.connect(self.hide_requested)
        head.addWidget(self.menu_btn)
        head.addWidget(self.min_btn)
        root.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        # left column: core + system readout
        left = QVBoxLayout()
        left.setSpacing(14)
        core_panel = HudPanel("ARC CORE")
        self.core = ArcCore()
        self.core.clicked.connect(self.orb_pressed)
        core_panel.layout().addWidget(self.core, 1)
        left.addWidget(core_panel, 1)
        sys_panel = HudPanel("SYSTEM")
        self.info = QLabel("")
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setFont(mono_font(12, 1.0))
        self.info.setStyleSheet("background:transparent;")
        sys_panel.layout().addWidget(self.info)
        left.addWidget(sys_panel)
        left.setContentsMargins(0, 0, 0, 0)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setFixedWidth(310)
        body.addWidget(left_w)

        # right column: log + command bar
        right = QVBoxLayout()
        right.setSpacing(12)
        log_panel = HudPanel("CONVERSATION LOG")
        self.pages = QStackedWidget()
        empty = QWidget()
        el = QVBoxLayout(empty)
        el.addStretch(3)
        self.greeting = QLabel("")
        self.greeting.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.greeting.setWordWrap(True)
        self.greeting.setFont(_font(HUD_FONTS, 30, QFont.Weight.Light, 1.5))
        self.greeting.setStyleSheet("color:#e8fbff; background:transparent;")
        hint = QLabel("AWAITING INPUT  ·  TYPE A COMMAND OR TAP THE CORE")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setFont(mono_font(11, 3.0))
        hint.setStyleSheet("color:#2fd8ff; background:transparent;")
        el.addWidget(self.greeting)
        el.addSpacing(10)
        el.addWidget(hint)
        el.addStretch(4)
        self.transcript = Transcript(user_name)
        self.pages.addWidget(empty)
        self.pages.addWidget(self.transcript)
        log_panel.layout().addWidget(self.pages, 1)
        right.addWidget(log_panel, 1)

        self.confirm = QFrame()
        self.confirm.setStyleSheet(
            "QFrame#cf{background:rgba(255,110,80,28);border:1px solid rgba(255,110,80,170);border-radius:2px;}"
        )
        self.confirm.setObjectName("cf")
        cl = QHBoxLayout(self.confirm)
        cl.setContentsMargins(14, 8, 8, 8)
        cl.setSpacing(10)
        self.confirm_text = QLabel("")
        self.confirm_text.setFont(mono_font(12, 1.0))
        self.confirm_text.setWordWrap(True)
        self.confirm_text.setStyleSheet("color:#ffd0c4; background:transparent; border:none;")
        cl.addWidget(self.confirm_text, 1)
        for label, ok, css in (("ALLOW", True, "#3cffd6"), ("DENY", False, "#ff7a5c")):
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setFont(mono_font(12, 2.0))
            b.setFixedSize(84, 32)
            b.setStyleSheet(
                f"QPushButton{{color:{css};background:transparent;border:1px solid {css};border-radius:2px;}}"
                f"QPushButton:hover{{background:rgba(255,255,255,30);}}"
            )
            b.clicked.connect(lambda _=False, v=ok: self._answer(v))
            cl.addWidget(b)
        self.confirm.hide()
        right.addWidget(self.confirm)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        prompt = QLabel("›")
        prompt.setFont(_font(MONO_FONTS, 24, QFont.Weight.Bold))
        prompt.setStyleSheet("color:#2fd8ff; background:transparent;")
        bar.addWidget(prompt)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a command for Prime…")
        self.input.setFont(body_font(15))
        self.input.setFixedHeight(40)
        self.input.setStyleSheet(
            "QLineEdit{background:rgba(47,216,255,18);color:#e8fbff;border:1px solid rgba(47,216,255,100);"
            "border-radius:2px;padding:0 14px;selection-background-color:#14788f;}"
            "QLineEdit:focus{border:1px solid #2fd8ff;background:rgba(47,216,255,28);}"
        )
        self.input.returnPressed.connect(self._send)
        bar.addWidget(self.input, 1)
        self.attach_btn = HudButton("attach", 40)
        self.attach_btn.clicked.connect(self.attach_requested)
        self.mic_btn = HudButton("mic", 40)
        self.mic_btn.clicked.connect(self._toggle_mute)
        self.send_btn = HudButton("send", 40)
        self.send_btn.clicked.connect(self._send)
        self.stop_btn = HudButton("stop", 40)
        self.stop_btn.clicked.connect(self.stop_requested)
        for b in (self.attach_btn, self.mic_btn, self.send_btn, self.stop_btn):
            bar.addWidget(b)
        right.addLayout(bar)
        body.addLayout(right, 1)

        self.grip = QSizeGrip(self)
        self.grip.setFixedSize(16, 16)
        self.set_state("idle")
        self._refresh_greeting()
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._refresh_info)
        self._tick.start(1000)
        self._refresh_info()

    # ---- public API ----
    def add_message(self, role: str, text: str) -> Message:
        self.pages.setCurrentIndex(1)
        m = self.transcript.add(role, text)
        self._refresh_info()
        return m

    def add_action_line(self, text: str) -> None:
        self.pages.setCurrentIndex(1)
        self.transcript.add("action", text)

    def show_confirm(self, text: str) -> None:
        self.confirm_text.setText(f"⚠  AUTHORISE?   {text}")
        self.confirm.show()
        self.show_and_focus()

    def _answer(self, ok: bool) -> None:
        self.confirm.hide()
        self.confirm_answered.emit(ok)

    def clear_chat(self) -> None:
        self.transcript.clear()
        self.pages.setCurrentIndex(0)
        self._refresh_greeting()
        self._refresh_info()

    def set_state(self, state: str) -> None:
        self.state = state
        self.core.set_state(state)
        self.stop_btn.setVisible(state in ("thinking", "replying", "speaking", "acting"))
        self._refresh_info()

    def set_level(self, v: float) -> None:
        self.core.set_level(v)

    def set_link(self, ok: bool) -> None:
        self.link_ok = ok
        self._refresh_info()

    def set_voice(self, on: bool) -> None:
        self.voice_on = on
        self._refresh_info()

    def show_and_focus(self) -> None:
        if not self.isVisible():
            scr = QGuiApplication.primaryScreen().availableGeometry()
            self.move(scr.center().x() - self.width() // 2, scr.center().y() - self.height() // 2)
        if self.pages.currentIndex() == 0:
            self._refresh_greeting()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()

    def toggle_input(self) -> None:
        self.input.setFocus()

    # ---- internals ----
    def _refresh_greeting(self) -> None:
        h = time.localtime().tm_hour
        part = "morning" if h < 12 else ("afternoon" if h < 17 else "evening")
        self.greeting.setText(f"Good {part}, {self.user_name}.")

    def _refresh_info(self) -> None:
        self.clock.setText(time.strftime("%H:%M:%S   %a %d %b").upper())
        up = int(time.monotonic() - self._t_start)
        up_s = f"{up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}"

        def row(k: str, v: str, col: str = "#9fe9fb") -> str:
            return (f"<tr><td style='color:{DIM};padding-right:14px;'>{k}</td>"
                    f"<td style='color:{col};'>{v}</td></tr>")

        warn, good = "#ffb02e", "#3cffd6"
        st_col = {"thinking": "#ffb02e", "acting": "#ff6e50"}.get(self.state, "#9fe9fb")
        rows = [
            row("STATE", STATE_LABEL.get(self.state, "—"), st_col),
            row("LINK", "ELEVENLABS · ONLINE" if self.link_ok else "NO API KEY", good if self.link_ok else warn),
            row("VOICE", "ENABLED" if self.voice_on else "MUTED", good if self.voice_on else warn),
            row("MIC", "MUTED" if self.muted else "LIVE", warn if self.muted else good),
            row("LOG", f"{len(self.transcript.messages)} MSG"),
            row("UPTIME", up_s),
        ]
        self.info.setText("<table cellspacing='4'>" + "".join(rows) + "</table>")

    def _send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.add_message("user", text)
        self.submitted.emit(text)

    def _toggle_mute(self) -> None:
        self.muted = not self.muted
        self.mic_btn.set_kind("mic_off" if self.muted else "mic")
        self.mic_toggled.emit(self.muted)
        self._refresh_info()

    def _open_menu(self) -> None:
        m = QMenu(self)
        m.setStyleSheet(
            "QMenu{background:#06141d;color:#d4f3fb;border:1px solid rgba(47,216,255,120);padding:6px;}"
            "QMenu::item{padding:8px 24px;} QMenu::item:selected{background:#0e5a6d;}"
            "QMenu::separator{height:1px;background:rgba(47,216,255,60);margin:4px 6px;}"
        )
        m.addAction("New chat", self._new_chat)
        m.addAction("Minimise to orb", self.hide_requested.emit)
        m.addSeparator()
        m.addAction("Quit Prime", self.quit_requested.emit)
        m.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height())))

    def _new_chat(self) -> None:
        self.clear_chat()
        self.new_chat_requested.emit()

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:
            self.hide_requested.emit()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and e.position().y() < 66:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()
        super().mousePressEvent(e)

    def _shape(self) -> QPolygonF:
        w, h, c = float(self.width()), float(self.height()), float(CUT)
        return QPolygonF([QPointF(0.5, 0.5), QPointF(w - c, 0.5), QPointF(w - 0.5, c),
                          QPointF(w - 0.5, h - 0.5), QPointF(c, h - 0.5), QPointF(0.5, h - c)])

    def _rebuild_bg(self) -> None:
        pm = QPixmap(self.size())
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addPolygon(self._shape())
        path.closeSubpath()
        p.setClipPath(path)
        p.fillPath(path, BG)
        glow = QRadialGradient(self.width() * 0.2, self.height() * 0.4, self.width() * 0.55)
        glow.setColorAt(0.0, QColor(10, 70, 95, 90))
        glow.setColorAt(1.0, QColor(10, 70, 95, 0))
        p.fillRect(self.rect(), glow)
        p.setPen(QPen(QColor(47, 216, 255, 16), 1))
        for x in range(0, self.width(), 26):
            for y in range(0, self.height(), 26):
                p.drawPoint(x, y)
        p.setClipping(False)
        p.setPen(QPen(A(CYAN, 120), 1.2))
        p.drawPath(path)
        p.setPen(QPen(A(CYAN, 235), 2.2))
        w, h, c, L = float(self.width()), float(self.height()), float(CUT), 34.0
        p.drawLine(QPointF(1, 1), QPointF(1 + L, 1))
        p.drawLine(QPointF(1, 1), QPointF(1, 1 + L))
        p.drawLine(QPointF(w - 1, h - 1), QPointF(w - 1 - L, h - 1))
        p.drawLine(QPointF(w - 1, h - 1), QPointF(w - 1, h - 1 - L))
        p.drawLine(QPointF(w - c - 10, 1), QPointF(w - c, 1))
        p.drawLine(QPointF(w - c, 1), QPointF(w - 1, c))
        p.drawLine(QPointF(c, h - 1), QPointF(1, h - c))
        p.drawLine(QPointF(c, h - 1), QPointF(c + 10, h - 1))
        p.end()
        self._bg = pm

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self.grip.move(self.width() - self.grip.width() - 6, self.height() - self.grip.height() - 6)
        self._rebuild_bg()

    def paintEvent(self, _e) -> None:
        if self._bg is None or self._bg.size() != self.size():
            self._rebuild_bg()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._bg)


# --- floating orb ----------------------------------------------------------------
class FloatingOrb(QWidget):
    """Always-on-top mini arc core that bobs on the desktop while the Prime window is closed."""

    activated = Signal()
    open_requested = Signal()
    quit_requested = Signal()

    SIZE = 108
    R = 30

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self.SIZE, self.SIZE + 12)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.state = "idle"
        self._col = QColor(STATE_COL["idle"])
        self._t0 = time.monotonic()
        self._press: QPoint | None = None
        self._origin = QPoint()
        self._dragged = False
        self._anim: QPropertyAnimation | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(16)
        self.place_default()

    def set_state(self, state: str) -> None:
        self.state = state

    def place_default(self) -> None:
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.width() - 12, scr.bottom() - self.height() - 70)

    def paintEvent(self, _e) -> None:
        t = time.monotonic() - self._t0
        tgt = STATE_COL.get(self.state, CYAN)
        c = self._col
        self._col = QColor(int(c.red() + (tgt.red() - c.red()) * 0.1),
                           int(c.green() + (tgt.green() - c.green()) * 0.1),
                           int(c.blue() + (tgt.blue() - c.blue()) * 0.1))
        speed = 2.6 if self.state in ("speaking", "listening", "replying", "acting") else 1.5
        bob = math.sin(t * speed) * 4.5
        cx, cy = self.width() / 2, self.height() / 2 + bob - 4
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # dark disc so the core reads on any wallpaper
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(2, 10, 18, 215))
        p.drawEllipse(QPointF(cx, cy), self.R * 1.0, self.R * 1.0)
        paint_core(p, cx, cy, self.R, t, self.state, 0.0, self._col, detail=False)
        p.setPen(QPen(A(self._col, 150), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(cx, cy), self.R * 1.0, self.R * 1.0)
        shadow = QRadialGradient(cx, self.height() - 7, self.R * 0.8)
        shadow.setColorAt(0.0, QColor(0, 0, 0, 70))
        shadow.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(shadow)
        p.drawEllipse(QPointF(cx, self.height() - 7), self.R * 0.8, 4)

    # ---- mouse: click to open, drag to move, snap to a side ----
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._origin = self.pos()
            self._dragged = False
            if self._anim:
                self._anim.stop()

    def mouseMoveEvent(self, e) -> None:
        if self._press is None:
            return
        delta = e.globalPosition().toPoint() - self._press
        if self._dragged or delta.manhattanLength() > 5:
            self._dragged = True
            self.move(self._origin + delta)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() != Qt.MouseButton.LeftButton or self._press is None:
            return
        self._press = None
        if self._dragged:
            self._snap()
        else:
            self.activated.emit()

    def contextMenuEvent(self, e) -> None:
        m = QMenu(self)
        m.setStyleSheet(
            "QMenu{background:#06141d;color:#d4f3fb;border:1px solid rgba(47,216,255,120);padding:6px;}"
            "QMenu::item{padding:8px 22px;} QMenu::item:selected{background:#0e5a6d;}"
        )
        m.addAction("Open Prime", self.open_requested.emit)
        m.addSeparator()
        m.addAction("Quit Prime", self.quit_requested.emit)
        m.exec(e.globalPos())

    def _snap(self) -> None:
        scr = QGuiApplication.screenAt(self.geometry().center()) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()
        x = g.left() + 6 if self.geometry().center().x() < g.center().x() else g.right() - self.width() - 6
        y = min(max(self.y(), g.top() + 6), g.bottom() - self.height() - 6)
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(260)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setEndValue(QPoint(x, y))
        self._anim.start()


def make_app(argv: list[str]) -> QApplication:
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("Prime")
    app.setQuitOnLastWindowClosed(False)
    return app
