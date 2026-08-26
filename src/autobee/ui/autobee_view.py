"""AutoBee 主视图：中任务列表 | 右任务详情。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QTimer, QThread
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QLineEdit,
    QTextEdit, QComboBox, QScrollArea, QPushButton, QStackedWidget,
    QSizePolicy, QListWidget, QListWidgetItem, QCheckBox, QDialog,
    QMessageBox,
)

from apscheduler.triggers.cron import CronTrigger

from tokbee.ui.styles.theme import Theme
from tokbee.ui.combo_style import apply_combo_popup_style
from tokbee.core.provider_store import ProviderStore

from wokbee.core.project_store import ProjectStore

from autobee.core.models import JobLog, ScheduledTask, TaskRunStatus, TaskType, new_task_id
from autobee.core.store import AutoBeeStore
from autobee.engine.nl_builder import NLBuilder
from autobee.engine.scheduler import SchedulerService, describe_cron

_STATUS_COLOR = {
    TaskRunStatus.SUCCESS: "success",
    TaskRunStatus.FAILED: "danger",
    TaskRunStatus.MISSED: "warning",
    TaskRunStatus.RUNNING: "accent",
}


def _tip(parent: QWidget, theme: Theme, message: str, title: str = "提示"):
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumSize(400, 160)
    dlg.resize(420, 200)
    dlg.setStyleSheet(f"background: {theme.colors['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {theme.colors['text']};")
    layout.addWidget(msg, stretch=1)
    row = QHBoxLayout()
    row.addStretch()
    ok = QPushButton("知道了")
    ok.setFixedSize(80, 34)
    ok.setCursor(Qt.CursorShape.PointingHandCursor)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {theme.colors["btn_bg"]}; color: {theme.colors["text"]};
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {theme.colors["btn_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(ok)
    layout.addLayout(row)
    dlg.exec()


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

        enabled = "已启用" if t.enabled else "已暂停"
        state = c["success"] if t.enabled else c["text_hint"]
        last_label = t.last_status or "未运行"
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


def _form_row(label: str, widget: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    lab = QLabel(label)
    lab.setMinimumWidth(72)
    row.addWidget(lab)
    row.addWidget(widget, 1)
    return row


class _TaskDetail(QWidget):
    """右栏：任务编辑器 + 运行历史。"""

    task_saved = Signal(str)
    task_deleted = Signal(str)
    task_state_changed = Signal(str)

    def __init__(self, theme: Theme, store: AutoBeeStore, scheduler: SchedulerService,
                 provider_store: ProviderStore, project_store: ProjectStore, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store
        self.scheduler = scheduler
        self.provider_store = provider_store
        self.project_store = project_store
        self._task_id: str | None = None
        self._current_type = TaskType.TEXT
        self._build()

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
        outer = QVBoxLayout(form)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(8)

        # 标题行
        head = QHBoxLayout()
        head.setSpacing(8)
        self._name = QLineEdit()
        self._name.setPlaceholderText("任务名称")
        self._name.setFixedHeight(32)
        head.addWidget(self._name, 1)
        self._enable_btn = QPushButton("暂停")
        self._enable_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._enable_btn.setFixedSize(60, 32)
        self._enable_btn.clicked.connect(self._on_toggle_enable)
        head.addWidget(self._enable_btn)
        outer.addLayout(head)

        # 自然语言生成
        gen_row = QHBoxLayout()
        gen_row.setSpacing(8)
        self._nl_input = QLineEdit()
        self._nl_input.setPlaceholderText("用自然语言描述，如：每天上午9点给企业微信群推问候")
        self._nl_input.setFixedHeight(30)
        gen_row.addWidget(self._nl_input, 1)
        gen_btn = QPushButton("用 AI 生成")
        gen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        gen_btn.setFixedSize(86, 30)
        gen_btn.setStyleSheet(f"""
            QPushButton {{ background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 12px; }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        gen_btn.clicked.connect(self._on_nl_generate)
        gen_row.addWidget(gen_btn)
        outer.addLayout(gen_row)

        # description
        self._description = QTextEdit()
        self._description.setPlaceholderText("任务描述（自然语言来源，供再次生成修饰）")
        self._description.setFixedHeight(48)
        outer.addWidget(self._description)

        # 类型 + 配置
        self._type_combo = self._build_type_combo()
        type_row = _form_row("执行类型", self._type_combo)
        outer.addLayout(type_row)
        self._config_stack = QStackedWidget()
        self._config_stack.addWidget(self._build_text_page())
        self._config_stack.addWidget(self._build_script_page())
        self._config_stack.addWidget(self._build_wokbee_page())
        outer.addWidget(self._config_stack)

        # 推送渠道（企业微信，作用到任意类型的结果）
        push_layout = QVBoxLayout()
        push_layout.setContentsMargins(0, 4, 0, 0)
        push_layout.setSpacing(4)
        self._push_wecom = QCheckBox("结果推送到企业微信")
        self._push_wecom.setStyleSheet(f"color: {c['text_secondary']}; background: transparent;")
        self._push_wecom.toggled.connect(self._on_push_toggled)
        push_layout.addWidget(self._push_wecom)
        self._push_box = QWidget()
        box_lay = QVBoxLayout(self._push_box)
        box_lay.setContentsMargins(0, 0, 0, 0)
        box_lay.setSpacing(4)
        self._webhook = QLineEdit()
        self._webhook.setPlaceholderText("企业微信群机器人 Webhook 地址")
        self._webhook.setFixedHeight(28)
        box_lay.addLayout(_form_row("Webhook", self._webhook))
        self._msgtype = QComboBox()
        self._msgtype.setFixedHeight(28)
        apply_combo_popup_style(self._msgtype, c)
        self._msgtype.addItem("文本", "text")
        self._msgtype.addItem("Markdown", "markdown")
        box_lay.addLayout(_form_row("消息类型", self._msgtype))
        self._mention = QLineEdit()
        self._mention.setPlaceholderText("提及 @all 或账号/手机号（可选）")
        self._mention.setFixedHeight(28)
        box_lay.addLayout(_form_row("提及", self._mention))
        push_layout.addWidget(self._push_box)
        outer.addLayout(push_layout)

        # schedule
        sched_row = QHBoxLayout()
        sched_row.setSpacing(8)
        sched_lab = QLabel("定时 (cron)")
        sched_lab.setMinimumWidth(72)
        sched_row.addWidget(sched_lab)
        self._schedule = QLineEdit()
        self._schedule.setPlaceholderText('如 0 9 * * * 或 */30 * * * *')
        self._schedule.setFixedHeight(30)
        self._schedule.textChanged.connect(self._update_cron_preview)
        sched_row.addWidget(self._schedule, 1)
        self._cron_text = QLineEdit()
        self._cron_text.setPlaceholderText("如：每天 09:00")
        self._cron_text.setFixedHeight(30)
        sched_row.addWidget(self._cron_text, 1)
        outer.addLayout(sched_row)
        self._cron_hint = QLabel("")
        self._cron_hint.setStyleSheet(
            f"font-size: 11px; color: {c['text_hint']}; background: transparent; border: none;"
        )
        outer.addWidget(self._cron_hint)

        # AI 模型
        model_row = QHBoxLayout()
        model_row.setSpacing(8)
        self._gen_combo = QComboBox()
        self._gen_combo.setFixedHeight(30)
        apply_combo_popup_style(self._gen_combo, c)
        self._exec_combo = QComboBox()
        self._exec_combo.setFixedHeight(30)
        apply_combo_popup_style(self._exec_combo, c)
        model_row.addWidget(self._fld("生成模型", self._gen_combo))
        model_row.addWidget(self._fld("执行模型", self._exec_combo))
        outer.addLayout(model_row)
        self._refill_model_combos()

        # 操作行
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._run_btn = QPushButton("立即运行")
        self._run_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._run_btn.setFixedSize(88, 34)
        self._run_btn.clicked.connect(self._on_run_now)
        btn_row.addWidget(self._run_btn)
        self._save_btn = QPushButton("保存")
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setFixedSize(72, 34)
        self._save_btn.setStyleSheet(f"""
            QPushButton {{ background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px; }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._save_btn)
        self._del_btn = QPushButton("删除")
        self._del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._del_btn.setFixedSize(72, 34)
        self._del_btn.setStyleSheet(f"""
            QPushButton {{ background: {c["btn_bg"]}; color: {c["danger"]};
                border: none; border-radius: 6px; font-size: 13px; }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        self._del_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self._del_btn)
        btn_row.addStretch()
        outer.addLayout(btn_row)

        # 运行历史
        outer.addWidget(QLabel("运行历史"))
        self._logs = QListWidget()
        self._logs.setStyleSheet(f"""
            QListWidget {{ background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 6px; font-size: 12px; }}
            QListWidget::item {{ padding: 4px; border-bottom: 1px solid {c["border_light"]}; }}
        """)
        outer.addWidget(self._logs, stretch=1)

        return form

    def _fld(self, label: str, widget: QWidget) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lab = QLabel(label)
        lab.setStyleSheet(
            f"font-size: 11px; color: {self.theme.colors['text_hint']};"
            "background: transparent; border: none;"
        )
        lay.addWidget(lab)
        lay.addWidget(widget)
        return w

    def _build_type_combo(self) -> QComboBox:
        c = self.theme.colors
        combo = QComboBox()
        combo.setFixedHeight(30)
        apply_combo_popup_style(combo, c)
        combo.addItem(TaskType.TEXT.label, TaskType.TEXT.value)
        combo.addItem(TaskType.SCRIPT.label, TaskType.SCRIPT.value)
        combo.addItem(TaskType.WOKBEE.label, TaskType.WOKBEE.value)
        combo.currentIndexChanged.connect(self._on_type_changed)
        return combo

    def _build_text_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)
        self._content = QTextEdit()
        self._content.setPlaceholderText("文本正文；若勾选下方用AI，则按描述生成正文")
        self._content.setFixedHeight(80)
        lay.addWidget(self._content)
        self._use_ai = QCheckBox("用 AI 按描述生成正文（用执行模型）")
        self._use_ai.setStyleSheet(
            f"color: {self.theme.colors['text_secondary']}; background: transparent;"
        )
        lay.addWidget(self._use_ai)
        return page

    def _build_script_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)
        self._code = QTextEdit()
        self._code.setPlaceholderText("Python 脚本代码（在任务运行时用系统 Python 执行）")
        self._code.setFixedHeight(110)
        lay.addWidget(self._code)
        self._timeout = QLineEdit()
        self._timeout.setFixedHeight(30)
        lay.addLayout(_form_row("超时(秒)", self._timeout))
        return page

    def _build_wokbee_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(6)
        self._project_combo = QComboBox()
        self._project_combo.setFixedHeight(30)
        apply_combo_popup_style(self._project_combo, self.theme.colors)
        self._refill_projects()
        lay.addLayout(_form_row("项目", self._project_combo))
        self._user_message = QTextEdit()
        self._user_message.setPlaceholderText("给 Agent 的指令（留空则用项目目标）")
        self._user_message.setFixedHeight(64)
        lay.addWidget(self._user_message)
        self._max_steps = QLineEdit()
        self._max_steps.setFixedHeight(30)
        lay.addLayout(_form_row("最大步数", self._max_steps))
        return page

    # ── 数据刷新 ───────────────────────────────────────────
    def _refill_projects(self):
        self._project_combo.blockSignals(True)
        self._project_combo.clear()
        self._project_combo.addItem("— 未关联项目 —", "")
        for p in self.project_store.list_projects():
            self._project_combo.addItem(f"{p.title} ({p.id})", p.id)
        self._project_combo.blockSignals(False)

    def _refill_model_combos(self):
        models = self.provider_store.list_selectable_models()
        for combo, sel_pid, sel_mid in [
            (self._gen_combo, "", ""), (self._exec_combo, "", ""),
        ]:
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("（未设置：回落到默认模型）", "")
            for m in models:
                combo.addItem(f"{m.provider_name} / {m.model_id}", (m.provider_id, m.model_id))
            combo.blockSignals(False)

    # ── 详情回填 ───────────────────────────────────────────
    def set_empty(self):
        self._task_id = None
        self._stack.setCurrentWidget(self._empty)

    def prepare_new(self):
        """进入新建态：清空表单并切到编辑页。"""
        self._task_id = None
        self._clear_form()
        self._set_edit_enabled(True)
        self._enable_btn.setEnabled(False)
        self._enable_btn.setText("")
        self._run_btn.setEnabled(False)
        self._del_btn.setEnabled(False)
        self._stack.setCurrentWidget(self._form)
        self._name.setText("")
        self._name.setPlaceholderText("新任务名称")
        self._name.setFocus()

    def _clear_form(self):
        self._name.setText("")
        self._description.setPlainText("")
        self._content.setPlainText("")
        self._use_ai.setChecked(False)
        self._code.clear()
        self._timeout.setText("120")
        self._user_message.clear()
        self._max_steps.setText("40")
        self._schedule.setText("*/30 * * * *")
        self._cron_text.clear()
        self._refill_projects()
        self._project_combo.setCurrentIndex(0)
        self._type_combo.setCurrentIndex(0)
        self._update_cron_preview()
        # 推送渠道
        self._push_wecom.setChecked(False)
        self._webhook.clear()
        self._msgtype.setCurrentIndex(0)
        self._mention.clear()
        self._push_box.setVisible(False)
        self._logs.clear()
        QListWidgetItem("暂无运行历史", self._logs)

    def load(self, task: ScheduledTask):
        self._task_id = task.id
        self._stack.setCurrentWidget(self._form)
        self._name.setText(task.name)
        self._description.setPlainText(task.description)
        self._schedule.setText(task.schedule)
        self._cron_text.setText(task.cron_text)
        self._update_cron_preview()

        idx = [TaskType.TEXT, TaskType.SCRIPT, TaskType.WOKBEE].index(task.task_type)
        self._type_combo.setCurrentIndex(idx)
        self._config_stack.setCurrentIndex(idx)

        self._content.setPlainText(task.content)
        self._use_ai.setChecked(task.use_ai)
        self._code.setPlainText(task.code)
        self._timeout.setText(str(task.timeout_s))
        self._refill_projects()
        self._select_project(task.project_id)
        self._user_message.setPlainText(task.user_message)
        self._max_steps.setText(str(task.max_steps))
        # 推送渠道
        self._push_wecom.setChecked(task.push_wecom)
        self._webhook.setText(task.webhook_url)
        self._msgtype.setCurrentIndex(0 if task.msgtype != "markdown" else 1)
        self._mention.setText(task.mention)
        self._push_box.setVisible(bool(task.push_wecom))

        def pick(combo, pid, mid):
            combo.blockSignals(True)
            for i in range(combo.count()):
                data = combo.itemData(i)
                if isinstance(data, tuple) and data[0] == pid and data[1] == mid:
                    combo.setCurrentIndex(i)
                    break
            combo.blockSignals(False)
        pick(self._gen_combo, task.gen_provider, task.gen_model_id)
        pick(self._exec_combo, task.exec_provider, task.exec_model_id)

        # 启用态按钮
        self._enable_btn.setText("暂停" if task.enabled else "启用")
        self._enable_btn.setEnabled(True)
        self._run_btn.setEnabled(True)
        self._del_btn.setEnabled(True)
        self._set_edit_enabled(True)
        self._load_logs(task.id)

    def _select_project(self, project_id: str):
        for i in range(self._project_combo.count()):
            if self._project_combo.itemData(i) == project_id:
                self._project_combo.setCurrentIndex(i)
                return

    def _set_edit_enabled(self, enabled: bool):
        for w in [self._name, self._description, self._content, self._use_ai,
                  self._code, self._timeout, self._user_message, self._max_steps,
                  self._schedule, self._cron_text, self._gen_combo, self._exec_combo,
                  self._type_combo, self._project_combo,
                  self._push_wecom, self._push_box]:
            w.setEnabled(enabled)
        self._save_btn.setEnabled(enabled)
        if enabled:
            self._enable_btn.setText("暂停")
            self._enable_btn.setEnabled(True)
            self._run_btn.setEnabled(True)
            self._del_btn.setEnabled(True)
        else:
            self._enable_btn.setText("")
            self._enable_btn.setEnabled(False)
            self._run_btn.setEnabled(False)
            self._del_btn.setEnabled(False)
            self._save_btn.setEnabled(False)

    def _load_logs(self, task_id: str):
        self._logs.clear()
        logs = self.store.list_logs(task_id)
        if not logs:
            QListWidgetItem("暂无运行历史", self._logs)
            return
        for log in logs:
            line = self._format_log(log)
            QListWidgetItem(line, self._logs)

    @staticmethod
    def _format_log(log: JobLog) -> str:
        status = log.status.label
        if log.finished_at and log.duration_s:
            tail = f"{log.finished_at.split()[1]} · {log.duration_s:.1f}s"
        elif log.started_at:
            tail = log.started_at.split()[1]
        else:
            tail = ""
        err = log.error or log.summary
        return f"[{status}] {tail}  {err}"[:180]

    def refresh_logs(self):
        if self._task_id:
            self._load_logs(self._task_id)

    # ── 信号处理 ───────────────────────────────────────────
    def _on_type_changed(self):
        idx = self._type_combo.currentIndex()
        self._config_stack.setCurrentIndex(idx)

    def _on_push_toggled(self, checked: bool):
        self._push_box.setVisible(checked)

    def _update_cron_preview(self):
        expr = (self._schedule.text() or "").strip()
        hint = describe_cron(expr)
        if expr and hint:
            self._cron_hint.setText(hint)
            self._cron_hint.setStyleSheet(
                f"font-size: 11px; color: {self.theme.colors['text_secondary']};"
                "background: transparent; border: none;"
            )
        else:
            self._cron_hint.setText("")
            self._cron_hint.setStyleSheet(
                f"font-size: 11px; color: {self.theme.colors['text_hint']};"
                "background: transparent; border: none;"
            )

    def _on_toggle_enable(self):
        if not self._task_id:
            return
        task = self.store.get(self._task_id)
        if not task:
            return
        if task.enabled:
            self.scheduler.pause(self._task_id)
        else:
            self.scheduler.resume(self._task_id)
        self.task_state_changed.emit(self._task_id)

    def _on_run_now(self):
        if not self._task_id:
            return
        try:
            self.scheduler.run_now(self._task_id)
            _tip(self, self.theme, "已提交立即运行，稍后可在运行历史查看。", "立即运行")
        except Exception as e:
            _tip(self, self.theme, f"立即运行失败：{e}", "错误")

    def _on_nl_generate(self):
        text = (self._nl_input.text() or "").strip()
        if not text:
            _tip(self, self.theme, "请先输入自然语言描述。", "提示")
            return
        model = self.provider_store.resolve_default() or self.provider_store.first_resolved()
        if not model:
            _tip(self, self.theme, "没有可用模型，请先在「AI配置」启用模型。", "提示")
            return
        self._run_btn.setEnabled(False)
        # 清理已结束的 worker，避免引用累积
        self._workers = [x for x in getattr(self, "_workers", []) if x.isRunning()]
        w = _NLWorker(NLBuilder(self.provider_store), text, model, self)
        w.done.connect(self._on_nl_done)
        w.finished.connect(w.deleteLater)
        self._workers.append(w)
        w.start()

    def _on_nl_done(self, result):
        self._run_btn.setEnabled(True)
        self._nl_input.clear()
        if isinstance(result, Exception):
            _tip(self, self.theme, f"AI 生成失败：{result}", "生成失败")
            return
        data = result or {}
        config = data.get("config") or {}
        self._name.setText(data.get("name") or "")
        self._schedule.setText(data.get("schedule") or "")
        self._cron_text.setText(data.get("cron_text") or "")
        self._update_cron_preview()
        ttype = (data.get("type") or "text").lower()
        if ttype not in (TaskType.TEXT.value, TaskType.SCRIPT.value, TaskType.WOKBEE.value):
            ttype = TaskType.TEXT.value
        idx = [TaskType.TEXT.value, TaskType.SCRIPT.value, TaskType.WOKBEE.value].index(ttype)
        self._type_combo.setCurrentIndex(idx)
        self._config_stack.setCurrentIndex(idx)
        self._content.setPlainText(str(config.get("content") or ""))
        self._use_ai.setChecked(bool(config.get("use_ai", False)))
        self._code.setPlainText(str(config.get("code") or ""))
        if config.get("timeout_s"):
            self._timeout.setText(str(config["timeout_s"]))
        if config.get("project_id"):
            self._select_project(str(config["project_id"]))
        if config.get("user_message"):
            self._user_message.setPlainText(str(config["user_message"]))
        if config.get("max_steps"):
            self._max_steps.setText(str(config["max_steps"]))
        # 推送渠道
        self._push_wecom.setChecked(bool(config.get("push_wecom", False)))
        self._webhook.setText(str(config.get("webhook_url") or ""))
        mt = str(config.get("msgtype") or "text")
        self._msgtype.setCurrentIndex(0 if mt != "markdown" else 1)
        self._mention.setText(str(config.get("mention") or ""))
        self._push_box.setVisible(self._push_wecom.isChecked())
        self._code.setPlainText(str(config.get("code") or ""))
        if config.get("timeout_s"):
            self._timeout.setText(str(config["timeout_s"]))
        if config.get("project_id"):
            self._select_project(str(config["project_id"]))
        if config.get("user_message"):
            self._user_message.setPlainText(str(config["user_message"]))
        if config.get("max_steps"):
            self._max_steps.setText(str(config["max_steps"]))
        self._set_edit_enabled(True)
        _tip(self, self.theme, "已生成配置，请确认后保存。", "生成完成")

    def _collect(self, task_type: TaskType) -> dict:
        """从控件收集当前表单值。"""
        data = {
            "name": (self._name.text() or "").strip(),
            "description": (self._description.toPlainText() or "").strip(),
            "task_type": task_type,
            "schedule": (self._schedule.text() or "").strip(),
            "cron_text": (self._cron_text.text() or "").strip(),
            "gen_provider": "", "gen_model_id": "",
            "exec_provider": "", "exec_model_id": "",
        }
        gd = self._gen_combo.currentData()
        if isinstance(gd, tuple):
            data["gen_provider"], data["gen_model_id"] = gd
        ed = self._exec_combo.currentData()
        if isinstance(ed, tuple):
            data["exec_provider"], data["exec_model_id"] = ed

        # 类型负载
        data["content"] = (self._content.toPlainText() or "").strip()
        data["use_ai"] = self._use_ai.isChecked()
        data["code"] = (self._code.toPlainText() or "").strip()
        data["timeout_s"] = self._to_int(self._timeout.text(), 120)
        data["project_id"] = self._project_combo.currentData() or ""
        data["user_message"] = (self._user_message.toPlainText() or "").strip()
        data["max_steps"] = self._to_int(self._max_steps.text(), 40)
        # 推送渠道
        data["push_wecom"] = self._push_wecom.isChecked()
        data["webhook_url"] = (self._webhook.text() or "").strip()
        data["msgtype"] = self._msgtype.currentData() or "text"
        data["mention"] = (self._mention.text() or "").strip()
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
            _tip(self, self.theme, "请填写任务名称。", "提示")
            return
        # 校验 cron
        try:
            CronTrigger.from_crontab(schedule)
        except (ValueError, TypeError):
            _tip(self, self.theme, f"cron 表达式无效：{schedule}。如示例：0 9 * * 1-5", "提示")
            return
        # 校验类型必填
        if task_type == TaskType.SCRIPT and not data["code"]:
            _tip(self, self.theme, "脚本类型需填写代码。", "提示")
            return
        if task_type == TaskType.WOKBEE and not data["project_id"]:
            _tip(self, self.theme, "WokBee 任务需选择关联项目。", "提示")
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
        self._enable_btn.setText("暂停" if task.enabled else "启用")
        self.task_saved.emit(task_id)
        _tip(self, self.theme, "已保存并更新调度。", "保存成功")

    def _on_delete(self):
        if not self._task_id:
            return
        if not self.store.get(self._task_id):
            return
        ret = QMessageBox.question(
            self, "删除任务", "确定删除该定时任务？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ret != QMessageBox.StandardButton.Yes:
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
        layout.addWidget(self.task_list)

        # 右栏：任务详情
        self.detail = _TaskDetail(
            self.theme, self.store, self.scheduler, self.provider_store, self.project_store,
        )
        self.detail.task_saved.connect(self._on_saved)
        self.detail.task_deleted.connect(self._on_deleted)
        self.detail.task_state_changed.connect(self._on_state_changed)
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

    def _on_tick(self):
        # 刷新所选任务的下一步运行时间与日志
        curr = self.task_list.current_selected()
        if curr:
            task = self.store.get(curr)
            if task and self.scheduler.running:
                task.next_run = self.scheduler.next_run_time(curr)
            self.detail.refresh_logs()
        # 列表中下次运行时间近似展示已由 item 卡片决定；此处仅作状态漂移刷新
        self.task_list.refresh()

    def shutdown(self):
        if getattr(self, "_timer", None):
            self._timer.stop()
