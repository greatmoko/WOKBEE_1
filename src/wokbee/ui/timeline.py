"""WokBee 时间线：事件气泡、工具步骤行状态机与实时状态条。"""

from __future__ import annotations

import json
import re

from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from tokbee.ui.styles.system import make_context_menu
from tokbee.ui.styles.theme import Theme

from wokbee.core.models import ProjectEvent

# 气泡正文预览：折叠时高度上限（约 10 行）
BUBBLE_PREVIEW_CHARS = 400
BUBBLE_PREVIEW_LINES = 10
BUBBLE_COLLAPSED_HEIGHT = 200
# 全局收拢模式：正文约 1 行（配合气泡头「角色 · 时间」共约两行可见信息）
BUBBLE_COMPACT_HEIGHT = 22
BUBBLE_COMPACT_CHARS = 80
SCROLL_STICK_THRESHOLD = 48


def _preview_text(full: str, *, max_chars: int = BUBBLE_PREVIEW_CHARS, max_lines: int = BUBBLE_PREVIEW_LINES) -> tuple[str, bool]:
    """返回 (预览文本, 是否被截断)。按行数与字数双重限制。"""
    text = full or ""
    if not text:
        return "", False
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        text = "\n".join(lines[:max_lines])
        truncated = True
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    if truncated and not text.endswith("…"):
        text = text.rstrip() + "…"
    return text, truncated


def _stabilize_markdown(text: str) -> str:
    if not text:
        return text
    if text.count("```") % 2 == 1:
        return text + "\n```"
    return text


def _event_ui_role(kind: str) -> str:
    """归类：user | ai | tool | system | error"""
    if kind == "user":
        return "user"
    if kind == "agent":
        return "ai"
    if kind == "tool":
        return "tool"
    if kind == "error":
        return "error"
    return "system"  # info / approval / lesson / …


class _AutoHeightMd(QTextBrowser):
    """按文档内容自适应高度的 Markdown 浏览器。"""

    def __init__(self, theme: Theme, *, danger: bool = False, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._height_cap = 0
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.setOpenExternalLinks(True)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        c = theme.colors
        color = c.get("danger", "#c0392b") if danger else c["text"]
        self.setStyleSheet(f"""
            QTextBrowser {{
                background: transparent; border: none;
                font-size: 13px; color: {color};
                padding: 0;
            }}
            QTextBrowser a {{ color: {c.get("accent", "#2f6fed")}; }}
        """)
        self.document().contentsChanged.connect(self._update_height)

    def set_height_cap(self, cap: int):
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
                w = parent.width() - 8
        return max(w, 160)

    def _update_height(self):
        doc = self.document()
        doc.setTextWidth(self._content_width())
        margins = self.contentsMargins()
        h = int(doc.size().height()) + margins.top() + margins.bottom() + 4
        h = max(h, 24)
        if self._height_cap > 0:
            h = min(h, self._height_cap)
        if abs(h - self.height()) >= 2:
            self.setFixedHeight(h)

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), self.height() or 24)

    def set_markdown(self, text: str):
        self.setMarkdown(_stabilize_markdown(text or ""))
        self._update_height()

    def set_danger(self, danger: bool):
        c = self.theme.colors
        color = c.get("danger", "#c0392b") if danger else c["text"]
        self.setStyleSheet(f"""
            QTextBrowser {{
                background: transparent; border: none;
                font-size: 13px; color: {color};
                padding: 0;
            }}
            QTextBrowser a {{ color: {c.get("accent", "#2f6fed")}; }}
        """)

    def contextMenuEvent(self, event):
        menu = make_context_menu(self, self.theme.colors)
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


def _tool_event_display_text(ev: ProjectEvent) -> str:
    """工具气泡正文：优先用结构化 meta 格式化，避免 call 挤成一行。"""
    meta = ev.meta if isinstance(ev.meta, dict) else {}
    phase = str(meta.get("phase") or "").lower()
    tool = str(meta.get("tool") or "").strip()
    content = (ev.content or "").strip()

    # 已是新格式
    if content.startswith("**call:**") or content.startswith("**callback:**"):
        return content

    if phase == "call" and tool:
        args = meta.get("args") if isinstance(meta.get("args"), dict) else {}
        try:
            from wokbee.engine.runner import format_tool_call_for_timeline

            return format_tool_call_for_timeline(tool, args)
        except Exception:
            pass
        # 轻量回退
        lines = [f"**call:** `{tool}`"]
        for k, v in list(args.items())[:8]:
            s = str(v)
            if k in ("content", "body", "text", "command") and len(s) > 200:
                s = s[:200] + f"…（共 {len(str(v))} 字）"
            s = s.replace("\\n", "\n")
            if "\n" in s:
                lines.append(f"- **{k}:**")
                lines.extend(f"  {ln}" for ln in s.splitlines()[:12])
            else:
                lines.append(f"- **{k}:** {s}")
        return "\n".join(lines)

    if phase == "callback" and tool:
        body = content
        for prefix in (f"callback: {tool}", f"callback: {tool}\n"):
            if body.startswith(prefix):
                body = body[len(prefix) :].lstrip("\n")
                break
        if body.startswith("callback:"):
            body = body.split("\n", 1)[-1] if "\n" in body else ""
        try:
            from wokbee.engine.runner import format_tool_callback_for_timeline

            return format_tool_callback_for_timeline(tool, body or content)
        except Exception:
            return f"**callback:** `{tool}`\n\n```\n{(body or content)[:2000]}\n```"

    # 旧版纯文本 call: write_file({...})
    if content.startswith("call: ") or content.startswith("call:"):
        rest = content.split(":", 1)[-1].strip()
        m = re.match(r"^([A-Za-z_][\w]*)\((.*)\)\s*$", rest, re.DOTALL)
        if m:
            name, args_s = m.group(1), m.group(2).strip()
            args: dict = {}
            try:
                val = json.loads(args_s)
                if isinstance(val, dict):
                    args = val
            except json.JSONDecodeError:
                # 截断的 JSON：尽量展示原始多行
                pretty = args_s.replace("\\n", "\n")
                return f"**call:** `{name}`\n\n```\n{pretty[:2500]}\n```"
            try:
                from wokbee.engine.runner import format_tool_call_for_timeline

                return format_tool_call_for_timeline(name, args)
            except Exception:
                return f"**call:** `{name}`\n\n```\n{args_s[:2000]}\n```"

    if content.startswith("callback:"):
        parts = content.split("\n", 1)
        head = parts[0]
        body = parts[1] if len(parts) > 1 else ""
        name = head.split(":", 1)[-1].strip() or "tool"
        return f"**callback:** `{name}`\n\n```\n{body[:2000]}\n```"

    return content


class _ExpandableBody(QWidget):
    """正文：Markdown 渲染；默认高度上限，可展开全部。"""

    def __init__(
        self,
        text: str,
        theme: Theme,
        *,
        danger: bool = False,
        default_collapsed: bool = False,
        toggle_text: str = "",
        hide_toggle: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self._full = text or ""
        self._danger = danger
        self._default_collapsed = default_collapsed
        self._toggle_text = toggle_text
        self._hide_toggle = hide_toggle
        self._expanded = not default_collapsed
        self._global_compact = False
        self._manual_override = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        c = theme.colors
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._browser = _AutoHeightMd(theme, danger=danger, parent=self)
        lay.addWidget(self._browser)
        self._toggle = QPushButton()
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.setFlat(True)
        self._toggle.setStyleSheet(
            f"QPushButton {{ color: {c['accent']}; font-size: 12px; border: none; "
            f"text-align: left; padding: 0; background: transparent; }}"
            f"QPushButton:hover {{ color: {c.get('accent_hover', '#06ad56')}; "
            f"background: transparent; }}"
            f"QPushButton:pressed {{ color: {c.get('accent_hover', '#06ad56')}; "
            f"background: transparent; }}"
        )
        self._toggle.clicked.connect(self._on_toggle)
        lay.addWidget(self._toggle)
        self._apply()

    def refresh_height(self):
        # QTimer.singleShot(0/30, ...) 可能在 deleteLater 后触发，跳过已销毁的 browser。
        try:
            self._browser._update_height()
        except RuntimeError:
            return

    def set_global_compact(self, compact: bool, *, reset_manual: bool = True) -> None:
        """全局收拢/展开：收拢时统一约一行正文，用户可单独展开某条。"""
        self._global_compact = bool(compact)
        if reset_manual:
            self._manual_override = False
        if self._global_compact:
            self._expanded = bool(self._manual_override)
        else:
            self._expanded = not self._default_collapsed
        self._apply()
        QTimer.singleShot(0, self.refresh_height)
        QTimer.singleShot(30, self.refresh_height)

    def _needs_expand(self) -> bool:
        full = self._full
        if not full:
            return False
        if self._global_compact and not self._manual_override:
            return (
                len(full.splitlines()) > 1
                or len(full) > BUBBLE_COMPACT_CHARS
            )
        if self._default_collapsed:
            return True
        _, need = _preview_text(full)
        if need:
            return True
        # 行少但 Markdown 渲染后仍可能很高：用折叠高度兜底
        return len(full) > BUBBLE_PREVIEW_CHARS or len(full.splitlines()) > BUBBLE_PREVIEW_LINES

    def set_content(self, text: str, *, danger: bool | None = None) -> None:
        """原位替换内容/配色（工具步骤行用），保持折叠状态。"""
        self._full = text or ""
        if danger is not None and danger != self._danger:
            self._danger = danger
            self._browser.set_danger(danger)
        self._apply()
        QTimer.singleShot(0, self.refresh_height)
        QTimer.singleShot(30, self.refresh_height)

    def _apply(self):
        full = self._full
        need = self._needs_expand()
        toggle_visible = need and not self._hide_toggle
        if self._global_compact and not self._manual_override:
            self._browser.set_height_cap(BUBBLE_COMPACT_HEIGHT)
            self._browser.set_markdown(full)
            self._toggle.setVisible(toggle_visible)
            self._toggle.setText("展开" if need else "")
        elif not need or self._expanded:
            self._browser.set_height_cap(0)
            self._browser.set_markdown(full)
            self._toggle.setVisible(toggle_visible)
            self._toggle.setText("收起" if need and not self._global_compact else "")
        else:
            self._browser.set_height_cap(BUBBLE_COLLAPSED_HEIGHT)
            self._browser.set_markdown(full)
            self._toggle.setVisible(toggle_visible)
            if self._toggle_text:
                self._toggle.setText(self._toggle_text)
            else:
                n_lines = len(full.splitlines())
                self._toggle.setText(f"展开全部（{n_lines} 行 / {len(full)} 字）")

    def _on_toggle(self):
        if self._global_compact:
            self._manual_override = not self._manual_override
            self._expanded = self._manual_override
        else:
            self._expanded = not self._expanded
        self._apply()
        # 展开后重新量高，避免残留空白
        QTimer.singleShot(0, self.refresh_height)
        QTimer.singleShot(30, self.refresh_height)


class _LiveStatusBar(QFrame):
    """实时状态条：显示「正在思考… / 正在调用工具…」等，配脉冲光点，避免界面像死机。"""

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        c = theme.colors
        accent = c.get("accent", "#2f6fed")
        self.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(6)
        self._dot = QLabel("●")
        self._dot.setStyleSheet(
            f"color: {accent}; font-size: 11px; background: transparent; border: none;"
        )
        lay.addWidget(self._dot)
        self._label = QLabel("")
        self._label.setStyleSheet(
            f"font-size: 12px; color: {c['text_hint']}; "
            "background: transparent; border: none;"
        )
        lay.addWidget(self._label)
        lay.addStretch(1)
        self.setVisible(False)
        self._pulse_on = False
        self._t = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(400)

    def _tick(self):
        if not self._pulse_on:
            return
        self._t += 1
        self._dot.setText("●" if self._t % 2 == 0 else "◐")

    def set_status(self, text: str):
        self._label.setText(text or "")
        self.setVisible(True)

    def set_pulse(self, on: bool):
        self._pulse_on = bool(on)
        if not on:
            self._dot.setText("●")

    def clear(self):
        self._label.setText("")
        self._pulse_on = False
        self.setVisible(False)


class _ThinkingBlock(QFrame):
    """AI 思考块：可折叠的「💭 思考过程」，默认折叠，借鉴 tokbee 的思路。"""

    def __init__(self, text: str, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        c = theme.colors
        accent = c.get("accent", "#2f6fed")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.setStyleSheet(f"""
            QFrame {{
                background: {c.get("accent_light", "#eaf1fe")};
                border: 1px solid {accent}55;
                border-left: 3px solid {accent};
                border-radius: 8px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        head = QLabel("💭 思考过程")
        head.setStyleSheet(
            f"font-size: 12px; font-weight: bold; color: {accent}; "
            "background: transparent; border: none;"
        )
        lay.addWidget(head)
        self._body = _ExpandableBody(
            text or "",
            self.theme,
            default_collapsed=True,
            toggle_text="查看思考",
        )
        lay.addWidget(self._body)
        self.bubble = self  # 作为气泡被 _bubbles 追踪


class _ToolStepRow(QFrame):
    """工具步骤行：把一次「工具调用 call + 结果 callback」合并成一行。

    头行默认只显示 工具名 + 状态chip + 折叠箭头；点击展开可见已传参数与返回详情。
    状态在 callback 到达时原位更新：running/pending → ok/empty/failed/skipped。
    """

    STATUS_LABELS = {
        "running": "调用中",
        "pending": "待确认",
        "ok": "成功",
        "empty": "返回为空",
        "failed": "失败",
        "skipped": "未完成",
    }
    STATUS_COLORS = {
        "running": "#f59e0b",
        "pending": "#f59e0b",
        "ok": "#10b981",
        "empty": "#6b7280",
        "failed": "#ef4444",
        "skipped": "#9ca3af",
    }
    _PULSE = ["调用中", "调用中·", "调用中··", "调用中···"]

    def __init__(
        self,
        step_id: str,
        tool: str,
        theme: Theme,
        *,
        args: dict | None = None,
        index: int = 0,
        parent=None,
    ):
        super().__init__(parent)
        self.step_id = step_id
        self.tool = (tool or "tool").strip() or "tool"
        self.theme = theme
        self._index = index
        self._status = "running"
        self._args = args if isinstance(args, dict) else None
        self._callback_display = ""
        self._global_compact = False
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick_pulse)
        self._pulse_i = 0
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        self._build()
        self.set_running(args=self._args)

    def _build(self):
        c = self.theme.colors
        self.setStyleSheet(f"""
            QFrame {{
                background: {c.get("tool_bg", "#fff8e1")};
                border: 1px solid {c.get("tool_border", "#f2d97e")};
                border-radius: 10px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(6)
        label = f"#{self._index} {self.tool}" if self._index else self.tool
        self._name = QLabel(label)
        self._name.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {c['text']}; "
            "background: transparent; border: none;"
        )
        header.addWidget(self._name)
        self._chip = QLabel()
        self._chip.setStyleSheet(
            f"font-size: 11px; padding: 0 8px; border-radius: 8px; "
            f"background: transparent; border: none;"
        )
        header.addWidget(self._chip)
        header.addStretch(1)
        self._toggle = QPushButton("▸")
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.setFlat(True)
        self._toggle.setFixedSize(22, 20)
        self._toggle.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"color: {c['text_hint']}; font-size: 12px; }}"
        )
        self._toggle.clicked.connect(self._toggle_body)
        header.addWidget(self._toggle)
        lay.addLayout(header)
        self._body = _ExpandableBody(
            "",
            self.theme,
            default_collapsed=True,
            hide_toggle=True,
            toggle_text="查看已传参数与返回",
        )
        lay.addWidget(self._body)

    # ── 状态机 ───────────────────────────────────────────────
    def set_running(self, args: dict | None = None):
        if args is not None and isinstance(args, dict):
            self._args = args
        self._status = "running"
        if not self._pulse_timer.isActive():
            self._pulse_timer.start(500)
        self._apply_header()
        self._apply_body()

    def set_success(self, callback_content: str):
        content = (callback_content or "").strip()
        self._status = "empty" if (not content or "（无输出）" in content) else "ok"
        self._callback_display = content
        self._stop_pulse()
        self._apply_header()
        self._apply_body()

    def set_failed(self, callback_content: str):
        content = (callback_content or "").strip()
        self._status = "failed"
        self._callback_display = content or (
            f"**callback:** `{self.tool}`\n\n```\n（工具抛出的错误未返回正文）\n```"
        )
        self._stop_pulse()
        self._apply_header()
        self._apply_body()

    def set_pending(self):
        self._status = "pending"
        self._stop_pulse()
        self._apply_header()
        self._apply_body()

    def set_skipped(self):
        self._status = "skipped"
        self._stop_pulse()
        self._apply_header()
        self._apply_body()

    def _stop_pulse(self):
        if self._pulse_timer.isActive():
            self._pulse_timer.stop()

    # ── 内部 ───────────────────────────────────────────────
    def _tick_pulse(self):
        if self._status != "running":
            return
        self._pulse_i = (self._pulse_i + 1) % len(self._PULSE)
        self._set_chip(self._PULSE[self._pulse_i], self.STATUS_COLORS["running"])

    def _apply_header(self):
        label = self.STATUS_LABELS.get(self._status, "调用中")
        color = self.STATUS_COLORS.get(self._status, "#f59e0b")
        self._chip.setText(label)
        self._chip.setStyleSheet(
            f"font-size: 11px; padding: 0 8px; border-radius: 8px; "
            f"background: {color}1f; color: {color}; border: none;"
        )

    def _apply_body(self):
        self._body.set_content(self._body_text(), danger=(self._status == "failed"))

    def _set_chip(self, text: str, color: str):
        self._chip.setText(text)
        self._chip.setStyleSheet(
            f"font-size: 11px; padding: 0 8px; border-radius: 8px; "
            f"background: {color}1f; color: {color}; border: none;"
        )

    def _body_text(self) -> str:
        parts = []
        if self._args:
            try:
                from wokbee.engine.runner import format_tool_call_for_timeline

                parts.append(format_tool_call_for_timeline(self.tool, self._args))
            except Exception:
                parts.append(f"**call:** `{self.tool}`")
        else:
            parts.append(f"**call:** `{self.tool}`")
        if self._callback_display:
            parts.append(self._callback_display)
        elif self._status in ("running", "pending"):
            parts.append(f"**callback:** `{self.tool}`\n\n（等待返回…）")
        else:
            parts.append(
                f"**callback:** `{self.tool}`\n\n（{self.STATUS_LABELS.get(self._status, '—')}）"
            )
        return "\n\n".join(parts)

    def _toggle_body(self):
        if getattr(self, "_global_compact", False):
            self._body.setVisible(not self._body.isVisible())
            if self._body.isVisible():
                self._body.set_global_compact(True, reset_manual=False)
                self._body._manual_override = True
                self._body._expanded = True
                self._body._apply()
            self._toggle.setText("▾" if self._body.isVisible() else "▸")
        else:
            self._body._on_toggle()
            expanded = getattr(self._body, "_expanded", False)
            self._toggle.setText("▾" if expanded else "▸")
        QTimer.singleShot(0, self._body.refresh_height)
        QTimer.singleShot(30, self._body.refresh_height)

    def set_global_compact(self, compact: bool) -> None:
        """全局收拢：仅保留头行；用户可点 ▸ 展开详情。"""
        self._global_compact = bool(compact)
        if compact:
            self._body.setVisible(False)
            self._body.set_global_compact(True, reset_manual=True)
            self._toggle.setText("▸")
        else:
            self._body.setVisible(True)
            self._body.set_global_compact(False, reset_manual=True)
            expanded = getattr(self._body, "_expanded", False)
            self._toggle.setText("▾" if expanded else "▸")


class _Timeline(QFrame):
    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._bubbles: list[QFrame] = []
        self._bodies: list[_ExpandableBody] = []
        # 工具步骤行注册表（Phase B）：按 tool_call_id 配对 call ↔ callback
        self._tool_steps: dict[str, _ToolStepRow] = {}
        self._batch_order: list[str] = []
        self._unmatched_calls: list[_ToolStepRow] = []
        self._pending_rows: set[str] = set()
        self._status_bar: _LiveStatusBar | None = None  # Phase C 填充
        self._stick_to_bottom = True
        self._agent_running = False
        self._scroll_programmatic = False
        self._global_compact = False
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setStyleSheet(f"_Timeline {{ background: {c['content_bg']}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._status_bar = _LiveStatusBar(self.theme)
        layout.addWidget(self._status_bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(10)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._container)
        self._scroll = scroll
        layout.addWidget(scroll)
        bar = self._scroll.verticalScrollBar()
        bar.valueChanged.connect(self._on_user_scroll)
        bar.rangeChanged.connect(self._on_scroll_range_changed)

    def _on_user_scroll(self, value: int) -> None:
        if self._scroll_programmatic:
            return
        bar = self._scroll.verticalScrollBar()
        self._stick_to_bottom = (bar.maximum() - value) <= SCROLL_STICK_THRESHOLD

    def _on_scroll_range_changed(self, _min: int, _max: int) -> None:
        """内容高度变化（布局逐步稳定）时，若应贴底则继续滚到底，避免停在旧位置。

        长会话/多项目重排时，_schedule_scroll_to_bottom 的固定 0/30/100/250/500ms
        重刷可能早于实际布局完成（气泡高度异步算出），此时 maximum 仍为 0，
        等到高度真正爆发后已无重刷跟进 → 时间线停在顶部。rangeChanged 在范围
        **真实变化**的那一刻触发，只要贴底就补滚到底，且范围不再变化就不会再滚。
        """
        if not self._stick_to_bottom:
            return
        self._scroll_to_bottom()

    def set_agent_running(self, running: bool) -> None:
        self._agent_running = bool(running)

    def reset_scroll_anchor(self) -> None:
        """切换项目等场景：下次刷新后滚到最新。"""
        self._stick_to_bottom = True

    def set_global_compact_mode(self, compact: bool) -> None:
        self._global_compact = bool(compact)
        for body in list(self._bodies):
            try:
                body.set_global_compact(compact)
            except RuntimeError:
                continue
        for bub in list(self._bubbles):
            if isinstance(bub, _ToolStepRow):
                try:
                    bub.set_global_compact(compact)
                except RuntimeError:
                    continue
        QTimer.singleShot(0, self._sync_bubble_widths)

    def show_empty(self, text: str = "选择或新建一个项目开始。"):
        self._clear()
        c = self.theme.colors
        empty = QLabel(text)
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setStyleSheet(f"font-size: 13px; color: {c['text_hint']}; padding: 40px;")
        self._layout.addWidget(empty)

    def _clear(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._bubbles = []
        self._bodies = []
        self._tool_steps = {}
        self._batch_order = []
        self._unmatched_calls = []
        self._pending_rows = set()

    def render_events(self, events: list[ProjectEvent]):
        """完整重绘（仅切换项目 / 归档后使用，运行中勿频繁调用）。"""
        self._clear()
        if not events:
            self.show_empty("尚无执行记录。在下方输入目标或指令，然后点击运行。")
            return
        for widget in self._build_rows_from_events(events):
            if isinstance(widget, _ToolStepRow):
                self._track_row(widget)
                widget = self._wrap_tool_row(widget)
            self._layout.addWidget(widget, 0, Qt.AlignmentFlag.AlignTop)
        self._sync_bubble_widths()
        self.reset_scroll_anchor()
        self._schedule_scroll_to_bottom(force=True)
        if self._global_compact:
            self.set_global_compact_mode(True)

    def append_event(self, ev: ProjectEvent):
        """增量追加一条气泡（运行中用，避免整表重绘闪烁）。"""
        # 若当前是空态文案，先清掉（兼容欢迎页「选择或新建一个项目开始。」与无记录占位文案）
        if self._layout.count() == 1:
            w = self._layout.itemAt(0).widget()
            if isinstance(w, QLabel) and ("尚无执行记录" in (w.text() or "") or "选择或新建一个项目开始" in (w.text() or "")):
                self._clear()
        if ev.kind == "tool":
            self._route_tool_event(ev)
        else:
            self._layout.addWidget(self._make_row(ev), 0, Qt.AlignmentFlag.AlignTop)
        self._maybe_scroll_to_bottom()

    def begin_run(self):
        """一次运行开始：清空配对注册表，显示状态条。"""
        self._tool_steps.clear()
        self._batch_order.clear()
        self._unmatched_calls.clear()
        self._pending_rows.clear()
        self._agent_running = True
        self._stick_to_bottom = True
        if self._status_bar is not None:
            self._status_bar.set_pulse(True)
            self._status_bar.set_status("正在启动…")

    def end_run(self):
        """一次运行结束：残留 running/pending 步骤行标记为未完成，隐藏状态条。"""
        self._agent_running = False
        for row in list(self._tool_steps.values()):
            if row._status in ("running", "pending"):
                row.set_skipped()
        for row in self._unmatched_calls:
            if row._status in ("running", "pending"):
                row.set_skipped()
        self._tool_steps.clear()
        self._batch_order.clear()
        self._unmatched_calls.clear()
        self._pending_rows.clear()
        if self._status_bar is not None:
            self._status_bar.clear()

    def _track_row(self, row: _ToolStepRow):
        self._bubbles.append(row)
        self._bodies.append(row._body)

    def _status(self, text: str, *, pulse: bool = True):
        if self._status_bar is not None:
            self._status_bar.set_status(text)
            if pulse:
                self._status_bar.set_pulse(True)

    def _route_tool_event(self, ev: ProjectEvent):
        """把 tool 事件按 call/callback 定位到某个 _ToolStepRow 原位更新。"""
        meta = ev.meta if isinstance(ev.meta, dict) else {}
        phase = str(meta.get("phase") or "").lower()
        tool = str(meta.get("tool") or "").strip() or "tool"
        tid = str(meta.get("tool_call_id") or "").strip()

        if phase == "call":
            index = len(self._batch_order) + 1
            args = meta.get("args") if isinstance(meta.get("args"), dict) else None
            row = _ToolStepRow(tid or f"call:{id(ev)}", tool, self.theme, args=args, index=index)
            self._add_row(row)
            if tid:
                self._tool_steps[tid] = row
                self._batch_order.append(tid)
            else:
                self._unmatched_calls.append(row)
            self._status(f"正在调用 {tool}…")
            return

        if phase == "callback":
            if tid and tid in self._tool_steps:
                row = self._tool_steps.pop(tid)
                if tid in self._batch_order:
                    self._batch_order.remove(tid)
                self._finish_tool_row(row, ev)
            else:
                row = self._find_unmatched(tool)
                if row is not None:
                    self._finish_tool_row(row, ev)
                else:
                    # 无配对 call：追加独立 callback 行，绝不丢信息
                    self._layout.addWidget(self._make_row(ev), 0, Qt.AlignmentFlag.AlignTop)
            return

        # 其它工具事件（如脚本 snippet）仍用普通气泡
        self._layout.addWidget(self._make_row(ev), 0, Qt.AlignmentFlag.AlignTop)

    def _add_row(self, row: _ToolStepRow):
        self._track_row(row)
        self._layout.addWidget(self._wrap_tool_row(row), 0, Qt.AlignmentFlag.AlignTop)
        # 只对新行 set 宽度；全量同步留给 resize / 整表重绘，避免长会话 O(n²)。
        self._apply_bubble_width(row)

    def _apply_bubble_width(self, row: QWidget):
        try:
            row.setFixedWidth(self._bubble_width())
        except RuntimeError:
            return

    def _wrap_tool_row(self, row: _ToolStepRow) -> QWidget:
        """给工具步骤行套一层与消息气泡一致的外观：左侧头部无 avatar、统一宽度。"""
        wrapper = QWidget()
        wrapper.setStyleSheet("background: transparent;")
        h = QHBoxLayout(wrapper)
        h.setContentsMargins(4, 0, 4, 0)
        h.setSpacing(8)
        av_bg, av_fg = self._avatar_spec("tool")
        avatar = QLabel("🔧")
        avatar.setFixedSize(40, 40)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background: {av_bg}; color: {av_fg}; border-radius: 20px; font-size: 18px;"
        )
        h.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        h.addWidget(row, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        h.addStretch(1)
        return wrapper

    def _finish_tool_row(self, row: _ToolStepRow, ev: ProjectEvent):
        meta = ev.meta if isinstance(ev.meta, dict) else {}
        if str(meta.get("status") or "success").lower() == "error":
            row.set_failed(ev.content)
        else:
            row.set_success(ev.content)
        self._status(f"{row.tool} 完成")

    def _find_unmatched(self, tool: str) -> _ToolStepRow | None:
        """按 tool 在注册表里找一条还在运行的 call 行，找到即移除（避免残留错配）。

        优先配无语序的超后 call（FIFO），再配带 tid 的 call（其 callback 通常稍后到）。
        """
        for i, row in enumerate(self._unmatched_calls):
            if row.tool == tool and row._status == "running":
                return self._unmatched_calls.pop(i)
        for tid, row in list(self._tool_steps.items()):
            if row.tool == tool and row._status == "running":
                del self._tool_steps[tid]
                if tid in self._batch_order:
                    self._batch_order.remove(tid)
                return row
        return None

    def on_approval_pending(self):
        """审批拦截：把仍在等待返回的工具步骤行标记为「待确认」。"""
        for row in list(self._tool_steps.values()) + list(self._unmatched_calls):
            if row._status == "running":
                row.set_pending()
                self._pending_rows.add(row.step_id)
        if self._status_bar is not None:
            self._status_bar.set_status("等待审批…")

    def resume_after_approval(self, approved: bool):
        """审批结果回来：通过则恢复 running（清除待确认），拒绝则标未完成。"""
        for row in list(self._tool_steps.values()) + list(self._unmatched_calls):
            if row.step_id in self._pending_rows:
                if approved:
                    row.set_running()
                else:
                    row.set_skipped()
        self._pending_rows.clear()

    def _build_rows_from_events(self, events: list[ProjectEvent]) -> list[QWidget]:
        """reload：把历史 tool 事件也按 tool_call_id 配成步骤行（纯函数式）。"""
        pending: dict[str, _ToolStepRow] = {}
        out: list[QWidget] = []
        for ev in events:
            if ev.kind != "tool":
                out.append(self._make_row(ev))
                continue
            meta = ev.meta if isinstance(ev.meta, dict) else {}
            phase = str(meta.get("phase") or "").lower()
            tid = str(meta.get("tool_call_id") or "").strip()
            tool = str(meta.get("tool") or "").strip() or "tool"
            if phase == "call":
                args = meta.get("args") if isinstance(meta.get("args"), dict) else None
                key = tid or f"__noid__{tool}:{len(out)}"
                row = _ToolStepRow(key, tool, self.theme, args=args, index=len(out) + 1)
                out.append(row)
                pending[key] = row
            elif phase == "callback":
                row = None
                if tid and tid in pending:
                    row = pending.pop(tid)
                else:
                    for k in list(pending):
                        if pending[k].tool == tool and pending[k]._status == "running":
                            row = pending.pop(k)
                            break
                if row is not None:
                    if str(meta.get("status") or "success").lower() == "error":
                        row.set_failed(ev.content)
                    else:
                        row.set_success(ev.content)
                else:
                    out.append(self._make_row(ev))
            else:
                out.append(self._make_row(ev))
        # 重载/回放：无 callback 的 call 行保持「调用中」脉冲且无人停 → 统一置未完成并停脉冲。
        for row in pending.values():
            if row._status in ("running", "pending"):
                row.set_skipped()
        return out

    def _scroll_to_bottom(self):
        self._scroll_programmatic = True
        try:
            bar = self._scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        finally:
            QTimer.singleShot(0, self._clear_scroll_programmatic)

    def _clear_scroll_programmatic(self):
        self._scroll_programmatic = False

    def _maybe_scroll_to_bottom(self):
        """运行中若用户已上翻，不强制跳回底部。"""
        if self._agent_running and not self._stick_to_bottom:
            return
        self._schedule_scroll_to_bottom()

    def _schedule_scroll_to_bottom(self, *, force: bool = False):
        """布局尚未算完时 maximum 会偏小，延迟多刷几次避免停在旧消息区域。"""
        if not force and self._agent_running and not self._stick_to_bottom:
            return
        self._scroll_to_bottom()
        for ms in (0, 30, 100, 250, 500):
            QTimer.singleShot(ms, self._scroll_to_bottom)

    def _viewport_width(self) -> int:
        return max(280, self._scroll.viewport().width())

    def _bubble_width(self) -> int:
        """对话窗口约 2/3 宽，统一所有气泡。"""
        return max(220, int(self._viewport_width() * 2 / 3))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_bubble_widths()

    def _sync_bubble_widths(self):
        w = self._bubble_width()
        alive_b: list[QFrame] = []
        for bub in getattr(self, "_bubbles", []):
            try:
                bub.setFixedWidth(w)
                alive_b.append(bub)
            except RuntimeError:
                continue
        self._bubbles = alive_b
        alive_body: list[_ExpandableBody] = []
        for body in getattr(self, "_bodies", []):
            try:
                body.refresh_height()
                alive_body.append(body)
            except RuntimeError:
                continue
        self._bodies = alive_body

    def _avatar_emoji(self, role: str, kind: str = "") -> str:
        if kind == "approval":
            return "✅"
        if kind == "lesson":
            return "📝"
        return {
            "user": "👤",
            "ai": "🤖",
            "tool": "🔧",
            "system": "💻",
            "error": "⚠️",
        }.get(role, "💻")

    def _avatar_spec(self, role: str) -> tuple[str, str]:
        """返回 (背景色, 前景色)。"""
        c = self.theme.colors
        specs = {
            "user": (c.get("btn_primary", "#2f6fed"), "#ffffff"),
            "ai": ("#dbeafe", "#1e40af"),
            "tool": ("#fde68a", "#854d0e"),
            "system": ("#f3f4f6", "#4b5563"),
            "error": ("#ffe4e6", c.get("danger", "#e11d48")),
        }
        return specs.get(role, ("#f3f4f6", "#4b5563"))

    def _bubble_colors(self, role: str) -> tuple[str, str]:
        """返回 (气泡背景, 边框)。"""
        c = self.theme.colors
        mapping = {
            "user": (c.get("accent_light", "#e8f0fe"), c.get("accent", "#2f6fed")),
            "ai": ("#eef6ff", "#93c5fd"),
            "tool": ("#f3f4f6", "#d1d5db"),
            "system": (c.get("card_bg", "#fafafa"), c.get("border_light", "#e5e7eb")),
            "error": ("#fff1f2", "#fecdd3"),
        }
        return mapping.get(role, (c.get("card_bg", "#fff"), c.get("border", "#e5e7eb")))

    def _agent_phase_tag(self, phase: str) -> str:
        return {
            "reasoning": "思考",
            "narration": "AI · 执行中",
            "answer": "AI",
            "hint": "提示",
            "lesson": "经验",
        }.get(phase, "AI")

    def _role_tag(self, role: str, kind: str, phase: str = "") -> str:
        if role == "ai" and kind == "agent" and phase:
            return self._agent_phase_tag(phase)
        tags = {
            "user": "用户",
            "ai": "AI",
            "tool": "工具",
            "system": {"approval": "审批", "lesson": "经验", "info": "系统"}.get(kind, "系统"),
            "error": "错误",
        }
        return tags.get(role, kind)

    def _make_row(self, ev: ProjectEvent) -> QWidget:
        role = _event_ui_role(ev.kind)
        align_right = role == "user"
        c = self.theme.colors
        meta = ev.meta if isinstance(ev.meta, dict) else {}
        phase = str(meta.get("phase") or "")
        is_reasoning = ev.kind == "agent" and phase == "reasoning"
        av_bg, av_fg = self._avatar_spec(role)
        emoji = "💭" if is_reasoning else self._avatar_emoji(role, ev.kind)
        bub_bg, bub_border = self._bubble_colors(role)
        if is_reasoning:
            bub_bg, bub_border = c.get("accent_light", "#eaf1fe"), c.get("accent", "#2f6fed")
        elif ev.kind == "approval":
            bub_bg, bub_border = ("#fff7e6", "#f59e0b")
        elif ev.kind == "lesson":
            bub_bg, bub_border = ("#ecfdf5", "#6ee7b7")

        row = QWidget()
        row.setStyleSheet("background: transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 0, 4, 0)
        h.setSpacing(8)

        avatar = QLabel(emoji)
        avatar.setFixedSize(40, 40)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar.setStyleSheet(
            f"background: {av_bg}; color: {av_fg}; border-radius: 20px; "
            f"font-size: 18px;"
        )

        bubble = QFrame()
        bubble.setFixedWidth(self._bubble_width())
        bubble.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Maximum)
        bubble.setStyleSheet(f"""
            QFrame {{
                background: {bub_bg};
                border: 1px solid {bub_border};
                border-radius: 10px;
            }}
        """)
        if not hasattr(self, "_bubbles"):
            self._bubbles = []
        if not hasattr(self, "_bodies"):
            self._bodies = []
        # 清理已销毁的引用
        alive: list[QFrame] = []
        for b in self._bubbles:
            try:
                b.objectName()
                alive.append(b)
            except RuntimeError:
                continue
        self._bubbles = alive
        self._bubbles.append(bubble)

        bl = QVBoxLayout(bubble)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(4)
        tag = self._role_tag(role, ev.kind, phase)
        head = QLabel(f"{tag} · {ev.created_at}")
        head_color = c.get("danger", "#e11d48") if role == "error" else c["text_hint"]
        head.setStyleSheet(
            f"font-size: 11px; color: {head_color}; background: transparent; border: none;"
        )
        bl.addWidget(head)
        display = (
            _tool_event_display_text(ev)
            if ev.kind == "tool"
            else (ev.content or "")
        )
        if is_reasoning:
            thinking = _ThinkingBlock(display, self.theme)
            self._bodies.append(thinking._body)
            bl.addWidget(thinking)
        else:
            body = _ExpandableBody(
                display,
                self.theme,
                danger=(role == "error"),
            )
            self._bodies.append(body)
            bl.addWidget(body)
            if self._global_compact:
                body.set_global_compact(True)

        if align_right:
            h.addStretch(1)
            h.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            h.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        else:
            h.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
            h.addWidget(bubble, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            h.addStretch(1)
        return row