"""WokBee 工作区：侧栏项目列表、项目要素、后台 worker、与三段式整体工作区。

聚合了四种自有定义：侧栏/项目列表、项目要素、后台 worker（上下文压缩 / AI 改名）、
以及把时间线 + 操作栏 + 侧栏拼成一体的 `_ProjectWorkspace`。时间线与操作栏本身
分别位于 `timeline.py` / `action_bar.py`。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from tokbee.core import context_manager as ctxman
from tokbee.core.ai_client import AIClient
from tokbee.core.provider_store import ProviderStore
from tokbee.ui.styles.system import make_context_menu
from tokbee.ui.styles.theme import Theme

from wokbee.core.context_usage import (
    estimate_project_usage,
    load_context_state,
    plan_project_compaction,
    save_context_state,
)
from wokbee.core.models import MAX_PROJECT_TITLE_LEN, Project, ProjectEvent, ProjectStatus
from wokbee.core.paths import deliverables_dir, list_deliverable_names, uploads_dir
from wokbee.core.project_store import MAX_ARCHIVES, ProjectStore, TRASH_RETENTION_DAYS
from wokbee.engine.lessons import build_success_path_from_timeline_events, collect_events_log
from wokbee.ui.action_bar import _ActionBar
from wokbee.ui.ask_user_dialog import AskUserDialog
from wokbee.ui.dialogs import (
    _ask_multiline,
    _ask_text,
    _confirm,
    _default_project_title,
    _prompt_approval_flags,
    open_path as _open_in_explorer,
    tip as _tip,
)
from wokbee.ui.timeline import _Timeline


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
        self._cancelled = False
        if client is not None:
            client.cancel_check = lambda: self._cancelled

    def cancel(self):
        self._cancelled = True

    def run(self):
        if self._cancelled:
            return
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
        if self._cancelled:
            return
        if not summary:
            summary = ctxman.mechanical_summary(
                self._to_compact, self._previous_summary,
            )
        if self._cancelled:
            return
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
        settings,
        project,
        *,
        current_title: str,
        current_goal: str,
        timeline_log: str,
        max_title_len: int,
        parent=None,
    ):
        super().__init__(parent)
        self._settings = settings
        self._project = project
        self._current_title = current_title
        self._current_goal = current_goal
        self._timeline_log = timeline_log
        self._max_title_len = max_title_len
        self._cancelled = False
        self._client = None

    def cancel(self):
        self._cancelled = True

    def run(self):
        if self._cancelled:
            return
        # 模型解析 + AIClient 构造放进 worker 线程，避免 UI 线程 import 重型引擎。
        from wokbee.engine import ensure_engine_warm

        ensure_engine_warm()
        from wokbee.engine.runner import resolve_model_for_project

        try:
            resolved = resolve_model_for_project(self._project, self._settings)
        except Exception as e:
            if not self._cancelled:
                self.failed.emit(str(e))
            return
        if not (resolved.api_key and resolved.api_host and resolved.model_id):
            self.failed.emit("请先在「厂商设置」配置可用模型。")
            return
        client = AIClient(
            resolved.api_host,
            resolved.api_key,
            resolved.model_id,
            family=resolved.family,
        )
        self._client = client
        client.cancel_check = lambda: self._cancelled
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
            if not self._cancelled:
                self.failed.emit(str(e))
            return
        if self._cancelled:
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
        # 直接创建空项目，名称默认「项目+时间」，目标稍后在详情里补
        project = self.store.create(title=_default_project_title(), goal="")
        self.project_changed.emit()
        self.select(project.id)

    def _on_context(self, project_id: str, pos):
        c = self.theme.colors
        menu = make_context_menu(self, c)
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
        self._ai_refine_btn.setToolTip("AI 更新项目名与目标")
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
        self._references.setToolTip("参考材料不归档")
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
                    self._references.setToolTip("参考材料不归档")
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
        # 运行/对话/经验 worker 的事件应落到的项目（发起时捕获），避免运行中切换项目串写
        self._worker_project_id: str | None = None
        self._compact_project_id: str | None = None
        self._refine_project_id: str | None = None
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
        self._actions.summarize_clicked.connect(self._on_summarize)
        self._actions.clear_experience_clicked.connect(self._on_clear_experience)
        self._actions.compact_mode_changed.connect(self._timeline.set_global_compact_mode)
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
        # 切换项目：始终刷新时间线并滚到最新；同项目仅 force 或空表时重绘
        if not same:
            events = self.store.list_events(project_id)
            self._timeline.render_events(events)
        elif force_timeline or not self._timeline._bubbles:
            events = self.store.list_events(project_id)
            self._timeline.render_events(events)
        self._timeline.set_agent_running(
            self._worker is not None and self._worker.isRunning()
        )
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

        self._compact_project_id = self._project_id  # 发起时捕获，切换项目不写错
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
        target = self._compact_project_id or self._project_id
        self._compact_worker = None
        self._compact_project_id = None
        if not target:
            return
        root = self.store.path_for(target)
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
        self._compact_project_id = None
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

        events = self.store.list_events(self._project_id)
        # 取最近片段，控制 token
        recent = events[-80:] if len(events) > 80 else events
        timeline_log = collect_events_log(recent, max_chars=8000)
        self._essentials.set_ai_refine_busy(True)
        self._refine_project_id = self._project_id  # 发起时捕获，切换项目不写错
        worker = _RefineMetaWorker(
            self.store.settings,
            project,
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
        target = self._refine_project_id or self._project_id
        self._refine_worker = None
        self._refine_project_id = None
        self._essentials.set_ai_refine_busy(False)
        if not target:
            return
        title = (title or "").strip()
        goal = (goal or "").strip()
        if not title and not goal:
            return
        patched = self.store.patch(
            target,
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
        self.store.append_event(target, ev)
        if target == self._project_id:
            self._timeline.append_event(ev)
            self._refresh_essentials()
        self.status_changed.emit()

    def _on_ai_refine_failed(self, err: str):
        target = self._refine_project_id or self._project_id
        self._refine_worker = None
        self._refine_project_id = None
        self._essentials.set_ai_refine_busy(False)
        if target:
            ev = ProjectEvent(kind="error", content=f"AI 更新名称/目标失败：{err}")
            self.store.append_event(target, ev)
            if target == self._project_id:
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
        # 运行中把策略改成「全部免审」时，自动放行当前已挂起的审批，否则会一直卡在待审批。
        # 只针对审批中断（approval），不影响 ask_user 澄清提问；仅在项目处于「待审批」状态才放行，
        # 避免在普通执行中误发「已自动批准」。
        all_skipped = (
            updated.skip_read
            and updated.skip_write
            and updated.skip_routine
            and updated.skip_high_risk
        )
        running = self._worker is not None and self._worker.isRunning()
        pending_n = getattr(self._worker, "_last_pending_count", 0) or 0
        awaiting = (
            (self.store.get(self._project_id) or project).status
            == ProjectStatus.AWAITING_APPROVAL
        )
        if all_skipped and running and pending_n > 0 and awaiting:
            self._worker.approve_all()
            note = ProjectEvent(
                kind="approval",
                content=f"策略已改为全部免审，自动批准当前 {pending_n} 项待审操作。",
            )
            self.store.append_event(self._project_id, note)
            self._timeline.append_event(note)

    def _on_send(self, text: str):
        """非运行期：对话模式回复提问（可与目标无关），可改名称/目标。"""
        from wokbee.engine.worker import AgentWorker

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
        if self._compact_worker and self._compact_worker.isRunning():
            _tip(self, self.theme, "正在压缩上下文，请稍候。")
            return
        if self._refine_worker and self._refine_worker.isRunning():
            _tip(self, self.theme, "正在更新项目信息，请稍候。")
            return

        project = self.store.get(self._project_id)
        if not project:
            return

        uev = ProjectEvent(kind="user", content=text)
        self.store.append_event(self._project_id, uev)
        self._timeline.append_event(uev)

        self._status_before_chat = project.status
        self.store.set_status(
            self._project_id,
            ProjectStatus.RUNNING,
            current_step="对话中",
        )
        self._schedule_essentials_refresh()

        self._worker_project_id = self._project_id  # 发起时捕获，运行中切项目不串写
        self._worker_mode = "chat"
        self._worker = AgentWorker(
            self.store.settings,
            project,
            self.store.path_for(project.id),
            text,
            project.approval.copy(),
            self.store.settings.max_steps,
            parent=self,
            mode="chat",
        )
        self._timeline.begin_run()
        self._worker.event_emitted.connect(self._on_engine_event)
        self._worker.approval_needed.connect(self._on_approval_needed)
        self._worker.ask_user_needed.connect(self._on_ask_user_needed)
        self._worker.finished_result.connect(self._on_engine_finished)
        self._worker.model_error.connect(self._on_worker_model_error)
        self._actions.set_running(True)
        self._actions.set_cache_stats("")
        self._actions.hide_approval()
        self._worker.start()

    def _on_run(self):
        from wokbee.engine.worker import AgentWorker

        if not self._project_id:
            _tip(self, self.theme, "请先新建或选择一个项目。")
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "当前项目已在运行中。")
            return
        if self._lesson_worker and self._lesson_worker.isRunning():
            _tip(self, self.theme, "正在总结经验，请稍候。")
            return
        if self._compact_worker and self._compact_worker.isRunning():
            _tip(self, self.theme, "正在压缩上下文，请稍候。")
            return
        if self._refine_worker and self._refine_worker.isRunning():
            _tip(self, self.theme, "正在更新项目信息，请稍候。")
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

        self._worker_project_id = self._project_id  # 发起时捕获，运行中切项目不串写
        self._worker = AgentWorker(
            self.store.settings,
            project,
            self.store.path_for(project.id),
            user_message,
            project.approval.copy(),
            self.store.settings.max_steps,
            parent=self,
            mode="run",
        )
        self._timeline.begin_run()
        self._worker.event_emitted.connect(self._on_engine_event)
        self._worker.approval_needed.connect(self._on_approval_needed)
        self._worker.ask_user_needed.connect(self._on_ask_user_needed)
        self._worker.finished_result.connect(self._on_engine_finished)
        self._worker.model_error.connect(self._on_worker_model_error)
        self._actions.set_running(True)
        self._actions.set_cache_stats("")
        self._actions.hide_approval()
        self._worker.start()

    def _active_project(self) -> tuple[str | None, bool]:
        """返回当前回调应写到的 (项目 id, 该项目是否正被查看)。

        运行中 worker / 压缩 / 改名回调都绑定其**发起时**的项目；若用户已切到别的项目，
        仍把事件/状态写回发起项目（保全数据），但不再污染当前可见时间线。
        """
        target = self._worker_project_id or self._project_id
        return target, target == self._project_id

    def _on_engine_event(self, kind: str, content: str, meta: object):
        target, visible = self._active_project()
        if not target:
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
        self.store.append_event(target, ev)
        if kind == "approval":
            # 状态写回发起项目；即便当前查看的是别的项目也要更新数据
            self.store.set_status(
                target,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="等待审批",
            )
        if not visible:
            # 运行中已切到别的项目：事件仍写回发起项目，但不污染当前时间线
            return
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
        # 名称/目标被工具改写后立刻刷新顶栏与侧栏
        if meta_d.get("project_meta") in ("title", "goal"):
            self._refresh_essentials()
            self.status_changed.emit()
        else:
            self._schedule_essentials_refresh()
        self._schedule_usage_refresh()

    def _on_approval_needed(self, pending: object):
        target, visible = self._active_project()
        items = pending if isinstance(pending, list) else []
        lines = []
        for i, act in enumerate(items, 1):
            if isinstance(act, dict):
                lines.append(
                    f"{i}. [{act.get('risk', '?')}] {act.get('name')}: {act.get('description')}"
                )
        text = "需要你审批以下工具调用：\n" + ("\n".join(lines) if lines else str(pending))
        self._actions.show_approval(text)
        if target:
            self.store.set_status(
                target,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="等待审批",
            )
            if visible:
                self._schedule_essentials_refresh()
        if visible:
            self._timeline.on_approval_pending()

    def _on_ask_user_needed(self, payload: object):
        """主线程弹窗收集澄清答案，再回传后台 Agent。"""
        target, visible = self._active_project()
        data = payload if isinstance(payload, dict) else {"type": "ask_user", "questions": []}
        if target:
            self.store.set_status(
                target,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="等待澄清意图",
            )
            if visible:
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

    def _on_worker_model_error(self, err: str):
        """worker 线程发现模型解析失败：复位 UI 并提示（不写进程事件）。"""
        target = self._worker_project_id or self._project_id
        self._worker = None
        self._worker_mode = "run"
        self._actions.set_running(False)
        self._actions.hide_approval()
        if target:
            prev = self._status_before_chat
            self._status_before_chat = None
            restore = prev if prev and prev != ProjectStatus.RUNNING else ProjectStatus.IDLE
            self.store.set_status(target, restore, current_step="待模型")
            self._schedule_essentials_refresh()
        self._worker_project_id = None
        _tip(self, self.theme, err)

    def _on_engine_finished(self, result: object):
        self._actions.set_running(False)
        self._actions.hide_approval()
        target = self._worker_project_id or self._project_id
        visible = target == self._project_id
        if visible:
            self._timeline.end_run()
        if not target:
            return
        outcome = getattr(result, "outcome", "failed")
        err = getattr(result, "error", "") or ""
        mode = self._worker_mode or "run"

        if mode == "chat":
            # 对话结束：尽量恢复进入对话前的状态，避免把「完成」冲掉
            prev = self._status_before_chat
            if outcome == "awaiting_approval":
                self.store.set_status(
                    target,
                    ProjectStatus.AWAITING_APPROVAL,
                    current_step="对话待审批",
                )
            elif outcome == "cancelled":
                restore = prev if prev and prev != ProjectStatus.RUNNING else ProjectStatus.IDLE
                self.store.set_status(
                    target,
                    restore,
                    current_step="对话已取消",
                )
            elif outcome == "failed":
                restore = prev if prev and prev != ProjectStatus.RUNNING else ProjectStatus.IDLE
                self.store.set_status(
                    target,
                    restore,
                    current_step="对话失败",
                )
                if err:
                    ev = ProjectEvent(kind="error", content=f"对话失败：{err}")
                    self.store.append_event(target, ev)
                    if visible:
                        self._timeline.append_event(ev)
            else:
                restore = prev if prev and prev != ProjectStatus.RUNNING else ProjectStatus.IDLE
                step = "空闲" if restore == ProjectStatus.IDLE else (
                    "完成" if restore == ProjectStatus.DONE else restore.value
                )
                self.store.set_status(
                    target,
                    restore,
                    current_step=step,
                )
            self._status_before_chat = None
            self._worker_mode = "run"
            self._worker = None
            self._worker_project_id = None
            self._schedule_essentials_refresh()
            self._refresh_context_usage()
            return

        # 结束类文案多由引擎事件已推送；这里只更新状态，避免重复气泡 + 全量重绘
        if outcome == "success":
            self.store.set_status(
                target,
                ProjectStatus.DONE,
                current_step="完成",
                progress_done=1,
                progress_total=1,
            )
        elif outcome == "cancelled":
            self.store.set_status(
                target,
                ProjectStatus.IDLE,
                current_step="已取消",
            )
        elif outcome == "awaiting_approval":
            self.store.set_status(
                target,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="仍待审批",
            )
        elif outcome == "incomplete":
            self.store.set_status(
                target,
                ProjectStatus.IDLE,
                current_step="未完成",
            )
        else:
            self.store.set_status(
                target,
                ProjectStatus.FAILED,
                current_step="失败",
            )
            if err:
                ev = ProjectEvent(
                    kind="error",
                    content=f"运行失败：{err}",
                )
                self.store.append_event(target, ev)
                if visible:
                    self._timeline.append_event(ev)
        project = self.store.get(target)
        if project:
            names = list_deliverable_names(
                self.store.path_for(target), limit=5
            )
            if names:
                project.artifacts_summary = ", ".join(names)
                self.store.save(project)
        self._worker = None
        self._worker_mode = "run"
        self._worker_project_id = None
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
            mode = getattr(self._worker, "mode", self._worker_mode) or "run"
            if mode == "chat":
                msg = "用户请求暂停：正在终止当前交互（含正在执行的命令）。"
            else:
                msg = "用户请求暂停/取消当前运行。"
            ev = ProjectEvent(kind="info", content=msg)
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
                + "\nAgent 运行时可直接读取调用；归档时保留上传资料，不会清空。"
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

    def _on_summarize(self):
        """人工发起：根据当前时间线写入一条经验（后台线程，避免卡住 UI）。"""
        from wokbee.engine.worker import LessonWorker

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

        notes = (
            "- 本条由用户点击「总结经验」人工发起。\n"
            "- 需要实时数据时必须联网，禁止凭记忆编造。\n"
            "- 高危 execute / 写文件是否免审取决于项目审核策略。"
        )
        if errors:
            notes = f"- 时间线错误摘录：{errors[:300]}\n" + notes

        self._worker_mode = "lesson"
        self._status_before_lesson = project.status
        self.store.set_status(
            self._project_id,
            ProjectStatus.RUNNING,
            current_step="总结经验中",
        )
        self._schedule_essentials_refresh()
        self._actions.set_running(True)
        self._worker_project_id = self._project_id  # 发起时捕获，运行中切项目不串写

        worker = LessonWorker(
            self.store.settings,
            project,
            self.store.path_for(project.id),
            user_message=project.goal or summary,
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
        target = self._worker_project_id or self._project_id
        self._lesson_worker = None
        self._worker_mode = "run"
        self._actions.set_running(False)
        if not target:
            self._status_before_lesson = None
            self._worker_project_id = None
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
        self.store.set_status(target, restore, current_step=step)
        self._worker_project_id = None
        self._schedule_essentials_refresh()

    def _on_lesson_finished(self, lesson: object):
        target = self._worker_project_id or self._project_id
        self._restore_after_lesson()
        visible = target == self._project_id
        if not target:
            return
        # 过程事件已增量追加；成功/失败只落时间线，不再弹窗
        if not lesson:
            ev = ProjectEvent(kind="error", content="经验写入失败，请查看日志。")
            self.store.append_event(target, ev)
            if visible:
                self._timeline.append_event(ev)
        if visible:
            self._refresh_essentials()
            self._timeline._schedule_scroll_to_bottom(force=True)
        self.status_changed.emit()

    def _on_lesson_failed(self, err: str):
        target = self._worker_project_id or self._project_id
        self._restore_after_lesson()
        visible = target == self._project_id
        if target:
            ev = ProjectEvent(kind="error", content=f"总结经验失败：{err}")
            self.store.append_event(target, ev)
            if visible:
                self._timeline.append_event(ev)
                self._timeline._schedule_scroll_to_bottom(force=True)

    def shutdown(self):
        """退出前收尾：取消并等待所有在途 worker。

        避免应用关闭时 QThread 仍在运行导致「QThread: Destroyed while thread is
        still running」或行为未定义。逐类取消后 wait() 兜底；给足超时以便真取消。
        """
        workers = (
            self._worker,
            self._lesson_worker,
            self._compact_worker,
            self._refine_worker,
        )
        for wk in workers:
            if wk is not None and wk.isRunning():
                try:
                    wk.cancel()
                except Exception:
                    pass
        for wk in workers:
            if wk is not None and wk.isRunning():
                try:
                    wk.wait(5000)
                except Exception:
                    pass

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
            "• 执行一次归档（对话、工作区、交付物等；uploads 上传资料保留）\n"
            "• 并把 memory/ 经验文档与 scripts/ 本地脚本一并归档后清空\n"
            "• 保留：项目名称、目标、审核策略、uploads/\n"
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
