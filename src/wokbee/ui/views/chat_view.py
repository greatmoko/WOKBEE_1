"""AI 对话页面 — 左侧对话列表 + 右侧对话工作区。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import logging

from PySide6.QtCore import Qt, Signal, QThread, QEvent, QTimer, QSize
from PySide6.QtGui import QPixmap, QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame,
    QLabel, QPushButton, QScrollArea, QTextEdit, QTextBrowser,
    QLineEdit, QMenu, QDialog, QSpinBox, QDoubleSpinBox,
    QCheckBox, QComboBox, QFileDialog, QSizePolicy,
)

from wokbee.ui.styles.theme import Theme
from wokbee.ui.viewmodels.chat_viewmodel import ChatViewModel
from wokbee.core.chat_manager import ChatManager, ChatSession
from wokbee.core.model_config import ModelConfigManager, ModelEntry
from wokbee.core.ai_client import AIClient
from wokbee.core.chat_params import ChatParams
from wokbee.core.ai_role import AIRoleManager
from wokbee.core.file_reader import (
    is_image, is_document, read_image_as_base64, read_file_as_text,
    build_file_filter,
)

logger = logging.getLogger("wokbee")

_RESOURCES = Path(__file__).parent.parent.parent / "resources"


class _AutoHeightBrowser(QTextBrowser):
    """QTextBrowser that auto-sizes its height to fit all content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self.document().contentsChanged.connect(self._update_height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()

    def _update_height(self):
        doc = self.document()
        doc.setTextWidth(self.viewport().width())
        margins = self.contentsMargins()
        h = int(doc.size().height()) + margins.top() + margins.bottom() + 2 * self.frameWidth()
        self.setFixedHeight(max(h, 30))

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self.minimumHeight() or 30)


class _AiNameWorker(QThread):
    """后台线程：调用 AI 生成对话标题。"""
    finished = Signal(str)
    failed = Signal(str)

    def __init__(self, client: AIClient, messages: list[dict], parent=None):
        super().__init__(parent)
        self._client = client
        self._messages = messages

    def run(self):
        try:
            resp = self._client.chat(self._messages, temperature=0.3, max_tokens=64)
            content = resp.content or ""
            if not content.strip() and resp.reasoning_content:
                content = resp.reasoning_content
            self.finished.emit(content)
        except Exception as e:
            self.failed.emit(str(e))


class _AIChatWorker(QThread):
    """后台线程：调用 AI chat/completions API（支持流式/非流式）。"""

    chunk_received = Signal(str, str, str)   # (session_id, content_delta, reasoning_delta)
    non_stream_done = Signal(str, str, str)  # (session_id, content, reasoning_content)
    stream_done = Signal(str)                # (session_id,)
    error = Signal(str, str)                 # (session_id, error_msg)

    def __init__(self, session_id: str, endpoint: str, api_key: str, model: str,
                 messages: list[dict], *, temperature: float = 0.7,
                 top_p: float = 1.0, max_tokens: int = 4096,
                 stream: bool = True, disable_thinking: bool = False,
                 reasoning_effort: str = "",
                 frequency_penalty: float = 0.0, presence_penalty: float = 0.0,
                 top_k: int = 0, stop: list[str] | None = None,
                 parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self._messages = messages
        self._temperature = temperature
        self._top_p = top_p
        self._max_tokens = max_tokens
        self._stream = stream
        self._disable_thinking = disable_thinking
        self._reasoning_effort = reasoning_effort
        self._frequency_penalty = frequency_penalty
        self._presence_penalty = presence_penalty
        self._top_k = top_k
        self._stop = stop
        self._cancelled = False
        self.accumulated_content = ""
        self.accumulated_reasoning = ""

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            client = AIClient(self._endpoint, self._api_key, self._model,
                              disable_thinking=self._disable_thinking,
                              reasoning_effort=self._reasoning_effort)
            if self._stream:
                self._run_stream(client)
            else:
                self._run_sync(client)
        except Exception as e:
            logger.error("AI API 调用失败: %s", e)
            self.error.emit(self.session_id, str(e))

    def _run_stream(self, client: AIClient):
        has_content = False
        try:
            for chunk in client.chat_stream(
                self._messages, temperature=self._temperature,
                top_p=self._top_p, max_tokens=self._max_tokens,
                frequency_penalty=self._frequency_penalty,
                presence_penalty=self._presence_penalty,
                top_k=self._top_k, stop=self._stop,
            ):
                if self._cancelled:
                    return
                if chunk.is_finished:
                    break
                if chunk.content_delta or chunk.reasoning_delta:
                    has_content = True
                    self.accumulated_content += chunk.content_delta
                    self.accumulated_reasoning += chunk.reasoning_delta
                    self.chunk_received.emit(self.session_id, chunk.content_delta, chunk.reasoning_delta)
            if not self._cancelled:
                self.stream_done.emit(self.session_id)
        except Exception as e:
            logger.error("AI 流式调用异常: %s", e)
            if has_content:
                self.stream_done.emit(self.session_id)
            else:
                raise

    def _run_sync(self, client: AIClient):
        resp = client.chat(
            self._messages, temperature=self._temperature,
            top_p=self._top_p, max_tokens=self._max_tokens,
            frequency_penalty=self._frequency_penalty,
            presence_penalty=self._presence_penalty,
            top_k=self._top_k, stop=self._stop,
        )
        self.non_stream_done.emit(self.session_id, resp.content, resp.reasoning_content)


# ═══════════════════════════════════════════════════════════════
# 对话列表项
# ═══════════════════════════════════════════════════════════════

class _ChatItem(QFrame):
    """对话列表中的单条项目。"""

    clicked = Signal(str)
    context_menu = Signal(str, object)  # (session_id, QPoint-global)

    def __init__(self, session: ChatSession, theme: Theme, selected: bool = False, parent=None):
        super().__init__(parent)
        self.session = session
        self.theme = theme
        self._selected = selected
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(58)
        self._build()

    def _build(self):
        c = self.theme.colors
        s = self.session

        bg = c["accent_light"] if self._selected else "transparent"
        border = f"border-left: 3px solid {c['accent']};" if self._selected else "border-left: 3px solid transparent;"
        self.setStyleSheet(f"""
            _ChatItem {{
                background: {bg};
                border-radius: 6px;
                {border}
            }}
            _ChatItem:hover {{
                background: {c["subnav_hover"]};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(4)
        if s.pinned:
            pin = QLabel("📌")
            pin.setStyleSheet("font-size: 11px;")
            top.addWidget(pin)

        title = QLabel(s.title)
        title.setStyleSheet(f"""
            font-size: 13px; font-weight: bold; color: {c["text"]};
        """)
        title.setMaximumWidth(160)
        top.addWidget(title, stretch=1)
        layout.addLayout(top)

        try:
            dt = datetime.strptime(s.updated_at, "%Y-%m-%d %H:%M:%S")
            time_str = dt.strftime("%m-%d %H:%M")
        except ValueError:
            time_str = s.updated_at

        info_parts = []
        if s.model_name:
            info_parts.append(s.model_name)
        info_parts.append(time_str)
        info_text = " · ".join(info_parts)

        info = QLabel(info_text)
        info.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        layout.addWidget(info)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.session.id)
        elif event.button() == Qt.MouseButton.RightButton:
            self.context_menu.emit(self.session.id, event.globalPosition().toPoint())
        super().mousePressEvent(event)


# ═══════════════════════════════════════════════════════════════
# 对话列表侧边栏
# ═══════════════════════════════════════════════════════════════

class _ChatSidebar(QFrame):
    """二级导航 — 对话列表面板。"""

    session_selected = Signal(str)
    session_deleted = Signal()

    def __init__(self, theme: Theme, chat_manager: ChatManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.manager = chat_manager
        self._selected_id: str | None = None
        self._build()
        self.refresh()

    def _build(self):
        c = self.theme.colors
        self.setMinimumWidth(200)
        self.setMaximumWidth(240)
        self.setStyleSheet(f"""
            _ChatSidebar {{
                background: {c["subnav_bg"]};
                border-right: 1px solid {c["border"]};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 搜索框 + 新建按钮（同一行）
        search_row = QHBoxLayout()
        search_row.setSpacing(6)

        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索对话...")
        self._search.setFixedHeight(30)
        self._search.textChanged.connect(lambda: self.refresh())
        search_row.addWidget(self._search, stretch=1)

        new_btn = QPushButton("＋")
        new_btn.setToolTip("新建对话")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setFixedSize(30, 30)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 16px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        new_btn.clicked.connect(self._on_new)
        search_row.addWidget(new_btn)

        layout.addLayout(search_row)

        # 列表区域
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._list_container = QWidget()
        self._list_container.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        scroll.setWidget(self._list_container)
        layout.addWidget(scroll, stretch=1)

    def refresh(self):
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        keyword = self._search.text()
        sessions = self.manager.search(keyword)

        if not sessions:
            c = self.theme.colors
            empty = QLabel("暂无对话")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 20px 0;")
            self._list_layout.addWidget(empty)
            return

        for s in sessions:
            item = _ChatItem(s, self.theme, selected=(s.id == self._selected_id))
            item.clicked.connect(self._on_select)
            item.context_menu.connect(self._on_context_menu)
            self._list_layout.addWidget(item)

    def select(self, session_id: str):
        self._selected_id = session_id
        self.refresh()
        self.session_selected.emit(session_id)

    def _on_select(self, session_id: str):
        self._selected_id = session_id
        self.refresh()
        self.session_selected.emit(session_id)

    def _on_new(self):
        from wokbee.core.model_config import ModelConfigManager
        mcm = ModelConfigManager()
        primary = mcm.get_primary()
        provider = primary.provider if primary else ""
        model = primary.model_name if primary else ""

        session = self.manager.create(provider=provider, model=model)
        self._selected_id = session.id
        self.refresh()
        self.session_selected.emit(session.id)

    def _on_context_menu(self, session_id: str, pos):
        c = self.theme.colors
        session = self.manager.get(session_id)
        if not session:
            return

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {c["content_bg"]};
                border: 1px solid {c["border"]};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 20px;
                border-radius: 4px;
                color: {c["text"]};
                font-size: 12px;
            }}
            QMenu::item:selected {{
                background: {c["subnav_hover"]};
            }}
        """)

        pin_text = "取消置顶" if session.pinned else "置顶"
        pin_action = menu.addAction(pin_text)
        rename_action = menu.addAction("重命名")
        menu.addSeparator()
        delete_action = menu.addAction("删除")
        delete_action.setProperty("color", c["danger"])

        action = menu.exec(pos)
        if action == pin_action:
            self.manager.toggle_pin(session_id)
            self.refresh()
        elif action == rename_action:
            self._show_rename_dialog(session_id, session.title)
        elif action == delete_action:
            self._show_delete_dialog(session_id)

    def _show_rename_dialog(self, session_id: str, old_title: str):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("重命名对话")
        dlg.setFixedSize(360, 170)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(12)

        label = QLabel("新名称")
        label.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {c['text']};")
        layout.addWidget(label)

        inp = QLineEdit(old_title)
        inp.setFixedHeight(36)
        inp.selectAll()
        layout.addWidget(inp)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setFixedSize(72, 34)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        ok_btn = QPushButton("确定")
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setFixedSize(72, 34)
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

        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_title = inp.text().strip()
            if new_title:
                self.manager.rename(session_id, new_title)
                self.refresh()
                self.session_selected.emit(session_id)

    def _show_delete_dialog(self, session_id: str):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("确认删除")
        dlg.setFixedSize(340, 150)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(16)

        msg = QLabel("确定删除这个对话吗？删除后无法恢复。")
        msg.setWordWrap(True)
        msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
        layout.addWidget(msg)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setFixedSize(72, 34)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        del_btn = QPushButton("删除")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setFixedSize(72, 34)
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["danger"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: #d32f2f; }}
        """)
        del_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(del_btn)

        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            was_selected = session_id == self._selected_id
            self.manager.delete(session_id)
            if was_selected:
                remaining = self.manager.list_sorted()
                self._selected_id = remaining[0].id if remaining else None
            self.refresh()
            self.session_deleted.emit()
            if self._selected_id:
                self.session_selected.emit(self._selected_id)


# ═══════════════════════════════════════════════════════════════
# 对话工作区
# ═══════════════════════════════════════════════════════════════

class _ChatWorkspace(QWidget):
    """对话工作区 — 顶部标题 + 消息区 + 底部输入区。"""

    session_created = Signal(str)
    title_changed = Signal(str)  # session_id — 标题被 AI 自动更新后通知 sidebar 刷新

    def __init__(self, theme: Theme, vm: ChatViewModel, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.vm = vm
        self.manager = vm.manager
        self._role_manager = vm.role_manager
        self._model_manager = ModelConfigManager()
        self._session: ChatSession | None = None
        self._current_model: ModelEntry | None = None
        self._workers: dict[str, _AIChatWorker] = {}
        self._drafts: dict[str, str] = {}
        self._pending_files: list[str] = []
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(100)
        self._scroll_timer.timeout.connect(self._do_scroll)

        self.vm.title_updated.connect(self._on_vm_title_updated)
        self.vm.model_changed.connect(self._on_vm_model_changed)
        self.vm.error.connect(self._show_tip)

        self._build()

    def _build(self):
        c = self.theme.colors
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 顶部标题栏 ──
        self._header = QFrame()
        self._header.setFixedHeight(48)
        self._header.setStyleSheet(f"""
            QFrame {{
                background: {c["content_bg"]};
                border-bottom: 1px solid {c["border_light"]};
            }}
        """)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(20, 0, 20, 0)

        self._title_label = QLabel("")
        self._title_label.setStyleSheet(f"""
            font-size: 14px; font-weight: bold; color: {c["text"]};
        """)
        header_layout.addWidget(self._title_label)

        self._time_label = QLabel("")
        self._time_label.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        header_layout.addWidget(self._time_label)
        header_layout.addStretch()

        layout.addWidget(self._header)

        # ── 消息区 ──
        msg_scroll = QScrollArea()
        msg_scroll.setWidgetResizable(True)
        msg_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        msg_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._msg_scroll = msg_scroll

        self._msg_container = QWidget()
        self._msg_container.setStyleSheet("background: transparent;")
        self._msg_layout = QVBoxLayout(self._msg_container)
        self._msg_layout.setContentsMargins(20, 12, 20, 12)
        self._msg_layout.setSpacing(10)
        self._msg_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        msg_scroll.setWidget(self._msg_container)
        layout.addWidget(msg_scroll, stretch=1)

        # ── 底部输入区 ──
        input_wrapper = QFrame()
        input_wrapper.setStyleSheet(f"""
            QFrame {{
                background: {c["content_bg"]};
                border-top: 1px solid {c["border_light"]};
            }}
        """)
        input_layout = QVBoxLayout(input_wrapper)
        input_layout.setContentsMargins(20, 10, 20, 14)
        input_layout.setSpacing(8)

        # 附件预览条
        self._attach_bar = QFrame()
        self._attach_bar.setStyleSheet(f"background: transparent;")
        self._attach_bar.setVisible(False)
        self._attach_bar_layout = QHBoxLayout(self._attach_bar)
        self._attach_bar_layout.setContentsMargins(0, 0, 0, 0)
        self._attach_bar_layout.setSpacing(6)
        self._attach_bar_layout.addStretch()
        input_layout.addWidget(self._attach_bar)

        self._input_box = QTextEdit()
        self._input_box.setPlaceholderText("输入消息...（Enter 发送，Shift+Enter 换行）")
        self._input_box.setFixedHeight(90)
        self._input_box.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]};
                border: 1px solid {c["input_border"]};
                border-radius: 8px;
                padding: 10px 12px;
                color: {c["text"]};
                font-size: 13px;
            }}
            QTextEdit:focus {{
                border-color: {c["input_focus_border"]};
            }}
        """)
        self._input_box.installEventFilter(self)
        input_layout.addWidget(self._input_box)

        bottom_bar = QHBoxLayout()
        bottom_bar.setSpacing(8)

        self._model_btn = QPushButton("未配置模型")
        self._model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._model_btn.setFixedHeight(30)
        self._model_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["card_bg"]};
                color: {c["text_secondary"]};
                border: 1px solid {c["border_light"]};
                border-radius: 6px;
                padding: 0 12px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background: {c["subnav_hover"]};
                border-color: {c["border"]};
            }}
            QPushButton::menu-indicator {{ image: none; }}
        """)
        self._model_btn.clicked.connect(self._show_model_menu)
        bottom_bar.addWidget(self._model_btn)

        bottom_bar.addStretch()

        self._attach_btn = QPushButton("📎")
        self._attach_btn.setToolTip("上传图片或文档")
        self._attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._attach_btn.setFixedSize(32, 32)
        self._attach_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["card_bg"]}; color: {c["text_secondary"]};
                border: 1px solid {c["border_light"]}; border-radius: 6px;
                font-size: 15px;
            }}
            QPushButton:hover {{
                background: {c["subnav_hover"]}; border-color: {c["border"]};
            }}
        """)
        self._attach_btn.clicked.connect(self._on_attach)
        bottom_bar.addWidget(self._attach_btn)

        self._params_btn = QPushButton("⚙")
        self._params_btn.setToolTip("对话参数设置")
        self._params_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._params_btn.setFixedSize(32, 32)
        self._params_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["card_bg"]}; color: {c["text_secondary"]};
                border: 1px solid {c["border_light"]}; border-radius: 6px;
                font-size: 15px;
            }}
            QPushButton:hover {{
                background: {c["subnav_hover"]}; border-color: {c["border"]};
            }}
        """)
        self._params_btn.clicked.connect(self._show_params_dialog)
        bottom_bar.addWidget(self._params_btn)

        self._send_btn = QPushButton("发送")
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setFixedSize(64, 32)
        self._send_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        self._send_btn.clicked.connect(self._on_send)
        bottom_bar.addWidget(self._send_btn)

        input_layout.addLayout(bottom_bar)
        layout.addWidget(input_wrapper)

        self._show_welcome()

    def eventFilter(self, obj, event):
        if obj is self._input_box and event.type() == QEvent.Type.KeyPress:
            key_event: QKeyEvent = event
            if key_event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    return False
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    def _cancel_worker_for_session(self, session_id: str):
        """取消指定 session 正在运行的 worker，保存已收到的流式内容。"""
        worker = self._workers.get(session_id)
        if worker is None:
            return
        if worker.isRunning():
            session = self.manager.get(session_id)
            if session and worker.accumulated_content:
                msg: dict = {"role": "assistant", "content": worker.accumulated_content}
                if worker.accumulated_reasoning:
                    msg["reasoning_content"] = worker.accumulated_reasoning
                session.messages.append(msg)
                self.manager.save()

            worker.cancel()
            worker.wait(3000)

        self._workers.pop(session_id, None)

    def load_session(self, session_id: str):
        session = self.manager.get(session_id)
        if not session:
            return

        if self._session:
            self._drafts[self._session.id] = self._input_box.toPlainText()

        self._session = session
        self.vm._session = session

        saved_draft = self._drafts.pop(session_id, "")
        self._input_box.setPlainText(saved_draft)
        self._pending_files.clear()
        self._refresh_attach_bar()

        self._header.show()
        self._title_label.setText(session.title)
        try:
            dt = datetime.strptime(session.updated_at, "%Y-%m-%d %H:%M:%S")
            self._time_label.setText(dt.strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            self._time_label.setText(session.updated_at)

        self._clear_messages()
        if session.messages:
            for msg in session.messages:
                content = msg["content"]
                reasoning = msg.get("reasoning_content", "")
                if msg["role"] == "assistant":
                    content, reasoning = ChatViewModel.parse_think_tags(content, reasoning)
                self._add_bubble(msg["role"], content, reasoning)

        worker = self._workers.get(session_id)
        if worker and worker.isRunning():
            self._stream_content = worker.accumulated_content
            self._stream_reasoning = worker.accumulated_reasoning
            self._stream_phase = "content" if self._stream_content else ("reasoning" if self._stream_reasoning else "idle")
            self._think_buf = ""
            self._in_think_tag = False
            self._create_stream_bubble()
            if self._stream_reasoning:
                self._stream_thinking_frame.setVisible(True)
                self._stream_reasoning_label.setMarkdown(self._stream_reasoning)
            if self._stream_content:
                self._stream_reply_label.setMarkdown(self._stream_content)
            self._set_sending(True)
        else:
            self._input_box.setEnabled(True)
            self._send_btn.setEnabled(True)
            if not session.messages:
                self._show_empty_chat()

        QTimer.singleShot(50, self._scroll_to_bottom)
        QTimer.singleShot(200, self._scroll_to_bottom)
        self._init_model_for_session()

    def _init_model_for_session(self):
        """根据当前对话的模型信息或主模型初始化模型选择器。"""
        self._model_manager = ModelConfigManager()
        models = self._model_manager.list_all()
        self._current_model = None

        if self._session and self._session.model_name:
            for m in models:
                if m.model_name == self._session.model_name and m.provider == self._session.model_provider:
                    self._current_model = m
                    break

        if not self._current_model:
            primary = self._model_manager.get_primary()
            if primary:
                self._current_model = primary
            elif models:
                self._current_model = models[0]

        self._model_btn.setEnabled(True)
        self._update_model_btn()

    def _update_model_btn(self):
        if self._current_model:
            display = f"🤖 {self._current_model.model_name}"
            self._model_btn.setText(display)
            self._model_btn.setToolTip(
                f"{self._current_model.provider} / {self._current_model.model_name}"
            )
        else:
            self._model_btn.setText("未配置模型")
            self._model_btn.setToolTip("请先在设置中添加模型")

    def _show_tip(self, message: str):
        c = self.theme.colors
        dlg = QDialog(self)
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

    def _show_model_menu(self):
        c = self.theme.colors
        models = ModelConfigManager().list_all()
        if not models:
            self._show_tip("请先在「AI配置 → 厂商设置」中添加模型")
            return

        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {c["content_bg"]};
                border: 1px solid {c["border"]};
                border-radius: 6px;
                padding: 4px;
            }}
            QMenu::item {{
                padding: 6px 16px;
                border-radius: 4px;
                color: {c["text"]};
                font-size: 12px;
            }}
            QMenu::item:selected {{
                background: {c["subnav_hover"]};
            }}
        """)

        for m in models:
            prefix = "⭐ " if m.is_primary else ""
            check = " ✓" if (self._current_model and m.id == self._current_model.id) else ""
            text = f"{prefix}{m.provider} / {m.model_name}{check}"
            action = menu.addAction(text)
            action.setData(m.id)

        chosen = menu.exec(self._model_btn.mapToGlobal(
            self._model_btn.rect().topLeft()
        ))
        if chosen:
            model_id = chosen.data()
            selected = next((m for m in models if m.id == model_id), None)
            if selected:
                self._current_model = selected
                self._update_model_btn()
                if self._session:
                    self._session.model_provider = selected.provider
                    self._session.model_name = selected.model_name
                    self.manager.save()

    def _show_welcome(self):
        """无任何对话被选中时的欢迎页。"""
        c = self.theme.colors
        self._header.hide()
        self._clear_messages()

        self._model_manager = ModelConfigManager()
        primary = self._model_manager.get_primary()
        has_model = primary is not None
        if not has_model:
            models = self._model_manager.list_all()
            if models:
                has_model = True
                primary = models[0]

        if has_model:
            self._current_model = primary
            self._input_box.setEnabled(True)
            self._input_box.setPlaceholderText("输入消息，自动创建新对话...")
            self._send_btn.setEnabled(True)
            self._model_btn.setEnabled(True)
            self._update_model_btn()
        else:
            self._current_model = None
            self._input_box.setEnabled(False)
            self._input_box.setPlaceholderText("请先在设置中配置模型")
            self._send_btn.setEnabled(False)
            self._model_btn.setEnabled(False)
            self._model_btn.setText("未配置模型")

        welcome = QWidget()
        wl = QVBoxLayout(welcome)
        wl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        logo_label = QLabel()
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_path = _RESOURCES / "logo.png"
        if logo_path.exists():
            pixmap = QPixmap(str(logo_path)).scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo_label.setPixmap(pixmap)
        else:
            logo_label.setText("🐝")
            logo_label.setStyleSheet("font-size: 48px;")
        wl.addWidget(logo_label)

        t = QLabel("WokBee")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {c['text']}; margin-top: 12px;")
        wl.addWidget(t)

        h = QLabel("选择一个对话或新建对话开始")
        h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.setStyleSheet(f"font-size: 13px; color: {c['text_hint']}; margin-top: 6px;")
        wl.addWidget(h)

        self._msg_layout.addWidget(welcome)

    def _show_empty_chat(self):
        c = self.theme.colors
        self._header.show()

        empty = QWidget()
        el = QVBoxLayout(empty)
        el.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon = QLabel("🐝")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 36px;")
        el.addWidget(icon)

        h = QLabel("开始和 AI 对话吧")
        h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.setStyleSheet(f"font-size: 14px; color: {c['text_hint']}; margin-top: 8px;")
        el.addWidget(h)

        self._msg_layout.addWidget(empty)

    def _clear_messages(self):
        while self._msg_layout.count():
            item = self._msg_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _make_md_browser(self, bg: str) -> _AutoHeightBrowser:
        """创建一个自适应高度的 Markdown 渲染控件。"""
        c = self.theme.colors
        tb = _AutoHeightBrowser()
        tb.setStyleSheet(f"""
            QTextBrowser {{
                background: {bg};
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 13px;
                color: {c["text"]};
                border: none;
            }}
            QTextBrowser a {{ color: {c["accent"]}; }}
        """)
        tb.document().setDefaultStyleSheet(f"""
            code {{
                background: #e8e8e8; padding: 1px 4px;
                border-radius: 3px; font-family: Consolas, monospace;
                font-size: 12px;
            }}
            pre {{
                background: #2d2d2d; color: #f8f8f2;
                padding: 10px; border-radius: 6px;
                font-family: Consolas, monospace; font-size: 12px;
            }}
            blockquote {{
                border-left: 3px solid {c["accent"]};
                padding-left: 10px; color: {c["text_secondary"]};
                margin: 4px 0;
            }}
            table {{ border-collapse: collapse; }}
            th, td {{
                border: 1px solid {c["border_light"]};
                padding: 4px 8px;
            }}
            th {{ background: {c["card_bg"]}; }}
        """)
        return tb

    def _add_bubble(self, role: str, content: str, reasoning: str = ""):
        c = self.theme.colors
        is_user = role == "user"

        row = QHBoxLayout()
        row.setSpacing(0)

        if is_user:
            bubble = QLabel(content)
            bubble.setWordWrap(True)
            bubble.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            bubble.setStyleSheet(f"""
                background: #dcf8c6;
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 13px;
                color: {c["text"]};
            """)
            row.addStretch(1)
            row.addWidget(bubble, 3)
        else:
            bubble_col = QVBoxLayout()
            bubble_col.setSpacing(0)
            bubble_col.setContentsMargins(0, 0, 0, 0)

            if reasoning:
                thinking_widget = self._build_thinking_widget(reasoning)
                bubble_col.addWidget(thinking_widget)

            reply_browser = self._make_md_browser(c["card_bg"])
            reply_browser.setMarkdown(content)
            bubble_col.addWidget(reply_browser)

            col_wrapper = QWidget()
            col_wrapper.setLayout(bubble_col)
            col_wrapper.setStyleSheet("background: transparent;")
            row.addWidget(col_wrapper, 3)
            row.addStretch(1)

        wrapper = QWidget()
        wrapper.setLayout(row)
        wrapper.setStyleSheet("background: transparent;")
        self._msg_layout.addWidget(wrapper)

    def _build_thinking_widget(self, reasoning: str) -> QWidget:
        """构建可展开/折叠的思考过程组件。"""
        c = self.theme.colors

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(0)

        toggle_btn = QPushButton("💭 查看思考过程")
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setFixedHeight(28)
        toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {c["accent"]};
                border: none;
                font-size: 12px;
                text-align: left;
                padding: 2px 6px;
            }}
            QPushButton:hover {{
                color: {c["accent_hover"]};
                text-decoration: underline;
            }}
        """)

        content_frame = QFrame()
        content_frame.setVisible(False)
        content_frame.setStyleSheet(f"""
            QFrame {{
                background: {c["accent_light"]};
                border-left: 3px solid {c["accent"]};
                border-radius: 6px;
                margin-bottom: 4px;
            }}
        """)
        frame_layout = QVBoxLayout(content_frame)
        frame_layout.setContentsMargins(12, 8, 12, 8)
        frame_layout.setSpacing(4)

        header = QLabel("💭 思考过程")
        header.setStyleSheet(f"""
            font-size: 11px;
            font-weight: bold;
            color: {c["accent"]};
            background: transparent;
            border: none;
        """)
        frame_layout.addWidget(header)

        reasoning_browser = _AutoHeightBrowser()
        reasoning_browser.setStyleSheet(f"""
            QTextBrowser {{
                font-size: 12px; color: {c["text_secondary"]};
                background: transparent; border: none;
            }}
        """)
        reasoning_browser.setMarkdown(reasoning)
        frame_layout.addWidget(reasoning_browser)

        def _toggle():
            visible = not content_frame.isVisible()
            content_frame.setVisible(visible)
            toggle_btn.setText("💭 收起思考过程" if visible else "💭 查看思考过程")

        toggle_btn.clicked.connect(_toggle)

        layout.addWidget(toggle_btn)
        layout.addWidget(content_frame)
        return container

    def _on_send(self):
        text = self._input_box.toPlainText().strip()
        has_files = bool(self._pending_files)
        if not text and not has_files:
            return

        if not self._session:
            if not self._current_model:
                return
            provider = self._current_model.provider
            model = self._current_model.model_name
            session = self.manager.create(provider=provider, model=model)
            self._session = session
            self.vm._session = session
            self.session_created.emit(session.id)

        if self._session.messages == []:
            self._clear_messages()

        self._header.show()
        self._title_label.setText(self._session.title)
        try:
            dt = datetime.strptime(self._session.updated_at, "%Y-%m-%d %H:%M:%S")
            self._time_label.setText(dt.strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            self._time_label.setText(self._session.updated_at)

        self._input_box.clear()
        self._input_box.setPlaceholderText("输入消息...")

        # 构建 API 消息内容（可能含多模态）
        api_parts: list[dict] = []
        display_text = text
        doc_prefix = ""

        for fp in self._pending_files:
            try:
                if is_image(fp):
                    b64, mime = read_image_as_base64(fp)
                    api_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                    display_text += f"\n[📎 {Path(fp).name}]"
                elif is_document(fp):
                    extracted = read_file_as_text(fp)
                    doc_prefix += f"--- {Path(fp).name} ---\n{extracted}\n---\n\n"
                    display_text += f"\n[📎 {Path(fp).name}]"
            except Exception as exc:
                self._show_tip(f"读取文件失败: {Path(fp).name}\n{exc}")
                logger.error("读取附件 %s 失败: %s", fp, exc)

        self._pending_files.clear()
        self._refresh_attach_bar()

        full_text = doc_prefix + text if doc_prefix else text

        has_images = any(p.get("type") == "image_url" for p in api_parts)
        if has_images:
            if full_text:
                api_parts.insert(0, {"type": "text", "text": full_text})
            api_user_content = api_parts
        else:
            api_user_content = full_text

        if not display_text.strip():
            display_text = "[附件]"

        self._session.messages.append({"role": "user", "content": display_text})
        self.manager.touch(self._session.id)
        self._auto_title_from_user_msg(self._session)
        self.manager.save()

        self._add_bubble("user", display_text)
        self._scroll_to_bottom()

        if not self._current_model or not self._current_model.endpoint:
            self._show_tip("请先在「AI配置 → 厂商设置」中配置模型的 API 地址")
            return

        params = self._session.get_params()
        self._set_sending(True)

        all_msgs = self._session.messages
        if params.history_rounds > 0:
            max_msgs = params.history_rounds * 2
            api_messages = [
                {"role": m["role"], "content": m["content"]}
                for m in all_msgs[-(max_msgs + 1):-1]
            ]
        else:
            api_messages = []

        api_messages.append({"role": "user", "content": api_user_content})

        if params.system_prompt.strip():
            api_messages.insert(0, {"role": "system", "content": params.system_prompt.strip()})

        use_stream = params.stream
        sid = self._session.id
        self._cancel_worker_for_session(sid)

        if use_stream:
            self._stream_content = ""
            self._stream_reasoning = ""
            self._stream_phase = "idle"
            self._think_buf = ""
            self._in_think_tag = False
            self._create_stream_bubble()
            self._scroll_to_bottom()

        _stop_list = [
            s.strip() for s in self._current_model.stop_sequences.split(",") if s.strip()
        ] or None

        worker = _AIChatWorker(
            session_id=sid,
            endpoint=self._current_model.endpoint,
            api_key=self._current_model.api_key,
            model=self._current_model.model_name,
            messages=api_messages,
            temperature=params.temperature,
            top_p=params.top_p,
            max_tokens=params.max_tokens,
            stream=use_stream,
            disable_thinking=self._current_model.disable_thinking,
            reasoning_effort=self._current_model.reasoning_effort,
            frequency_penalty=self._current_model.frequency_penalty,
            presence_penalty=self._current_model.presence_penalty,
            top_k=self._current_model.top_k,
            stop=_stop_list,
            parent=self,
        )
        self._workers[sid] = worker
        worker.chunk_received.connect(self._on_chunk)
        worker.stream_done.connect(self._on_stream_finished)
        worker.non_stream_done.connect(self._on_non_stream_done)
        worker.error.connect(self._on_reply_error)
        worker.start()

    def _set_sending(self, sending: bool):
        self._input_box.setEnabled(not sending)
        self._send_btn.setEnabled(not sending)
        self._model_btn.setEnabled(not sending)
        self._attach_btn.setEnabled(not sending)
        if sending:
            self._send_btn.setText("…")
        else:
            self._send_btn.setText("发送")

    # ── 附件相关 ──
    def _on_attach(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", "", build_file_filter(),
        )
        for p in paths:
            if p not in self._pending_files:
                self._pending_files.append(p)
        self._refresh_attach_bar()

    def _refresh_attach_bar(self):
        layout = self._attach_bar_layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not self._pending_files:
            self._attach_bar.setVisible(False)
            return
        self._attach_bar.setVisible(True)
        c = self.theme.colors
        for fp in self._pending_files:
            name = Path(fp).name
            chip = QFrame()
            chip.setStyleSheet(f"""
                QFrame {{
                    background: {c["input_bg"]};
                    border: 1px solid {c["border_light"]};
                    border-radius: 4px;
                }}
            """)
            hl = QHBoxLayout(chip)
            hl.setContentsMargins(6, 2, 2, 2)
            hl.setSpacing(4)
            lbl = QLabel(name)
            lbl.setStyleSheet(f"font-size: 11px; color: {c['text_secondary']}; background: transparent; border: none;")
            lbl.setToolTip(fp)
            hl.addWidget(lbl)
            x_btn = QPushButton("×")
            x_btn.setFixedSize(16, 16)
            x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            x_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {c["text_hint"]};
                    border: none; font-size: 12px; font-weight: bold;
                }}
                QPushButton:hover {{ color: {c["danger"]}; }}
            """)
            x_btn.clicked.connect(lambda _, f=fp: self._remove_file(f))
            hl.addWidget(x_btn)
            layout.addWidget(chip)
        layout.addStretch()

    def _remove_file(self, fp: str):
        if fp in self._pending_files:
            self._pending_files.remove(fp)
        self._refresh_attach_bar()

    def _create_stream_bubble(self):
        """创建流式输出气泡：思考过程区域（初始显示）+ 回复内容区域。"""
        c = self.theme.colors

        row = QHBoxLayout()
        row.setSpacing(0)

        bubble_col = QVBoxLayout()
        bubble_col.setSpacing(0)
        bubble_col.setContentsMargins(0, 0, 0, 0)

        # 思考过程区域（流式期间自动展开）
        self._stream_thinking_frame = QFrame()
        self._stream_thinking_frame.setVisible(False)
        self._stream_thinking_frame.setStyleSheet(f"""
            QFrame {{
                background: {c["accent_light"]};
                border-left: 3px solid {c["accent"]};
                border-radius: 6px;
                margin-bottom: 4px;
            }}
        """)
        thinking_layout = QVBoxLayout(self._stream_thinking_frame)
        thinking_layout.setContentsMargins(12, 8, 12, 8)
        thinking_layout.setSpacing(4)

        thinking_header = QLabel("💭 思考中...")
        thinking_header.setStyleSheet(f"""
            font-size: 11px; font-weight: bold;
            color: {c["accent"]}; background: transparent; border: none;
        """)
        self._stream_thinking_header = thinking_header
        thinking_layout.addWidget(thinking_header)

        self._stream_reasoning_label = _AutoHeightBrowser()
        self._stream_reasoning_label.setStyleSheet(f"""
            QTextBrowser {{
                font-size: 12px; color: {c["text_secondary"]};
                background: transparent; border: none;
            }}
        """)
        thinking_layout.addWidget(self._stream_reasoning_label)
        bubble_col.addWidget(self._stream_thinking_frame)

        # 回复内容区域（Markdown 渲染）
        self._stream_reply_label = self._make_md_browser(c["card_bg"])
        bubble_col.addWidget(self._stream_reply_label)

        col_wrapper = QWidget()
        col_wrapper.setLayout(bubble_col)
        col_wrapper.setStyleSheet("background: transparent;")
        row.addWidget(col_wrapper, 3)
        row.addStretch(1)

        self._stream_wrapper = QWidget()
        self._stream_wrapper.setLayout(row)
        self._stream_wrapper.setStyleSheet("background: transparent;")
        self._msg_layout.addWidget(self._stream_wrapper)

    def _on_chunk(self, worker_sid: str, content_delta: str, reasoning_delta: str):
        """处理每个流式 chunk，增量更新气泡文本。

        支持两种思考模式：
        - reasoning_content 字段（DeepSeek V3 等）
        - <think>...</think> 标签嵌入 content（DeepSeek R1 蒸馏模型等）
        """
        if not self._session or self._session.id != worker_sid:
            return
        if reasoning_delta:
            self._show_reasoning_chunk(reasoning_delta)

        if content_delta:
            self._think_buf += content_delta

            while self._think_buf:
                if self._in_think_tag:
                    end_pos = self._think_buf.find("</think>")
                    if end_pos != -1:
                        self._show_reasoning_chunk(self._think_buf[:end_pos])
                        self._think_buf = self._think_buf[end_pos + len("</think>"):]
                        self._in_think_tag = False
                        if self._stream_phase == "reasoning":
                            self._stream_thinking_header.setText("💭 思考过程")
                            self._stream_phase = "content"
                    else:
                        hold = len("</think>") - 1
                        if len(self._think_buf) > hold:
                            safe = self._think_buf[:-hold]
                            self._show_reasoning_chunk(safe)
                            self._think_buf = self._think_buf[len(safe):]
                        break
                else:
                    start_pos = self._think_buf.find("<think>")
                    if start_pos != -1:
                        if start_pos > 0:
                            self._show_content_chunk(self._think_buf[:start_pos])
                        self._think_buf = self._think_buf[start_pos + len("<think>"):]
                        self._in_think_tag = True
                    elif "<" in self._think_buf:
                        idx = self._think_buf.rfind("<")
                        tail = self._think_buf[idx:]
                        if "<think>".startswith(tail):
                            if idx > 0:
                                self._show_content_chunk(self._think_buf[:idx])
                            self._think_buf = tail
                            break
                        else:
                            self._show_content_chunk(self._think_buf)
                            self._think_buf = ""
                    else:
                        self._show_content_chunk(self._think_buf)
                        self._think_buf = ""

        self._request_scroll()

    def _show_reasoning_chunk(self, text: str):
        if not text:
            return
        if self._stream_phase != "reasoning":
            self._stream_phase = "reasoning"
        if not self._stream_thinking_frame.isVisible():
            self._stream_thinking_frame.setVisible(True)
        self._stream_reasoning += text
        self._stream_reasoning_label.setMarkdown(self._stream_reasoning)

    def _show_content_chunk(self, text: str):
        if not text:
            return
        if self._stream_phase == "reasoning":
            self._stream_thinking_header.setText("💭 思考过程")
            self._stream_phase = "content"
        elif self._stream_phase == "idle":
            self._stream_phase = "content"
        self._stream_content += text
        self._stream_reply_label.setMarkdown(self._stream_content)

    def _on_stream_finished(self, worker_sid: str):
        """流式输出完成：持久化消息、转换思考区域为可折叠。"""
        worker = self._workers.pop(worker_sid, None)

        if worker:
            self.vm.save_stream_result(
                worker_sid, worker.accumulated_content, worker.accumulated_reasoning,
            )

        if self._session and self._session.id == worker_sid:
            self._set_sending(False)

            if hasattr(self, "_think_buf") and self._think_buf:
                if self._in_think_tag:
                    self._show_reasoning_chunk(self._think_buf)
                else:
                    self._show_content_chunk(self._think_buf)
                self._think_buf = ""

            if not self._stream_content.strip() and self._stream_reasoning.strip():
                self._stream_content = self._stream_reasoning
                self._stream_reasoning = ""
                if self._stream_reply_label:
                    self._stream_reply_label.setMarkdown(self._stream_content)
                if self._stream_thinking_frame:
                    self._stream_thinking_frame.setVisible(False)
                    self._stream_thinking_frame = None

            if self._stream_phase == "reasoning":
                self._stream_thinking_header.setText("💭 思考过程")

            if self._stream_reasoning and self._stream_thinking_frame:
                self._convert_thinking_to_collapsible()

            if self._stream_reply_label and self._stream_content:
                self._stream_reply_label.setMarkdown("")
                self._stream_reply_label.setMarkdown(self._stream_content)

            self._stream_wrapper = None
            self._stream_reply_label = None
            self._stream_reasoning_label = None
            self._stream_thinking_frame = None
            self._stream_thinking_header = None
            self._scroll_to_bottom()

    def _convert_thinking_to_collapsible(self):
        """流结束后，将思考区域改为可折叠（默认收起）。"""
        c = self.theme.colors

        frame = self._stream_thinking_frame
        toggle_btn = QPushButton("💭 查看思考过程")
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setFixedHeight(28)
        toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["accent"]};
                border: none; font-size: 12px; text-align: left; padding: 2px 6px;
            }}
            QPushButton:hover {{
                color: {c["accent_hover"]}; text-decoration: underline;
            }}
        """)

        frame.setVisible(False)

        parent_layout = frame.parentWidget().layout()
        idx = parent_layout.indexOf(frame)
        parent_layout.insertWidget(idx, toggle_btn)

        def _toggle():
            visible = not frame.isVisible()
            frame.setVisible(visible)
            toggle_btn.setText("💭 收起思考过程" if visible else "💭 查看思考过程")

        toggle_btn.clicked.connect(_toggle)

    def _on_non_stream_done(self, worker_sid: str, content: str, reasoning: str):
        """非流式模式：一次性收到完整回复。"""
        self._workers.pop(worker_sid, None)

        content, reasoning = ChatViewModel.parse_think_tags(content, reasoning)
        if not content.strip() and reasoning.strip():
            content = reasoning
            reasoning = ""

        self.vm.save_sync_result(worker_sid, content, reasoning)

        if self._session and self._session.id == worker_sid:
            self._set_sending(False)
            self._add_bubble("assistant", content, reasoning)
            self._scroll_to_bottom()

    def _on_reply_error(self, worker_sid: str, error_msg: str):
        self._workers.pop(worker_sid, None)
        if not self._session or self._session.id != worker_sid:
            return
        self._set_sending(False)
        if hasattr(self, "_stream_wrapper") and self._stream_wrapper:
            self._stream_wrapper.deleteLater()
            self._stream_wrapper = None
        self._show_tip(f"AI 回复失败:\n{error_msg}")

    def _show_params_dialog(self):
        if not self._session:
            self._show_tip("请先选择或新建一个对话")
            return

        c = self.theme.colors
        p = self._session.get_params()

        dlg = QDialog(self)
        dlg.setWindowTitle("对话参数设置")
        dlg.setFixedSize(460, 684)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)

        lbl_style = f"font-size: 13px; font-weight: bold; color: {c['text']};"
        hint_style = f"font-size: 11px; color: {c['text_hint']}; margin-bottom: 2px;"

        combo_style = f"""
            QComboBox {{
                background: {c["input_bg"]};
                border: 1px solid {c["input_border"]};
                border-radius: 6px;
                padding: 5px 30px 5px 10px;
                color: {c["text"]};
                font-size: 12px;
            }}
            QComboBox:hover {{
                border-color: {c["input_focus_border"]};
            }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 28px; border: none;
            }}
            QComboBox::down-arrow {{
                image: none; width: 0; height: 0;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {c["text_secondary"]};
            }}
            QComboBox QAbstractItemView {{
                background: {c["content_bg"]};
                border: 1px solid {c["input_border"]};
                border-radius: 4px; padding: 4px; outline: none;
                selection-background-color: {c["accent_light"]};
                selection-color: {c["accent"]};
            }}
            QComboBox QAbstractItemView::item {{
                padding: 5px 10px; border-radius: 4px; color: {c["text"]};
            }}
            QComboBox QAbstractItemView::item:hover {{
                background: {c["subnav_hover"]};
            }}
        """

        # 角色选择
        role_lbl = QLabel("系统角色设定")
        role_lbl.setStyleSheet(lbl_style)
        layout.addWidget(role_lbl)

        role_hint = QLabel("从已有角色选择，或手动编辑下方内容")
        role_hint.setStyleSheet(hint_style)
        layout.addWidget(role_hint)

        role_row = QHBoxLayout()
        role_row.setSpacing(8)

        role_combo = QComboBox()
        role_combo.setStyleSheet(combo_style)
        roles = self._role_manager.list_all()
        role_combo.addItem("— 自定义 —", "")
        for r in roles:
            role_combo.addItem(r.name, r.id)
        role_row.addWidget(role_combo, stretch=1)

        quick_btn = QPushButton("+ 快速创建")
        quick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quick_btn.setFixedHeight(30)
        quick_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["accent"]};
                border: none; border-radius: 6px; padding: 0 12px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        role_row.addWidget(quick_btn)
        layout.addLayout(role_row)

        sys_input = QTextEdit()
        sys_input.setPlainText(p.system_prompt)
        sys_input.setFixedHeight(216)
        sys_input.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 6px 8px; color: {c["text"]}; font-size: 12px;
            }}
            QTextEdit:focus {{ border-color: {c["input_focus_border"]}; }}
        """)
        layout.addWidget(sys_input)

        def _on_role_selected(index: int):
            role_id = role_combo.itemData(index)
            if role_id:
                role = self._role_manager.get(role_id)
                if role:
                    sys_input.setPlainText(role.description)

        role_combo.currentIndexChanged.connect(_on_role_selected)

        def _on_quick_create():
            self._show_quick_role_dialog(dlg, role_combo, sys_input)

        quick_btn.clicked.connect(_on_quick_create)

        params_row = QHBoxLayout()
        params_row.setSpacing(16)

        temp_box = QDoubleSpinBox()
        temp_box.setRange(0.0, 2.0)
        temp_box.setSingleStep(0.1)
        temp_box.setDecimals(1)
        temp_box.setValue(p.temperature)
        temp_col = QVBoxLayout()
        temp_lbl = QLabel("温度")
        temp_lbl.setStyleSheet(lbl_style)
        temp_col.addWidget(temp_lbl)
        temp_col.addWidget(temp_box)
        params_row.addLayout(temp_col)

        topp_box = QDoubleSpinBox()
        topp_box.setRange(0.0, 1.0)
        topp_box.setSingleStep(0.05)
        topp_box.setDecimals(2)
        topp_box.setValue(p.top_p)
        topp_col = QVBoxLayout()
        topp_lbl = QLabel("Top P")
        topp_lbl.setStyleSheet(lbl_style)
        topp_col.addWidget(topp_lbl)
        topp_col.addWidget(topp_box)
        params_row.addLayout(topp_col)

        layout.addLayout(params_row)

        params_row2 = QHBoxLayout()
        params_row2.setSpacing(16)

        max_tok_box = QSpinBox()
        max_tok_box.setRange(1, 128000)
        max_tok_box.setSingleStep(256)
        max_tok_box.setValue(p.max_tokens)
        mt_col = QVBoxLayout()
        mt_lbl = QLabel("最大输出 Token")
        mt_lbl.setStyleSheet(lbl_style)
        mt_col.addWidget(mt_lbl)
        mt_col.addWidget(max_tok_box)
        params_row2.addLayout(mt_col)

        hist_box = QSpinBox()
        hist_box.setRange(0, 100)
        hist_box.setSingleStep(1)
        hist_box.setValue(p.history_rounds)
        hist_col = QVBoxLayout()
        hist_lbl = QLabel("历史对话轮数")
        hist_lbl.setStyleSheet(lbl_style)
        hist_col.addWidget(hist_lbl)
        hist_h = QLabel("0 表示不携带历史")
        hist_h.setStyleSheet(hint_style)
        hist_col.addWidget(hist_h)
        hist_col.addWidget(hist_box)
        params_row2.addLayout(hist_col)

        layout.addLayout(params_row2)

        stream_chk = QCheckBox("启用流式输出")
        stream_chk.setChecked(p.stream)
        stream_chk.setStyleSheet(f"font-size: 13px; color: {c['text']};")
        layout.addWidget(stream_chk)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setFixedSize(72, 34)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setFixedSize(72, 34)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        save_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_params = ChatParams(
                system_prompt=sys_input.toPlainText().strip(),
                temperature=temp_box.value(),
                top_p=topp_box.value(),
                max_tokens=max_tok_box.value(),
                history_rounds=hist_box.value(),
                stream=stream_chk.isChecked(),
            )
            self.vm.save_chat_params(new_params)

    def _show_quick_role_dialog(self, parent_dlg: QDialog, role_combo: QComboBox, sys_input: QTextEdit):
        c = self.theme.colors

        dlg = QDialog(parent_dlg)
        dlg.setWindowTitle("快速创建角色")
        dlg.setFixedSize(420, 300)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(10)

        name_lbl = QLabel("角色名称")
        name_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {c['text']};")
        layout.addWidget(name_lbl)

        name_input = QLineEdit()
        name_input.setPlaceholderText("例如：翻译助手")
        layout.addWidget(name_input)

        desc_lbl = QLabel("角色描述")
        desc_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {c['text']};")
        layout.addWidget(desc_lbl)

        desc_input = QTextEdit()
        desc_input.setPlaceholderText("描述 AI 的角色和行为方式...")
        desc_input.setMinimumHeight(80)
        desc_input.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 6px 10px; color: {c["text"]}; font-size: 13px;
            }}
            QTextEdit:focus {{ border-color: {c["input_focus_border"]}; }}
        """)
        layout.addWidget(desc_input)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(72, 34)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("创建")
        save_btn.setFixedSize(72, 34)
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)

        def _on_create():
            name = name_input.text().strip()
            if not name:
                name_input.setFocus()
                return
            dlg.accept()

        save_btn.clicked.connect(_on_create)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            name = name_input.text().strip()
            desc = desc_input.toPlainText().strip()
            new_role = self.vm.create_role(name, desc)

            role_combo.addItem(new_role.name, new_role.id)
            role_combo.setCurrentIndex(role_combo.count() - 1)
            sys_input.setPlainText(desc)

    def _on_vm_title_updated(self, session_id: str):
        """ViewModel 通知标题已更新。"""
        if self._session and self._session.id == session_id:
            s = self.manager.get(session_id)
            if s:
                self._title_label.setText(s.title)
        self.title_changed.emit(session_id)

    def _on_vm_model_changed(self, model: ModelEntry | None):
        self._current_model = model
        self._update_model_btn()

    def _auto_title_from_user_msg(self, session: ChatSession):
        """首次提问时，先用前30字做临时标题，再后台调用主模型 AI 命名。"""
        needs_ai = self.vm.auto_title(session)
        if self._session and self._session.id == session.id:
            self._title_label.setText(session.title)

        if not needs_ai:
            return

        raw = session.messages[0].get("content", "") if session.messages else ""
        if isinstance(raw, list):
            text_parts = [p.get("text", "") for p in raw if isinstance(p, dict) and p.get("type") == "text"]
            raw = " ".join(text_parts)

        primary = self._model_manager.get_primary()
        if not primary or not primary.endpoint:
            return
        client = AIClient(primary.endpoint, primary.api_key, primary.model_name,
                          disable_thinking=primary.disable_thinking)
        messages = [
            {"role": "system", "content": (
                '根据用户的对话内容，生成一个简短的中文对话标题。\n'
                '输出 JSON：{"name": "对话标题"}\n'
                '要求：中文，15字以内，只输出 JSON。'
            )},
            {"role": "user", "content": raw.strip()[:500]},
        ]
        sid = session.id
        worker = _AiNameWorker(client, messages, parent=self)
        worker.finished.connect(lambda reply, _sid=sid: self._on_auto_name_done(_sid, reply))
        worker.failed.connect(lambda _err: None)
        self._auto_name_worker = worker
        worker.start()

    def _on_auto_name_done(self, session_id: str, reply: str):
        """AI 命名完成回调 — 委托给 ViewModel。"""
        self.vm.apply_ai_name(session_id, reply)

    def _request_scroll(self):
        """节流滚动：流式输出期间最多每 100ms 滚动一次。"""
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _do_scroll(self):
        sb = self._msg_scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _scroll_to_bottom(self):
        QTimer.singleShot(0, self._do_scroll)

    def show_welcome(self):
        self._session = None
        self.vm._session = None
        self._show_welcome()


# ═══════════════════════════════════════════════════════════════
# 对话页面整体
# ═══════════════════════════════════════════════════════════════

class ChatView(QWidget):
    """AI 对话页：左侧对话列表 + 右侧对话工作区。"""

    def __init__(self, theme: Theme, role_manager: AIRoleManager | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._role_manager = role_manager or AIRoleManager()
        self.manager = ChatManager()
        self._vm = ChatViewModel(self.manager, self._role_manager)
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._sidebar = _ChatSidebar(self.theme, self.manager)
        self._sidebar.session_selected.connect(self._on_session_selected)
        self._sidebar.session_deleted.connect(self._on_session_deleted)
        layout.addWidget(self._sidebar)

        self._workspace = _ChatWorkspace(self.theme, self._vm)
        self._workspace.session_created.connect(self._on_session_created)
        self._workspace.title_changed.connect(self._on_title_changed)
        layout.addWidget(self._workspace, stretch=1)

        sessions = self.manager.list_sorted()
        if sessions:
            self._sidebar.select(sessions[0].id)

    def _on_session_selected(self, session_id: str):
        self._workspace.load_session(session_id)

    def _on_session_created(self, session_id: str):
        self._sidebar.select(session_id)

    def _on_title_changed(self, session_id: str):
        self._sidebar.refresh()

    def _on_session_deleted(self):
        sessions = self.manager.list_sorted()
        if not sessions:
            self._workspace.show_welcome()

    def refresh_after_clear(self):
        """外部清空聊天记录后刷新 UI。"""
        self._sidebar.refresh()
        sessions = self.manager.list_sorted()
        if sessions:
            self._sidebar.select(sessions[0].id)
        else:
            self._workspace.show_welcome()
