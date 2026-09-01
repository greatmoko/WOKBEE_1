"""主窗口布局 — 三栏结构：一级导航 | 二级导航 | 工作区。"""

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QPushButton, QStackedWidget, QFrame,
)

from tokbee import __app_name__, __version__
from tokbee.core.config import Config
from tokbee.ui.styles.theme import Theme

_RESOURCES = Path(__file__).parent.parent / "resources"


# ────────────────────────── 一级导航栏 ──────────────────────────

class _PrimaryNav(QFrame):
    """最左侧一级导航栏 — 数据驱动，循环构建。"""

    nav_changed = Signal(str)

    # (nav_id, icon_key, label_text, font_size, position)
    # icon_key: "chat" 消息图标 / "logo" 蜜蜂 logo / 其它为 emoji 文本
    # position: "top" 正常顺序, "bottom" 贴底
    NAV_ITEMS = [
        ("chat",       "chat",  "TokBee",  18, "top"),
        ("wokbee",     "🐝",    "WokBee",  18, "top"),
        ("autobee",    "⏰",    "AutoBee", 18, "top"),
        ("automation", "⚡",    "AIConfig",  20, "top"),
        ("settings",   "⚙",    "Settings",    22, "bottom"),
    ]

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._current = ""
        self._buttons: dict[str, QPushButton] = {}
        self._labels: dict[str, QLabel] = {}
        self._font_sizes: dict[str, int] = {}
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setFixedWidth(56)
        self.setObjectName("primaryNav")
        self.setStyleSheet(f"""
            QFrame#primaryNav {{
                background: {c["sidebar_bg"]};
                border: none;
                border-right: 1px solid {c["border"]};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        top_items = [i for i in self.NAV_ITEMS if i[4] == "top"]
        bottom_items = [i for i in self.NAV_ITEMS if i[4] == "bottom"]

        for idx, (nav_id, icon_text, label_text, font_size, _) in enumerate(top_items):
            if idx > 0:
                layout.addSpacing(6)
            btn, lbl = self._create_nav_button(nav_id, icon_text, label_text, font_size)
            layout.addLayout(self._make_nav_item(btn, lbl))

        layout.addStretch()

        for nav_id, icon_text, label_text, font_size, _ in bottom_items:
            btn, lbl = self._create_nav_button(nav_id, icon_text, label_text, font_size)
            layout.addLayout(self._make_nav_item(btn, lbl))

        self._apply_styles()

    def _create_nav_button(self, nav_id: str, icon_key: str | None,
                           label_text: str, font_size: int) -> tuple[QPushButton, QLabel]:
        btn = QPushButton()
        btn.setFixedSize(40, 40)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(label_text)
        btn.setAutoDefault(False)
        btn.setDefault(False)

        if icon_key == "logo":
            logo_path = _RESOURCES / "logo.png"
            if logo_path.exists():
                pm = QPixmap(str(logo_path)).scaled(
                    30, 30,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                btn.setIcon(QIcon(pm))
                btn.setIconSize(QSize(30, 30))
            else:
                btn.setText("🐝")
        elif icon_key == "chat":
            chat_path = _RESOURCES / "chat.png"
            if chat_path.exists():
                pm = QPixmap(str(chat_path)).scaled(
                    28, 28,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                btn.setIcon(QIcon(pm))
                btn.setIconSize(QSize(28, 28))
            else:
                btn.setText("💬")
        elif icon_key:
            btn.setText(icon_key)
        else:
            btn.setText("•")

        btn.clicked.connect(lambda _, nid=nav_id: self._on_click(nid))

        lbl = QLabel(label_text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("background: transparent; border: none;")

        self._buttons[nav_id] = btn
        self._labels[nav_id] = lbl
        self._font_sizes[nav_id] = font_size
        return btn, lbl

    @staticmethod
    def _make_nav_item(btn: QPushButton, label: QLabel) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        col.addWidget(btn, alignment=Qt.AlignmentFlag.AlignHCenter)
        col.addWidget(label, alignment=Qt.AlignmentFlag.AlignHCenter)
        return col

    def _apply_styles(self):
        c = self.theme.colors
        # 与二级导航一致：浅绿底 + 绿色文字
        for nav_id, btn in self._buttons.items():
            active = self._current == nav_id
            bg = c["sidebar_active"] if active else "transparent"
            color = c["sidebar_text_active"] if active else c["sidebar_text"]
            fs = self._font_sizes.get(nav_id, 20)

            btn.setStyleSheet(f"""
                QPushButton {{
                    background: {bg};
                    color: {color};
                    border: none;
                    border-radius: 8px;
                    font-size: {fs}px;
                    outline: none;
                }}
                QPushButton:hover {{
                    background: {c["sidebar_hover"]};
                }}
                QPushButton:focus {{ outline: none; border: none; }}
            """)
            self._labels[nav_id].setStyleSheet(
                f"font-size: 9px; background: transparent; border: none; color: {color};"
            )
    def _on_click(self, nav_id: str):
        if nav_id == self._current:
            return
        self._current = nav_id
        self._apply_styles()
        self.nav_changed.emit(nav_id)

    def select(self, nav_id: str):
        self._on_click(nav_id)


# ────────────────────────── 主窗口 ──────────────────────────

class MainWindow(QMainWindow):
    """主窗口：一级导航 | 二级导航+工作区。"""

    def __init__(self, config: Config, theme: Theme):
        super().__init__()
        self.theme = theme
        self._views: dict[str, QWidget] = {}

        from tokbee.core.services import ServiceRegistry
        self._services = ServiceRegistry(config)

        self._setup_window()
        self._build_layout()
        self._register_views()
        self._set_titlebar_color()

        self._nav.select("chat")

    def _setup_window(self):
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.resize(960, 640)
        self.setMinimumSize(QSize(720, 480))

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
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.setStyleSheet(self.theme.stylesheet())

    def _build_layout(self):
        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self._nav = _PrimaryNav(self.theme)
        self._nav.nav_changed.connect(self._switch_view)
        root_layout.addWidget(self._nav)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet("background: transparent;")
        root_layout.addWidget(self._stack, stretch=1)

    def _register_views(self):
        from tokbee.ui.views.chat_view import ChatView
        from tokbee.ui.views.settings_view import SettingsView
        from tokbee.ui.views.automation_view import AutomationView
        from tokbee.core.provider_store import ProviderStore
        from wokbee.ui.wokbee_view import WokBeeView
        from autobee.ui.autobee_view import AutoBeeView

        svc = self._services

        chat = ChatView(self.theme, svc.role_manager)
        wokbee = WokBeeView(
            self.theme,
            store=svc.wokbee_store,
            settings=svc.wokbee_settings,
            gateway_manager=svc.gateway_manager,
        )
        settings = SettingsView(
            self.theme, chat.manager, project_store=svc.wokbee_store
        )
        settings.chats_cleared.connect(chat.refresh_after_clear)
        settings.projects_cleared.connect(wokbee.refresh_after_projects_cleared)

        automation = AutomationView(
            self.theme,
            svc.role_manager,
            wokbee_settings=svc.wokbee_settings,
            gateway_manager=svc.gateway_manager,
        )
        autobee = AutoBeeView(
            self.theme,
            store=svc.autobee_store,
            scheduler=svc.autobee_scheduler,
            provider_store=ProviderStore(),
            project_store=svc.wokbee_store,
        )

        self._views["chat"] = chat
        self._views["wokbee"] = wokbee
        self._views["autobee"] = autobee
        self._views["automation"] = automation
        self._views["settings"] = settings

        self._stack.addWidget(chat)
        self._stack.addWidget(wokbee)
        self._stack.addWidget(autobee)
        self._stack.addWidget(automation)
        self._stack.addWidget(settings)

        # 启动定时任务调度（幂等）
        svc.autobee_scheduler.start()
        # 启动消息网关（幂等；未启用会打印提示而不报错）
        svc.gateway_manager.start()

    def _switch_view(self, nav_id: str):
        view = self._views.get(nav_id)
        if view:
            self._stack.setCurrentWidget(view)

    def closeEvent(self, event):
        try:
            self._services.autobee_scheduler.shutdown(wait=False)
        except Exception:
            pass
        try:
            self._services.gateway_manager.shutdown(wait=False)
        except Exception:
            pass
        # 先收尾后台线程（agent/lesson/压缩/改名），避免 QThread 仍在运行时销毁
        wokbee = self._views.get("wokbee")
        if wokbee is not None:
            try:
                wokbee.shutdown()
            except Exception:
                pass
        super().closeEvent(event)

    def _set_titlebar_color(self):
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            color = 0x00FFFFFF
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 35,
                ctypes.byref(ctypes.c_int(color)),
                ctypes.sizeof(ctypes.c_int),
            )
        except Exception:
            pass
