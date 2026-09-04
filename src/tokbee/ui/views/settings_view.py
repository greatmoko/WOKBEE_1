"""设置页面 — 左侧二级导航 + 右侧工作区。"""

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame,
    QLabel, QPushButton, QStackedWidget, QScrollArea,
    QDialog, QTextEdit,
)

from tokbee.ui.styles.theme import Theme
from tokbee.ui.styles.system import (
    apply_danger_btn,
    apply_textedit,
    apply_secondary_btn,
    style_hint_label,
)
from tokbee.core.chat_manager import ChatManager
from wokbee.core.project_store import ProjectStore, TRASH_RETENTION_DAYS
from wokbee.core.settings import WokBeeSettings
from wokbee.engine.runtime_env import (
    build_runtime_env_settings_text,
    ensure_runtime_env,
)


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


def _danger_action_qss(theme: "Theme") -> str:
    c = theme.colors
    return f"""
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
    """


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
    """通用设置页 — 聊天记录管理、本机运行环境等。"""

    chats_cleared = Signal()
    projects_cleared = Signal()
    _env_text_ready = Signal(str)
    _memory_reset_ready = Signal(bool, str)

    def __init__(
        self,
        theme: Theme,
        chat_manager: ChatManager,
        project_store: ProjectStore | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.manager = chat_manager
        self.project_store = project_store
        self._wokbee_settings = WokBeeSettings()
        self._env_text_ready.connect(self._apply_env_text)
        self._memory_reset_ready.connect(self._on_memory_reset_done)
        self._build()
        self._reload_env_text()

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
        clear_btn.setStyleSheet(_danger_action_qss(self.theme))
        clear_btn.clicked.connect(self._confirm_clear)
        row.addWidget(clear_btn)

        layout.addLayout(row)

        wb_sep = QFrame()
        wb_sep.setFrameShape(QFrame.Shape.HLine)
        wb_sep.setStyleSheet(f"color: {c['border_light']};")
        layout.addWidget(wb_sep)

        wb_group = QLabel("WokBee 项目记录管理")
        wb_group.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {c['text']}; margin-top: 4px;")
        layout.addWidget(wb_group)

        wb_row = QHBoxLayout()
        wb_row.setSpacing(12)
        wb_desc = QLabel(
            f"删除所有未置顶项目（移入工作区 _trash，与 WokBee 页面删除一致），"
            f"置顶项目不受影响。回收站最多保留 {TRASH_RETENTION_DAYS} 天。"
        )
        wb_desc.setWordWrap(True)
        style_hint_label(wb_desc, c)
        wb_row.addWidget(wb_desc, stretch=1)

        wb_btn = QPushButton("删除非置顶项目")
        wb_btn.setFixedHeight(34)
        wb_btn.setStyleSheet(_danger_action_qss(self.theme))
        wb_btn.setToolTip("与 WokBee 侧栏删除相同：未置顶移入回收站，置顶跳过")
        wb_btn.clicked.connect(self._confirm_clear_projects)
        wb_row.addWidget(wb_btn)
        layout.addLayout(wb_row)

        memory_sep = QFrame()
        memory_sep.setFrameShape(QFrame.Shape.HLine)
        memory_sep.setStyleSheet(f"color: {c['border_light']};")
        layout.addWidget(memory_sep)

        memory_group = QLabel("Agent 记忆管理")
        memory_group.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {c['text']}; margin-top: 4px;"
        )
        layout.addWidget(memory_group)

        memory_row = QHBoxLayout()
        memory_row.setSpacing(12)
        memory_desc = QLabel(
            "清空全局记忆仓库，并将记忆概述恢复为系统初始版本。项目目录中的经验文件不受影响。"
        )
        memory_desc.setWordWrap(True)
        style_hint_label(memory_desc, c)
        memory_row.addWidget(memory_desc, stretch=1)
        self._memory_reset_btn = QPushButton("一键重置记忆")
        self._memory_reset_btn.setFixedHeight(34)
        self._memory_reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._memory_reset_btn.setStyleSheet(_danger_action_qss(self.theme))
        self._memory_reset_btn.setToolTip("清空记忆仓库，并恢复初始记忆概述")
        self._memory_reset_btn.clicked.connect(self._confirm_reset_memory)
        memory_row.addWidget(self._memory_reset_btn)
        layout.addLayout(memory_row)

        env_sep = QFrame()
        env_sep.setFrameShape(QFrame.Shape.HLine)
        env_sep.setStyleSheet(f"color: {c['border_light']};")
        layout.addWidget(env_sep)

        env_group = QLabel("本机运行环境")
        env_group.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {c['text']}; margin-top: 4px;")
        layout.addWidget(env_group)

        env_hint = QLabel(
            "首次为空时自动探测并保存到本机配置；之后所有 Agent 直接加载此缓存，不会每次重扫。"
            "安装新软件后可点「重新探测」刷新。"
        )
        env_hint.setWordWrap(True)
        style_hint_label(env_hint, c)
        layout.addWidget(env_hint)

        self._env_edit = QTextEdit()
        self._env_edit.setReadOnly(True)
        self._env_edit.setMinimumHeight(220)
        apply_textedit(self._env_edit, c)
        layout.addWidget(self._env_edit)

        env_btn_row = QHBoxLayout()
        env_btn_row.addStretch()
        self._env_refresh_btn = QPushButton("重新探测")
        self._env_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        apply_secondary_btn(self._env_refresh_btn, c, height=34)
        self._env_refresh_btn.clicked.connect(self._refresh_env)
        env_btn_row.addWidget(self._env_refresh_btn)
        layout.addLayout(env_btn_row)

        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    def showEvent(self, event):
        super().showEvent(event)
        self._reload_env_text()

    def _reload_env_text(self):
        self._apply_env_text(build_runtime_env_settings_text(self._wokbee_settings))

    def _apply_env_text(self, text: str):
        self._env_edit.setPlainText(text)
        self._env_refresh_btn.setEnabled(True)

    def _refresh_env(self):
        self._env_refresh_btn.setEnabled(False)
        self._env_edit.setPlainText("正在重新探测本机环境，请稍候…")

        settings = self._wokbee_settings

        def _worker():
            try:
                ensure_runtime_env(settings, force=True)
                text = build_runtime_env_settings_text(settings)
            except Exception as e:
                text = f"探测失败：{e}"
            self._env_text_ready.emit(text)

        threading.Thread(target=_worker, daemon=True, name="settings-runtime-env").start()

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

    def _confirm_clear_projects(self):
        if self.project_store is None:
            _show_styled_tip(self, self.theme, "项目存储未就绪")
            return
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("确认删除")
        dlg.setFixedSize(420, 200)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        msg = QLabel(
            "确定删除所有未置顶的 WokBee 项目吗？\n\n"
            f"与 WokBee 页面删除相同：移入工作区 _trash，回收站最多保留 "
            f"{TRASH_RETENTION_DAYS} 天。置顶与运行中的项目不会删除。"
        )
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
        apply_danger_btn(confirm_btn, c, height=32)
        confirm_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(confirm_btn)

        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            count = self.project_store.delete_unpinned(trash=True)
            self.projects_cleared.emit()
            self._show_result(count, kind="projects")

    def _confirm_reset_memory(self):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("确认重置记忆")
        dlg.setFixedSize(440, 220)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)
        msg = QLabel(
            "确定要重置 Agent 记忆吗？\n\n"
            "这会清空全局记忆仓库，并覆盖记忆概述为系统初始版本。\n"
            "项目目录中的 memory/experiences/ 与 scripts/ 不受影响。"
        )
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
                background: {c["card_bg"]}; color: {c["text"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)
        confirm_btn = QPushButton("确认重置")
        confirm_btn.setFixedSize(100, 32)
        confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        confirm_btn.setStyleSheet(_danger_action_qss(self.theme))
        confirm_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(confirm_btn)
        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._memory_reset_btn.setEnabled(False)
            self._memory_reset_btn.setText("正在重置…")
            threading.Thread(
                target=self._reset_memory_worker,
                daemon=True,
                name="settings-reset-memory",
            ).start()

    def _reset_memory_worker(self):
        try:
            from wokbee.engine.agent_memory import reset_memory

            reset_memory()
            self._memory_reset_ready.emit(
                True, "Agent 记忆已重置，记忆概述已恢复为初始版本。"
            )
        except Exception as e:  # noqa: BLE001
            self._memory_reset_ready.emit(False, f"重置 Agent 记忆失败：{e}")

    def _on_memory_reset_done(self, _ok: bool, message: str):
        self._memory_reset_btn.setEnabled(True)
        self._memory_reset_btn.setText("一键重置记忆")
        _show_styled_tip(self, self.theme, message)

    def _show_result(self, count: int, *, kind: str = "chats"):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("提示")
        dlg.setFixedSize(300, 120)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        if kind == "projects":
            text = (
                f"已将 {count} 个未置顶项目移入回收站。"
                if count > 0
                else "没有需要删除的未置顶项目。"
            )
        else:
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
    projects_cleared = Signal()

    def __init__(
        self,
        theme: Theme,
        chat_manager: ChatManager,
        project_store: ProjectStore | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self._chat_manager = chat_manager
        self._project_store = project_store
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

        general_page = _GeneralWorkspace(
            self.theme, self._chat_manager, project_store=self._project_store
        )
        general_page.chats_cleared.connect(self.chats_cleared)
        general_page.projects_cleared.connect(self.projects_cleared)
        self._pages["general"] = general_page
        self._stack.addWidget(general_page)

        self._subnav.select("general")

    def _switch_page(self, nav_id: str):
        page = self._pages.get(nav_id)
        if page:
            self._stack.setCurrentWidget(page)
