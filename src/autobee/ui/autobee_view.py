"""AutoBee 主视图：中任务列表 | 右任务详情。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QThread, QSize
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QLineEdit,
    QTextEdit, QComboBox, QScrollArea, QPushButton, QStackedWidget,
    QSizePolicy, QListWidget, QListWidgetItem, QDialog, QMessageBox,
)

from apscheduler.triggers.cron import CronTrigger

from tokbee.ui.styles.theme import Theme
from tokbee.ui.combo_style import apply_combo_popup_style, secondary_btn_qss
from tokbee.core.provider_store import ProviderStore

from wokbee.core.project_store import ProjectStore

from autobee.core.models import JobLog, ScheduledTask, TaskRunStatus, TaskType, new_task_id
from autobee.core.store import AutoBeeStore, MAX_LOGS_PER_TASK
from autobee.engine.nl_builder import NLBuilder
from autobee.engine.scheduler import SchedulerService, describe_cron

_STATUS_COLOR = {
    TaskRunStatus.SUCCESS: "success",
    TaskRunStatus.FAILED: "danger",
    TaskRunStatus.MISSED: "warning",
    TaskRunStatus.RUNNING: "accent",
}


def _time_part(ts: str) -> str:
    """从 'YYYY-MM-DD HH:MM:SS' 取时间或原样返回。"""
    s = (ts or "").strip()
    if not s:
        return "—"
    parts = s.split()
    return parts[1] if len(parts) > 1 else s


def _date_time(ts: str) -> str:
    return (ts or "").strip() or "—"


class _LogDetailDialog(QDialog):
    """运行详情弹窗。"""

    def __init__(self, theme: Theme, log: JobLog, task_name: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("运行详情")
        self.setMinimumSize(520, 420)
        self.resize(560, 480)
        c = theme.colors
        self.setStyleSheet(f"background: {c['content_bg']};")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)

        title = QLabel(task_name or "定时任务")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        lay.addWidget(title)

        color_key = _STATUS_COLOR.get(log.status, "text_secondary")
        status_color = c.get(color_key, c["text_secondary"])
        lines = [
            f"状态：{log.status.label}",
            f"开始：{_date_time(log.started_at)}",
            f"结束：{_date_time(log.finished_at)}",
        ]
        if log.duration_s:
            lines.append(f"耗时：{log.duration_s:.1f}s")
        meta = QLabel("    ".join(lines))
        meta.setWordWrap(True)
        meta.setStyleSheet(
            f"font-size: 12px; color: {status_color};"
            "background: transparent; border: none;"
        )
        lay.addWidget(meta)

        body = QTextEdit()
        body.setReadOnly(True)
        parts = []
        if log.summary:
            parts.append(log.summary)
        if log.error:
            parts.append(f"错误：\n{log.error}")
        if log.meta:
            try:
                import json
                parts.append("元数据：\n" + json.dumps(log.meta, ensure_ascii=False, indent=2))
            except Exception:
                parts.append(f"元数据：{log.meta}")
        body.setPlainText("\n\n".join(parts) if parts else "（无详细内容）")
        body.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 8px;
                padding: 10px; font-size: 13px;
            }}
        """)
        lay.addWidget(body, stretch=1)

        row = QHBoxLayout()
        row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setFixedSize(80, 34)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(secondary_btn_qss(c))
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        lay.addLayout(row)


def _status_label(raw: str) -> str:
    if not raw:
        return "未运行"
    try:
        return TaskRunStatus(raw).label
    except ValueError:
        return raw


class _LogRow(QFrame):
    """列表行：运行时间 / 任务名 / 完成情况。"""

    def __init__(self, theme: Theme, log: JobLog, task_name: str, parent=None):
        super().__init__(parent)
        c = theme.colors
        self.setObjectName("logRow")
        self.setStyleSheet(f"""
            QFrame#logRow {{
                background: transparent; border: none;
            }}
            QFrame#logRow QLabel {{
                background: transparent; border: none;
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(10)

        time_lbl = QLabel(_time_part(log.finished_at or log.started_at))
        time_lbl.setFixedWidth(64)
        time_lbl.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        lay.addWidget(time_lbl)

        name_lbl = QLabel(task_name or "—")
        name_lbl.setStyleSheet(f"font-size: 13px; color: {c['text']}; font-weight: 600;")
        name_lbl.setMinimumWidth(80)
        lay.addWidget(name_lbl, stretch=2)

        color_key = _STATUS_COLOR.get(log.status, "text_secondary")
        status_color = c.get(color_key, c["text_secondary"])
        status_lbl = QLabel(log.status.label)
        status_lbl.setFixedWidth(48)
        status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_lbl.setStyleSheet(
            f"font-size: 12px; color: {status_color}; font-weight: bold;"
        )
        lay.addWidget(status_lbl)

        dur = f"{log.duration_s:.1f}s" if log.duration_s else "—"
        dur_lbl = QLabel(dur)
        dur_lbl.setFixedWidth(52)
        dur_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        dur_lbl.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        lay.addWidget(dur_lbl)

        brief = (log.error or log.summary or "").replace("\n", " ").strip()
        if len(brief) > 36:
            brief = brief[:36] + "…"
        brief_lbl = QLabel(brief or "—")
        brief_lbl.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        lay.addWidget(brief_lbl, stretch=3)

    def sizeHint(self) -> QSize:
        return QSize(200, 44)


class _NLWorker(QThread):
    """自然语言生成定时配置：后台跑 AI，避免冻结 UI。"""

    done = Signal(object)  # dict | Exception

    def __init__(self, builder: NLBuilder, text: str, model, parent=None):
        super().__init__(parent)
        self._builder = builder
        self._text = text
        self._model = model

    def run(self):
        try:
            data = self._builder.generate(self._text, self._model)
            self.done.emit(data)
        except Exception as e:
            self.done.emit(e)


class _TaskItem(QFrame):
    clicked = Signal(str)
    toggle_enabled = Signal(str)

    def __init__(self, task: ScheduledTask, theme: Theme, selected: bool = False, parent=None):
        super().__init__(parent)
        self.task = task
        self.theme = theme
        self._selected = selected
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(66)
        self._build()

    def _build(self):
        c = self.theme.colors
        t = self.task
        bg = c["card_bg"] if self._selected else "transparent"
        border = (
            f"border-left: 3px solid {c['accent']};"
            if self._selected
            else "border-left: 3px solid transparent;"
        )
        self.setStyleSheet(f"""
            _TaskItem {{ background: {bg}; border-radius: 6px; {border} }}
            _TaskItem:hover {{ background: {c["subnav_hover"]}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        top = QHBoxLayout()
        status = TaskRunStatus(t.last_status) if t.last_status else None
        color = c.get(_STATUS_COLOR.get(status, "text_hint"), c["text_hint"])
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {color}; font-size: 10px; background: transparent; border: none;")
        top.addWidget(dot)
        name = QLabel(t.name)
        name.setWordWrap(False)
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        name.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        top.addWidget(name, 1)

        # 列表内启用/停用开关
        toggle = QPushButton("停用" if t.enabled else "启用")
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setFixedSize(44, 22)
        toggle.setToolTip("停用定时" if t.enabled else "启用定时")
        if t.enabled:
            toggle.setStyleSheet(f"""
                QPushButton {{
                    background: {c["btn_bg"]}; color: {c["text_secondary"]};
                    border: none; border-radius: 4px; font-size: 11px;
                }}
                QPushButton:hover {{ background: {c["btn_hover"]}; color: {c["danger"]}; }}
            """)
        else:
            toggle.setStyleSheet(f"""
                QPushButton {{
                    background: {c.get("accent_light", c["btn_bg"])}; color: {c["accent"]};
                    border: none; border-radius: 4px; font-size: 11px;
                }}
                QPushButton:hover {{ background: {c["btn_hover"]}; }}
            """)
        toggle.clicked.connect(lambda: self.toggle_enabled.emit(self.task.id))
        top.addWidget(toggle)
        layout.addLayout(top)

        mid = QHBoxLayout()
        chip = QLabel(t.task_type.label)
        chip.setStyleSheet(
            f"background: {c['tag_bg']}; color: {c['text_secondary']};"
            "border-radius: 4px; padding: 1px 6px; font-size: 10px;"
        )
        mid.addWidget(chip)
        sub = QLabel(describe_cron(t.schedule) or t.schedule)
        sub.setStyleSheet(
            f"font-size: 11px; color: {c['text_hint']}; background: transparent; border: none;"
        )
        mid.addWidget(sub)
        mid.addStretch()
        layout.addLayout(mid)

        enabled = "已启用" if t.enabled else "已停用"
        state = c["success"] if t.enabled else c["text_hint"]
        if t.last_status == TaskRunStatus.RUNNING.value:
            state = c["accent"]
        last_label = _status_label(t.last_status)
        info = QLabel(f"{enabled} · {last_label}")
        info.setStyleSheet(
            f"font-size: 11px; color: {state}; background: transparent; border: none;"
        )
        layout.addWidget(info)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.task.id)
        super().mousePressEvent(event)


class _TaskList(QFrame):
    task_selected = Signal(str)
    new_task = Signal()
    toggle_enabled = Signal(str)

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._filter_keys: list[str] = []
        self._selected_id: str | None = None
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setMinimumWidth(250)
        self.setMaximumWidth(340)
        self.setStyleSheet(f"""
            _TaskList {{ background: {c["content_bg"]}; border-right: 1px solid {c["border"]}; }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        new_btn = QPushButton("＋ 新建任务")
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setFixedHeight(34)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        new_btn.clicked.connect(self.new_task.emit)
        layout.addWidget(new_btn)

        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索任务...")
        self._search.setFixedHeight(30)
        self._search.textChanged.connect(lambda: self.refresh())
        self._search.setStyleSheet(f"""
            QLineEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 6px; padding: 0 8px;
            }}
        """)
        layout.addWidget(self._search)

        self._count = QLabel("")
        self._count.setStyleSheet(
            f"font-size: 11px; color: {c['text_hint']}; background: transparent; border: none;"
        )
        layout.addWidget(self._count)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        self._container = QWidget()
        self._container.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(2)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._container)
        layout.addWidget(scroll, stretch=1)

    def set_filter(self, keys: list[str]):
        self._filter_keys = list(keys or [])
        self.refresh()

    def set_tasks(self, tasks: list[ScheduledTask]):
        self._tasks = list(tasks)
        self.refresh()

    def refresh(self):
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        c = self.theme.colors
        kw = (self._search.text() or "").strip().lower()
        tasks = getattr(self, "_tasks", [])
        filtered = []
        for t in tasks:
            if kw and kw not in t.name.lower() and kw not in t.schedule.lower():
                continue
            if self._filter_keys:
                if "__enabled" in self._filter_keys and not t.enabled:
                    continue
                if "__disabled" in self._filter_keys and t.enabled:
                    continue
                if t.task_type.value not in self._filter_keys:
                    continue
            filtered.append(t)

        self._count.setText(f"{len(filtered)} 个任务")
        if not filtered:
            empty = QLabel("暂无任务")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 20px 0;")
            self._list_layout.addWidget(empty)
            self._selected_id = None
            return

        found_selected = False
        for t in filtered:
            if t.id == self._selected_id:
                found_selected = True
            item = _TaskItem(t, self.theme, selected=(t.id == self._selected_id))
            item.clicked.connect(self._on_select)
            item.toggle_enabled.connect(self.toggle_enabled.emit)
            self._list_layout.addWidget(item)

        # 若当前选中的任务不在过滤队列内，清空选中（详情保持上次编辑不丢）
        if self._selected_id and not found_selected:
            self._selected_id = None

    def _on_select(self, task_id: str):
        self._selected_id = task_id
        self.refresh()
        self.task_selected.emit(task_id)

    def select(self, task_id: str):
        self._selected_id = task_id
        self.refresh()
        self.task_selected.emit(task_id)

    def clear_selection(self):
        self._selected_id = None
        self.refresh()

    def current_selected(self) -> str | None:
        return self._selected_id


class _TaskDetail(QWidget):
    """右栏：任务编辑器 + 运行历史。"""

    task_saved = Signal(str)
    task_deleted = Signal(str)

    def __init__(self, theme: Theme, store: AutoBeeStore, scheduler: SchedulerService,
                 provider_store: ProviderStore, project_store: ProjectStore, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store
        self.scheduler = scheduler
        self.provider_store = provider_store
        self.project_store = project_store
        self._task_id: str | None = None
        self._current_type = TaskType.WOKBEE
        self._log_cache: dict[str, JobLog] = {}
        self._build()
        self.scheduler.notifier.task_started.connect(self._on_task_run_started)
        self.scheduler.notifier.task_progress.connect(self._on_task_run_progress)
        self.scheduler.notifier.task_finished.connect(self._on_task_run_finished)

    # ── 构建 ───────────────────────────────────────────────
    def _build(self):
        # 空态占位页 + 编辑表单页，未选中任务时展示空态
        self._empty = self._build_empty_page()
        self._form = self._build_form()
        self._stack = QStackedWidget()
        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._form)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._stack)

    def _build_empty_page(self) -> QWidget:
        c = self.theme.colors
        page = QWidget()
        page.setStyleSheet(f"background: {c['content_bg']};")
        lay = QVBoxLayout(page)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel("⏰")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(f"font-size: 40px; color: {c['text_hint']}; background: transparent;")
        lay.addWidget(icon)
        t = QLabel("选择一个任务，或点击「新建任务」开始")
        t.setAlignment(Qt.AlignmentFlag.AlignCenter)
        t.setStyleSheet(f"font-size: 14px; color: {c['text_hint']}; background: transparent; margin-top: 8px;")
        lay.addWidget(t)
        return page

    def _build_form(self) -> QWidget:
        form = QWidget()
        c = self.theme.colors
        form.setStyleSheet(f"background: {c['content_bg']};")
        form_lay = QVBoxLayout(form)
        form_lay.setContentsMargins(0, 0, 0, 0)
        form_lay.setSpacing(0)

        # 上半：可滚动表单区（避免矮窗口下推送地址被挡住）
        body = QWidget()
        body.setStyleSheet(f"background: {c['content_bg']};")
        outer = QVBoxLayout(body)
        outer.setContentsMargins(20, 16, 20, 8)
        outer.setSpacing(10)

        # 统一输入样式
        self._line_qss = f"""
            QLineEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 6px;
                padding: 0 10px; font-size: 13px;
            }}
            QLineEdit:focus {{ border: 1px solid {c["input_focus_border"]}; }}
        """
        self._text_qss = f"""
            QTextEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 8px;
                padding: 8px; font-size: 13px;
            }}
            QTextEdit:focus {{ border: 1px solid {c["input_focus_border"]}; }}
        """
        self._btn_qss = f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
            QPushButton:disabled {{ color: {c["text_hint"]}; }}
        """

        # ① 自然语言描述（单独一行）
        self._nl_input = QTextEdit()
        self._nl_input.setPlaceholderText(
            "用自然语言描述定时任务，如：每天上午9点给企业微信群推送问候"
        )
        self._nl_input.setFixedHeight(52)
        self._nl_input.setStyleSheet(self._text_qss)
        self._nl_input.textChanged.connect(self._update_action_btns)
        outer.addWidget(self._nl_input)

        # ② 操作栏：生成模型 + AI 生成 + 保存 + 删除 + 立即运行
        ai_row = QHBoxLayout()
        ai_row.setSpacing(8)
        self._gen_combo = QComboBox()
        self._gen_combo.setFixedHeight(34)
        self._gen_combo.setMinimumWidth(160)
        self._gen_combo.setToolTip("AI 生成所用模型")
        apply_combo_popup_style(self._gen_combo, c, rounded=True)
        ai_row.addWidget(self._gen_combo, 1)

        grey_btn = f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
            QPushButton:disabled {{ color: {c["text_hint"]}; }}
        """
        self._gen_btn = QPushButton("AI 生成")
        self._gen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._gen_btn.setFixedSize(80, 34)
        self._gen_btn.setStyleSheet(grey_btn)
        self._gen_btn.clicked.connect(self._on_nl_generate)
        ai_row.addWidget(self._gen_btn)

        self._save_btn = QPushButton("保存")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setFixedSize(64, 34)
        self._save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
            QPushButton:disabled {{ background: {c["btn_bg"]}; color: {c["text_hint"]}; }}
        """)
        self._save_btn.clicked.connect(self._on_save)
        ai_row.addWidget(self._save_btn)

        self._del_btn = QPushButton("删除")
        self._del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._del_btn.setFixedSize(64, 34)
        self._del_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["danger"]};
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
            QPushButton:disabled {{ color: {c["text_hint"]}; }}
        """)
        self._del_btn.clicked.connect(self._on_delete)
        ai_row.addWidget(self._del_btn)

        self._run_btn = QPushButton("立即运行")
        self._run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._run_btn.setFixedSize(80, 34)
        self._run_btn.setStyleSheet(grey_btn)
        self._run_btn.clicked.connect(self._on_run_now)
        ai_row.addWidget(self._run_btn)
        outer.addLayout(ai_row)
        self._update_action_btns()

        # ③ 任务名称 | 定时 cron
        row2 = QHBoxLayout()
        row2.setSpacing(12)
        self._name = QLineEdit()
        self._name.setPlaceholderText("定时任务名称")
        self._name.setFixedHeight(34)
        self._name.setStyleSheet(self._line_qss)
        row2.addWidget(self._fld("任务名称", self._name), 1)

        self._schedule = QLineEdit()
        self._schedule.setPlaceholderText("cron，如 0 9 * * *")
        self._schedule.setFixedHeight(34)
        self._schedule.setStyleSheet(self._line_qss)
        self._schedule.textChanged.connect(self._update_cron_preview)
        cron_fld, self._cron_title = self._fld_with_label("定时 (cron)", self._schedule)
        self._cron_text_value = ""
        row2.addWidget(cron_fld, 1)
        outer.addLayout(row2)

        # ④ 执行模型 + 任务类型
        row3 = QHBoxLayout()
        row3.setSpacing(12)
        self._exec_combo = QComboBox()
        self._exec_combo.setFixedHeight(34)
        apply_combo_popup_style(self._exec_combo, c, rounded=True)
        row3.addWidget(self._fld("执行模型", self._exec_combo), 1)
        self._type_combo = self._build_type_combo()
        row3.addWidget(self._fld("任务类型", self._type_combo), 1)
        outer.addLayout(row3)
        self._refill_model_combos()

        # 类型专属配置
        self._config_stack = QStackedWidget()
        self._config_stack.addWidget(self._build_text_page())
        self._config_stack.addWidget(self._build_script_page())
        self._config_stack.addWidget(self._build_wokbee_page())
        outer.addWidget(self._config_stack)

        # ⑤ 微信推送地址（有值即开启推送）
        self._webhook = QLineEdit()
        self._webhook.setPlaceholderText("企业微信群机器人 Webhook 地址（填写即开启推送）")
        self._webhook.setFixedHeight(34)
        self._webhook.setStyleSheet(self._line_qss)
        outer.addWidget(self._fld("微信推送地址", self._webhook))

        # ⑥ 运行历史（接在推送地址后，随表单滚动；默认最近 10 条）
        hist_lab = QLabel(f"运行历史（最近 {MAX_LOGS_PER_TASK} 条）")
        hist_lab.setStyleSheet(
            f"font-size: 12px; color: {c['text_secondary']};"
            "background: transparent; border: none; font-weight: bold;"
        )
        outer.addWidget(hist_lab)
        self._logs = QListWidget()
        self._logs.setMinimumHeight(120)
        self._logs.setMaximumHeight(220)
        self._logs.setCursor(Qt.CursorShape.PointingHandCursor)
        self._logs.setStyleSheet(f"""
            QListWidget {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 8px; padding: 4px; outline: none;
            }}
            QListWidget::item {{
                border: none; border-bottom: 1px solid {c["border_light"]};
                border-radius: 4px; margin: 1px 0;
                min-height: 40px;
            }}
            QListWidget::item:selected {{
                background: {c["subnav_active"]};
            }}
            QListWidget::item:hover {{
                background: {c["subnav_hover"]};
            }}
        """)
        self._logs.itemClicked.connect(self._on_log_clicked)
        outer.addWidget(self._logs)
        outer.addStretch(1)

        # 默认任务类型：WokBee
        self._set_task_type(TaskType.WOKBEE)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.setWidget(body)
        form_lay.addWidget(scroll, stretch=1)

        return form

    def _fld(self, label: str, widget: QWidget) -> QWidget:
        w, _ = self._fld_with_label(label, widget)
        return w

    def _fld_with_label(self, label: str, widget: QWidget) -> tuple[QWidget, QLabel]:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setStyleSheet(
            f"font-size: 11px; color: {self.theme.colors['text_hint']};"
            "background: transparent; border: none;"
        )
        lay.addWidget(lab)
        lay.addWidget(widget)
        return w, lab

    def _build_type_combo(self) -> QComboBox:
        c = self.theme.colors
        combo = QComboBox()
        combo.setFixedHeight(34)
        apply_combo_popup_style(combo, c, rounded=True)
        combo.addItem(TaskType.TEXT.label, TaskType.TEXT.value)
        combo.addItem(TaskType.SCRIPT.label, TaskType.SCRIPT.value)
        combo.addItem(TaskType.WOKBEE.label, TaskType.WOKBEE.value)
        combo.currentIndexChanged.connect(self._on_type_changed)
        return combo

    def _build_text_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._content = QTextEdit()
        self._content.setPlaceholderText("文本正文")
        self._content.setFixedHeight(72)
        self._content.setStyleSheet(getattr(self, "_text_qss", ""))
        lay.addWidget(self._content)
        return page

    def _build_script_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._script_lang = QComboBox()
        self._script_lang.setFixedHeight(34)
        apply_combo_popup_style(self._script_lang, self.theme.colors, rounded=True)
        self._script_lang.addItem("Python", "python")
        self._script_lang.addItem("JavaScript (Node.js)", "javascript")
        self._script_lang.currentIndexChanged.connect(self._on_script_lang_changed)
        lay.addWidget(self._fld("脚本语言", self._script_lang))
        self._code = QTextEdit()
        self._code.setPlaceholderText("Python 脚本代码（用系统 Python 执行）")
        self._code.setFixedHeight(72)
        self._code.setStyleSheet(getattr(self, "_text_qss", ""))
        lay.addWidget(self._code)
        return page

    def _on_script_lang_changed(self):
        lang = self._script_lang.currentData() or "python"
        if lang == "javascript":
            self._code.setPlaceholderText("JavaScript 脚本代码（用系统 Node.js 执行）")
        else:
            self._code.setPlaceholderText("Python 脚本代码（用系统 Python 执行）")

    def _build_wokbee_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._project_id = QLineEdit()
        self._project_id.setPlaceholderText("粘贴 WokBee 项目 ID（在项目列表右键「复制项目 ID」）")
        self._project_id.setFixedHeight(34)
        self._project_id.setStyleSheet(getattr(self, "_line_qss", ""))
        lay.addWidget(self._fld("项目 ID", self._project_id))
        hint = QLabel("定时触发时将按该项目目标自动运行，无需额外填写指令。")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"font-size: 11px; color: {self.theme.colors['text_hint']};"
            "background: transparent; border: none;"
        )
        lay.addWidget(hint)
        return page

    # ── 数据刷新 ───────────────────────────────────────────
    def _refill_model_combos(self):
        models = self.provider_store.list_selectable_models()
        default = self.provider_store.resolve_default() or self.provider_store.first_resolved()

        for combo in (self._gen_combo, self._exec_combo):
            combo.blockSignals(True)
            combo.clear()
            if not models:
                combo.addItem("（暂无可用模型）", "")
            else:
                for m in models:
                    combo.addItem(
                        f"{m.provider_name} / {m.model_id}", (m.provider_id, m.model_id)
                    )
                self._pick_model(combo, default)
            combo.blockSignals(False)

    @staticmethod
    def _pick_model(combo: QComboBox, model) -> None:
        """按 ResolvedModel 或 (provider_id, model_id) 选中项；找不到则保持现状。"""
        if model is None:
            return
        if hasattr(model, "provider_id"):
            pid, mid = model.provider_id, model.model_id
        elif isinstance(model, tuple) and len(model) == 2:
            pid, mid = model
        else:
            return
        for i in range(combo.count()):
            data = combo.itemData(i)
            if isinstance(data, tuple) and data[0] == pid and data[1] == mid:
                combo.setCurrentIndex(i)
                return

    # ── 详情回填 ───────────────────────────────────────────
    def set_empty(self):
        self._task_id = None
        self._stack.setCurrentWidget(self._empty)

    def prepare_new(self):
        """进入新建态：清空表单并切到编辑页。"""
        self._task_id = None
        self._clear_form()
        self._stack.setCurrentWidget(self._form)
        self._set_edit_enabled(True)
        self._name.setText("")
        self._name.setPlaceholderText("新任务名称")
        self._name.setFocus()
        self._update_action_btns()

    def _clear_form(self):
        self._name.setText("")
        self._nl_input.setPlainText("")
        self._cron_text_value = ""
        self._content.setPlainText("")
        self._code.clear()
        self._script_lang.setCurrentIndex(0)
        self._on_script_lang_changed()
        self._schedule.setText("*/30 * * * *")
        self._project_id.clear()
        self._set_task_type(TaskType.WOKBEE)
        self._refill_model_combos()
        self._update_cron_preview()
        self._webhook.clear()
        self._log_cache.clear()
        self._logs.clear()
        empty = QListWidgetItem("暂无运行历史")
        empty.setFlags(Qt.ItemFlag.NoItemFlags)
        self._logs.addItem(empty)

    def load(self, task: ScheduledTask):
        self._task_id = task.id
        self._stack.setCurrentWidget(self._form)
        self._name.setText(task.name)
        self._nl_input.setPlainText(task.description)
        self._schedule.setText(task.schedule)
        self._cron_text_value = task.cron_text or ""
        self._update_cron_preview()

        idx = [TaskType.TEXT, TaskType.SCRIPT, TaskType.WOKBEE].index(task.task_type)
        self._type_combo.setCurrentIndex(idx)
        self._config_stack.setCurrentIndex(idx)

        self._content.setPlainText(task.content)
        self._code.setPlainText(task.code)
        lang = getattr(task, "script_lang", "python") or "python"
        lang_idx = 1 if lang == "javascript" else 0
        self._script_lang.setCurrentIndex(lang_idx)
        self._on_script_lang_changed()
        self._project_id.setText(task.project_id or "")
        self._webhook.setText(task.webhook_url)

        self._refill_model_combos()
        if task.gen_provider and task.gen_model_id:
            self._pick_model(self._gen_combo, (task.gen_provider, task.gen_model_id))
        if task.exec_provider and task.exec_model_id:
            self._pick_model(self._exec_combo, (task.exec_provider, task.exec_model_id))

        self._set_edit_enabled(True)
        self._load_logs(task.id)

    def _update_action_btns(self, *, generating: bool = False):
        """按输入/是否已保存/是否运行中更新操作按钮可用状态。"""
        has_nl = bool((self._nl_input.toPlainText() or "").strip())
        saved = bool(self._task_id)
        running = bool(
            self._task_id and self.scheduler.is_task_running(self._task_id)
        )
        self._gen_btn.setEnabled(not generating and not running and has_nl)
        self._save_btn.setEnabled(not generating and not running)
        self._del_btn.setEnabled(not generating and not running and saved)
        if running:
            self._run_btn.setEnabled(False)
            self._run_btn.setText("运行中…")
        else:
            self._run_btn.setText("立即运行")
            self._run_btn.setEnabled(not generating and saved)

    def _on_task_run_started(self, task_id: str):
        if self._task_id == task_id:
            self._update_action_btns()
            self.refresh_logs()

    def _on_task_run_progress(self, task_id: str, message: str):
        if self._task_id != task_id:
            return
        self._update_action_btns()
        self.refresh_logs()

    def _on_task_run_finished(self, task_id: str, _status: str, _message: str):
        if self._task_id == task_id:
            self._update_action_btns()
            self.refresh_logs()

    def _set_edit_enabled(self, enabled: bool):
        for w in [self._name, self._nl_input, self._content,
                  self._code, self._script_lang,
                  self._schedule, self._gen_combo, self._exec_combo,
                  self._type_combo, self._project_id, self._webhook]:
            w.setEnabled(enabled)
        if enabled:
            self._update_action_btns()
        else:
            self._gen_btn.setEnabled(False)
            self._save_btn.setEnabled(False)
            self._del_btn.setEnabled(False)
            self._run_btn.setEnabled(False)

    def _load_logs(self, task_id: str):
        self._logs.clear()
        self._log_cache.clear()
        logs = self.store.list_logs(task_id, limit=MAX_LOGS_PER_TASK)
        # 新的在前
        logs = list(reversed(logs))
        if not logs:
            empty = QListWidgetItem("暂无运行历史")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._logs.addItem(empty)
            return
        task = self.store.get(task_id)
        task_name = (task.name if task else "") or "—"
        for log in logs:
            self._log_cache[log.id] = log
            row = _LogRow(self.theme, log, task_name)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, log.id)
            item.setSizeHint(row.sizeHint())
            self._logs.addItem(item)
            self._logs.setItemWidget(item, row)

    def _on_log_clicked(self, item: QListWidgetItem):
        log_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not log_id:
            return
        log = self._log_cache.get(str(log_id))
        if not log:
            return
        task = self.store.get(self._task_id or "") if self._task_id else None
        name = (task.name if task else "") or ""
        _LogDetailDialog(self.theme, log, name, self).exec()

    def refresh_logs(self):
        if self._task_id:
            self._load_logs(self._task_id)

    # ── 信号处理 ───────────────────────────────────────────
    def _set_task_type(self, ttype: TaskType):
        """切换任务类型下拉与配置页（含默认 WokBee）。"""
        order = [TaskType.TEXT, TaskType.SCRIPT, TaskType.WOKBEE]
        idx = order.index(ttype) if ttype in order else 0
        self._type_combo.blockSignals(True)
        self._type_combo.setCurrentIndex(idx)
        self._type_combo.blockSignals(False)
        self._config_stack.setCurrentIndex(idx)
        self._current_type = ttype

    def _on_type_changed(self):
        idx = self._type_combo.currentIndex()
        self._config_stack.setCurrentIndex(idx)
        data = self._type_combo.currentData()
        try:
            self._current_type = TaskType(data) if data else TaskType.WOKBEE
        except ValueError:
            self._current_type = TaskType.WOKBEE

    def _update_cron_preview(self):
        expr = (self._schedule.text() or "").strip()
        hint = describe_cron(expr)
        if expr and hint:
            self._cron_title.setText(f"定时 (cron) · {hint}")
        else:
            self._cron_title.setText("定时 (cron)")

    def _on_run_now(self):
        if not self._task_id:
            return
        if self.scheduler.is_task_running(self._task_id):
            return
        self._run_btn.setEnabled(False)
        self._run_btn.setText("运行中…")
        try:
            started = self.scheduler.run_now(self._task_id)
            if not started:
                self._update_action_btns()
        except Exception:
            self._update_action_btns()

    def _on_nl_generate(self):
        text = (self._nl_input.toPlainText() or "").strip()
        if not text:
            return
        model = None
        gd = self._gen_combo.currentData()
        if isinstance(gd, tuple):
            model = self.provider_store.resolve(gd[0], gd[1])
        if not model:
            model = self.provider_store.resolve_default() or self.provider_store.first_resolved()
        if not model:
            return
        self._update_action_btns(generating=True)
        self._workers = [x for x in getattr(self, "_workers", []) if x.isRunning()]
        w = _NLWorker(NLBuilder(self.provider_store), text, model, self)
        w.done.connect(self._on_nl_done)
        w.finished.connect(w.deleteLater)
        self._workers.append(w)
        w.start()

    def _on_nl_done(self, result):
        self._update_action_btns()
        if isinstance(result, Exception):
            return
        data = result or {}
        config = data.get("config") or {}
        self._name.setText(data.get("name") or "")
        self._schedule.setText(data.get("schedule") or "")
        self._cron_text_value = data.get("cron_text") or ""
        self._update_cron_preview()
        ttype = (data.get("type") or "text").lower()
        if ttype not in (TaskType.TEXT.value, TaskType.SCRIPT.value, TaskType.WOKBEE.value):
            ttype = TaskType.TEXT.value
        idx = [TaskType.TEXT.value, TaskType.SCRIPT.value, TaskType.WOKBEE.value].index(ttype)
        self._type_combo.setCurrentIndex(idx)
        self._config_stack.setCurrentIndex(idx)
        self._content.setPlainText(str(config.get("content") or ""))
        self._code.setPlainText(str(config.get("code") or ""))
        lang = str(config.get("script_lang") or "python").lower()
        self._script_lang.setCurrentIndex(1 if lang in ("js", "javascript", "node") else 0)
        self._on_script_lang_changed()
        if config.get("project_id"):
            self._project_id.setText(str(config["project_id"]))
        # 推送：有 webhook 即填入
        webhook = str(config.get("webhook_url") or "")
        if webhook:
            self._webhook.setText(webhook)
        self._set_edit_enabled(True)

    def _collect(self, task_type: TaskType) -> dict:
        """从控件收集当前表单值。"""
        webhook = (self._webhook.text() or "").strip()
        data = {
            "name": (self._name.text() or "").strip(),
            "description": (self._nl_input.toPlainText() or "").strip(),
            "task_type": task_type,
            "schedule": (self._schedule.text() or "").strip(),
            "cron_text": (self._cron_text_value or "").strip(),
            "gen_provider": "", "gen_model_id": "",
            "exec_provider": "", "exec_model_id": "",
            "content": (self._content.toPlainText() or "").strip(),
            "use_ai": False,
            "code": (self._code.toPlainText() or "").strip(),
            "script_lang": self._script_lang.currentData() or "python",
            "timeout_s": 120,
            "project_id": (self._project_id.text() or "").strip(),
            "user_message": "",
            "max_steps": 40,
            "push_wecom": bool(webhook),
            "webhook_url": webhook,
            "msgtype": "text",
            "mention": "",
        }
        gd = self._gen_combo.currentData()
        if isinstance(gd, tuple):
            data["gen_provider"], data["gen_model_id"] = gd
        ed = self._exec_combo.currentData()
        if isinstance(ed, tuple):
            data["exec_provider"], data["exec_model_id"] = ed
        return data

    @staticmethod
    def _to_int(text: str, default: int) -> int:
        try:
            return max(1, int(str(text).strip() or default))
        except ValueError:
            return default

    def _on_save(self):
        task_type = TaskType(self._type_combo.currentData() or TaskType.TEXT.value)
        data = self._collect(task_type)
        name = data["name"]
        schedule = data["schedule"]
        if not name:
            return
        # 校验 cron
        try:
            CronTrigger.from_crontab(schedule)
        except (ValueError, TypeError):
            return
        # 校验类型必填
        if task_type == TaskType.SCRIPT and not data["code"]:
            return
        if task_type == TaskType.WOKBEE:
            pid = data["project_id"]
            if not pid:
                # 不再静默放弃：提示用户补齐项目（NL 生成也可能缺 project_id）
                QMessageBox.warning(
                    self, "缺少关联项目",
                    "WokBee 任务需要关联一个项目，请先在下方填写项目 ID。",
                )
                self._project_id.setFocus()
                return
            if self.project_store.get(pid) is None:
                QMessageBox.warning(
                    self, "项目不存在",
                    f"项目 {pid} 不存在，请重新选择。",
                )
                self._project_id.setFocus()
                return

        if self._task_id and self.store.get(self._task_id):
            task = self.store.get(self._task_id)
            for k, v in data.items():
                setattr(task, k, v)
            task.touch()
            self.store.save_task(task)
            task_id = task.id
        else:
            task = self.store.create(**data)
            task_id = task.id
        self._task_id = task_id
        self.scheduler.add_or_update(task)
        self.task_saved.emit(task_id)
        self._update_action_btns()

    def _on_delete(self):
        if not self._task_id:
            return
        if not self.store.get(self._task_id):
            return
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("删除任务")
        dlg.setFixedSize(360, 140)
        dlg.setStyleSheet(f"background: {c['content_bg']};")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(12)
        msg = QLabel("确定删除该定时任务？此操作不可撤销。")
        msg.setWordWrap(True)
        msg.setStyleSheet(f"font-size: 14px; color: {c['text']}; background: transparent; border: none;")
        lay.addWidget(msg)
        lay.addStretch()
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.setFixedSize(72, 34)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(secondary_btn_qss(c))
        cancel.clicked.connect(dlg.reject)
        ok = QPushButton("删除")
        ok.setFixedSize(72, 34)
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["danger"]};
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        ok.clicked.connect(dlg.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        tid = self._task_id
        self.store.delete_task(tid)
        self.scheduler.remove(tid)
        self.set_empty()
        self.task_deleted.emit(tid)


class AutoBeeView(QWidget):
    """AutoBee 模块容器：三栏布局 + 2s 轮询刷新。"""

    def __init__(self, theme: Theme, store: AutoBeeStore, scheduler: SchedulerService,
                 provider_store: ProviderStore, project_store: ProjectStore, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store
        self.scheduler = scheduler
        self.provider_store = provider_store
        self.project_store = project_store
        self._tasks: list[ScheduledTask] = []

        self._build()
        self._refresh_tasks()
        self.scheduler.notifier.task_started.connect(self._on_scheduler_task_started)
        self.scheduler.notifier.task_progress.connect(self._on_scheduler_task_progress)
        self.scheduler.notifier.task_finished.connect(self._on_scheduler_task_finished)
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    def _build(self):
        self.setStyleSheet(f"background: {self.theme.colors['content_bg']};")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 中间栏：搜索 + 新建 + 任务列表
        self.task_list = _TaskList(self.theme)
        self.task_list.new_task.connect(self._on_new)
        self.task_list.task_selected.connect(self._on_task_selected)
        self.task_list.toggle_enabled.connect(self._on_list_toggle)
        layout.addWidget(self.task_list)

        # 右栏：任务详情
        self.detail = _TaskDetail(
            self.theme, self.store, self.scheduler, self.provider_store, self.project_store,
        )
        self.detail.task_saved.connect(self._on_saved)
        self.detail.task_deleted.connect(self._on_deleted)
        layout.addWidget(self.detail, stretch=1)

    # ── 数据流 ─────────────────────────────────────────────
    def _refresh_tasks(self):
        self._tasks = self.store.list_tasks()
        self.task_list.set_tasks(self._tasks)

    def _on_new(self):
        self._refresh_tasks()
        self.task_list.clear_selection()
        self.detail.prepare_new()

    def _on_task_selected(self, task_id: str):
        task = self.store.get(task_id)
        if task:
            self.detail.load(task)

    def _on_saved(self, task_id: str):
        self._refresh_tasks()
        self.task_list.select(task_id)

    def _on_deleted(self, task_id: str):
        self._refresh_tasks()
        if self._tasks:
            self.task_list.select(self._tasks[0].id)
        else:
            self.detail.set_empty()

    def _on_state_changed(self, task_id: str):
        self._refresh_tasks()
        task = self.store.get(task_id)
        if task:
            self.task_list.select(task_id)

    def _on_list_toggle(self, task_id: str):
        """左侧列表启用/停用。"""
        task = self.store.get(task_id)
        if not task:
            return
        if task.enabled:
            self.scheduler.pause(task_id)
        else:
            self.scheduler.resume(task_id)
        self._on_state_changed(task_id)

    def _on_scheduler_task_started(self, task_id: str):
        self._refresh_tasks()
        if self.task_list.current_selected() == task_id:
            self.detail._update_action_btns()
            self.detail.refresh_logs()

    def _on_scheduler_task_progress(self, task_id: str, _message: str):
        self._refresh_tasks()

    def _on_scheduler_task_finished(self, task_id: str, _status: str, _message: str):
        self._refresh_tasks()
        if self.task_list.current_selected() == task_id:
            self.detail._update_action_btns()
            self.detail.refresh_logs()

    def _on_tick(self):
        # 刷新所选任务的下一步运行时间与日志
        curr = self.task_list.current_selected()
        if curr:
            task = self.store.get(curr)
            if task and self.scheduler.running:
                task.next_run = self.scheduler.next_run_time(curr)
            self.detail.refresh_logs()
        if self.scheduler.any_task_running():
            self._refresh_tasks()
        else:
            self.task_list.refresh()

    def shutdown(self):
        if getattr(self, "_timer", None):
            self._timer.stop()
