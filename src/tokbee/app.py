"""应用程序主类，负责初始化和生命周期管理。"""

import os
import sys
from pathlib import Path

# 须在创建 QApplication 之前：屏蔽 Windows 上常见的无害字体探测告警
# （Fixedsys / 部分字体缺 OpenType script 支持时 Qt 会刷屏）
os.environ.setdefault(
    "QT_LOGGING_RULES",
    "qt.qpa.fonts.warning=false;qt.text.font.db.warning=false",
)

from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication

from tokbee.ui.main_window import MainWindow
from tokbee.ui.styles.theme import Theme
from tokbee.ui.no_wheel import install_no_wheel_value_change, install_no_focus_frame_style
from tokbee.core.config import Config

_RESOURCES = Path(__file__).parent / "resources"


def _pick_ui_font() -> QFont:
    """优先中易黑体 / 雅黑，避免落到损坏的 Fixedsys 等系统等宽字体。"""
    preferred = (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "Segoe UI",
        "PingFang SC",
        "Noto Sans CJK SC",
        "Sans Serif",
    )
    families = set(QFontDatabase.families())
    for name in preferred:
        if name in families:
            font = QFont(name, 10)
            font.setStyleHint(QFont.StyleHint.SansSerif)
            return font
    font = QFont()
    font.setPointSize(10)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    return font


class Application:
    """WokBee 应用程序入口类。"""

    def __init__(self):
        self.config = Config()
        self.theme = Theme()

        self.qt_app = QApplication.instance() or QApplication(sys.argv)
        self.qt_app.setStyle("Fusion")
        self.qt_app.setApplicationName("WokBee")
        self.qt_app.setFont(_pick_ui_font())
        icon_path = _RESOURCES / "icon.ico"
        if icon_path.exists():
            self.qt_app.setWindowIcon(QIcon(str(icon_path)))
        # 全局去掉焦点黑框，并禁止滚轮改动 SpinBox / ComboBox
        self._no_focus_style = install_no_focus_frame_style(self.qt_app)
        self._no_wheel_filter = install_no_wheel_value_change(self.qt_app)

        self.window = MainWindow(self.config, self.theme)

    def run(self) -> int:
        """启动应用主循环，返回退出码。"""
        self.window.show()
        return self.qt_app.exec()
