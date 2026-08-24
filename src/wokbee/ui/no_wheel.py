"""全局 UI 行为：禁止用鼠标滚轮改动数值框 / 下拉框的值；去掉焦点黑框。"""

from __future__ import annotations

from PySide6.QtCore import QObject, QEvent
from PySide6.QtWidgets import (
    QAbstractSpinBox, QComboBox, QAbstractScrollArea, QApplication,
    QProxyStyle, QStyle,
)


class NoFocusFrameStyle(QProxyStyle):
    """去掉控件焦点黑框（含下拉选项的 PE_FrameFocusRect）。"""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element == QStyle.PrimitiveElement.PE_FrameFocusRect:
            return
        super().drawPrimitive(element, option, painter, widget)


class NoWheelValueChangeFilter(QObject):
    """拦截 QSpinBox / QDoubleSpinBox / QComboBox 上的滚轮，避免滚动页面时误改参数。

    滚轮会转交给外层滚动区域；下拉弹出列表仍可正常滚轮浏览。
    """

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Wheel and isinstance(obj, (QAbstractSpinBox, QComboBox)):
            w = obj.parentWidget()
            while w is not None:
                if isinstance(w, QAbstractScrollArea):
                    QApplication.sendEvent(w.viewport(), event)
                    return True
                w = w.parentWidget()
            return True
        return super().eventFilter(obj, event)


def install_no_wheel_value_change(app: QApplication) -> NoWheelValueChangeFilter:
    """安装到 QApplication，全局生效。"""
    filt = NoWheelValueChangeFilter(app)
    app.installEventFilter(filt)
    return filt


def install_no_focus_frame_style(app: QApplication) -> NoFocusFrameStyle:
    style = NoFocusFrameStyle(app.style())
    app.setStyle(style)
    return style
