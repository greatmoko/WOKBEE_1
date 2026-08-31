"""应用程序主类，负责初始化和生命周期管理。"""

import ctypes
import os
import sys
from pathlib import Path

# 须在创建 QApplication 之前：屏蔽 Windows 上常见的无害字体探测告警
# （Fixedsys / 部分字体缺 OpenType script 支持时 Qt 会刷屏）
os.environ.setdefault(
    "QT_LOGGING_RULES",
    "qt.qpa.fonts.warning=false;qt.text.font.db.warning=false",
)

# Windows：独立 AppUserModelID，任务栏才显示应用图标而非 python.exe 图标
if sys.platform == "win32":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WokBee.App")
    except Exception:
        pass

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QStyleFactory

from tokbee.ui.main_window import MainWindow
from tokbee.ui.styles.theme import Theme
from tokbee.ui.no_wheel import install_no_wheel_value_change, install_no_focus_frame_style
from tokbee.core.config import Config
from wokbee.core.settings import WokBeeSettings
from wokbee.engine.runtime_env import ensure_runtime_env_async

_RESOURCES = Path(__file__).parent / "resources"


def load_app_icon() -> QIcon:
    """加载应用图标：优先多尺寸 ICO，并叠 logo.png，保证任务栏小图标清晰。"""
    icon = QIcon()
    ico_path = _RESOURCES / "icon.ico"
    logo_path = _RESOURCES / "logo.png"
    if ico_path.exists():
        icon.addFile(str(ico_path))
    if logo_path.exists():
        pm = QPixmap(str(logo_path))
        if not pm.isNull():
            for size in (16, 24, 32, 48, 64, 128, 256):
                icon.addPixmap(
                    pm.scaled(
                        size,
                        size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
    return icon


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


def _pick_qt_style() -> str:
    """Windows 用系统原生样式，Tooltip 由 OS 绘制；其它平台用 Fusion。"""
    if sys.platform == "win32":
        for name in ("windowsvista", "Windows", "Fusion"):
            if QStyleFactory.create(name) is not None:
                return name
    return "Fusion"


class Application:
    """WokBee 应用程序入口类。"""

    def __init__(self):
        self.config = Config()
        self.theme = Theme()

        self.qt_app = QApplication.instance() or QApplication(sys.argv)
        self.qt_app.setStyle(_pick_qt_style())
        self.qt_app.setApplicationName("WokBee")
        self.qt_app.setFont(_pick_ui_font())
        app_icon = load_app_icon()
        if not app_icon.isNull():
            self.qt_app.setWindowIcon(app_icon)
        # 全局去掉焦点黑框，并禁止滚轮改动 SpinBox / ComboBox
        self._no_focus_style = install_no_focus_frame_style(self.qt_app)
        self._no_wheel_filter = install_no_wheel_value_change(self.qt_app)

        self.window = MainWindow(self.config, self.theme)
        if not app_icon.isNull():
            self.window.setWindowIcon(app_icon)
        # 仅当环境缓存为空时在后台探测一次，不阻塞 UI
        ensure_runtime_env_async(WokBeeSettings(self.config))

    def run(self) -> int:
        """启动应用主循环，返回退出码。"""
        self.window.show()
        # 窗口先显示，再在后台线程预加载引擎（deepagents 栈），避免拖慢启动
        from wokbee.engine import start_engine_warmup

        start_engine_warmup()
        return self.qt_app.exec()
