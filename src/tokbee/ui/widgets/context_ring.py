"""上下文用量环形指示器（Cursor 风格）。点击触发压缩。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import QWidget

from tokbee.ui.styles.theme import Theme


class ContextUsageRing(QWidget):
    """细圆环：弧长表示 used/limit；点击请求压缩。"""

    compress_clicked = Signal()

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._ratio = 0.0
        self._used = 0
        self._limit = 0
        self._enabled_ring = True
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("上下文用量")
        self._update_tooltip()

    def set_usage(self, used: int, limit: int):
        self._used = max(0, int(used))
        self._limit = max(0, int(limit))
        self._ratio = (self._used / self._limit) if self._limit > 0 else 0.0
        self._update_tooltip()
        self.update()

    def set_ring_enabled(self, enabled: bool):
        self._enabled_ring = enabled
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
        )
        self._update_tooltip()

    def _update_tooltip(self):
        if self._limit <= 0:
            self.setToolTip("未设置模型上下文窗口\n可在「厂商设置」中为模型填写上下文 tokens")
            return
        pct = min(self._ratio * 100.0, 999.0)
        tip = f"上下文用量 {self._format(self._used)} / {self._format(self._limit)}（{pct:.1f}%）"
        if self._enabled_ring:
            tip += "\n点击压缩对话上下文"
        self.setToolTip(tip)

    @staticmethod
    def _format(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    def _progress_color(self) -> QColor:
        c = self.theme.colors
        if self._ratio >= 0.85:
            return QColor(c.get("danger", "#f56c6c"))
        if self._ratio >= 0.60:
            return QColor(c.get("warning", "#faad14"))
        return QColor(c.get("text_secondary", "#666666"))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.theme.colors
        track = QColor(c.get("border", "#e5e5e5"))
        track.setAlpha(180)

        margin = 2.0
        rect = QRectF(margin, margin, self.width() - 2 * margin, self.height() - 2 * margin)
        pen_w = 2.2

        track_pen = QPen(track, pen_w)
        track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(track_pen)
        p.drawEllipse(rect)

        ratio = min(max(self._ratio, 0.0), 1.0)
        if ratio > 0.001 and self._limit > 0:
            prog_pen = QPen(self._progress_color(), pen_w)
            prog_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(prog_pen)
            # Qt: 0° = 3 点钟，逆时针为正。要做 12 点顺时针：
            # start = 90°*16，span = -ratio*360*16
            start = 90 * 16
            span = int(-ratio * 360 * 16)
            p.drawArc(rect, start, span)

        p.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._enabled_ring:
            self.compress_clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)
