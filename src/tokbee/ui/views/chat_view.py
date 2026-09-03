"""AI 对话页面 — 左侧对话列表 + 右侧对话工作区。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import logging

from PySide6.QtCore import Qt, Signal, QThread, QEvent, QTimer, QSize, QMimeData
from PySide6.QtGui import QPixmap, QKeyEvent, QImage, QMouseEvent, QPainter, QColor, QPen, QTextDocument, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame,
    QLabel, QPushButton, QScrollArea, QTextEdit, QTextBrowser,
    QLineEdit, QMenu, QDialog,
    QComboBox, QFileDialog, QSizePolicy, QApplication,
)

from tokbee.ui.styles.theme import Theme, COLORS
from tokbee.ui.styles.system import make_context_menu, exec_text_edit_context_menu
from tokbee.ui.viewmodels.chat_viewmodel import ChatViewModel
from tokbee.ui.widgets.context_ring import ContextUsageRing
from tokbee.core.chat_manager import ChatManager, ChatSession
from tokbee.core.provider_store import ProviderStore, ResolvedModel
from tokbee.core.ai_client import AIClient
from tokbee.core.session_settings import SessionSettings, ProviderOptions
from tokbee.core.ai_role import AIRoleManager
from tokbee.core import context_manager as ctxman
from tokbee.core.file_reader import (
    is_image, is_document, read_image_as_base64, read_file_as_text,
    build_file_filter, save_qimage, persist_attachment,
)

logger = logging.getLogger("tokbee")

_RESOURCES = Path(__file__).parent.parent.parent / "resources"

_INPUT_MIN_HEIGHT = 90  # 当前默认高度


def _model_settings(model: ResolvedModel) -> SessionSettings:
    return SessionSettings(
        temperature=model.temperature,
        top_p=model.top_p,
        max_tokens=model.max_tokens,
        stream=model.stream,
        provider_options=ProviderOptions(
            openai_reasoning_effort=(
                model.deepseek_reasoning_effort
                if model.family == "deepseek" else model.openai_reasoning_effort
            ) if model.reasoning_enabled else "",
            thinking_enabled=(
                "on" if model.reasoning_enabled and model.family == "deepseek" else "off"
            ),
        ),
    )


def _stabilize_markdown(text: str) -> str:
    """补全未闭合的代码围栏，减轻流式半截 Markdown 导致的高度跳动。"""
    if not text:
        return text
    # 奇数个 ``` 表示围栏未闭合
    if text.count("```") % 2 == 1:
        return text + "\n```"
    return text


class _InputResizeHandle(QWidget):
    """输入框顶部拖拽条：向上拖高、向下拖矮。"""

    drag_delta = Signal(int)  # 正值 = 增高（鼠标上移）

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.setFixedHeight(8)
        self.setCursor(Qt.CursorShape.SizeVerCursor)
        self.setToolTip("拖动调整高度")
        self._dragging = False
        self._last_y = 0

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = self.theme.colors
        # 中间短横作为拖拽暗示
        mid_y = self.height() / 2
        x0 = self.width() / 2 - 14
        x1 = self.width() / 2 + 14
        pen = QPen(QColor(c.get("border", "#e5e5e5")))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(int(x0), int(mid_y), int(x1), int(mid_y))
        p.end()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_y = event.globalPosition().toPoint().y()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._dragging:
            y = event.globalPosition().toPoint().y()
            delta = self._last_y - y  # 上移为正 → 增高
            self._last_y = y
            if delta:
                self.drag_delta.emit(delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _ChatInputEdit(QTextEdit):
    """支持粘贴/拖入图片与文件的输入框。"""

    files_dropped = Signal(list)  # list[str] 本地路径

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self._session_id_provider = lambda: "tmp"
        self._menu_colors: dict = dict(COLORS)

    def set_menu_colors(self, colors: dict):
        self._menu_colors = colors

    def contextMenuEvent(self, event):
        exec_text_edit_context_menu(self, event, self._menu_colors)

    def set_session_id_provider(self, fn):
        self._session_id_provider = fn

    def insertFromMimeData(self, source: QMimeData):
        paths: list[str] = []
        sid = self._session_id_provider() or "tmp"

        if source.hasUrls():
            for url in source.urls():
                if url.isLocalFile():
                    fp = url.toLocalFile()
                    if is_image(fp) or is_document(fp):
                        paths.append(fp)

        if not paths and source.hasImage():
            img = source.imageData()
            try:
                if isinstance(img, QPixmap) and not img.isNull():
                    paths.append(save_qimage(img, sid))
                elif isinstance(img, QImage) and not img.isNull():
                    paths.append(save_qimage(img, sid))
            except Exception as e:
                logger.error("保存粘贴图片失败: %s", e)

        if paths:
            self.files_dropped.emit(paths)
            # 不把 file:// 路径或文件名再插入输入框
            return

        super().insertFromMimeData(source)

    def dragEnterEvent(self, event):
        md = event.mimeData()
        if md.hasUrls() or md.hasImage():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        md = event.mimeData()
        paths: list[str] = []
        sid = self._session_id_provider() or "tmp"
        if md.hasUrls():
            for url in md.urls():
                if url.isLocalFile():
                    fp = url.toLocalFile()
                    if is_image(fp) or is_document(fp):
                        paths.append(fp)
        if not paths and md.hasImage():
            img = md.imageData()
            try:
                if isinstance(img, (QImage, QPixmap)):
                    paths.append(save_qimage(img, sid))
            except Exception as e:
                logger.error("保存拖入图片失败: %s", e)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class _AutoHeightBrowser(QTextBrowser):
    """QTextBrowser that auto-sizes its height to fit all content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self._menu_colors: dict = dict(COLORS)
        # 流式高度上限：避免半截 Markdown / 宽度为 0 时算出天文数字高度
        self._height_cap = 0
        self.document().contentsChanged.connect(self._update_height)

    def set_menu_colors(self, colors: dict):
        self._menu_colors = colors

    def contextMenuEvent(self, event):
        menu = make_context_menu(self, self._menu_colors)
        cursor = self.textCursor()
        has_sel = cursor.hasSelection()
        has_text = bool(self.toPlainText())
        copy_act = menu.addAction("复制")
        copy_act.setShortcut("Ctrl+C")
        copy_act.setEnabled(has_sel or has_text)
        link = self.anchorAt(event.pos())
        copy_link_act = None
        if link:
            copy_link_act = menu.addAction("复制链接")
        menu.addSeparator()
        select_all_act = menu.addAction("全选")
        select_all_act.setEnabled(has_text)
        action = menu.exec(event.globalPos())
        if action == copy_act:
            if has_sel:
                text = cursor.selectedText().replace("\u2029", "\n")
            else:
                text = self.toPlainText()
            QApplication.clipboard().setText(text)
        elif copy_link_act and action == copy_link_act:
            QApplication.clipboard().setText(link)
        elif action == select_all_act:
            cursor.select(QTextCursor.SelectionType.Document)
            self.setTextCursor(cursor)
        event.accept()

    def set_height_cap(self, cap: int):
        """cap<=0 表示不限制（结束后收紧到真实内容高度）。"""
        self._height_cap = max(0, int(cap))
        self._update_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_height()

    def _content_width(self) -> int:
        w = self.viewport().width()
        if w <= 1:
            w = self.width()
        if w <= 1:
            parent = self.parentWidget()
            if parent is not None:
                w = parent.width() - 24
        return max(w, 200)

    def _update_height(self):
        doc = self.document()
        doc.setTextWidth(self._content_width())
        margins = self.contentsMargins()
        h = int(doc.size().height()) + margins.top() + margins.bottom() + 2 * self.frameWidth()
        h = max(h, 30)
        if self._height_cap > 0:
            h = min(h, self._height_cap)
        # 仅在变化明显时改高度，减少微抖
        if abs(h - self.height()) >= 2:
            self.setFixedHeight(h)

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self.minimumHeight() or 30)


class _UserBubbleLabel(QLabel):
    """用户气泡：按最大宽度正确换行并计算高度，避免长文被裁切。"""

    _H_PAD = 28  # 左右 padding 估算
    _V_PAD = 24  # 上下 padding 估算

    def __init__(self, text: str, max_width: int, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        c = theme.colors
        self._max_w = max(160, int(max_width))
        font = self.font()
        font.setPixelSize(13)
        self.setFont(font)
        self.setText(text)
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Minimum)
        self.setStyleSheet(f"""
            QLabel {{
                background: #dcf8c6;
                border-radius: 10px;
                padding: 10px 14px;
                font-size: 13px;
                color: {c["text"]};
            }}
        """)
        self.setMaximumWidth(self._max_w)
        self._apply_size()

    def contextMenuEvent(self, event):
        menu = make_context_menu(self, self.theme.colors)
        copy_act = menu.addAction("复制")
        copy_act.setShortcut("Ctrl+C")
        copy_act.setEnabled(bool(self.text()))
        action = menu.exec(event.globalPos())
        if action == copy_act:
            QApplication.clipboard().setText(self.text())
        event.accept()

    def set_max_bubble_width(self, max_width: int):
        self._max_w = max(160, int(max_width))
        self.setMaximumWidth(self._max_w)
        self._apply_size()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._calc_size(width)[1]

    def sizeHint(self) -> QSize:
        w, h = self._calc_size(self._max_w)
        return QSize(w, h)

    def minimumSizeHint(self) -> QSize:
        return QSize(60, 36)

    def _calc_size(self, max_width: int) -> tuple[int, int]:
        max_width = max(80, int(max_width))
        doc = QTextDocument()
        doc.setDefaultFont(self.font())
        doc.setPlainText(self.text())
        content_max = max(40, max_width - self._H_PAD)
        # idealWidth：不换行时的自然宽度
        doc.setTextWidth(-1)
        ideal = int(doc.idealWidth()) + 4
        content_w = min(content_max, max(ideal, 40))
        doc.setTextWidth(content_w)
        h = int(doc.size().height()) + self._V_PAD
        w = content_w + self._H_PAD
        return max(60, w), max(36, h)

    def _apply_size(self):
        w, h = self._calc_size(self._max_w)
        self.setFixedSize(w, h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 父级变窄时收紧
        parent = self.parentWidget()
        if parent is not None and parent.width() > 0:
            avail = parent.width()
            if avail < self._max_w:
                self._max_w = max(160, avail)
                self.setMaximumWidth(self._max_w)
                self._apply_size()


class _AiNameWorker(QThread):
    """后台线程：调用 AI 生成对话标题。"""
    name_ready = Signal(str)
    failed = Signal(str)

    def __init__(self, client: AIClient, messages: list[dict], settings: SessionSettings, parent=None):
        super().__init__(parent)
        self._client = client
        self._messages = messages
        self._settings = settings

    def run(self):
        try:
            resp = self._client.chat(self._messages, settings=self._settings)
            content = resp.content or ""
            if not content.strip() and resp.reasoning_content:
                content = resp.reasoning_content
            self.name_ready.emit(content)
        except Exception as e:
            self.failed.emit(str(e))


class _CompactWorker(QThread):
    """后台线程：生成上下文摘要并返回新的 compaction point。"""
    compact_done = Signal(str, int, int)  # summary, boundary_index, pin_end
    failed = Signal(str)

    def __init__(
        self,
        client: AIClient | None,
        to_compact: list[dict],
        previous_summary: str,
        new_boundary: int,
        parent=None,
        *,
        pin_end: int = 0,
        settings: SessionSettings | None = None,
    ):
        super().__init__(parent)
        self._client = client
        self._to_compact = to_compact
        self._previous_summary = previous_summary
        self._new_boundary = new_boundary
        self._pin_end = int(pin_end or 0)
        self._settings = settings

    def run(self):
        summary = ""
        if self._client is not None:
            try:
                msgs = ctxman.build_summary_prompt_messages(
                    self._to_compact, self._previous_summary,
                )
                resp = self._client.chat(msgs, settings=self._settings)
                summary = (resp.content or "").strip()
                if not summary and resp.reasoning_content:
                    summary = resp.reasoning_content.strip()
            except Exception as e:
                logger.warning("LLM 摘要失败，改用机械摘要: %s", e)
        if not summary:
            summary = ctxman.mechanical_summary(self._to_compact, self._previous_summary)
        if not summary.strip():
            self.failed.emit("无法生成摘要")
            return
        self.compact_done.emit(summary, self._new_boundary, self._pin_end)


class _AIChatWorker(QThread):
    """后台线程：调用 AI chat/completions API（支持流式/非流式）。"""

    chunk_received = Signal(str, str, str)   # (session_id, content_delta, reasoning_delta)
    non_stream_done = Signal(str, str, str)  # (session_id, content, reasoning_content)
    stream_done = Signal(str)                # (session_id,)
    error = Signal(str, str)                 # (session_id, error_msg)

    def __init__(self, session_id: str, model: ResolvedModel,
                 messages: list[dict], settings: SessionSettings,
                 parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self._model = model
        self._messages = messages
        self._settings = SessionSettings.from_dict(settings.to_dict())
        self._settings.temperature = model.temperature
        self._settings.top_p = model.top_p
        self._settings.max_tokens = model.max_tokens
        self._settings.stream = model.stream
        self._settings.provider_options = ProviderOptions(
            openai_reasoning_effort=(
                (model.deepseek_reasoning_effort if model.family == "deepseek" else model.openai_reasoning_effort)
                if model.reasoning_enabled else ""
            ),
            thinking_enabled=(
                "on" if model.reasoning_enabled and model.family == "deepseek" else "off"
            ),
        )
        self._stream = model.stream
        self._cancelled = False
        self.accumulated_content = ""
        self.accumulated_reasoning = ""

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            client = AIClient(
                self._model.api_host, self._model.api_key, self._model.model_id,
                family=self._model.family,
                protocol=self._model.api_protocol,
            )
            # 让同步请求的等待/退避可被取消（A7）
            client.cancel_check = lambda: self._cancelled
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
            for chunk in client.chat_stream(self._messages, settings=self._settings):
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
                # 已有部分内容：不再当完整回答落盘，标记为「已中断」由 error 路径保留
                self.error.emit(self.session_id, f"流式中断（保留部分内容）：{e}")
            else:
                raise

    def _run_sync(self, client: AIClient):
        resp = client.chat(self._messages, settings=self._settings)
        if self._cancelled:
            return
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
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(4)
        if s.pinned:
            pin = QLabel("📌")
            pin.setStyleSheet("font-size: 11px;")
            pin.setFixedWidth(16)
            top.addWidget(pin, 0, Qt.AlignmentFlag.AlignLeft)

        title = QLabel(s.title)
        title.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        title.setWordWrap(False)
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title.setStyleSheet(f"""
            font-size: 13px; font-weight: bold; color: {c["text"]};
            background: transparent; border: none;
        """)
        top.addWidget(title, 1)
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
        info.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        info.setWordWrap(False)
        info.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        info.setStyleSheet(f"font-size: 11px; color: {c['text_hint']}; background: transparent; border: none;")
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
        # 新建对话：预填默认模型（或首个可用模型）
        store = ProviderStore()
        primary = store.first_resolved()
        session = self.manager.create(
            provider=primary.provider_id if primary else "",
            model=primary.model_id if primary else "",
        )
        self._selected_id = session.id
        self.refresh()
        self.session_selected.emit(session.id)

    def _on_context_menu(self, session_id: str, pos):
        c = self.theme.colors
        session = self.manager.get(session_id)
        if not session:
            return

        menu = make_context_menu(self, c)

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
        self._provider_store = ProviderStore()
        self._session: ChatSession | None = None
        self._current_model: ResolvedModel | None = None
        self._workers: dict[str, _AIChatWorker] = {}
        self._drafts: dict[str, str] = {}
        self._pending_files: list[str] = []
        self._compact_worker: _CompactWorker | None = None
        self._pending_send: dict | None = None  # 压缩完成后继续发送
        self._compact_rounds = 0
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(100)
        self._scroll_timer.timeout.connect(self._do_scroll)

        # 流式 UI 节流：合并短时间内多次 chunk 刷新，减少 Markdown 重排跳动
        self._stream_ui_timer = QTimer(self)
        self._stream_ui_timer.setSingleShot(True)
        self._stream_ui_timer.setInterval(60)
        self._stream_ui_timer.timeout.connect(self._flush_stream_ui)
        self._stream_ui_dirty_content = False
        self._stream_ui_dirty_reasoning = False

        self._usage_timer = QTimer(self)
        self._usage_timer.setInterval(400)
        self._usage_timer.timeout.connect(self._refresh_context_ring)

        self.vm.title_updated.connect(self._on_vm_title_updated)
        self.vm.model_changed.connect(self._on_vm_model_changed)
        self.vm.error.connect(self._show_tip)

        self._build()
        self._usage_timer.start()

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
        self._input_wrapper = input_wrapper
        input_layout = QVBoxLayout(input_wrapper)
        input_layout.setContentsMargins(20, 0, 20, 14)
        input_layout.setSpacing(8)

        self._input_resize = _InputResizeHandle(self.theme)
        self._input_resize.drag_delta.connect(self._on_input_resize_delta)
        input_layout.addWidget(self._input_resize)

        # 附件预览条
        self._attach_bar = QFrame()
        self._attach_bar.setStyleSheet(f"background: transparent;")
        self._attach_bar.setVisible(False)
        self._attach_bar_layout = QHBoxLayout(self._attach_bar)
        self._attach_bar_layout.setContentsMargins(0, 0, 0, 0)
        self._attach_bar_layout.setSpacing(6)
        self._attach_bar_layout.addStretch()
        input_layout.addWidget(self._attach_bar)

        self._input_box = _ChatInputEdit()
        self._input_box.set_menu_colors(c)
        self._input_box.setPlaceholderText("输入消息...（Enter 发送，Shift+Enter 换行，可粘贴图片）")
        self._input_min_h = _INPUT_MIN_HEIGHT
        self._input_box.setMinimumHeight(self._input_min_h)
        self._input_box.setMaximumHeight(self._input_min_h * 8)  # 窗口 resize 时再收紧
        self._input_box.setFixedHeight(self._input_min_h)
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
        self._input_box.set_session_id_provider(
            lambda: self._session.id if self._session else "tmp"
        )
        self._input_box.files_dropped.connect(self._add_pending_files)
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

        self._ctx_ring = ContextUsageRing(self.theme)
        self._ctx_ring.compress_clicked.connect(self._on_compress_clicked)
        bottom_bar.addWidget(self._ctx_ring)

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
        QTimer.singleShot(0, self._clamp_input_height)

    def _input_max_height(self) -> int:
        """软件整体高度的 2/3。"""
        win = self.window()
        h = win.height() if win is not None else self.height()
        if h <= 0:
            h = 600
        return max(self._input_min_h, int(h * 2 / 3))

    def _clamp_input_height(self):
        if not hasattr(self, "_input_box"):
            return
        max_h = self._input_max_height()
        self._input_box.setMaximumHeight(max_h)
        cur = self._input_box.height()
        new_h = min(max(cur, self._input_min_h), max_h)
        if new_h != cur:
            self._input_box.setFixedHeight(new_h)

    def _on_input_resize_delta(self, delta: int):
        if not delta:
            return
        max_h = self._input_max_height()
        new_h = min(max(self._input_box.height() + delta, self._input_min_h), max_h)
        self._input_box.setMaximumHeight(max_h)
        self._input_box.setFixedHeight(new_h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_input_height()

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
                self._add_bubble(
                    msg["role"], content, reasoning,
                    attachments=msg.get("attachments") or [],
                )

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
        self._refresh_context_ring()

    def _init_model_for_session(self):
        """根据当前对话已保存的模型初始化；无则用默认/首个可用模型。"""
        self._provider_store = ProviderStore()
        self._current_model = None
        if self._session:
            p = self._session.get_params()
            if p.provider and p.model_id:
                self._current_model = self._provider_store.resolve(p.provider, p.model_id)
            if not self._current_model and self._session.model_name:
                self._current_model = self._provider_store.resolve(
                    self._session.model_provider, self._session.model_name,
                )
        if not self._current_model:
            self._current_model = self._provider_store.first_resolved()
            if self._current_model and self._session:
                params = self._session.get_params()
                params.provider = self._current_model.provider_id
                params.model_id = self._current_model.model_id
                self._session.set_params(params)
                self.manager.save()
        self._model_btn.setEnabled(True)
        self._update_model_btn()

    def _update_model_btn(self):
        if self._current_model:
            display = f"🤖 {self._current_model.model_id}"
            self._model_btn.setText(display)
            ctx = int(self._current_model.context_window or 0)
            tip = f"{self._current_model.provider_name}/{self._current_model.model_id}"
            if ctx > 0:
                tip = f"{tip} {ctx // 1000}k"
            self._model_btn.setToolTip(tip[:30])
        else:
            self._model_btn.setText("未配置模型")
            self._model_btn.setToolTip("请选择模型")
        self._refresh_context_ring()

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
        models = ProviderStore().list_selectable_models()
        if not models:
            self._show_tip("请先在「AI配置 → 厂商设置」中配置并启用模型")
            return

        menu = make_context_menu(self, c)

        for m in models:
            check = ""
            if (self._current_model
                    and m.provider_id == self._current_model.provider_id
                    and m.model_id == self._current_model.model_id):
                check = " ✓"
            label = f"{m.provider_name} / {m.model_id}{check}"
            action = menu.addAction(label)
            action.setData(f"{m.provider_id}|{m.model_id}")

        chosen = menu.exec(self._model_btn.mapToGlobal(
            self._model_btn.rect().topLeft()
        ))
        if chosen:
            key = chosen.data()
            if key and "|" in str(key):
                pid, mid = str(key).split("|", 1)
                selected = ProviderStore().resolve(pid, mid)
                if selected:
                    self._current_model = selected
                    self._update_model_btn()
                    if self._session:
                        params = self._session.get_params()
                        params.provider = selected.provider_id
                        params.model_id = selected.model_id
                        self._session.set_params(params)
                        self.manager.save()

    def _show_welcome(self):
        """无任何对话被选中时的欢迎页。"""
        c = self.theme.colors
        self._header.hide()
        self._clear_messages()

        self._provider_store = ProviderStore()
        has_model = self._provider_store.has_any_model()
        self._current_model = self._provider_store.first_resolved() if has_model else None

        if has_model:
            self._input_box.setEnabled(True)
            self._input_box.setPlaceholderText("输入消息开始对话…")
            self._send_btn.setEnabled(True)
            self._model_btn.setEnabled(True)
            self._update_model_btn()
        else:
            self._input_box.setEnabled(False)
            self._input_box.setPlaceholderText("请先在 AI配置 → 厂商设置 中添加厂商并勾选模型")
            self._send_btn.setEnabled(False)
            self._model_btn.setEnabled(False)
            self._model_btn.setText("未选择模型")

        welcome = QWidget()
        wl = QVBoxLayout(welcome)
        wl.setAlignment(Qt.AlignmentFlag.AlignCenter)

        logo_label = QLabel()
        logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_path = _RESOURCES / "chat.png"
        if logo_path.exists():
            pixmap = QPixmap(str(logo_path)).scaled(
                64, 64,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            logo_label.setPixmap(pixmap)
        else:
            logo_label.setText("💬")
            logo_label.setStyleSheet("font-size: 48px;")
        wl.addWidget(logo_label)

        t = QLabel("TokBee")
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

        icon = QLabel("💬")
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
        tb.set_menu_colors(c)
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

    def _add_bubble(
        self,
        role: str,
        content: str,
        reasoning: str = "",
        attachments: list | None = None,
    ):
        c = self.theme.colors
        is_user = role == "user"
        attachments = attachments or []

        row = QHBoxLayout()
        row.setSpacing(0)

        if is_user:
            bubble_col = QVBoxLayout()
            bubble_col.setSpacing(6)
            bubble_col.setContentsMargins(0, 0, 0, 0)

            for att in attachments:
                if isinstance(att, str):
                    path, name, kind = att, Path(att).name, ("image" if is_image(att) else "doc")
                else:
                    path = str(att.get("path") or "")
                    name = str(att.get("name") or Path(path).name)
                    kind = str(att.get("kind") or ("image" if is_image(path) else "doc"))
                if kind == "image" and path and Path(path).is_file():
                    img = QLabel()
                    pix = QPixmap(path)
                    if not pix.isNull():
                        scaled = pix.scaled(
                            280, 280,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                        img.setPixmap(scaled)
                        img.setStyleSheet("background: transparent; border: none;")
                        img.setToolTip(name)
                        bubble_col.addWidget(img, alignment=Qt.AlignmentFlag.AlignRight)
                elif name:
                    chip = QLabel(f"📎 {name}")
                    chip.setStyleSheet(f"""
                        font-size: 12px; color: {c["text_secondary"]};
                        background: transparent; border: none;
                    """)
                    bubble_col.addWidget(chip, alignment=Qt.AlignmentFlag.AlignRight)

            text = (content or "").strip()
            # 去掉旧版纯文本附件标记行
            if text:
                lines = [
                    ln for ln in text.splitlines()
                    if not re.match(r"^\[📎 .+\]$", ln.strip())
                ]
                text = "\n".join(lines).strip()
            if text:
                max_w = 360
                if hasattr(self, "_msg_scroll") and self._msg_scroll.viewport().width() > 0:
                    max_w = max(200, int(self._msg_scroll.viewport().width() * 0.72))
                bubble = _UserBubbleLabel(text, max_w, self.theme)
                bubble_col.addWidget(bubble, alignment=Qt.AlignmentFlag.AlignRight)
            elif not attachments:
                bubble = _UserBubbleLabel("[附件]", 120, self.theme)
                bubble_col.addWidget(bubble, alignment=Qt.AlignmentFlag.AlignRight)

            col_wrapper = QWidget()
            col_wrapper.setLayout(bubble_col)
            col_wrapper.setStyleSheet("background: transparent;")
            row.addStretch(1)
            row.addWidget(col_wrapper, 3)
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
        return wrapper

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
        reasoning_browser.set_menu_colors(c)
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
                self._show_tip("请先选择模型")
                return
            provider = self._current_model.provider_id
            model = self._current_model.model_id
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
        doc_prefix = ""
        saved_attachments: list[dict] = []
        sid = self._session.id

        for fp in list(self._pending_files):
            try:
                stored = persist_attachment(fp, sid)
                if is_image(stored):
                    b64, mime = read_image_as_base64(stored)
                    api_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    })
                    saved_attachments.append({
                        "path": stored,
                        "name": Path(fp).name,
                        "kind": "image",
                    })
                elif is_document(stored):
                    extracted = read_file_as_text(stored)
                    doc_prefix += f"--- {Path(fp).name} ---\n{extracted}\n---\n\n"
                    saved_attachments.append({
                        "path": stored,
                        "name": Path(fp).name,
                        "kind": "doc",
                    })
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

        display_text = text
        if not display_text.strip() and saved_attachments:
            display_text = ""

        # 无模型时先提示再返回：不持久化用户消息、不显示气泡，避免「孤儿提问」（A3）
        if not self._current_model or not self._current_model.api_host:
            self._show_tip("请先在「AI配置 → 厂商设置」中配置模型的 API 地址")
            return

        user_msg: dict = {"role": "user", "content": display_text}
        if saved_attachments:
            user_msg["attachments"] = saved_attachments
        self._session.messages.append(user_msg)
        self.manager.touch(self._session.id)
        self._auto_title_from_user_msg(self._session)
        self.manager.save()

        self._add_bubble("user", display_text, attachments=saved_attachments)
        # 提问后立刻定位到最新用户气泡（布局完成前多次补滚）
        self._scroll_to_latest(force=True)

        params = self._session.get_params()
        params.provider = self._current_model.provider_id
        params.model_id = self._current_model.model_id
        params.temperature = self._current_model.temperature
        params.top_p = self._current_model.top_p
        params.max_tokens = self._current_model.max_tokens
        params.stream = self._current_model.stream
        params.provider_options = ProviderOptions(
            openai_reasoning_effort=(
                (self._current_model.deepseek_reasoning_effort
                if self._current_model.family == "deepseek"
                else self._current_model.openai_reasoning_effort)
                if self._current_model.reasoning_enabled else ""
            ),
            thinking_enabled=(
                "on" if self._current_model.reasoning_enabled
                and self._current_model.family == "deepseek" else "off"
            ),
        )
        self._session.set_params(params)
        self._set_sending(True)

        usage = self._compute_usage(include_draft=False)
        if ctxman.needs_compaction(usage, auto_compaction=params.auto_compaction):
            self._pending_send = {
                "sid": sid,
                "api_user_content": api_user_content,
                "params": params,
            }
            self._compact_rounds = 0
            self._start_compaction(manual=False)
            return

        self._start_chat_request(sid, api_user_content, params)

    def _history_to_api_messages(self, hist: list[dict]) -> list[dict]:
        api_messages: list[dict] = []
        for m in hist:
            atts = m.get("attachments") or []
            img_parts = []
            doc_parts = []
            for att in atts:
                path = att.get("path") if isinstance(att, dict) else str(att)
                if not path:
                    continue
                if is_image(path) and Path(path).is_file():
                    try:
                        b64, mime = read_image_as_base64(path)
                        img_parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}"},
                        })
                    except Exception:
                        pass
                elif is_document(path) and Path(path).is_file():
                    # 历史条目的文档文本发送时未落盘，这里按路径重新提取（A4）
                    try:
                        txt = read_file_as_text(path)
                    except Exception:
                        txt = ""
                    if txt.strip():
                        doc_parts.append(txt)
            content = m.get("content") or ""
            if doc_parts:
                doc_text = "\n\n".join(doc_parts)
                content = (content + "\n\n" if content else "") + doc_text
            # 有附件但文本为空时给非空兜底，避免 content:"" 触发部分端点 400（A4）
            if not content.strip() and (img_parts or atts):
                content = "（附件）"
            if img_parts:
                parts = []
                if content.strip():
                    parts.append({"type": "text", "text": content})
                parts.extend(img_parts)
                api_messages.append({"role": m["role"], "content": parts})
            else:
                api_messages.append({"role": m["role"], "content": content})
        return api_messages
        return api_messages

    def _assemble_api_messages(self, api_user_content, params: SessionSettings, session=None) -> list[dict]:
        # 按传入会话组装，缺省用当前显示会话。压缩/发送回调必须用「发起时」的会话，
        # 不能用 self._session —— 否则压缩 worker 运行期间切到别的会话，会用错上下文。
        sess = session or self._session
        assert sess is not None
        ctx_window = 0
        max_out = params.max_tokens
        if self._current_model:
            ctx_window = int(self._current_model.context_window or 0)
        summary, hist = ctxman.build_context_message_dicts(
            messages=sess.messages,
            compaction_points=sess.compaction_points,
            system_prompt=params.system_prompt,
            max_context_message_count=params.max_context_message_count,
            context_window=ctx_window,
            max_output=max_out,
            exclude_last=True,
        )
        api_messages = self._history_to_api_messages(hist)
        api_messages.append({"role": "user", "content": api_user_content})
        # system + summary 插在最前（摘要紧随 system）
        head: list[dict] = []
        if params.system_prompt.strip():
            head.append({"role": "system", "content": params.system_prompt.strip()})
        if summary.strip():
            head.append(ctxman.summary_as_message(summary))
        return head + api_messages

    def _start_chat_request(self, sid: str, api_user_content, params: SessionSettings):
        if not self._current_model:
            self._set_sending(False)
            return
        # 历史必须按该 sid 对应会话组装，而非当前显示会话（压缩/异步回调期间可能已切会话）
        session = self.manager.get(sid) or self._session
        api_messages = self._assemble_api_messages(api_user_content, params, session=session)
        use_stream = params.stream
        self._cancel_worker_for_session(sid)

        if use_stream:
            self._stream_content = ""
            self._stream_reasoning = ""
            self._stream_phase = "idle"
            self._think_buf = ""
            self._in_think_tag = False
            self._create_stream_bubble()
            self._scroll_to_latest(force=True)

        worker = _AIChatWorker(
            session_id=sid,
            model=self._current_model,
            messages=api_messages,
            settings=params,
            parent=self,
        )
        self._workers[sid] = worker
        worker.chunk_received.connect(self._on_chunk)
        worker.stream_done.connect(self._on_stream_finished)
        worker.non_stream_done.connect(self._on_non_stream_done)
        worker.error.connect(self._on_reply_error)
        worker.start()
        self._refresh_context_ring()

    def _compute_usage(self, *, include_draft: bool = True, session=None) -> ctxman.ContextUsage:
        # 传 session 时为「非当前显示会话」计算（如压缩回调对发起会话做决策），
        # 此时输入框草稿/待附图片属当前视图，不应混入其它会话。
        sess = session or self._session
        visible = sess is self._session
        params = sess.get_params() if sess else SessionSettings()
        ctx_window = int(self._current_model.context_window or 0) if self._current_model else 0
        draft = self._input_box.toPlainText() if (include_draft and visible) else ""
        pending_imgs = (sum(1 for p in self._pending_files if is_image(p))) if visible else 0
        return ctxman.estimate_session_usage(
            messages=sess.messages if sess else [],
            compaction_points=sess.compaction_points if sess else [],
            system_prompt=params.system_prompt,
            max_context_message_count=params.max_context_message_count,
            context_window=ctx_window,
            compaction_threshold=params.compaction_threshold,
            max_output=params.max_tokens,
            draft_text=draft,
            pending_image_count=pending_imgs,
        )

    def _refresh_context_ring(self):
        if not hasattr(self, "_ctx_ring"):
            return
        usage = self._compute_usage(include_draft=True)
        self._ctx_ring.set_usage(usage.used, usage.limit)
        busy = self._compact_worker is not None and self._compact_worker.isRunning()
        self._ctx_ring.set_ring_enabled(bool(self._session) and not busy)

    def _on_compress_clicked(self):
        if not self._session:
            self._show_tip("请先开始对话")
            return
        if not self._current_model or not self._current_model.api_host:
            self._show_tip("请先配置模型后再压缩")
            return
        if self._compact_worker and self._compact_worker.isRunning():
            return
        plan = ctxman.plan_compaction(self._session.messages, self._session.compaction_points)
        if plan is None:
            self._show_tip("当前对话较短，无需压缩")
            return
        self._pending_send = None
        self._start_compaction(manual=True)

    def _start_compaction(self, *, manual: bool, session=None):
        # session 缺省为当前显示会话；压缩回调期间可能切走，回调要用「发起时」的会话对象
        sess = session or self._session
        if not sess:
            return
        plan = ctxman.plan_compaction(sess.messages, sess.compaction_points)
        if plan is None:
            if manual:
                self._show_tip("当前对话较短，无需压缩")
            elif self._pending_send:
                ps = self._pending_send
                self._pending_send = None
                self._start_chat_request(ps["sid"], ps["api_user_content"], ps["params"])
            return

        to_compact, _retained, new_boundary, prev_summary, pin_end = plan
        client = None
        if self._current_model and self._current_model.api_host:
            client = AIClient(
                self._current_model.api_host,
                self._current_model.api_key,
                self._current_model.model_id,
                family=self._current_model.family,
                protocol=self._current_model.api_protocol,
            )
        if manual:
            self._set_sending(True)
            self._send_btn.setText("压缩中")

        worker = _CompactWorker(
            client,
            to_compact,
            prev_summary,
            new_boundary,
            parent=self,
            pin_end=pin_end,
            settings=_model_settings(self._current_model) if self._current_model else None,
        )
        self._compact_worker = worker
        worker.compact_done.connect(
            lambda summary, boundary, pin, m=manual, s=sess: self._on_compact_done(
                summary, boundary, m, pin_end=pin, sess=s
            )
        )
        worker.failed.connect(lambda err, m=manual: self._on_compact_failed(err, m))
        worker.start()
        self._refresh_context_ring()

    def _on_compact_done(self, summary: str, boundary: int, manual: bool, pin_end: int = 0, sess=None):
        self._compact_worker = None
        # 用「发起压缩时」的会话，而非当前显示会话（压缩期间用户可能已切换）
        session = sess or self._session
        if session is None:
            self._set_sending(False)
            self._pending_send = None
            return
        session.compaction_points = ctxman.append_compaction_point(
            session.compaction_points,
            summary=summary,
            boundary_index=boundary,
            pin_end=pin_end,
        )
        self.manager.save()
        self._refresh_context_ring()
        if manual and not self._pending_send:
            self._set_sending(False)
            self._show_tip("已压缩上下文")
            return
        if self._pending_send:
            ps = self._pending_send
            self._pending_send = None
            usage = self._compute_usage(include_draft=False, session=session)
            params = ps["params"]
            self._compact_rounds += 1
            if (
                self._compact_rounds < 3
                and ctxman.needs_compaction(usage, auto_compaction=params.auto_compaction)
            ):
                self._pending_send = ps
                self._start_compaction(manual=False, session=session)
            else:
                self._compact_rounds = 0
                self._start_chat_request(ps["sid"], ps["api_user_content"], params)

    def _on_compact_failed(self, err: str, manual: bool):
        self._compact_worker = None
        logger.error("上下文压缩失败: %s", err)
        if self._pending_send:
            # 自动压缩失败时仍尝试发送（由 token 裁剪兜底）
            ps = self._pending_send
            self._pending_send = None
            self._start_chat_request(ps["sid"], ps["api_user_content"], ps["params"])
            return
        self._set_sending(False)
        if manual:
            self._show_tip(f"压缩失败: {err}")
        self._refresh_context_ring()

    def _set_sending(self, sending: bool):
        self._input_box.setEnabled(not sending)
        self._send_btn.setEnabled(not sending)
        self._model_btn.setEnabled(not sending)
        self._attach_btn.setEnabled(not sending)
        if hasattr(self, "_ctx_ring"):
            self._ctx_ring.set_ring_enabled(not sending and bool(self._session))
        if sending:
            if self._send_btn.text() != "压缩中":
                self._send_btn.setText("…")
        else:
            self._send_btn.setText("发送")

    # ── 附件相关 ──
    def _add_pending_files(self, paths: list):
        for p in paths:
            if p and p not in self._pending_files:
                self._pending_files.append(p)
        self._refresh_attach_bar()
        self._refresh_context_ring()

    def _on_attach(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择文件", "", build_file_filter(),
        )
        self._add_pending_files(paths)

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
            if is_image(fp) and Path(fp).is_file():
                thumb = QLabel()
                pix = QPixmap(fp)
                if not pix.isNull():
                    thumb.setPixmap(pix.scaled(
                        28, 28,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    ))
                    thumb.setStyleSheet("background: transparent; border: none;")
                    hl.addWidget(thumb)
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
        self._stream_reasoning_label.set_menu_colors(c)
        # 仅作异常高度兜底（宽度未就绪时文档会算出天文数字）；正常长回复不应触顶
        vp_h = self._msg_scroll.viewport().height() if hasattr(self, "_msg_scroll") else 600
        stream_cap = max(4000, int(vp_h) * 4)
        self._stream_reasoning_label.set_height_cap(stream_cap)
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
        self._stream_reply_label.set_height_cap(stream_cap)
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

        # 滚动由 _flush_stream_ui 统一触发

    def _schedule_stream_ui(self):
        if not self._stream_ui_timer.isActive():
            self._stream_ui_timer.start()

    def _flush_stream_ui(self):
        """节流后的实际界面刷新。"""
        if self._stream_ui_dirty_reasoning and self._stream_reasoning_label is not None:
            self._stream_reasoning_label.setMarkdown(
                _stabilize_markdown(self._stream_reasoning)
            )
            self._stream_reasoning_label._update_height()
            self._stream_ui_dirty_reasoning = False
        if self._stream_ui_dirty_content and self._stream_reply_label is not None:
            self._stream_reply_label.setMarkdown(
                _stabilize_markdown(self._stream_content)
            )
            self._stream_reply_label._update_height()
            self._stream_ui_dirty_content = False
        self._request_scroll()

    def _show_reasoning_chunk(self, text: str):
        if not text:
            return
        if self._stream_phase != "reasoning":
            self._stream_phase = "reasoning"
        if self._stream_thinking_frame is not None and not self._stream_thinking_frame.isVisible():
            self._stream_thinking_frame.setVisible(True)
        self._stream_reasoning += text
        self._stream_ui_dirty_reasoning = True
        self._schedule_stream_ui()

    def _show_content_chunk(self, text: str):
        if not text:
            return
        if self._stream_phase == "reasoning":
            if self._stream_thinking_header is not None:
                self._stream_thinking_header.setText("💭 思考过程")
            self._stream_phase = "content"
        elif self._stream_phase == "idle":
            self._stream_phase = "content"
        self._stream_content += text
        self._stream_ui_dirty_content = True
        self._schedule_stream_ui()

    def _on_stream_finished(self, worker_sid: str):
        """流式输出完成：持久化消息、转换思考区域为可折叠。"""
        worker = self._workers.pop(worker_sid, None)

        if worker:
            self.vm.save_stream_result(
                worker_sid, worker.accumulated_content, worker.accumulated_reasoning,
            )

        if self._session and self._session.id == worker_sid:
            self._set_sending(False)
            self._refresh_context_ring()

            if hasattr(self, "_think_buf") and self._think_buf:
                if self._in_think_tag:
                    self._show_reasoning_chunk(self._think_buf)
                else:
                    self._show_content_chunk(self._think_buf)
                self._think_buf = ""

            # 刷出节流队列中的最后一帧，再用最终 Markdown 定稿
            self._stream_ui_timer.stop()
            self._flush_stream_ui()

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
                self._stream_reply_label.set_height_cap(0)
                self._stream_reply_label.setMarkdown(self._stream_content)
                self._stream_reply_label._update_height()

            if self._stream_reasoning_label is not None:
                self._stream_reasoning_label.set_height_cap(0)

            self._stream_wrapper = None
            self._stream_reply_label = None
            self._stream_reasoning_label = None
            self._stream_thinking_frame = None
            self._stream_thinking_header = None
            self._stream_ui_dirty_content = False
            self._stream_ui_dirty_reasoning = False
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
            self._refresh_context_ring()

    def _on_reply_error(self, worker_sid: str, error_msg: str):
        self._workers.pop(worker_sid, None)
        if not self._session or self._session.id != worker_sid:
            return
        self._set_sending(False)
        self._refresh_context_ring()
        # 有部分流式内容：保留气泡并标注「已中断」，不落盘为完整回答（见 A2）
        partial = (
            (self._stream_content or "").strip()
            or (self._stream_reasoning or "").strip()
            or (getattr(self, "_think_buf", None) or "").strip()
        )
        if partial and getattr(self, "_stream_wrapper", None):
            self._stream_ui_timer.stop()
            self._flush_stream_ui()
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
            if self._stream_phase == "reasoning" and self._stream_thinking_frame:
                self._convert_thinking_to_collapsible()
            if self._stream_reply_label:
                self._stream_reply_label.set_height_cap(0)
                banner = f"\n\n> ⚠ **回复中断**（保留部分内容）：{(error_msg or '未知错误')[:200]}"
                self._stream_reply_label.setMarkdown(
                    (self._stream_content or "") + banner
                )
                self._stream_reply_label._update_height()
            self._stream_wrapper = None
            self._stream_reply_label = None
            self._stream_reasoning_label = None
            self._stream_thinking_frame = None
            self._stream_thinking_header = None
            self._stream_ui_dirty_content = False
            self._stream_ui_dirty_reasoning = False
            self._scroll_to_bottom()
        else:
            if getattr(self, "_stream_wrapper", None):
                self._stream_wrapper.deleteLater()
                self._stream_wrapper = None
            self._show_tip(f"AI 回复失败:\n{error_msg}")

    def _show_params_dialog(self):
        self._show_tip("模型调用参数已统一移至「AI配置 → 厂商设置 → 模型设置」")

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

    def _on_vm_model_changed(self, model: ResolvedModel | None):
        self._current_model = model
        self._update_model_btn()
        self._refresh_context_ring()

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

        primary = self._current_model or ProviderStore().first_resolved()
        if not primary or not primary.api_host:
            return
        client = AIClient(
            primary.api_host, primary.api_key, primary.model_id,
            family=primary.family, protocol=primary.api_protocol,
        )
        messages = [
            {"role": "system", "content": (
                '根据用户的对话内容，生成一个简短的中文对话标题。\n'
                '输出 JSON：{"name": "对话标题"}\n'
                '要求：中文，15字以内，只输出 JSON。'
            )},
            {"role": "user", "content": raw.strip()[:500]},
        ]
        sid = session.id
        worker = _AiNameWorker(client, messages, _model_settings(primary), parent=self)
        worker.name_ready.connect(lambda reply, _sid=sid: self._on_auto_name_done(_sid, reply))
        worker.failed.connect(lambda _err: None)
        self._auto_name_worker = worker
        worker.start()

    def _on_auto_name_done(self, session_id: str, reply: str):
        """AI 命名完成回调 — 委托给 ViewModel。"""
        self.vm.apply_ai_name(session_id, reply)

    def _request_scroll(self):
        """节流滚动；用户上翻阅读时不强制拉回底部。"""
        sb = self._msg_scroll.verticalScrollBar()
        if sb.maximum() - sb.value() > 120:
            return
        if not self._scroll_timer.isActive():
            self._scroll_timer.start()

    def _do_scroll(self):
        # 先让布局吃到最新气泡高度，再滚到底
        container = self._msg_scroll.widget()
        if container is not None:
            container.adjustSize()
            container.updateGeometry()
        sb = self._msg_scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _scroll_to_bottom(self):
        self._do_scroll()
        QTimer.singleShot(0, self._do_scroll)
        QTimer.singleShot(50, self._do_scroll)

    def _scroll_to_latest(self, *, force: bool = True):
        """定位到消息区最新气泡（提问后 / 流式气泡出现后）。"""
        def _go():
            container = self._msg_scroll.widget()
            if container is not None:
                container.adjustSize()
                container.updateGeometry()
            # 滚到最新一条消息控件
            if self._msg_layout.count() > 0:
                item = self._msg_layout.itemAt(self._msg_layout.count() - 1)
                w = item.widget() if item else None
                if w is not None:
                    self._msg_scroll.ensureWidgetVisible(w, 0, 24)
            sb = self._msg_scroll.verticalScrollBar()
            sb.setValue(sb.maximum())

        if not force:
            self._request_scroll()
            return
        _go()
        QTimer.singleShot(0, _go)
        QTimer.singleShot(40, _go)
        QTimer.singleShot(120, _go)

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
