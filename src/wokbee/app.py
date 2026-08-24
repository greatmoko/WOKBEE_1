"""应用程序主类，负责初始化和生命周期管理。"""

import sys

from PySide6.QtWidgets import QApplication

from wokbee.ui.main_window import MainWindow
from wokbee.ui.styles.theme import Theme
from wokbee.ui.no_wheel import install_no_wheel_value_change, install_no_focus_frame_style
from wokbee.core.config import Config


class Application:
    """WokBee 应用程序入口类。"""

    def __init__(self):
        self.config = Config()
        self.theme = Theme()

        self.qt_app = QApplication.instance() or QApplication(sys.argv)
        self.qt_app.setStyle("Fusion")
        # 全局去掉焦点黑框，并禁止滚轮改动 SpinBox / ComboBox
        self._no_focus_style = install_no_focus_frame_style(self.qt_app)
        self._no_wheel_filter = install_no_wheel_value_change(self.qt_app)

        self.window = MainWindow(self.config, self.theme)

    def run(self) -> int:
        """启动应用主循环，返回退出码。"""
        self.window.show()
        return self.qt_app.exec()
