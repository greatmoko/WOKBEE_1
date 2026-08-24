"""设置页面 — 左侧二级导航 + 右侧工作区。"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame,
    QLabel, QPushButton, QStackedWidget, QScrollArea,
    QDialog,
)

from wokbee.ui.styles.theme import Theme
from wokbee.core.chat_manager import ChatManager


def _show_styled_tip(parent: QWidget, theme: "Theme", message: str):
    """统一的提示弹窗，替代 QMessageBox。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("提示")
    dlg.setFixedSize(340, 140)
    dlg.setStyleSheet(f"background: {c['content_bg']};")

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(16)

    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
    layout.addWidget(msg)

    layout.addStretch()

    btn_row = QHBoxLayout()
    btn_row.addStretch()
    ok_btn = QPushButton("知道了")
    ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    ok_btn.setFixedSize(80, 34)
    ok_btn.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    ok_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(ok_btn)
    layout.addLayout(btn_row)

    dlg.exec()


class _SubNavButton(QPushButton):
    """二级导航按钮：左侧图标 + 右侧文字。"""

    def __init__(self, icon_text: str, label: str, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._active = False
        self._icon_text = icon_text
        self._label = label
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(36)
        self.setText(f"  {icon_text}   {label}")
        self._apply_style()

    def _apply_style(self):
        c = self._theme.colors
        if self._active:
            bg = c["subnav_active"]
            tc = c["subnav_text_active"]
            fw = "bold"
        else:
            bg = "transparent"
            tc = c["subnav_text"]
            fw = "normal"
        self.setStyleSheet(f"""
            QPushButton {{
                background: {bg};
                color: {tc};
                font-weight: {fw};
                border: none;
                border-radius: 6px;
                padding: 0 12px;
                text-align: left;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {c["subnav_hover"]};
            }}
        """)

    def set_active(self, active: bool):
        self._active = active
        self._apply_style()


# ═══════════════════════════════════════════════════════════════
# 二级导航面板
# ═══════════════════════════════════════════════════════════════

class _SubNav(QFrame):
    """设置页的二级导航栏。"""

    nav_changed = Signal(str)

    ITEMS = [
        ("general", "⚙", "通用设置"),
    ]

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._buttons: dict[str, _SubNavButton] = {}
        self._current = ""
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setMinimumWidth(180)
        self.setMaximumWidth(220)
        self.setStyleSheet(f"""
            _SubNav {{
                background: {c["subnav_bg"]};
                border-right: 1px solid {c["border"]};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(2)

        for nav_id, icon, label in self.ITEMS:
            btn = _SubNavButton(icon, label, self.theme)
            btn.clicked.connect(lambda _, nid=nav_id: self._on_click(nid))
            layout.addWidget(btn)
            self._buttons[nav_id] = btn

        layout.addStretch()

    def _on_click(self, nav_id: str):
        if nav_id == self._current:
            return
        self._current = nav_id
        for nid, btn in self._buttons.items():
            btn.set_active(nid == nav_id)
        self.nav_changed.emit(nav_id)

    def select(self, nav_id: str):
        self._on_click(nav_id)


# ═══════════════════════════════════════════════════════════════
# 模型卡片
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════

class _GeneralWorkspace(QWidget):
    """通用设置页 — 聊天记录管理等。"""

    chats_cleared = Signal()

    def __init__(self, theme: Theme, chat_manager: ChatManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.manager = chat_manager
        self._build()

    def _build(self):
        c = self.theme.colors

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(20)

        title = QLabel("通用设置")
        title.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {c['text']};")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {c['border_light']};")
        layout.addWidget(sep)

        group_label = QLabel("聊天记录管理")
        group_label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {c['text']}; margin-top: 4px;")
        layout.addWidget(group_label)

        row = QHBoxLayout()
        row.setSpacing(12)
        desc = QLabel("删除所有未置顶的聊天记录，置顶对话不受影响。")
        desc.setStyleSheet(f"font-size: 13px; color: {c['text_secondary']};")
        desc.setWordWrap(True)
        row.addWidget(desc, stretch=1)

        clear_btn = QPushButton("清空非置顶聊天")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setFixedHeight(34)
        clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["danger"]};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 0 16px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {c.get("danger_hover", "#c0392b")};
            }}
        """)
        clear_btn.clicked.connect(self._confirm_clear)
        row.addWidget(clear_btn)

        layout.addLayout(row)
        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _confirm_clear(self):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("确认清空")
        dlg.setFixedSize(380, 170)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        msg = QLabel("确定要删除所有非置顶的聊天记录吗？\n\n此操作不可撤销，置顶对话不受影响。")
        msg.setWordWrap(True)
        msg.setStyleSheet(f"font-size: 13px; color: {c['text']};")
        layout.addWidget(msg)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(80, 32)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["card_bg"]};
                color: {c["text"]};
                border: 1px solid {c["border"]};
                border-radius: 6px;
                font-size: 13px;
            }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        confirm_btn = QPushButton("确认删除")
        confirm_btn.setFixedSize(100, 32)
        confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["danger"]};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {c.get("danger_hover", "#c0392b")};
            }}
        """)
        confirm_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(confirm_btn)

        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            count = self.manager.delete_unpinned()
            self.chats_cleared.emit()
            self._show_result(count)

    def _show_result(self, count: int):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("提示")
        dlg.setFixedSize(300, 120)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        text = f"已清空 {count} 条非置顶聊天记录。" if count > 0 else "没有需要清空的聊天记录。"
        msg = QLabel(text)
        msg.setStyleSheet(f"font-size: 13px; color: {c['text']};")
        layout.addWidget(msg)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("知道了")
        ok_btn.setFixedSize(80, 32)
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                font-size: 13px;
            }}
        """)
        ok_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        dlg.exec()


# ═══════════════════════════════════════════════════════════════
# 设置页面整体（二级导航 + 工作区）
# ═══════════════════════════════════════════════════════════════

class SettingsView(QWidget):
    """设置页：左侧二级导航 + 右侧工作区。"""

    chats_cleared = Signal()

    def __init__(self, theme: Theme, chat_manager: ChatManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._chat_manager = chat_manager
        self._pages: dict[str, QWidget] = {}
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._subnav = _SubNav(self.theme)
        self._subnav.nav_changed.connect(self._switch_page)
        layout.addWidget(self._subnav)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {self.theme.colors['content_bg']};")
        layout.addWidget(self._stack, stretch=1)

        general_page = _GeneralWorkspace(self.theme, self._chat_manager)
        general_page.chats_cleared.connect(self.chats_cleared)
        self._pages["general"] = general_page
        self._stack.addWidget(general_page)

        self._subnav.select("general")

    def _switch_page(self, nav_id: str):
        page = self._pages.get(nav_id)
        if page:
            self._stack.setCurrentWidget(page)
