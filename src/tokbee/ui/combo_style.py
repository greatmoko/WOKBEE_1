"""QComboBox 下拉列表样式：去掉选项焦点黑框，统一悬停底色。"""

from __future__ import annotations

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QStyledItemDelegate, QStyle, QStyleOptionViewItem, QComboBox, QFrame,
)


class _NoFocusDelegate(QStyledItemDelegate):
    """完全自绘选项，避免系统/样式再画焦点黑框。"""

    def __init__(self, parent=None, *, hover_color: str = "#e5e5e5", text_color: str = "#1a1a1a"):
        super().__init__(parent)
        self._hover = QColor(hover_color)
        self._text = QColor(text_color)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        rect = opt.rect
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if hovered or selected:
            painter.fillRect(rect, self._hover)

        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text is None:
            text = ""
        painter.save()
        painter.setPen(self._text)
        painter.drawText(
            rect.adjusted(10, 0, -10, 0),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            str(text),
        )
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), 28))


def apply_combo_popup_style(combo: QComboBox, colors: dict):
    """对下拉弹出的 QListView 强制应用无边框悬停样式。"""
    hover = colors.get("btn_hover", "#e5e5e5")
    bg = colors.get("content_bg", "#ffffff")
    text = colors.get("text", "#1a1a1a")
    border = colors.get("input_border", "#e0e0e0")

    view = combo.view()
    view.setItemDelegate(_NoFocusDelegate(view, hover_color=hover, text_color=text))
    view.setFrameShape(QFrame.Shape.NoFrame)
    view.setMouseTracking(True)
    view.setStyleSheet(f"""
        QAbstractItemView, QListView {{
            background: {bg};
            border: 1px solid {border};
            outline: 0px;
        }}
        QAbstractItemView::item, QListView::item {{
            border: none;
            outline: 0px;
        }}
    """)
