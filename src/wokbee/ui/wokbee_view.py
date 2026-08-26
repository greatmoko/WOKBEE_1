"""WokBee 主视图：项目列表 + 三段式工作区。"""

from __future__ import annotations

import os
import json
import re
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer, QThread, QEvent, QSize
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame, QLabel, QPushButton,
    QLineEdit, QScrollArea, QTextEdit, QSizePolicy, QDialog,
    QMenu, QFileDialog, QComboBox, QTextBrowser, QApplication,
)

from tokbee.ui.styles.theme import Theme
from tokbee.core.provider_store import ProviderStore
from tokbee.ui.combo_style import apply_combo_popup_style
from tokbee.ui.widgets.context_ring import ContextUsageRing
from tokbee.core import context_manager as ctxman
from tokbee.core.ai_client import AIClient

from wokbee.core.models import (
    ApprovalFlags,
    Project,
    ProjectEvent,
    ProjectStatus,
    MAX_PROJECT_TITLE_LEN,
)
from wokbee.core.paths import (
    deliverables_dir,
    list_deliverable_names,
    references_dir,
    uploads_dir,
)
from wokbee.core.project_store import ProjectStore, TRASH_RETENTION_DAYS, MAX_ARCHIVES
from wokbee.core.settings import WokBeeSettings
from wokbee.core.context_usage import (
    estimate_project_usage,
    load_context_state,
    plan_project_compaction,
    save_context_state,
)
from wokbee.engine.lessons import (
    LessonStore,
    build_success_path_from_timeline_events,
    collect_events_log,
)
from wokbee.engine.runner import AgentRunner, RunRequest, resolve_model_for_project
from wokbee.engine.worker import AgentWorker, LessonWorker
from wokbee.ui.ask_user_dialog import AskUserDialog
from wokbee.ui.settings_workspace import (
    apply_flags_to_checks,
    build_approval_checkboxes,
    flags_from_checks,
)


STATUS_LABEL = {
    ProjectStatus.IDLE: "空闲",
    ProjectStatus.RUNNING: "运行中",
    ProjectStatus.AWAITING_APPROVAL: "待审批",
    ProjectStatus.FAILED: "失败",
    ProjectStatus.DONE: "完成",
}

STATUS_COLOR_KEY = {
    ProjectStatus.IDLE: "text_hint",
    ProjectStatus.RUNNING: "accent",
    ProjectStatus.AWAITING_APPROVAL: "warning",
    ProjectStatus.FAILED: "danger",
    ProjectStatus.DONE: "success",
}

# 新建项目时，名称默认取目标前 N 个字（也是名称硬上限）
TITLE_FROM_GOAL_LEN = MAX_PROJECT_TITLE_LEN


class _CompactWorker(QThread):
    """后台生成上下文摘要，写入 compaction point。"""

    finished = Signal(str, int, int)  # summary, boundary_index, pin_end
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
    ):
        super().__init__(parent)
        self._client = client
        self._to_compact = to_compact
        self._previous_summary = previous_summary
        self._new_boundary = new_boundary
        self._pin_end = int(pin_end or 0)

    def run(self):
        summary = ""
        if self._client is not None:
            try:
                msgs = ctxman.build_summary_prompt_messages(
                    self._to_compact, self._previous_summary,
                )
                resp = self._client.chat(msgs, temperature=0.2, max_tokens=800)
                summary = (resp.content or "").strip()
                if not summary and resp.reasoning_content:
                    summary = resp.reasoning_content.strip()
            except Exception:
                summary = ""
        if not summary:
            summary = ctxman.mechanical_summary(
                self._to_compact, self._previous_summary,
            )
        if not summary.strip():
            self.failed.emit("无法生成摘要")
            return
        self.finished.emit(summary, self._new_boundary, self._pin_end)


class _RefineMetaWorker(QThread):
    """后台调用 AI，根据目标与时间线生成新的项目名称与目标。"""

    finished_ok = Signal(str, str)  # title, goal
    failed = Signal(str)

    def __init__(
        self,
        client: AIClient,
        *,
        current_title: str,
        current_goal: str,
        timeline_log: str,
        max_title_len: int,
        parent=None,
    ):
        super().__init__(parent)
        self._client = client
        self._current_title = current_title
        self._current_goal = current_goal
        self._timeline_log = timeline_log
        self._max_title_len = max_title_len

    def run(self):
        system = (
            "你是项目元信息助手。根据当前名称、目标与最近运行/对话记录，"
            "生成更贴切的「项目名称」和「项目目标」。\n"
            f"硬性要求：\n"
            f"1. 名称尽量短，不超过 {self._max_title_len} 个字，不要整句目标当名称。\n"
            "2. 目标用自然语言写清要完成的事，可多句，不要空。\n"
            "3. 只输出一个 JSON 对象，不要 Markdown，不要解释。格式：\n"
            '{"title":"名称","goal":"目标全文"}'
        )
        user = (
            f"当前名称：{self._current_title or '（空）'}\n"
            f"当前目标：{self._current_goal or '（空）'}\n\n"
            f"最近记录：\n{self._timeline_log or '（无）'}"
        )
        try:
            resp = self._client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                max_tokens=800,
            )
            raw = (resp.content or "").strip() or (resp.reasoning_content or "").strip()
        except Exception as e:
            self.failed.emit(str(e))
            return

        title, goal = self._parse(raw)
        if not title and not goal:
            self.failed.emit("模型未返回可用的名称/目标")
            return
        if title and len(title) > self._max_title_len:
            title = title[: self._max_title_len]
        self.finished_ok.emit(title or self._current_title, goal or self._current_goal)

    @staticmethod
    def _parse(raw: str) -> tuple[str, str]:
        text = (raw or "").strip()
        if not text:
            return "", ""
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", text)
            if not m:
                return "", ""
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                return "", ""
        if not isinstance(data, dict):
            return "", ""
        title = str(data.get("title") or "").replace("\x00", "").strip()
        goal = str(data.get("goal") or "").replace("\x00", "").strip()
        return title, goal


def _title_from_goal(goal: str, max_len: int = TITLE_FROM_GOAL_LEN) -> str:
    s = " ".join((goal or "").split())
    if not s:
        return "未命名项目"
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def _tip(parent: QWidget, theme: Theme, message: str, title: str = "提示"):
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumSize(400, 160)
    dlg.resize(420, 200)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
    layout.addWidget(msg, stretch=1)
    row = QHBoxLayout()
    row.addStretch()
    ok = QPushButton("知道了")
    ok.setFixedSize(80, 34)
    ok.setCursor(Qt.CursorShape.PointingHandCursor)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(ok)
    layout.addLayout(row)
    dlg.exec()


def _ask_text(
    parent: QWidget,
    theme: Theme,
    title: str,
    label: str,
    default: str = "",
    *,
    max_length: int | None = None,
) -> str | None:
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(400, 180)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(12)
    layout.addWidget(QLabel(label))
    inp = QLineEdit(default)
    inp.setFixedHeight(34)
    if max_length is not None and max_length > 0:
        inp.setMaxLength(max_length)
    inp.setStyleSheet(f"""
        QLineEdit {{
            background: {c["input_bg"]}; color: {c["text"]};
            border: 1px solid {c["input_border"]}; border-radius: 6px; padding: 0 10px;
        }}
    """)
    layout.addWidget(inp)
    layout.addStretch()
    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(72, 34)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("确定")
    ok.setFixedSize(72, 34)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_primary"]}; color: white;
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return inp.text().strip()
    return None


def _textedit_qss(theme: Theme) -> str:
    c = theme.colors
    return f"""
        QTextEdit {{
            background: {c["input_bg"]}; color: {c["text"]};
            border: 1px solid {c["input_border"]}; border-radius: 6px;
            padding: 8px; font-size: 13px;
        }}
        QTextEdit:focus {{ border: 1px solid {c["input_focus_border"]}; }}
    """


def _ask_multiline(
    parent: QWidget,
    theme: Theme,
    title: str,
    label: str,
    default: str = "",
    *,
    min_lines: int = 5,
) -> str | None:
    """多行文本输入（至少约 min_lines 行，超出滚动）。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumSize(480, 320)
    dlg.resize(520, 360)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)
    tip = QLabel(label)
    tip.setWordWrap(True)
    tip.setStyleSheet(f"font-size: 13px; color: {c['text']};")
    layout.addWidget(tip)
    inp = QTextEdit()
    inp.setPlainText(default or "")
    # ~22px/行 + padding
    inp.setMinimumHeight(max(5, min_lines) * 22 + 16)
    inp.setStyleSheet(_textedit_qss(theme))
    layout.addWidget(inp, stretch=1)
    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(72, 34)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("确定")
    ok.setFixedSize(72, 34)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_primary"]}; color: white;
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return inp.toPlainText().strip()
    return None


def _default_project_title(when: datetime | None = None) -> str:
    """默认名称：项目 + 创建时间（不超过名称上限）。"""
    dt = when or datetime.now()
    raw = f"项目{dt.strftime('%m-%d %H:%M')}"
    return raw[:MAX_PROJECT_TITLE_LEN]


def _ask_new_project(parent: QWidget, theme: Theme) -> tuple[str, str] | None:
    """新建项目：名称与目标均可留空；空名称默认「项目+创建时间」。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("新建项目")
    dlg.setMinimumSize(520, 420)
    dlg.resize(560, 460)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)

    tip = QLabel("名称和目标都可以先留空，之后再补充。")
    tip.setWordWrap(True)
    tip.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
    layout.addWidget(tip)

    goal_lab = QLabel("项目目标（可选）")
    goal_lab.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {c['text']};")
    layout.addWidget(goal_lab)
    goal_hint = QLabel("用自然语言描述要完成的事；可稍后编辑。超出可滚动。")
    goal_hint.setWordWrap(True)
    goal_hint.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
    layout.addWidget(goal_hint)

    goal_inp = QTextEdit()
    goal_inp.setPlaceholderText("例如：查询深圳今日天气，写成小红书风格文案并保存到产物目录…")
    goal_inp.setMinimumHeight(5 * 22 + 16)
    goal_inp.setStyleSheet(_textedit_qss(theme))
    layout.addWidget(goal_inp, stretch=1)

    title_lab = QLabel(
        f"项目名称（可选，最多 {MAX_PROJECT_TITLE_LEN} 字；"
        f"默认「{_default_project_title()}」）"
    )
    title_lab.setStyleSheet(f"font-size: 13px; color: {c['text']};")
    layout.addWidget(title_lab)
    title_inp = QLineEdit()
    title_inp.setFixedHeight(34)
    title_inp.setMaxLength(MAX_PROJECT_TITLE_LEN)
    title_inp.setPlaceholderText(_default_project_title())
    title_inp.setStyleSheet(f"""
        QLineEdit {{
            background: {c["input_bg"]}; color: {c["text"]};
            border: 1px solid {c["input_border"]}; border-radius: 6px; padding: 0 10px;
        }}
    """)
    layout.addWidget(title_inp)

    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(72, 34)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("创建")
    ok.setFixedSize(88, 34)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_primary"]}; color: white;
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    goal = goal_inp.toPlainText().strip()
    title = title_inp.text().strip() or _default_project_title()
    if len(title) > MAX_PROJECT_TITLE_LEN:
        title = title[:MAX_PROJECT_TITLE_LEN]
    return title, goal


def _open_md_in_browser(path: Path) -> bool:
    """用系统默认浏览器打开文件（优先 HTML；.md 亦尝试 file URI）。"""
    try:
        if not path.exists():
            return False
        webbrowser.open(path.resolve().as_uri())
        return True
    except OSError:
        return False


def _prompt_approval_flags(
    parent: QWidget,
    theme: Theme,
    current: ApprovalFlags,
    title: str = "项目审核策略",
) -> ApprovalFlags | None:
    """弹窗编辑四个审核勾选项。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(420, 280)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)

    tip = QLabel("勾选表示该级别免审；未勾选则执行时需要人工审批。仅影响当前项目。")
    tip.setWordWrap(True)
    tip.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
    layout.addWidget(tip)

    box, checks = build_approval_checkboxes(theme)
    apply_flags_to_checks(checks, current)
    layout.addWidget(box)
    layout.addStretch()

    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(72, 34)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("保存")
    ok.setFixedSize(72, 34)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_primary"]}; color: white;
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)

    if dlg.exec() == QDialog.DialogCode.Accepted:
        return flags_from_checks(checks)
    return None


def _confirm(parent: QWidget, theme: Theme, title: str, message: str) -> bool:
    """主题化确认框，避免原生 QMessageBox 在 Windows 上发黑。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(400, 170)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(12)
    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
    layout.addWidget(msg)
    layout.addStretch()
    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(80, 34)
    cancel.setCursor(Qt.CursorShape.PointingHandCursor)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("确定")
    ok.setFixedSize(80, 34)
    ok.setCursor(Qt.CursorShape.PointingHandCursor)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["danger"]}; color: white;
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["danger_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)
    return dlg.exec() == QDialog.DialogCode.Accepted


# ─── 列表项 ───

class _ProjectItem(QFrame):
    clicked = Signal(str)
    context_menu = Signal(str, object)

    def __init__(self, project: Project, theme: Theme, selected: bool = False, parent=None):
        super().__init__(parent)
        self.project = project
        self.theme = theme
        self._selected = selected
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(62)
        self._build()

    def _build(self):
        c = self.theme.colors
        p = self.project
        bg = c["accent_light"] if self._selected else "transparent"
        border = (
            f"border-left: 3px solid {c['accent']};"
            if self._selected
            else "border-left: 3px solid transparent;"
        )
        self.setStyleSheet(f"""
            _ProjectItem {{
                background: {bg};
                border-radius: 6px;
                {border}
            }}
            _ProjectItem:hover {{ background: {c["subnav_hover"]}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        top = QHBoxLayout()
        color = c[STATUS_COLOR_KEY.get(p.status, "text_hint")]
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 10px; background: transparent; border: none;")
        top.addWidget(dot)

        title = QLabel(p.title)
        title.setWordWrap(False)
        title.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        top.addWidget(title, 1)
        if p.pinned:
            pin = QLabel("📌")
            pin.setStyleSheet(
                "font-size: 11px; background: transparent; border: none;"
            )
            pin.setFixedWidth(16)
            top.addWidget(pin, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(top)

        try:
            dt = datetime.strptime(p.updated_at, "%Y-%m-%d %H:%M:%S")
            time_str = dt.strftime("%m-%d %H:%M")
        except ValueError:
            time_str = p.updated_at

        status = STATUS_LABEL.get(p.status, p.status.value)
        info = QLabel(f"{status} · {time_str}")
        info.setStyleSheet(
            f"font-size: 11px; color: {c['text_hint']}; background: transparent; border: none;"
        )
        layout.addWidget(info)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.project.id)
        elif event.button() == Qt.MouseButton.RightButton:
            self.context_menu.emit(self.project.id, event.globalPosition().toPoint())
        super().mousePressEvent(event)


class _ProjectSidebar(QFrame):
    project_selected = Signal(str)
    project_changed = Signal()

    def __init__(self, theme: Theme, store: ProjectStore, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store
        self._selected_id: str | None = None
        self._build()
        self.refresh()

    def _build(self):
        c = self.theme.colors
        self.setMinimumWidth(200)
        self.setMaximumWidth(240)
        self.setStyleSheet(f"""
            _ProjectSidebar {{
                background: {c["subnav_bg"]};
                border-right: 1px solid {c["border"]};
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索项目...")
        self._search.setFixedHeight(30)
        self._search.textChanged.connect(lambda: self.refresh())
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 6px; padding: 0 8px;
            }}
        """)
        search_row.addWidget(self._search, stretch=1)

        new_btn = QPushButton("＋")
        new_btn.setToolTip("新建项目")
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

        projects = self.store.search(self._search.text())
        c = self.theme.colors
        if not projects:
            empty = QLabel("暂无项目")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 20px 0;")
            self._list_layout.addWidget(empty)
            return

        for p in projects:
            item = _ProjectItem(p, self.theme, selected=(p.id == self._selected_id))
            item.clicked.connect(self._on_select)
            item.context_menu.connect(self._on_context)
            self._list_layout.addWidget(item)

    def select(self, project_id: str):
        self._selected_id = project_id
        self.refresh()
        self.project_selected.emit(project_id)

    def _on_select(self, project_id: str):
        self.select(project_id)

    def _on_new(self):
        result = _ask_new_project(self, self.theme)
        if result is None:
            return
        title, goal = result
        project = self.store.create(title=title, goal=goal)
        self.project_changed.emit()
        self.select(project.id)

    def _on_context(self, project_id: str, pos):
        c = self.theme.colors
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
            QMenu::separator {{
                height: 1px;
                background: {c["border"]};
                margin: 4px 8px;
            }}
        """)
        rename_a = menu.addAction("重命名")
        open_a = menu.addAction("打开工作文件夹")
        copy_a = menu.addAction("复制项目 ID")
        project = self.store.get(project_id)
        pin_a = menu.addAction(
            "取消置顶" if project and project.pinned else "置顶"
        )
        menu.addSeparator()
        del_a = menu.addAction("删除（移入回收）")
        if project and project.pinned:
            del_a.setEnabled(False)
            del_a.setText("删除（请先取消置顶）")
        action = menu.exec(pos)
        if action == rename_a:
            project = self.store.get(project_id)
            if not project:
                return
            new_title = _ask_text(
                self,
                self.theme,
                "重命名",
                f"新名称（最多 {MAX_PROJECT_TITLE_LEN} 字）",
                project.title[:MAX_PROJECT_TITLE_LEN],
                max_length=MAX_PROJECT_TITLE_LEN,
            )
            if new_title:
                self.store.rename(project_id, new_title)
                self.refresh()
                self.project_changed.emit()
                if self._selected_id == project_id:
                    self.project_selected.emit(project_id)
        elif action == open_a:
            path = self.store.path_for(project_id)
            _open_in_explorer(path)
        elif action == copy_a:
            QApplication.clipboard().setText(project_id)
            _tip(self, self.theme, f"已复制项目 ID：{project_id}", "复制成功")
        elif action == pin_a:
            self.store.toggle_pin(project_id)
            self.refresh()
            self.project_changed.emit()
        elif action == del_a:
            project = self.store.get(project_id)
            if project and project.pinned:
                return
            if _confirm(
                self,
                self.theme,
                "删除项目",
                f"确定将该项目移入工作区 _trash？\n"
                f"回收站最多保留 {TRASH_RETENTION_DAYS} 天，过期将永久删除。",
            ):
                if not self.store.delete(project_id, trash=True):
                    return
                if self._selected_id == project_id:
                    self._selected_id = None
                self.refresh()
                self.project_changed.emit()
                projects = self.store.list_projects()
                if projects:
                    self.select(projects[0].id)
                else:
                    self.project_selected.emit("")


def _open_in_explorer(path):
    path = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


# ─── 工作区上段：项目要素 ───

class _ProjectEssentials(QFrame):
    goal_edit_requested = Signal()
    approval_edit_requested = Signal()
    ai_refine_requested = Signal()

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setStyleSheet(f"""
            _ProjectEssentials {{
                background: {c["card_bg"]};
                border-bottom: 1px solid {c["border"]};
            }}
        """)
        self.setMinimumHeight(88)
        self.setMaximumHeight(120)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(6)

        row1 = QHBoxLayout()
        self._title = QLabel("未选择项目")
        self._title.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {c['text']};")
        row1.addWidget(self._title)

        self._ai_refine_btn = QPushButton("AI")
        self._ai_refine_btn.setToolTip("用 AI 根据当前目标与最近记录更新项目名称和目标")
        self._ai_refine_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ai_refine_btn.setFixedSize(32, 28)
        self._ai_refine_btn.setEnabled(False)
        self._ai_refine_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 11px; font-weight: 600;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
            QPushButton:disabled {{ color: {c["text_hint"]}; }}
        """)
        self._ai_refine_btn.clicked.connect(self.ai_refine_requested.emit)
        row1.addWidget(self._ai_refine_btn)
        row1.addStretch(1)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        row1.addWidget(self._status)

        self._policy_btn = QPushButton("审核策略")
        self._policy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._policy_btn.setFixedHeight(28)
        self._policy_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; padding: 0 10px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        self._policy_btn.clicked.connect(self.approval_edit_requested.emit)
        row1.addWidget(self._policy_btn)
        layout.addLayout(row1)

        self._goal = QLabel("目标：—")
        self._goal.setWordWrap(True)
        self._goal.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        self._goal.setCursor(Qt.CursorShape.PointingHandCursor)
        self._goal.mousePressEvent = lambda e: self.goal_edit_requested.emit()  # type: ignore
        layout.addWidget(self._goal)

        row3 = QHBoxLayout()
        self._progress = QLabel("进度：—")
        self._progress.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        row3.addWidget(self._progress)
        self._artifacts = QLabel("交付物：—")
        self._artifacts.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        row3.addWidget(self._artifacts, stretch=1)
        self._references = QLabel("参考：—")
        self._references.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        self._references.setToolTip("references/ 参考材料（不归档）")
        row3.addWidget(self._references)
        self._uploads = QLabel("上传：—")
        self._uploads.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        row3.addWidget(self._uploads)
        self._policy = QLabel("")
        self._policy.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        row3.addWidget(self._policy)
        layout.addLayout(row3)

    def clear(self):
        self._title.setText("未选择项目")
        self._status.setText("")
        self._goal.setText("目标：—")
        self._progress.setText("进度：—")
        self._artifacts.setText("交付物：—")
        self._uploads.setText("上传：—")
        self._policy.setText("")
        self._policy_btn.setEnabled(False)
        self._ai_refine_btn.setEnabled(False)
        self._ai_refine_btn.setText("AI")

    def set_ai_refine_busy(self, busy: bool):
        if busy:
            self._ai_refine_btn.setEnabled(False)
            self._ai_refine_btn.setText("…")
        else:
            self._ai_refine_btn.setEnabled(True)
            self._ai_refine_btn.setText("AI")

    def bind(self, project: Project, *, project_root: Path | None = None):
        c = self.theme.colors
        self._title.setText(project.title)
        # 刷新顶栏时勿打断「AI 更新中」状态
        if self._ai_refine_btn.text() != "…":
            self._ai_refine_btn.setEnabled(True)
            self._ai_refine_btn.setText("AI")
        self._policy_btn.setEnabled(True)
        st = STATUS_LABEL.get(project.status, project.status.value)
        color = c[STATUS_COLOR_KEY.get(project.status, "text_hint")]
        self._status.setText(st)
        self._status.setStyleSheet(f"font-size: 12px; color: {color};")

        goal = project.goal.strip() or "（点击设置目标）"
        self._goal.setText(f"目标：{goal}")

        step = f" · {project.current_step}" if project.current_step else ""
        self._progress.setText(f"进度：{project.progress_text()}{step}")

        art = project.artifacts_summary.strip()
        if not art and project_root is not None:
            names = list_deliverable_names(project_root, limit=5)
            art = ", ".join(names) if names else "暂无（目录 deliverables/）"
        elif not art:
            art = "暂无（目录 deliverables/）"
        self._artifacts.setText(f"交付物：{art}")

        up_text = "暂无（目录 uploads/）"
        if project_root is not None:
            ud = uploads_dir(project_root)
            if ud.exists():
                ups = [
                    p.name
                    for p in ud.iterdir()
                    if p.is_file() and p.name.lower() not in ("readme.txt", "readme.md")
                ][:5]
                if ups:
                    up_text = ", ".join(ups)
        self._uploads.setText(f"上传：{up_text}")

        ref_text = "暂无（目录 references/）"
        if project_root is not None:
            try:
                from wokbee.core.references import count_reference_files

                items = count_reference_files(project_root, limit=5)
                if items:
                    ref_text = ", ".join(items)
                    self._references.setToolTip("references/ 参考材料（不归档；点击「🗂」打开目录）")
            except Exception:
                ref_text = "暂无（目录 references/）"
        self._references.setText(f"参考：{ref_text}")

        summary = project.approval.summary()
        self._policy.setText(f"策略：{summary}")
        if project.approval.skip_high_risk:
            self._policy.setStyleSheet(
                f"font-size: 12px; font-weight: bold; color: {c['danger']};"
            )
        else:
            self._policy.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")


# 气泡正文预览：折叠时高度上限（约 10 行）
BUBBLE_PREVIEW_CHARS = 400
BUBBLE_PREVIEW_LINES = 10
BUBBLE_COLLAPSED_HEIGHT = 200


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
        self._browser._update_height()

    def _needs_expand(self) -> bool:
        full = self._full
        if not full:
            return False
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
        if not need or self._expanded:
            self._browser.set_height_cap(0)
            self._browser.set_markdown(full)
            self._toggle.setVisible(toggle_visible)
            self._toggle.setText("收起" if need else "")
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
        self._body._on_toggle()
        expanded = getattr(self._body, "_expanded", False)
        self._toggle.setText("▾" if expanded else "▸")
        QTimer.singleShot(0, self._body.refresh_height)
        QTimer.singleShot(30, self._body.refresh_height)


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
        self._schedule_scroll_to_bottom()

    def append_event(self, ev: ProjectEvent):
        """增量追加一条气泡（运行中用，避免整表重绘闪烁）。"""
        # 若当前是空态文案，先清掉
        if self._layout.count() == 1:
            w = self._layout.itemAt(0).widget()
            if isinstance(w, QLabel) and "尚无执行记录" in (w.text() or ""):
                self._clear()
        if ev.kind == "tool":
            self._route_tool_event(ev)
        else:
            self._layout.addWidget(self._make_row(ev), 0, Qt.AlignmentFlag.AlignTop)
        self._schedule_scroll_to_bottom()

    def begin_run(self):
        """一次运行开始：清空配对注册表，显示状态条。"""
        self._tool_steps.clear()
        self._batch_order.clear()
        self._unmatched_calls.clear()
        self._pending_rows.clear()
        if self._status_bar is not None:
            self._status_bar.set_pulse(True)
            self._status_bar.set_status("正在启动…")

    def end_run(self):
        """一次运行结束：残留 running 步骤行标记为未完成，隐藏状态条。"""
        for row in list(self._tool_steps.values()):
            if row._status == "running":
                row.set_skipped()
        for row in self._unmatched_calls:
            if row._status == "running":
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
        self._sync_bubble_widths()

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
        for row in self._unmatched_calls:
            if row.tool == tool and row._status == "running":
                return row
        for row in self._tool_steps.values():
            if row.tool == tool and row._status == "running":
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
        return out

    def _scroll_to_bottom(self):
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _schedule_scroll_to_bottom(self):
        """布局尚未算完时 maximum 会偏小，延迟多刷几次避免停在旧消息区域。"""
        self._scroll_to_bottom()
        for ms in (0, 30, 100):
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

        if align_right:
            h.addStretch(1)
            h.addWidget(bubble, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            h.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
        else:
            h.addWidget(avatar, 0, Qt.AlignmentFlag.AlignTop)
            h.addWidget(bubble, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            h.addStretch(1)
        return row


# ─── 下段：输入与操作 ───

class _ActionBar(QFrame):
    run_clicked = Signal()
    pause_clicked = Signal()
    open_folder_clicked = Signal()
    summarize_clicked = Signal()
    clear_experience_clicked = Signal()
    upload_clicked = Signal()
    open_deliverables_clicked = Signal()
    open_references_clicked = Signal()
    import_references_clicked = Signal()
    send_clicked = Signal(str)
    approve_clicked = Signal()
    reject_clicked = Signal()
    model_changed = Signal(str, str)  # provider_id, model_id
    compress_clicked = Signal()
    draft_changed = Signal()

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._model_updating = False
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setStyleSheet(f"""
            _ActionBar {{
                background: {c["content_bg"]};
                border-top: 1px solid {c["border"]};
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(8)

        self._approval_bar = QFrame()
        self._approval_bar.setVisible(False)
        self._approval_bar.setStyleSheet(f"""
            QFrame {{
                background: #fff7e6;
                border: 1px solid {c["warning"]};
                border-radius: 8px;
            }}
        """)
        ap_lay = QVBoxLayout(self._approval_bar)
        ap_lay.setContentsMargins(12, 8, 12, 8)
        ap_lay.setSpacing(6)
        self._approval_label = QLabel("等待审批…")
        self._approval_label.setWordWrap(True)
        self._approval_label.setStyleSheet(f"font-size: 12px; color: {c['text']}; background: transparent; border: none;")
        ap_lay.addWidget(self._approval_label)
        ap_btns = QHBoxLayout()
        ap_btns.addStretch()
        reject_btn = QPushButton("拒绝")
        reject_btn.setFixedHeight(30)
        reject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reject_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["danger"]}; color: white;
                border: none; border-radius: 6px; padding: 0 14px;
            }}
            QPushButton:hover {{ background: {c["danger_hover"]}; }}
        """)
        reject_btn.clicked.connect(self.reject_clicked.emit)
        approve_btn = QPushButton("全部通过")
        approve_btn.setFixedHeight(30)
        approve_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        approve_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; padding: 0 14px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        approve_btn.clicked.connect(self.approve_clicked.emit)
        ap_btns.addWidget(reject_btn)
        ap_btns.addWidget(approve_btn)
        ap_lay.addLayout(ap_btns)
        layout.addWidget(self._approval_bar)

        self._input = QTextEdit()
        self._input.setPlaceholderText(
            "输入提问或指令…（Enter 发送，Shift+Enter 换行；发送=完整能力，运行=经验管线）"
        )
        self._input.setFixedHeight(72)
        self._input.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 8px;
                padding: 8px; font-size: 13px;
            }}
            QTextEdit:focus {{ border: 1px solid {c["input_focus_border"]}; }}
        """)
        self._input.textChanged.connect(self.draft_changed.emit)
        self._input.installEventFilter(self)
        layout.addWidget(self._input)

        row = QHBoxLayout()
        row.setSpacing(6)

        for icon, tip, slot in (
            ("📁", "打开目录", self.open_folder_clicked.emit),
            ("📦", "交付物：打开 deliverables/ 目录", self.open_deliverables_clicked.emit),
            ("⬆️", "上传文件到 uploads/，Agent 可读取；运行前会自动归档", self.upload_clicked.emit),
            ("🗂", "参考材料：打开 references/ 目录（不归档）", self.open_references_clicked.emit),
            ("📎", "导入材料到 references/（第三方代码/配置/环境参数）", self.import_references_clicked.emit),
            ("📝", "总结经验：上一份经验 + 运行日志 + scripts → AI 新建经验", self.summarize_clicked.emit),
            (
                "🧹",
                "清空经验：归档会话，并把 memory 经验与 scripts 一并归档后清空",
                self.clear_experience_clicked.emit,
            ),
        ):
            btn = QPushButton(icon)
            btn.setToolTip(tip)
            btn.setFixedSize(34, 34)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(self._icon_btn_qss())
            btn.clicked.connect(slot)
            row.addWidget(btn)

        row.addStretch()

        self._model_combo = QComboBox()
        self._model_combo.setFixedHeight(34)
        self._model_combo.setMinimumWidth(200)
        self._model_combo.setMaximumWidth(320)
        self._model_combo.setToolTip("切换本项目使用的 AI 模型（来自厂商设置中已启用的模型）")
        self._model_combo.setStyleSheet(f"""
            QComboBox {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 6px;
                padding: 0 10px; font-size: 12px;
            }}
            QComboBox:hover {{ border: 1px solid {c["input_focus_border"]}; }}
            QComboBox:disabled {{ color: {c["text_hint"]}; }}
            QComboBox::drop-down {{ border: none; width: 22px; }}
        """)
        apply_combo_popup_style(self._model_combo, c)
        self._model_combo.currentIndexChanged.connect(self._on_model_index_changed)
        row.addWidget(self._model_combo)

        self._ctx_ring = ContextUsageRing(self.theme)
        self._ctx_ring.setToolTip("上下文用量（点击压缩）")
        self._ctx_ring.compress_clicked.connect(self.compress_clicked.emit)
        row.addWidget(self._ctx_ring)

        self._cache_label = QLabel("")
        self._cache_label.setToolTip(
            "DeepSeek 前缀缓存：本轮命中率 · 会话累计命中率\n"
            "对齐 Reasonix 双指标；无用量时为空"
        )
        self._cache_label.setStyleSheet(
            f"font-size: 11px; color: {c['text_hint']}; padding: 0 4px;"
        )
        self._cache_label.setMinimumWidth(0)
        row.addWidget(self._cache_label)

        self._run_btn = QPushButton("运行")
        self._run_btn.setFixedSize(59, 34)
        self._run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._run_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; font-size: 13px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
            QPushButton:disabled {{ background: {c["btn_bg"]}; color: {c["text_hint"]}; }}
        """)
        self._run_btn.clicked.connect(self.run_clicked.emit)
        row.addWidget(self._run_btn)

        pause_btn = QPushButton("暂停")
        pause_btn.setFixedSize(48, 34)
        pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pause_btn.setStyleSheet("""
            QPushButton {
                background: #dc2626; color: white;
                border: none; border-radius: 6px; font-size: 13px; font-weight: bold;
            }
            QPushButton:hover { background: #b91c1c; }
            QPushButton:pressed { background: #991b1b; }
        """)
        pause_btn.clicked.connect(self.pause_clicked.emit)
        row.addWidget(pause_btn)

        send_btn = QPushButton("发送")
        send_btn.setFixedSize(48, 34)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet(self._sec_qss())
        send_btn.clicked.connect(self._on_send)
        row.addWidget(send_btn)
        layout.addLayout(row)

        self.reload_models()

    def _sec_qss(self) -> str:
        c = self.theme.colors
        return f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; padding: 0 12px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """

    def _icon_btn_qss(self) -> str:
        c = self.theme.colors
        return f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 16px;
                padding: 0;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
            QPushButton:pressed {{ background: {c["accent_light"]}; }}
        """

    def eventFilter(self, obj, event):
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            key_event = event
            if isinstance(key_event, QKeyEvent) and key_event.key() in (
                Qt.Key.Key_Return,
                Qt.Key.Key_Enter,
            ):
                if key_event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    return False  # Shift+Enter 换行
                self._on_send()
                return True  # Enter 发送，拦截默认换行
        return super().eventFilter(obj, event)

    def _on_send(self):
        text = self._input.toPlainText().strip()
        if text:
            self.send_clicked.emit(text)
            self._input.clear()

    def take_input(self) -> str:
        text = self._input.toPlainText().strip()
        self._input.clear()
        return text

    def set_draft(self, text: str):
        self._input.setPlainText(text or "")

    def set_running(self, running: bool):
        self._run_btn.setEnabled(not running)
        self._run_btn.setText("运行中…" if running else "运行")
        self._model_combo.setEnabled(not running)

    def show_approval(self, text: str):
        self._approval_label.setText(text)
        self._approval_bar.setVisible(True)

    def hide_approval(self):
        self._approval_bar.setVisible(False)

    def reload_models(
        self,
        provider_id: str = "",
        model_id: str = "",
        *,
        fallback_provider: str = "",
        fallback_model: str = "",
    ):
        """刷新可选模型列表，并选中项目当前模型（或回退默认）。"""
        self._model_updating = True
        try:
            self._model_combo.clear()
            try:
                store = ProviderStore()
                models = store.list_selectable_models()
            except Exception:
                store = None
                models = []
            if not models:
                self._model_combo.addItem("未配置模型（请到厂商设置启用）", ("", ""))
                self._model_combo.setEnabled(False)
                return
            self._model_combo.setEnabled(True)
            target = (provider_id or "", model_id or "")
            # 回退顺序：厂商默认模型 → 调用方 fallback（WokBee 设置）→ 列表第一项
            if not target[1] and store is not None:
                try:
                    default = store.resolve_default()
                    if default:
                        target = (default.provider_id, default.model_id)
                except Exception:
                    pass
            if not target[1]:
                target = (fallback_provider or "", fallback_model or "")
            select = 0
            matched = False
            for i, m in enumerate(models):
                label = f"{m.provider_name} / {m.model_id}"
                self._model_combo.addItem(label, (m.provider_id, m.model_id))
                if (m.provider_id, m.model_id) == target and target[1]:
                    select = i
                    matched = True
            if not matched and not target[1]:
                select = 0
            self._model_combo.setCurrentIndex(select)
        finally:
            self._model_updating = False

    def set_context_usage(self, used: int, limit: int, *, enabled: bool = True):
        self._ctx_ring.set_usage(used, limit)
        self._ctx_ring.set_ring_enabled(enabled)

    def set_cache_stats(self, text: str = "", *, tooltip: str = ""):
        self._cache_label.setText(text or "")
        if tooltip:
            self._cache_label.setToolTip(tooltip)
        self._cache_label.setVisible(bool(text))

    def draft_text(self) -> str:
        return self._input.toPlainText()

    def selected_model(self) -> tuple[str, str]:
        data = self._model_combo.currentData()
        if isinstance(data, tuple) and len(data) == 2:
            return str(data[0] or ""), str(data[1] or "")
        return "", ""

    def _on_model_index_changed(self, _index: int):
        if self._model_updating:
            return
        provider, model = self.selected_model()
        if model:
            self.model_changed.emit(provider, model)


# ─── 工作区整体 ───

class _ProjectWorkspace(QWidget):
    status_changed = Signal()  # 通知侧栏同步项目状态

    def __init__(self, theme: Theme, store: ProjectStore, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store
        self._project_id: str | None = None
        self._worker: AgentWorker | None = None
        self._lesson_worker: LessonWorker | None = None
        self._runner: AgentRunner | None = None
        self._worker_mode: str = "run"  # run | chat | lesson
        self._status_before_chat: ProjectStatus | None = None
        self._status_before_lesson: ProjectStatus | None = None
        self._compact_worker: _CompactWorker | None = None
        self._refine_worker: _RefineMetaWorker | None = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._essentials = _ProjectEssentials(self.theme)
        self._essentials.goal_edit_requested.connect(self._edit_goal)
        self._essentials.approval_edit_requested.connect(self._edit_approval)
        self._essentials.ai_refine_requested.connect(self._on_ai_refine_meta)
        layout.addWidget(self._essentials)

        self._timeline = _Timeline(self.theme)
        layout.addWidget(self._timeline, stretch=1)

        self._actions = _ActionBar(self.theme)
        self._actions.run_clicked.connect(self._on_run)
        self._actions.pause_clicked.connect(self._on_pause)
        self._actions.open_folder_clicked.connect(self._on_open_folder)
        self._actions.open_deliverables_clicked.connect(self._on_open_deliverables)
        self._actions.upload_clicked.connect(self._on_upload)
        self._actions.open_references_clicked.connect(self._on_open_references)
        self._actions.import_references_clicked.connect(self._on_import_references)
        self._actions.summarize_clicked.connect(self._on_summarize)
        self._actions.clear_experience_clicked.connect(self._on_clear_experience)
        self._actions.send_clicked.connect(self._on_send)
        self._actions.approve_clicked.connect(self._on_approve)
        self._actions.reject_clicked.connect(self._on_reject)
        self._actions.model_changed.connect(self._on_model_changed)
        self._actions.compress_clicked.connect(self._on_compress_clicked)
        self._actions.draft_changed.connect(self._schedule_usage_refresh)
        layout.addWidget(self._actions)

        self._essentials_timer = QTimer(self)
        self._essentials_timer.setSingleShot(True)
        self._essentials_timer.setInterval(200)
        self._essentials_timer.timeout.connect(self._refresh_essentials)
        self._sidebar_timer = QTimer(self)
        self._sidebar_timer.setSingleShot(True)
        self._sidebar_timer.setInterval(250)
        self._sidebar_timer.timeout.connect(self.status_changed.emit)
        self._usage_timer = QTimer(self)
        self._usage_timer.setSingleShot(True)
        self._usage_timer.setInterval(400)
        self._usage_timer.timeout.connect(self._refresh_context_usage)
        self.show_welcome()

    def show_welcome(self):
        self._project_id = None
        self._essentials.clear()
        self._timeline.show_empty()
        self._actions.hide_approval()
        self._actions.set_running(False)
        self._actions.reload_models(
            fallback_provider=self.store.settings.default_provider,
            fallback_model=self.store.settings.default_model_id,
        )
        self._refresh_context_usage()

    def load_project(self, project_id: str, *, force_timeline: bool = False):
        if not project_id:
            self.show_welcome()
            return
        project = self.store.get(project_id)
        if not project:
            self.show_welcome()
            return
        same = self._project_id == project_id
        self._project_id = project_id
        root = self.store.path_for(project_id)
        self._essentials.bind(project, project_root=root)
        # 同一项目勿整表重绘：总结经验/弹窗关闭时会误触发，导致视口跳回旧消息
        if force_timeline or not same or not self._timeline._bubbles:
            events = self.store.list_events(project_id)
            self._timeline.render_events(events)
        self._actions.reload_models(
            project.provider,
            project.model_id,
            fallback_provider=self.store.settings.default_provider,
            fallback_model=self.store.settings.default_model_id,
        )
        # 项目尚未绑定模型时，把当前下拉选择写回，保证运行用同一模型
        if not (project.provider and project.model_id):
            p, m = self._actions.selected_model()
            if m:
                project.provider = p
                project.model_id = m
                self.store.save(project)
        self._refresh_context_usage()

    def _on_model_changed(self, provider_id: str, model_id: str):
        if not self._project_id:
            # 无项目时：同步到厂商「默认模型」，避免写进 WokBee 旧配置覆盖厂商默认
            if provider_id and model_id:
                try:
                    ProviderStore().set_default_model(provider_id, model_id)
                except Exception:
                    pass
            return
        project = self.store.get(self._project_id)
        if not project:
            return
        if project.provider == provider_id and project.model_id == model_id:
            self._refresh_context_usage()
            return
        project.provider = provider_id or ""
        project.model_id = model_id or ""
        self.store.save(project)
        # 气泡用厂商显示名，与下拉框一致（勿用 provider_id）
        display = (self._actions._model_combo.currentText() or "").strip()
        if not display or display.startswith("未配置"):
            try:
                resolved = ProviderStore().resolve(provider_id, model_id)
                if resolved:
                    display = f"{resolved.provider_name} / {resolved.model_id}"
            except Exception:
                display = ""
        if not display:
            display = f"{provider_id}/{model_id}"
        ev = ProjectEvent(
            kind="info",
            content=f"已切换模型：{display}",
            meta={"provider": provider_id, "model_id": model_id, "display": display},
        )
        self.store.append_event(self._project_id, ev)
        self._timeline.append_event(ev)
        self._refresh_context_usage()

    def _schedule_usage_refresh(self):
        self._usage_timer.start()

    def _context_window_for_current(self) -> int:
        provider_id, model_id = self._actions.selected_model()
        if not model_id:
            return 0
        try:
            resolved = ProviderStore().resolve(provider_id, model_id)
            if resolved:
                return int(getattr(resolved, "context_window", 0) or 0)
        except Exception:
            pass
        return 0

    def _refresh_context_usage(self):
        if not self._project_id:
            self._actions.set_context_usage(0, 0, enabled=False)
            return
        root = self.store.path_for(self._project_id)
        events = self.store.list_events(self._project_id)
        usage = estimate_project_usage(
            events=events,
            project_root=root,
            context_window=self._context_window_for_current(),
            draft_text=self._actions.draft_text(),
        )
        busy = bool(self._compact_worker and self._compact_worker.isRunning())
        self._actions.set_context_usage(
            usage.used, usage.limit, enabled=not busy,
        )

    def _on_compress_clicked(self):
        if not self._project_id:
            _tip(self, self.theme, "请先新建或选择一个项目。")
            return
        if self._compact_worker and self._compact_worker.isRunning():
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "Agent 运行中，请稍后再压缩。")
            return
        root = self.store.path_for(self._project_id)
        events = self.store.list_events(self._project_id)
        plan = plan_project_compaction(events, root)
        if plan is None:
            _tip(self, self.theme, "当前上下文较短，无需压缩。")
            return

        to_compact, _retained, new_boundary, prev_summary, pin_end = plan
        client = None
        provider_id, model_id = self._actions.selected_model()
        try:
            resolved = ProviderStore().resolve(provider_id, model_id)
            if resolved and resolved.api_host:
                client = AIClient(
                    resolved.api_host,
                    resolved.api_key,
                    resolved.model_id,
                    family=resolved.family,
                )
        except Exception:
            client = None

        worker = _CompactWorker(
            client,
            to_compact,
            prev_summary,
            new_boundary,
            parent=self,
            pin_end=pin_end,
        )
        self._compact_worker = worker
        worker.finished.connect(self._on_compact_done)
        worker.failed.connect(self._on_compact_failed)
        worker.start()
        self._refresh_context_usage()

    def _on_compact_done(self, summary: str, boundary: int, pin_end: int = 0):
        self._compact_worker = None
        if not self._project_id:
            return
        root = self.store.path_for(self._project_id)
        state = load_context_state(root)
        state["compaction_points"] = ctxman.append_compaction_point(
            state.get("compaction_points") or [],
            summary=summary,
            boundary_index=boundary,
            pin_end=pin_end,
        )
        save_context_state(root, state)
        self._refresh_context_usage()
        _tip(self, self.theme, "已压缩上下文（已钉住首条任务前缀）。")

    def _on_compact_failed(self, err: str):
        self._compact_worker = None
        self._refresh_context_usage()
        _tip(self, self.theme, f"压缩失败：{err}")

    def _refresh(self):
        """完整刷新（切换项目、归档、手动操作后）。运行中请勿频繁调用。"""
        if self._project_id:
            self.load_project(self._project_id, force_timeline=True)

    def _refresh_essentials(self):
        if not self._project_id:
            return
        project = self.store.get(self._project_id)
        if project:
            self._essentials.bind(
                project, project_root=self.store.path_for(self._project_id)
            )

    def _schedule_essentials_refresh(self):
        self._essentials_timer.start()
        # 同步刷新左侧项目列表状态（防抖）
        self._sidebar_timer.start()

    def _edit_goal(self):
        if not self._project_id:
            return
        project = self.store.get(self._project_id)
        if not project:
            return
        new_goal = _ask_multiline(
            self,
            self.theme,
            "编辑目标",
            "项目目标（多行，超出可滚动）",
            project.goal,
            min_lines=5,
        )
        if new_goal is None:
            return
        self.store.update_goal(self._project_id, new_goal)
        self._refresh()

    def _on_ai_refine_meta(self):
        """一键调用 AI，根据当前目标与最近时间线更新名称+目标。"""
        if not self._project_id:
            return
        if self._refine_worker and self._refine_worker.isRunning():
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "请先等待当前运行/对话结束。")
            return
        if self._lesson_worker and self._lesson_worker.isRunning():
            _tip(self, self.theme, "正在总结经验，请稍候。")
            return
        project = self.store.get(self._project_id)
        if not project:
            return
        try:
            resolved = resolve_model_for_project(project, self.store.settings)
        except ValueError as e:
            _tip(self, self.theme, str(e))
            return
        if not (resolved.api_key and resolved.api_host and resolved.model_id):
            _tip(self, self.theme, "请先在「厂商设置」配置可用模型。")
            return

        events = self.store.list_events(self._project_id)
        # 取最近片段，控制 token
        recent = events[-80:] if len(events) > 80 else events
        timeline_log = collect_events_log(recent, max_chars=8000)
        client = AIClient(
            resolved.api_host,
            resolved.api_key,
            resolved.model_id,
            family=resolved.family,
        )
        self._essentials.set_ai_refine_busy(True)
        worker = _RefineMetaWorker(
            client,
            current_title=project.title,
            current_goal=project.goal,
            timeline_log=timeline_log,
            max_title_len=MAX_PROJECT_TITLE_LEN,
            parent=self,
        )
        self._refine_worker = worker
        worker.finished_ok.connect(self._on_ai_refine_ok)
        worker.failed.connect(self._on_ai_refine_failed)
        worker.start()

    def _on_ai_refine_ok(self, title: str, goal: str):
        self._refine_worker = None
        self._essentials.set_ai_refine_busy(False)
        if not self._project_id:
            return
        title = (title or "").strip()
        goal = (goal or "").strip()
        if not title and not goal:
            return
        patched = self.store.patch(
            self._project_id,
            **{k: v for k, v in (("title", title), ("goal", goal)) if v},
        )
        if not patched:
            return
        ev = ProjectEvent(
            kind="info",
            content=(
                f"AI 已更新项目元信息：\n"
                f"- 名称：{patched.title}\n"
                f"- 目标：{patched.goal or '（空）'}"
            ),
        )
        self.store.append_event(self._project_id, ev)
        self._timeline.append_event(ev)
        self._refresh_essentials()
        self.status_changed.emit()

    def _on_ai_refine_failed(self, err: str):
        self._refine_worker = None
        self._essentials.set_ai_refine_busy(False)
        if self._project_id:
            ev = ProjectEvent(kind="error", content=f"AI 更新名称/目标失败：{err}")
            self.store.append_event(self._project_id, ev)
            self._timeline.append_event(ev)

    def _edit_approval(self):
        if not self._project_id:
            return
        project = self.store.get(self._project_id)
        if not project:
            return
        updated = _prompt_approval_flags(self, self.theme, project.approval)
        if updated is None:
            return
        self.store.set_approval(self._project_id, updated)
        ev = ProjectEvent(
            kind="approval",
            content=f"已更新本项目审核策略：{updated.summary()}",
        )
        self.store.append_event(self._project_id, ev)
        self._timeline.append_event(ev)
        self._schedule_essentials_refresh()

    def _on_send(self, text: str):
        """非运行期：对话模式回复提问（可与目标无关），可改名称/目标。"""
        if not self._project_id:
            _tip(self, self.theme, "请先新建或选择一个项目。")
            return
        text = (text or "").strip()
        if not text:
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "当前正在运行或对话中，请稍候或先点暂停。")
            return
        if self._lesson_worker and self._lesson_worker.isRunning():
            _tip(self, self.theme, "正在总结经验，请稍候。")
            return

        project = self.store.get(self._project_id)
        if not project:
            return

        uev = ProjectEvent(kind="user", content=text)
        self.store.append_event(self._project_id, uev)
        self._timeline.append_event(uev)

        try:
            resolved = resolve_model_for_project(project, self.store.settings)
        except ValueError as e:
            _tip(self, self.theme, str(e))
            return

        self._status_before_chat = project.status
        self.store.set_status(
            self._project_id,
            ProjectStatus.RUNNING,
            current_step="对话中",
        )
        self._schedule_essentials_refresh()

        req = RunRequest(
            project=project,
            project_root=self.store.path_for(project.id),
            user_message=text,
            resolved=resolved,
            approval=project.approval.copy(),
            max_steps=self.store.settings.max_steps,
        )
        self._runner = AgentRunner(self.store.settings)
        self._worker_mode = "chat"
        self._worker = AgentWorker(self._runner, req, parent=self, mode="chat")
        self._timeline.begin_run()
        self._worker.event_emitted.connect(self._on_engine_event)
        self._worker.approval_needed.connect(self._on_approval_needed)
        self._worker.ask_user_needed.connect(self._on_ask_user_needed)
        self._worker.finished_result.connect(self._on_engine_finished)
        self._actions.set_running(True)
        self._actions.set_cache_stats("")
        self._actions.hide_approval()
        self._worker.start()

    def _on_run(self):
        if not self._project_id:
            _tip(self, self.theme, "请先新建或选择一个项目。")
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "当前项目已在运行中。")
            return
        if self._lesson_worker and self._lesson_worker.isRunning():
            _tip(self, self.theme, "正在总结经验，请稍候。")
            return

        text = self._actions.take_input()
        project = self.store.get(self._project_id)
        if not project:
            if text:
                self._actions.set_draft(text)
            return

        goal = (project.goal or "").strip()
        if not goal:
            # 运行前必须有目标：弹窗让用户补填；取消则还原输入框
            filled = _ask_multiline(
                self,
                self.theme,
                "请填写项目目标",
                "当前项目目标为空，运行前需要先设置目标。",
                text or "",
                min_lines=5,
            )
            if not filled:
                if text:
                    self._actions.set_draft(text)
                else:
                    _tip(self, self.theme, "请先设置项目目标后再运行。")
                return
            self.store.update_goal(self._project_id, filled)
            project = self.store.get(self._project_id) or project
            goal = filled
            self._schedule_essentials_refresh()
            # 输入框内容若已用作目标，不再重复当指令；无额外指令时用目标运行
            text = ""

        # 运行前自动归档上一轮（有内容才归档；保留经验与 scripts）
        self._auto_archive_before_run()
        project = self.store.get(self._project_id) or project

        if text:
            uev = ProjectEvent(kind="user", content=text)
            self.store.append_event(self._project_id, uev)
            self._timeline.append_event(uev)
        user_message = text or goal
        if not user_message:
            _tip(self, self.theme, "请先设置项目目标或在输入框填写指令。")
            return

        try:
            resolved = resolve_model_for_project(project, self.store.settings)
        except ValueError as e:
            _tip(self, self.theme, str(e))
            return

        self._status_before_chat = None
        self._worker_mode = "run"
        self.store.set_status(
            self._project_id,
            ProjectStatus.RUNNING,
            current_step="Deep Agents 执行中",
            progress_done=0,
            progress_total=self.store.settings.max_steps,
        )
        self._schedule_essentials_refresh()

        req = RunRequest(
            project=project,
            project_root=self.store.path_for(project.id),
            user_message=user_message,
            resolved=resolved,
            approval=project.approval.copy(),
            max_steps=self.store.settings.max_steps,
        )
        self._runner = AgentRunner(self.store.settings)
        self._worker = AgentWorker(self._runner, req, parent=self, mode="run")
        self._timeline.begin_run()
        self._worker.event_emitted.connect(self._on_engine_event)
        self._worker.approval_needed.connect(self._on_approval_needed)
        self._worker.ask_user_needed.connect(self._on_ask_user_needed)
        self._worker.finished_result.connect(self._on_engine_finished)
        self._actions.set_running(True)
        self._actions.set_cache_stats("")
        self._actions.hide_approval()
        self._worker.start()

    def _on_engine_event(self, kind: str, content: str, meta: object):
        if not self._project_id:
            return
        meta_d = meta if isinstance(meta, dict) else {}
        if kind == "cache" or meta_d.get("cache"):
            now_pct = meta_d.get("now_pct")
            avg_pct = meta_d.get("avg_pct")
            if now_pct is not None or avg_pct is not None:
                now_s = f"{now_pct}%" if now_pct is not None else "—"
                avg_s = f"{avg_pct}%" if avg_pct is not None else "—"
                tag = f"cache {now_s} · avg {avg_s}"
                tip = (
                    f"本轮 hit={meta_d.get('last_hit', 0)} miss={meta_d.get('last_miss', 0)}\n"
                    f"会话 hit={meta_d.get('hit_total', 0)} miss={meta_d.get('miss_total', 0)}\n"
                    f"prefix={meta_d.get('prefix_fp') or '—'}"
                )
                self._actions.set_cache_stats(tag, tooltip=tip)
            if kind == "cache":
                # 不刷时间线，避免每轮刷屏
                return
        ev = ProjectEvent(
            kind=kind,
            content=content,
            meta=meta_d,
        )
        self.store.append_event(self._project_id, ev)
        # 增量追加（含工具 call / callback）
        self._timeline.append_event(ev)
        # 实时状态条：工具事件由 _route_tool_event 内部驱动，这里补其余类型
        if kind == "agent":
            self._timeline._status(
                "正在思考…" if str(meta_d.get("phase") or "") == "reasoning" else "正在执行…"
            )
        elif kind == "approval":
            self._timeline._status("等待审批…", pulse=False)
        elif kind == "error":
            self._timeline._status("出现错误")
        if kind == "approval":
            self.store.set_status(
                self._project_id,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="等待审批",
            )
        # 名称/目标被工具改写后立刻刷新顶栏与侧栏
        if meta_d.get("project_meta") in ("title", "goal"):
            self._refresh_essentials()
            self.status_changed.emit()
        else:
            self._schedule_essentials_refresh()
        self._schedule_usage_refresh()

    def _on_approval_needed(self, pending: object):
        items = pending if isinstance(pending, list) else []
        lines = []
        for i, act in enumerate(items, 1):
            if isinstance(act, dict):
                lines.append(
                    f"{i}. [{act.get('risk', '?')}] {act.get('name')}: {act.get('description')}"
                )
        text = "需要你审批以下工具调用：\n" + ("\n".join(lines) if lines else str(pending))
        self._actions.show_approval(text)
        self._timeline.on_approval_pending()
        if self._project_id:
            self.store.set_status(
                self._project_id,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="等待审批",
            )
            self._schedule_essentials_refresh()

    def _on_ask_user_needed(self, payload: object):
        """主线程弹窗收集澄清答案，再回传后台 Agent。"""
        data = payload if isinstance(payload, dict) else {"type": "ask_user", "questions": []}
        if self._project_id:
            self.store.set_status(
                self._project_id,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="等待澄清意图",
            )
            self._schedule_essentials_refresh()
        dlg = AskUserDialog(data, self.theme, parent=self.window() or self)
        accepted = dlg.exec() == AskUserDialog.DialogCode.Accepted
        answers = dlg.result_payload() if accepted else {"cancelled": True}
        if self._worker and self._worker.isRunning():
            self._worker.resolve_ask_user(answers)

    def _on_approve(self):
        if self._worker and self._worker.isRunning():
            self._actions.hide_approval()
            self._timeline.resume_after_approval(approved=True)
            self._worker.approve_all()

    def _on_reject(self):
        if self._worker and self._worker.isRunning():
            self._actions.hide_approval()
            self._timeline.resume_after_approval(approved=False)
            self._worker.reject_all("用户拒绝该操作")

    def _on_engine_finished(self, result: object):
        self._actions.set_running(False)
        self._actions.hide_approval()
        self._timeline.end_run()
        if not self._project_id:
            return
        outcome = getattr(result, "outcome", "failed")
        err = getattr(result, "error", "") or ""
        mode = self._worker_mode or "run"

        if mode == "chat":
            # 对话结束：尽量恢复进入对话前的状态，避免把「完成」冲掉
            prev = self._status_before_chat
            if outcome == "awaiting_approval":
                self.store.set_status(
                    self._project_id,
                    ProjectStatus.AWAITING_APPROVAL,
                    current_step="对话待审批",
                )
            elif outcome == "cancelled":
                restore = prev if prev and prev != ProjectStatus.RUNNING else ProjectStatus.IDLE
                self.store.set_status(
                    self._project_id,
                    restore,
                    current_step="对话已取消",
                )
            elif outcome == "failed":
                restore = prev if prev and prev != ProjectStatus.RUNNING else ProjectStatus.IDLE
                self.store.set_status(
                    self._project_id,
                    restore,
                    current_step="对话失败",
                )
                if err:
                    ev = ProjectEvent(kind="error", content=f"对话失败：{err}")
                    self.store.append_event(self._project_id, ev)
                    self._timeline.append_event(ev)
            else:
                restore = prev if prev and prev != ProjectStatus.RUNNING else ProjectStatus.IDLE
                step = "空闲" if restore == ProjectStatus.IDLE else (
                    "完成" if restore == ProjectStatus.DONE else restore.value
                )
                self.store.set_status(
                    self._project_id,
                    restore,
                    current_step=step,
                )
            self._status_before_chat = None
            self._worker_mode = "run"
            self._worker = None
            self._runner = None
            self._schedule_essentials_refresh()
            self._refresh_context_usage()
            return

        # 结束类文案多由引擎事件已推送；这里只更新状态，避免重复气泡 + 全量重绘
        if outcome == "success":
            self.store.set_status(
                self._project_id,
                ProjectStatus.DONE,
                current_step="完成",
                progress_done=1,
                progress_total=1,
            )
        elif outcome == "cancelled":
            self.store.set_status(
                self._project_id,
                ProjectStatus.IDLE,
                current_step="已取消",
            )
        elif outcome == "awaiting_approval":
            self.store.set_status(
                self._project_id,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="仍待审批",
            )
        else:
            self.store.set_status(
                self._project_id,
                ProjectStatus.FAILED,
                current_step="失败",
            )
            if err:
                ev = ProjectEvent(
                    kind="error",
                    content=f"运行失败：{err}",
                )
                self.store.append_event(self._project_id, ev)
                self._timeline.append_event(ev)
        project = self.store.get(self._project_id)
        if project:
            names = list_deliverable_names(
                self.store.path_for(self._project_id), limit=5
            )
            if names:
                project.artifacts_summary = ", ".join(names)
                self.store.save(project)
        self._worker = None
        self._runner = None
        self._worker_mode = "run"
        self._schedule_essentials_refresh()
        self._refresh_context_usage()

    def _on_pause(self):
        if not self._project_id:
            return
        if self._lesson_worker and self._lesson_worker.isRunning():
            _tip(self, self.theme, "正在总结经验，请稍候完成（暂不支持中途取消）。")
            return
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            ev = ProjectEvent(kind="info", content="用户请求暂停/取消当前运行。")
            self.store.append_event(self._project_id, ev)
            self._timeline.append_event(ev)
            return
        self.store.set_status(self._project_id, ProjectStatus.IDLE, current_step="已暂停")
        ev = ProjectEvent(kind="info", content="当前无运行中的任务。")
        self.store.append_event(self._project_id, ev)
        self._timeline.append_event(ev)
        self._schedule_essentials_refresh()

    def _on_open_folder(self):
        if not self._project_id:
            _tip(self, self.theme, "请先选择项目。")
            return
        _open_in_explorer(self.store.path_for(self._project_id))

    def _on_open_deliverables(self):
        if not self._project_id:
            _tip(self, self.theme, "请先选择项目。")
            return
        path = deliverables_dir(self.store.path_for(self._project_id))
        path.mkdir(parents=True, exist_ok=True)
        _open_in_explorer(path)

    def _on_upload(self):
        if not self._project_id:
            _tip(self, self.theme, "请先选择项目。")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要上传的文件",
            "",
            "所有文件 (*.*)",
        )
        if not files:
            return
        dest_dir = uploads_dir(self.store.path_for(self._project_id))
        dest_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        from datetime import datetime as _dt

        saved: list[str] = []
        for src in files:
            src_path = Path(src)
            name = src_path.name
            target = dest_dir / name
            if target.exists():
                stem, suf = src_path.stem, src_path.suffix
                stamp = _dt.now().strftime("%H%M%S")
                target = dest_dir / f"{stem}_{stamp}{suf}"
            try:
                shutil.copy2(src_path, target)
                saved.append(target.name)
            except OSError as e:
                _tip(self, self.theme, f"上传失败：{name}\n{e}")
                return
        ev = ProjectEvent(
            kind="info",
            content=(
                f"已上传 {len(saved)} 个文件到 uploads/：\n"
                + "\n".join(f"- `{n}`" for n in saved)
                + "\nAgent 运行时可直接读取调用；归档时会一并归档。"
            ),
        )
        self.store.append_event(self._project_id, ev)
        self._timeline.append_event(ev)
        self._schedule_essentials_refresh()
        _tip(
            self,
            self.theme,
            f"已保存到 uploads/：\n" + "\n".join(saved),
            title="上传完成",
        )

    def _on_open_references(self):
        if not self._project_id:
            _tip(self, self.theme, "请先选择项目。")
            return
        path = references_dir(self.store.path_for(self._project_id))
        path.mkdir(parents=True, exist_ok=True)
        _open_in_explorer(path)

    def _on_import_references(self):
        if not self._project_id:
            _tip(self, self.theme, "请先选择项目。")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择要导入参考材料（第三方代码/配置/环境参数）的文件",
            "",
            "所有文件 (*.*)",
        )
        if not files:
            return
        dest_dir = references_dir(self.store.path_for(self._project_id))
        dest_dir.mkdir(parents=True, exist_ok=True)
        import shutil
        from datetime import datetime as _dt

        saved: list[str] = []
        for src in files:
            src_path = Path(src)
            name = src_path.name
            target = dest_dir / name
            if target.exists():
                stem, suf = src_path.stem, src_path.suffix
                stamp = _dt.now().strftime("%H%M%S")
                target = dest_dir / f"{stem}_{stamp}{suf}"
            try:
                shutil.copy2(src_path, target)
                saved.append(target.name)
            except OSError as e:
                _tip(self, self.theme, f"导入失败：{name}\n{e}")
                return
        ev = ProjectEvent(
            kind="info",
            content=(
                f"已导 {len(saved)} 个文件到 references/：\n"
                + "\n".join(f"- `{n}`" for n in saved)
                + "\n参考材料不会被归档，供下次稳定复跑；敏感信息仅供本机使用，勿外发。"
            ),
        )
        self.store.append_event(self._project_id, ev)
        self._timeline.append_event(ev)
        self._schedule_essentials_refresh()
        _tip(
            self,
            self.theme,
            f"已保存到 references/：\n" + "\n".join(saved),
            title="导入完成",
        )

    def _on_summarize(self):
        """人工发起：根据当前时间线写入一条经验（后台线程，避免卡住 UI）。"""
        if not self._project_id:
            _tip(self, self.theme, "请先选择项目。")
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "请先等待当前运行结束，再总结经验。")
            return
        if self._lesson_worker and self._lesson_worker.isRunning():
            _tip(self, self.theme, "正在总结经验，请稍候。")
            return
        project = self.store.get(self._project_id)
        if not project:
            return
        events = self.store.list_events(self._project_id)
        useful = [
            e
            for e in events
            if e.kind in ("tool", "agent", "error", "user", "info", "approval")
        ]
        if not useful:
            _tip(self, self.theme, "当前没有可总结的运行记录，请先运行一次。")
            return

        path_text, summary, errors = build_success_path_from_timeline_events(events)
        if project.status == ProjectStatus.DONE:
            outcome = "success"
        elif project.status == ProjectStatus.FAILED:
            outcome = "failed"
        elif any(e.kind == "error" for e in events):
            outcome = "failed"
        elif any("运行结束：成功" in (e.content or "") for e in events):
            outcome = "success"
        else:
            outcome = "partial"

        if not summary:
            summary = (project.goal or "人工总结当前会话")[:800]

        try:
            resolved = resolve_model_for_project(project, self.store.settings)
        except ValueError:
            from types import SimpleNamespace

            resolved = SimpleNamespace(
                provider_name="unknown",
                model_id="unknown",
                api_key="",
                api_host="",
            )

        notes = (
            "- 本条由用户点击「总结经验」人工发起。\n"
            "- 需要实时数据时必须联网，禁止凭记忆编造。\n"
            "- 高危 execute / 写文件是否免审取决于项目审核策略。"
        )
        if errors:
            notes = f"- 时间线错误摘录：{errors[:300]}\n" + notes

        req = RunRequest(
            project=project,
            project_root=self.store.path_for(project.id),
            user_message=project.goal or summary,
            resolved=resolved,  # type: ignore[arg-type]
            approval=project.approval.copy(),
            max_steps=self.store.settings.max_steps,
        )
        runner = AgentRunner(self.store.settings)
        self._runner = runner
        self._worker_mode = "lesson"
        self._status_before_lesson = project.status
        self.store.set_status(
            self._project_id,
            ProjectStatus.RUNNING,
            current_step="总结经验中",
        )
        self._schedule_essentials_refresh()
        self._actions.set_running(True)

        worker = LessonWorker(
            runner,
            req,
            outcome=outcome,
            summary=summary,
            errors=errors,
            success_path=path_text,
            notes=notes,
            events=events,
            parent=self,
        )
        self._lesson_worker = worker
        worker.event_emitted.connect(self._on_engine_event)
        worker.finished_lesson.connect(self._on_lesson_finished)
        worker.failed.connect(self._on_lesson_failed)
        worker.start()

    def _restore_after_lesson(self):
        self._lesson_worker = None
        self._runner = None
        self._worker_mode = "run"
        self._actions.set_running(False)
        if not self._project_id:
            self._status_before_lesson = None
            return
        prev = self._status_before_lesson
        self._status_before_lesson = None
        restore = prev if prev and prev != ProjectStatus.RUNNING else ProjectStatus.IDLE
        step = (
            "完成" if restore == ProjectStatus.DONE
            else (
                "失败" if restore == ProjectStatus.FAILED
                else ("空闲" if restore == ProjectStatus.IDLE else restore.value)
            )
        )
        self.store.set_status(self._project_id, restore, current_step=step)
        self._schedule_essentials_refresh()

    def _on_lesson_finished(self, lesson: object):
        self._restore_after_lesson()
        if not self._project_id:
            return
        # 过程事件已增量追加；成功/失败只落时间线，不再弹窗
        if not lesson:
            ev = ProjectEvent(kind="error", content="经验写入失败，请查看日志。")
            self.store.append_event(self._project_id, ev)
            self._timeline.append_event(ev)
        self._refresh_essentials()
        self._timeline._schedule_scroll_to_bottom()
        self.status_changed.emit()

    def _on_lesson_failed(self, err: str):
        self._restore_after_lesson()
        if self._project_id:
            ev = ProjectEvent(kind="error", content=f"总结经验失败：{err}")
            self.store.append_event(self._project_id, ev)
            self._timeline.append_event(ev)
            self._timeline._schedule_scroll_to_bottom()

    def _auto_archive_before_run(self) -> None:
        """运行前自动归档上一轮会话（无内容则跳过）。"""
        if not self._project_id:
            return
        if not self.store.needs_auto_archive(self._project_id):
            return
        dest = self.store.archive_session(
            self._project_id,
            include_memory=False,
            reason="auto_before_run",
        )
        if not dest:
            return
        # 归档会清空时间线并写入一条提示，整表刷新到最新
        events = self.store.list_events(self._project_id)
        self._timeline.render_events(events)
        self._refresh_essentials()
        self.status_changed.emit()

    def _on_clear_experience(self):
        """归档会话，并把经验文档一并归档后清空。"""
        if not self._project_id:
            return
        if self._worker and self._worker.isRunning():
            return
        if self._lesson_worker and self._lesson_worker.isRunning():
            return
        ok = _confirm(
            self,
            self.theme,
            "清空经验",
            "确认后将：\n"
            "• 执行一次归档（对话、工作区、交付物、上传等）\n"
            "• 并把 memory/ 经验文档与 scripts/ 本地脚本一并归档后清空\n"
            "• 保留：项目名称、目标、审核策略\n"
            f"• 每个项目最多保留 {MAX_ARCHIVES} 份存档，超出自动删除最旧的\n"
            "• 之后 Agent 禁止访问 archives/\n\n"
            "是否继续？",
        )
        if not ok:
            return
        dest = self.store.archive_session(
            self._project_id,
            include_memory=True,
            reason="clear_experience",
        )
        if not dest:
            return
        self._refresh()


# ─── WokBee 页 ───

class WokBeeView(QWidget):
    """wokbee 主页面：中栏项目列表 + 右栏三段工作区。"""

    def __init__(
        self,
        theme: Theme,
        store: ProjectStore | None = None,
        settings: WokBeeSettings | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.settings = settings or WokBeeSettings()
        self.store = store or ProjectStore(self.settings)
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._sidebar = _ProjectSidebar(self.theme, self.store)
        self._sidebar.project_selected.connect(self._on_selected)
        self._sidebar.project_changed.connect(self._on_list_changed)
        layout.addWidget(self._sidebar)

        self._workspace = _ProjectWorkspace(self.theme, self.store)
        self._workspace.status_changed.connect(self._on_workspace_status_changed)
        layout.addWidget(self._workspace, stretch=1)

        projects = self.store.list_projects()
        if projects:
            self._sidebar.select(projects[0].id)

    def _on_selected(self, project_id: str):
        self._workspace.load_project(project_id)

    def _on_list_changed(self):
        # 删除后无项目时清空工作区
        if not self.store.list_projects():
            self._workspace.show_welcome()

    def _on_workspace_status_changed(self):
        """工作区状态变更时刷新侧栏，保持「完成/运行中」一致。"""
        self._sidebar.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._sidebar.refresh()
        # 同一项目不要反复 load_project/整表重绘，否则总结经验或弹窗关闭后会跳到旧记录
        sel = self._sidebar._selected_id
        if not sel:
            return
        if self._workspace._project_id != sel:
            self._workspace.load_project(sel)
        else:
            self._workspace._refresh_essentials()
