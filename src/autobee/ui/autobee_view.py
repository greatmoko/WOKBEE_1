"""AutoBee 主视图：项目列表 + 三段式工作区。"""

from __future__ import annotations

import os
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame, QLabel, QPushButton,
    QLineEdit, QScrollArea, QTextEdit, QSizePolicy, QDialog,
    QMenu, QFileDialog,
)

from wokbee.ui.styles.theme import Theme

from autobee.core.models import (
    ApprovalFlags,
    Project,
    ProjectEvent,
    ProjectStatus,
)
from autobee.core.paths import (
    deliverables_dir,
    list_deliverable_names,
    uploads_dir,
)
from autobee.core.project_store import ProjectStore
from autobee.core.settings import AutoBeeSettings
from autobee.engine.lessons import LessonStore, build_success_path_from_timeline_events
from autobee.engine.runner import AgentRunner, RunRequest, resolve_model_for_project
from autobee.engine.worker import AgentWorker
from autobee.ui.settings_workspace import (
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

# 新建项目时，名称默认取目标前 N 个字
TITLE_FROM_GOAL_LEN = 24



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


def _ask_text(parent: QWidget, theme: Theme, title: str, label: str, default: str = "") -> str | None:
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


def _ask_new_project(parent: QWidget, theme: Theme) -> tuple[str, str] | None:
    """新建项目：先填目标（必填、多行），名称默认取目标前 N 字。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("新建项目")
    dlg.setMinimumSize(520, 420)
    dlg.resize(560, 460)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)

    goal_lab = QLabel("项目目标（必填）")
    goal_lab.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {c['text']};")
    layout.addWidget(goal_lab)
    goal_hint = QLabel("用自然语言描述要完成的事；至少约 5 行可视区域，超出可滚动。")
    goal_hint.setWordWrap(True)
    goal_hint.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
    layout.addWidget(goal_hint)

    goal_inp = QTextEdit()
    goal_inp.setPlaceholderText("例如：查询深圳今日天气，写成小红书风格文案并保存到产物目录…")
    goal_inp.setMinimumHeight(5 * 22 + 16)
    goal_inp.setStyleSheet(_textedit_qss(theme))
    layout.addWidget(goal_inp, stretch=1)

    title_lab = QLabel(f"项目名称（默认取目标前 {TITLE_FROM_GOAL_LEN} 字，可改）")
    title_lab.setStyleSheet(f"font-size: 13px; color: {c['text']};")
    layout.addWidget(title_lab)
    title_inp = QLineEdit()
    title_inp.setFixedHeight(34)
    title_inp.setPlaceholderText("未命名项目")
    title_inp.setStyleSheet(f"""
        QLineEdit {{
            background: {c["input_bg"]}; color: {c["text"]};
            border: 1px solid {c["input_border"]}; border-radius: 6px; padding: 0 10px;
        }}
    """)
    layout.addWidget(title_inp)

    title_dirty = {"v": False}

    def _sync_title_from_goal():
        if title_dirty["v"]:
            return
        title_inp.blockSignals(True)
        title_inp.setText(_title_from_goal(goal_inp.toPlainText()))
        title_inp.blockSignals(False)

    def _on_title_edited(_text: str):
        title_dirty["v"] = True

    goal_inp.textChanged.connect(_sync_title_from_goal)
    title_inp.textEdited.connect(_on_title_edited)

    err = QLabel("")
    err.setStyleSheet(f"font-size: 12px; color: {c['danger']};")
    layout.addWidget(err)

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

    def _accept():
        goal = goal_inp.toPlainText().strip()
        if not goal:
            err.setText("请先填写项目目标。")
            return
        dlg.accept()

    ok.clicked.connect(_accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)

    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    goal = goal_inp.toPlainText().strip()
    title = title_inp.text().strip() or _title_from_goal(goal)
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
        menu.addSeparator()
        del_a = menu.addAction("删除（移入回收）")
        action = menu.exec(pos)
        if action == rename_a:
            project = self.store.get(project_id)
            if not project:
                return
            new_title = _ask_text(self, self.theme, "重命名", "新名称", project.title)
            if new_title:
                self.store.rename(project_id, new_title)
                self.refresh()
                self.project_changed.emit()
                if self._selected_id == project_id:
                    self.project_selected.emit(project_id)
        elif action == open_a:
            path = self.store.path_for(project_id)
            _open_in_explorer(path)
        elif action == del_a:
            if _confirm(
                self,
                self.theme,
                "删除项目",
                "确定将该项目移入工作区 _trash？",
            ):
                self.store.delete(project_id, trash=True)
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
        row1.addWidget(self._title, stretch=1)

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

    def bind(self, project: Project, *, project_root: Path | None = None):
        c = self.theme.colors
        self._title.setText(project.title)
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

        summary = project.approval.summary()
        self._policy.setText(f"策略：{summary}")
        if project.approval.skip_high_risk:
            self._policy.setStyleSheet(
                f"font-size: 12px; font-weight: bold; color: {c['danger']};"
            )
        else:
            self._policy.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        self._policy_btn.setEnabled(True)


# 气泡正文预览：最多 10 行或 400 字，超出可展开
BUBBLE_PREVIEW_CHARS = 400
BUBBLE_PREVIEW_LINES = 10


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


def _is_tool_call_event(ev: ProjectEvent) -> bool:
    """工具「调用中」事件：不单独刷 UI，等返回完整后再展示。"""
    if ev.kind != "tool":
        return False
    meta = ev.meta or {}
    if meta.get("phase") == "call":
        return True
    content = (ev.content or "").lstrip()
    return content.startswith("⟶") or "调用工具" in content[:20]


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


class _ExpandableBody(QWidget):
    """正文：默认最多展示 10 行 / 400 字，可展开全部。"""

    def __init__(self, text: str, theme: Theme, *, danger: bool = False, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._full = text or ""
        self._danger = danger
        self._expanded = False
        c = theme.colors
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._label = QLabel()
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        color = c.get("danger", "#c0392b") if danger else c["text"]
        self._label.setStyleSheet(
            f"font-size: 13px; color: {color}; background: transparent; border: none;"
        )
        lay.addWidget(self._label)
        self._toggle = QPushButton()
        self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._toggle.setFlat(True)
        self._toggle.setStyleSheet(
            f"QPushButton {{ color: {c['accent']}; font-size: 12px; border: none; "
            f"text-align: left; padding: 0; background: transparent; }}"
            f"QPushButton:hover {{ color: {c.get('accent_light', c['accent'])}; }}"
        )
        self._toggle.clicked.connect(self._on_toggle)
        lay.addWidget(self._toggle)
        self._apply()

    def _apply(self):
        full = self._full
        preview, need = _preview_text(full)
        if not need or self._expanded:
            self._label.setText(full)
            self._toggle.setVisible(need)
            self._toggle.setText("收起" if need else "")
        else:
            self._label.setText(preview)
            self._toggle.setVisible(True)
            n_lines = len(full.splitlines())
            self._toggle.setText(f"展开全部（{n_lines} 行 / {len(full)} 字）")

    def _on_toggle(self):
        self._expanded = not self._expanded
        self._apply()


class _Timeline(QFrame):
    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._has_stretch = False
        self._bubbles: list[QFrame] = []
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setStyleSheet(f"_Timeline {{ background: {c['content_bg']}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

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
        self._has_stretch = False
        self._bubbles = []

    def _ensure_stretch(self):
        if getattr(self, "_has_stretch", False):
            return
        self._layout.addStretch(1)
        self._has_stretch = True

    def render_events(self, events: list[ProjectEvent]):
        """完整重绘（仅切换项目 / 归档后使用，运行中勿频繁调用）。"""
        self._clear()
        visible = [e for e in events if not _is_tool_call_event(e)]
        if not visible:
            self.show_empty("尚无执行记录。在下方输入目标或指令，然后点击运行。")
            return
        for ev in visible:
            self._layout.addWidget(self._make_row(ev))
        self._ensure_stretch()
        self._sync_bubble_widths()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def append_event(self, ev: ProjectEvent):
        """增量追加一条气泡（运行中用，避免整表重绘闪烁）。"""
        if _is_tool_call_event(ev):
            return
        # 若当前是空态文案，先清掉
        if self._layout.count() == 1:
            w = self._layout.itemAt(0).widget()
            if isinstance(w, QLabel) and "尚无执行记录" in (w.text() or ""):
                self._clear()
        row = self._make_row(ev)
        # 始终插在末尾 stretch 之前（勿用缓存下标，否则会倒序）
        if getattr(self, "_has_stretch", False) and self._layout.count() > 0:
            self._layout.insertWidget(self._layout.count() - 1, row)
        else:
            self._layout.addWidget(row)
            self._ensure_stretch()
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self):
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

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
        for bub in getattr(self, "_bubbles", []):
            try:
                bub.setFixedWidth(w)
            except RuntimeError:
                pass

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
            "tool": ("#e5e7eb", "#374151"),
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

    def _role_tag(self, role: str, kind: str) -> str:
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
        av_bg, av_fg = self._avatar_spec(role)
        emoji = self._avatar_emoji(role, ev.kind)
        bub_bg, bub_border = self._bubble_colors(role)
        if ev.kind == "approval":
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
        bubble.setStyleSheet(f"""
            QFrame {{
                background: {bub_bg};
                border: 1px solid {bub_border};
                border-radius: 10px;
            }}
        """)
        if not hasattr(self, "_bubbles"):
            self._bubbles = []
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
        tag = self._role_tag(role, ev.kind)
        head = QLabel(f"{tag} · {ev.created_at}")
        head_color = c.get("danger", "#e11d48") if role == "error" else c["text_hint"]
        head.setStyleSheet(
            f"font-size: 11px; color: {head_color}; background: transparent; border: none;"
        )
        bl.addWidget(head)
        bl.addWidget(
            _ExpandableBody(
                ev.content or "",
                self.theme,
                danger=(role == "error"),
            )
        )

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
    archive_clicked = Signal()
    upload_clicked = Signal()
    open_deliverables_clicked = Signal()
    send_clicked = Signal(str)
    approve_clicked = Signal()
    reject_clicked = Signal()

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
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
        self._input.setPlaceholderText("输入补充指令或追问…（项目目标请在上方编辑）")
        self._input.setFixedHeight(72)
        self._input.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 8px;
                padding: 8px; font-size: 13px;
            }}
            QTextEdit:focus {{ border: 1px solid {c["input_focus_border"]}; }}
        """)
        layout.addWidget(self._input)

        row = QHBoxLayout()
        row.setSpacing(8)

        folder_btn = QPushButton("打开目录")
        folder_btn.setFixedHeight(34)
        folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        folder_btn.setStyleSheet(self._sec_qss())
        folder_btn.clicked.connect(self.open_folder_clicked.emit)
        row.addWidget(folder_btn)

        deliv_btn = QPushButton("交付物")
        deliv_btn.setToolTip("打开 deliverables/ 交付物目录")
        deliv_btn.setFixedHeight(34)
        deliv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        deliv_btn.setStyleSheet(self._sec_qss())
        deliv_btn.clicked.connect(self.open_deliverables_clicked.emit)
        row.addWidget(deliv_btn)

        upload_btn = QPushButton("上传文件")
        upload_btn.setToolTip("上传到 uploads/，Agent 可读取；归档时一并归档")
        upload_btn.setFixedHeight(34)
        upload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        upload_btn.setStyleSheet(self._sec_qss())
        upload_btn.clicked.connect(self.upload_clicked.emit)
        row.addWidget(upload_btn)

        summarize_btn = QPushButton("总结经验")
        summarize_btn.setToolTip("根据当前对话时间线手动写入一条经验（已有经验后需人工发起）")
        summarize_btn.setFixedHeight(34)
        summarize_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        summarize_btn.setStyleSheet(self._sec_qss())
        summarize_btn.clicked.connect(self.summarize_clicked.emit)
        row.addWidget(summarize_btn)

        archive_btn = QPushButton("归档")
        archive_btn.setToolTip("归档运行记录与工作区，清空对话页；保留目标与经验")
        archive_btn.setFixedHeight(34)
        archive_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        archive_btn.setStyleSheet(self._sec_qss())
        archive_btn.clicked.connect(self.archive_clicked.emit)
        row.addWidget(archive_btn)

        row.addStretch()

        send_btn = QPushButton("发送")
        send_btn.setFixedSize(72, 34)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet(self._sec_qss())
        send_btn.clicked.connect(self._on_send)
        row.addWidget(send_btn)

        pause_btn = QPushButton("暂停")
        pause_btn.setFixedSize(72, 34)
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

        self._run_btn = QPushButton("运行")
        self._run_btn.setFixedSize(88, 34)
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
        layout.addLayout(row)

    def _sec_qss(self) -> str:
        c = self.theme.colors
        return f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; padding: 0 12px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """

    def _on_send(self):
        text = self._input.toPlainText().strip()
        if text:
            self.send_clicked.emit(text)
            self._input.clear()

    def take_input(self) -> str:
        text = self._input.toPlainText().strip()
        self._input.clear()
        return text

    def set_running(self, running: bool):
        self._run_btn.setEnabled(not running)
        self._run_btn.setText("运行中…" if running else "运行")

    def show_approval(self, text: str):
        self._approval_label.setText(text)
        self._approval_bar.setVisible(True)

    def hide_approval(self):
        self._approval_bar.setVisible(False)


# ─── 工作区整体 ───

class _ProjectWorkspace(QWidget):
    def __init__(self, theme: Theme, store: ProjectStore, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store
        self._project_id: str | None = None
        self._worker: AgentWorker | None = None
        self._runner: AgentRunner | None = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._essentials = _ProjectEssentials(self.theme)
        self._essentials.goal_edit_requested.connect(self._edit_goal)
        self._essentials.approval_edit_requested.connect(self._edit_approval)
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
        self._actions.archive_clicked.connect(self._on_archive)
        self._actions.send_clicked.connect(self._on_send)
        self._actions.approve_clicked.connect(self._on_approve)
        self._actions.reject_clicked.connect(self._on_reject)
        layout.addWidget(self._actions)

        self._essentials_timer = QTimer(self)
        self._essentials_timer.setSingleShot(True)
        self._essentials_timer.setInterval(200)
        self._essentials_timer.timeout.connect(self._refresh_essentials)
        self.show_welcome()

    def show_welcome(self):
        self._project_id = None
        self._essentials.clear()
        self._timeline.show_empty()
        self._actions.hide_approval()
        self._actions.set_running(False)

    def load_project(self, project_id: str):
        if not project_id:
            self.show_welcome()
            return
        project = self.store.get(project_id)
        if not project:
            self.show_welcome()
            return
        self._project_id = project_id
        root = self.store.path_for(project_id)
        self._essentials.bind(project, project_root=root)
        events = self.store.list_events(project_id)
        self._timeline.render_events(events)

    def _refresh(self):
        """完整刷新（切换项目、归档、手动操作后）。运行中请勿频繁调用。"""
        if self._project_id:
            self.load_project(self._project_id)

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
        if not self._project_id:
            _tip(self, self.theme, "请先新建或选择一个项目。")
            return
        project = self.store.get(self._project_id)
        if project and not project.goal.strip():
            self.store.update_goal(self._project_id, text)
        self.store.append_event(
            self._project_id,
            ProjectEvent(kind="user", content=text),
        )
        self._timeline.append_event(
            ProjectEvent(kind="user", content=text)
        )
        self._schedule_essentials_refresh()

    def _on_run(self):
        if not self._project_id:
            _tip(self, self.theme, "请先新建或选择一个项目。")
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "当前项目已在运行中。")
            return

        text = self._actions.take_input()
        project = self.store.get(self._project_id)
        if not project:
            return
        if text:
            if not project.goal.strip():
                self.store.update_goal(self._project_id, text)
                project = self.store.get(self._project_id) or project
            uev = ProjectEvent(kind="user", content=text)
            self.store.append_event(self._project_id, uev)
            self._timeline.append_event(uev)
        goal = (project.goal or "").strip()
        user_message = text or goal
        if not user_message:
            _tip(self, self.theme, "请先设置项目目标或在输入框填写指令。")
            return

        try:
            resolved = resolve_model_for_project(project, self.store.settings)
        except ValueError as e:
            _tip(self, self.theme, str(e))
            return

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
        self._worker = AgentWorker(self._runner, req, parent=self)
        self._worker.event_emitted.connect(self._on_engine_event)
        self._worker.approval_needed.connect(self._on_approval_needed)
        self._worker.finished_result.connect(self._on_engine_finished)
        self._actions.set_running(True)
        self._actions.hide_approval()
        self._worker.start()

    def _on_engine_event(self, kind: str, content: str, meta: object):
        if not self._project_id:
            return
        ev = ProjectEvent(
            kind=kind,
            content=content,
            meta=meta if isinstance(meta, dict) else {},
        )
        self.store.append_event(self._project_id, ev)
        # 增量追加；工具「调用中」不刷界面，等完整返回再显示，避免闪烁卡顿
        self._timeline.append_event(ev)
        if kind == "approval":
            self.store.set_status(
                self._project_id,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="等待审批",
            )
        self._schedule_essentials_refresh()

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
        if self._project_id:
            self.store.set_status(
                self._project_id,
                ProjectStatus.AWAITING_APPROVAL,
                current_step="等待审批",
            )
            self._schedule_essentials_refresh()

    def _on_approve(self):
        if self._worker and self._worker.isRunning():
            self._actions.hide_approval()
            self._worker.approve_all()

    def _on_reject(self):
        if self._worker and self._worker.isRunning():
            self._actions.hide_approval()
            self._worker.reject_all("用户拒绝该操作")

    def _on_engine_finished(self, result: object):
        self._actions.set_running(False)
        self._actions.hide_approval()
        if not self._project_id:
            return
        outcome = getattr(result, "outcome", "failed")
        err = getattr(result, "error", "") or ""
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
        self._schedule_essentials_refresh()

    def _on_pause(self):
        if not self._project_id:
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

    def _on_summarize(self):
        """人工发起：根据当前时间线写入一条经验。"""
        if not self._project_id:
            _tip(self, self.theme, "请先选择项目。")
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "请先等待当前运行结束，再总结经验。")
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

        ok = _confirm(
            self,
            self.theme,
            "总结经验",
            "将根据当前对话时间线覆盖更新 memory/EXPERIENCE.md（项目唯一经验），"
            "并把可确定性步骤固化为 scripts/ 本地脚本。\n"
            "（仅当经验为空时，运行结束会自动总结；之后都需像这样手动覆盖更新。）\n"
            "scripts/ 不参与归档。是否继续？",
        )
        if not ok:
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
            # 无模型也可写经验，用占位
            resolved = None

        notes = (
            "- 本条由用户点击「总结经验」人工发起。\n"
            "- 需要实时数据时必须联网，禁止凭记忆编造。\n"
            "- 高危 execute / 写文件是否免审取决于项目审核策略。"
        )
        if errors:
            notes = f"- 时间线错误摘录：{errors[:300]}\n" + notes

        # 构造最小 RunRequest 供 write_lesson_manual 使用
        if resolved is None:
            from types import SimpleNamespace

            resolved = SimpleNamespace(
                provider_name="unknown",
                model_id="unknown",
            )

        req = RunRequest(
            project=project,
            project_root=self.store.path_for(project.id),
            user_message=project.goal or summary,
            resolved=resolved,  # type: ignore[arg-type]
            approval=project.approval.copy(),
            max_steps=self.store.settings.max_steps,
        )
        runner = AgentRunner(self.store.settings)
        written: list[tuple[str, str, dict]] = []

        def _capture(kind: str, content: str, meta: dict):
            written.append((kind, content, meta))
            self.store.append_event(
                self._project_id,
                ProjectEvent(kind=kind, content=content, meta=meta or {}),
            )

        runner.on_event = _capture
        lesson = runner.write_lesson_manual(
            req,
            outcome,
            summary,
            errors,
            success_path=path_text,
            notes=notes,
            events=events,
        )
        self._refresh()
        if lesson:
            n = len(lesson.scripts or [])
            extra = f"\n并固化/更新 {n} 个本地脚本到 scripts/" if n else ""
            _tip(
                self,
                self.theme,
                f"已覆盖更新：memory/EXPERIENCE.md{extra}",
                title="总结完成",
            )
        else:
            _tip(self, self.theme, "经验写入失败，请查看日志。")

    def _on_archive(self):
        if not self._project_id:
            _tip(self, self.theme, "请先选择项目。")
            return
        if self._worker and self._worker.isRunning():
            _tip(self, self.theme, "请先暂停/等待当前运行结束，再归档。")
            return
        ok = _confirm(
            self,
            self.theme,
            "归档当前会话",
            "将把运行记录、workspace、deliverables（交付物）、uploads（上传）、"
            "artifacts 存入 archives/，并清空对话与上述目录。\n"
            "保留：项目目标、名称、审核策略、memory 经验、scripts。\n"
            "archives / scripts 自身不会被归档。是否继续？",
        )
        if not ok:
            return
        dest = self.store.archive_session(self._project_id)
        if not dest:
            _tip(self, self.theme, "归档失败，请检查项目目录权限。")
            return
        self._refresh()
        _tip(
            self,
            self.theme,
            f"已归档到：\n{dest}\n对话页已清空，可基于目标与经验再次运行。",
            title="归档完成",
        )


# ─── AutoBee 页 ───

class AutoBeeView(QWidget):
    """autobee 主页面：中栏项目列表 + 右栏三段工作区。"""

    def __init__(
        self,
        theme: Theme,
        store: ProjectStore | None = None,
        settings: AutoBeeSettings | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.settings = settings or AutoBeeSettings()
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

    def showEvent(self, event):
        super().showEvent(event)
        self._sidebar.refresh()
        if self._sidebar._selected_id:
            self._workspace.load_project(self._sidebar._selected_id)
