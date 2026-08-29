"""WokBee UI — 设置页（挂在 AI 配置二级导航）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QLineEdit, QComboBox, QCheckBox,
    QSpinBox, QFileDialog, QDialog,
)

from tokbee.ui.styles.theme import Theme
from tokbee.ui.combo_style import (
    apply_combo_popup_style,
    rounded_lineedit_qss,
    rounded_spin_qss,
    secondary_btn_qss,
    checkbox_qss,
    hint_label_qss,
)
from tokbee.core.provider_store import ProviderStore

from wokbee.core.settings import WokBeeSettings
from wokbee.ui.dialogs import (
    apply_flags_to_checks,
    build_approval_checkboxes,
    flags_from_checks,
    tip as _tip,
)


class WokBeeSettingsWorkspace(QWidget):
    """WokBee 必要配置。"""

    def __init__(
        self,
        theme: Theme,
        settings: WokBeeSettings | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.settings = settings or WokBeeSettings()
        self._provider_store = ProviderStore()
        self._build()
        self._load()

    def _build(self):
        c = self.theme.colors
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(f"background: {c['content_bg']}; border: none;")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(28, 20, 28, 12)
        title = QLabel("WokBee 设置")
        title.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        hl.addWidget(title)
        tip = QLabel(
            "配置工作区根目录、默认审核勾选与模型。"
            "新建项目会拷贝此处的审核策略，之后可在项目内单独修改。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(
            f"font-size: 12px; color: {c['text_hint']};"
            "background: transparent; border: none;"
        )
        hl.addWidget(tip)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(28, 16, 28, 16)
        bl.setSpacing(18)

        bl.addWidget(self._section_label("工作区根目录"))
        ws_row = QHBoxLayout()
        self._ws_edit = QLineEdit()
        self._ws_edit.setFixedHeight(34)
        self._ws_edit.setPlaceholderText("选择 WokBee 工作区根目录…")
        self._style_input(self._ws_edit)
        ws_row.addWidget(self._ws_edit, stretch=1)
        browse = QPushButton("浏览…")
        browse.setFixedSize(72, 34)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.setStyleSheet(self._secondary_btn_qss())
        browse.clicked.connect(self._browse_workspace)
        ws_row.addWidget(browse)
        bl.addLayout(ws_row)
        hint = QLabel(
            "新建项目将创建：memory|workspace|deliverables|uploads|runs|archives|scripts…；"
            "交付物→deliverables/；上传→uploads/（归档时保留）；"
            "经验→memory/experiences/exp_时间戳.md（只加载最新）；"
            "Agent 禁止访问 archives/；Skills 全局挂载不复制"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(hint_label_qss(c))
        bl.addWidget(hint)

        bl.addWidget(self._section_label("已授权附加目录（项目外）"))
        ext_intro = QLabel(
            "列表中的目录已加入全局白名单，Agent 可经 /ext/<slug>/… 虚拟路径用文件工具访问。"
            "运行中也可让 Agent 调 request_access 申请新目录（走人工高危审批，获批后自动写入此列表）。"
        )
        ext_intro.setWordWrap(True)
        ext_intro.setStyleSheet(hint_label_qss(c))
        bl.addWidget(ext_intro)
        self._ext_box = QFrame()
        self._ext_box.setStyleSheet(
            f"QFrame {{ background: {c['content_bg']}; border: none; border-radius: 6px; }}"
        )
        self._ext_lay = QVBoxLayout(self._ext_box)
        self._ext_lay.setContentsMargins(12, 10, 12, 10)
        self._ext_lay.setSpacing(6)
        bl.addWidget(self._ext_box)
        ext_add = QPushButton("添加目录…")
        ext_add.setFixedHeight(34)
        ext_add.setCursor(Qt.CursorShape.PointingHandCursor)
        ext_add.setStyleSheet(self._secondary_btn_qss())
        ext_add.clicked.connect(self._add_ext_dir)
        bl.addWidget(ext_add, alignment=Qt.AlignmentFlag.AlignLeft)
        ext_note = QLabel(
            "移除仅撤销该目录的访问权；需要时可在运行中由 Agent 重新申请，或在此重新添加。"
        )
        ext_note.setWordWrap(True)
        ext_note.setStyleSheet(hint_label_qss(c))
        bl.addWidget(ext_note)

        bl.addWidget(self._section_label("默认审核策略（勾选 = 免审）"))
        approval_box, self._approval_checks = build_approval_checkboxes(self.theme)
        bl.addWidget(approval_box)
        ap_hint = QLabel(
            "未勾选的级别在执行时需要人工审批。"
            "新项目会继承这些勾选，互不影响；改全局不会自动改已有项目。"
        )
        ap_hint.setWordWrap(True)
        ap_hint.setStyleSheet(hint_label_qss(c))
        bl.addWidget(ap_hint)

        bl.addWidget(self._section_label("默认模型（OpenAI 风格 API）"))
        self._model_combo = QComboBox()
        apply_combo_popup_style(
            self._model_combo, c, rounded=True,
            fixed_width=300, fixed_height=40,
        )
        bl.addWidget(self._model_combo, alignment=Qt.AlignmentFlag.AlignLeft)
        model_hint = QLabel(
            "新建项目优先使用「厂商设置」里带「默认」徽章的模型；"
            "此处仅在未设置厂商默认时作为回退。Key / Host 仍在厂商设置中维护。"
        )
        model_hint.setWordWrap(True)
        model_hint.setStyleSheet(hint_label_qss(c))
        bl.addWidget(model_hint)

        bl.addWidget(self._section_label("执行上限"))
        spin_qss = rounded_spin_qss(c)
        lbl_qss = f"font-size: 13px; color: {c['text']};"
        limits = QHBoxLayout()
        limits.setSpacing(16)
        max_steps_lbl = QLabel("最大步数")
        max_steps_lbl.setStyleSheet(lbl_qss)
        limits.addWidget(max_steps_lbl)
        self._max_steps = QSpinBox()
        self._max_steps.setRange(1, 500)
        self._max_steps.setFixedWidth(100)
        self._max_steps.setStyleSheet(spin_qss)
        self._max_steps.setToolTip("Agent 步数上限")
        limits.addWidget(self._max_steps)
        max_par_lbl = QLabel("最大并行工具")
        max_par_lbl.setStyleSheet(lbl_qss)
        limits.addWidget(max_par_lbl)
        self._max_parallel = QSpinBox()
        self._max_parallel.setRange(1, 16)
        self._max_parallel.setFixedWidth(100)
        self._max_parallel.setStyleSheet(spin_qss)
        limits.addWidget(self._max_parallel)
        max_ph_lbl = QLabel("管线阶段上限")
        max_ph_lbl.setStyleSheet(lbl_qss)
        limits.addWidget(max_ph_lbl)
        self._max_phases = QSpinBox()
        self._max_phases.setRange(1, 500)
        self._max_phases.setFixedWidth(100)
        self._max_phases.setStyleSheet(spin_qss)
        self._max_phases.setToolTip("pipeline 阶段上限")
        limits.addWidget(self._max_phases)
        limits.addStretch()
        bl.addLayout(limits)
        phase_hint = QLabel(
            "管线阶段按总结写入的执行顺序一路推进（可为 脚本×N → AI×N → 脚本…），"
            "不是强制「脚本、AI」一一交错。"
        )
        phase_hint.setWordWrap(True)
        phase_hint.setStyleSheet(hint_label_qss(c))
        bl.addWidget(phase_hint)

        bl.addWidget(self._section_label("AI 调用节流"))
        ai_int_row = QHBoxLayout()
        ai_int_row.setSpacing(10)
        ai_int_lbl = QLabel("调用最小间隔（毫秒）")
        ai_int_lbl.setStyleSheet(lbl_qss)
        ai_int_row.addWidget(ai_int_lbl)
        self._ai_interval = QSpinBox()
        self._ai_interval.setRange(0, 60000)
        self._ai_interval.setSingleStep(500)
        self._ai_interval.setFixedWidth(120)
        self._ai_interval.setSuffix(" ms")
        self._ai_interval.setStyleSheet(spin_qss)
        self._ai_interval.setToolTip("AI 调用最小间隔")
        ai_int_row.addWidget(self._ai_interval)
        ai_int_row.addStretch()
        bl.addLayout(ai_int_row)
        ai_int_hint = QLabel(
            "间隔按「发起时间」计算（下一次 ≥ 上一次发起 + 间隔），而非响应结束时间。"
            "设 0 表示关掉节流、完全无额外开销；设为如 2000 则每次 AI 调用至少相隔 2 秒。"
        )
        ai_int_hint.setWordWrap(True)
        ai_int_hint.setStyleSheet(hint_label_qss(c))
        bl.addWidget(ai_int_hint)

        bl.addWidget(self._section_label("工具执行超时"))
        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(10)
        timeout_lbl = QLabel("单工具超时（秒）")
        timeout_lbl.setStyleSheet(lbl_qss)
        timeout_row.addWidget(timeout_lbl)
        self._tool_timeout = QSpinBox()
        self._tool_timeout.setRange(5, 3600)
        self._tool_timeout.setSingleStep(10)
        self._tool_timeout.setFixedWidth(120)
        self._tool_timeout.setSuffix(" s")
        self._tool_timeout.setStyleSheet(spin_qss)
        self._tool_timeout.setToolTip("单个工具执行超过该时长即终止并返回失败，交由 AI 接管")
        timeout_row.addWidget(self._tool_timeout)
        timeout_row.addStretch()
        bl.addLayout(timeout_row)
        timeout_hint = QLabel(
            "execute（子进程）与联网工具各自命中该阈值即被真正终止；"
            "纯 Python 工具超时则返回失败、退出后台线程。ask_user/task（子代理）不套此超时。"
        )
        timeout_hint.setWordWrap(True)
        timeout_hint.setStyleSheet(hint_label_qss(c))
        bl.addWidget(timeout_hint)

        bl.addWidget(self._section_label("模型调用超时"))
        mt_row = QHBoxLayout()
        mt_row.setSpacing(10)
        mt_lbl = QLabel("单次模型请求（秒）")
        mt_lbl.setStyleSheet(lbl_qss)
        mt_row.addWidget(mt_lbl)
        self._model_timeout = QSpinBox()
        self._model_timeout.setRange(10, 3600)
        self._model_timeout.setSingleStep(30)
        self._model_timeout.setFixedWidth(120)
        self._model_timeout.setSuffix(" s")
        self._model_timeout.setStyleSheet(spin_qss)
        self._model_timeout.setToolTip("模型单次请求超过该时长即中止本轮，避免运行永久卡在「运行中」")
        mt_row.addWidget(self._model_timeout)
        mt_row.addStretch()
        bl.addLayout(mt_row)
        mt_hint = QLabel(
            "流式响应按「相邻字节间隔」计数，持续出 token 的正常长回复不受限；"
            "仅真正无响应的挂死连接会在这之后结束本轮。可配合「终止」让任务尽快收尾。"
        )
        mt_hint.setWordWrap(True)
        mt_hint.setStyleSheet(hint_label_qss(c))
        bl.addWidget(mt_hint)

        ds_box = QVBoxLayout()
        ds_box.setSpacing(4)
        self._enable_search = QCheckBox("启用 DeepSeek 服务端搜索工具")
        self._enable_search.setStyleSheet(checkbox_qss(c))
        self._enable_search.setToolTip("需 DeepSeek 官方 API")
        ds_box.addWidget(self._enable_search)
        ds_hint = QLabel(
            "把 DeepSeek 官方联网搜索包成工具给 Agent 用（多轮检索+引用）。"
            "需在「厂商设置」添加 DeepSeek 官方 API 才能使用；使用其他模型时也可以调用该工具。"
        )
        ds_hint.setWordWrap(True)
        ds_hint.setFrameShape(QFrame.Shape.NoFrame)
        ds_hint.setStyleSheet(hint_label_qss(c))
        ds_box.addWidget(ds_hint)
        bl.addLayout(ds_box)

        bl.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, stretch=1)

        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(28, 10, 28, 18)
        btn_bar.addStretch()
        save_btn = QPushButton("保存 WokBee 设置")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setFixedHeight(34)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; padding: 0 18px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        save_btn.clicked.connect(self._on_save)
        btn_bar.addWidget(save_btn)
        root.addLayout(btn_bar)

    def _section_label(self, text: str) -> QLabel:
        c = self.theme.colors
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {c['text']};"
            " background: transparent; border: none;"
        )
        return lbl

    def _style_input(self, edit: QLineEdit):
        edit.setStyleSheet(rounded_lineedit_qss(self.theme.colors))

    def _secondary_btn_qss(self) -> str:
        return secondary_btn_qss(self.theme.colors)

    def _browse_workspace(self):
        start = self._ws_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择 WokBee 工作区根目录", start)
        if path:
            self._ws_edit.setText(path)

    def _load(self):
        self._ws_edit.setText(str(self.settings.workspace_root))
        apply_flags_to_checks(self._approval_checks, self.settings.approval)
        self._max_steps.setValue(self.settings.max_steps)
        self._max_parallel.setValue(self.settings.max_parallel_tools)
        self._max_phases.setValue(self.settings.max_pipeline_phases)
        self._ai_interval.setValue(self.settings.ai_interval_ms)
        self._tool_timeout.setValue(self.settings.tool_timeout_seconds)
        self._model_timeout.setValue(self.settings.model_timeout_seconds)
        self._enable_search.setChecked(self.settings.enable_deepseek_search)
        self._reload_ext_dirs()
        self._reload_models()

    def _reload_ext_dirs(self):
        """刷新「已授权附加目录」列表。"""
        while self._ext_lay.count():
            item = self._ext_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        entries = self.settings.additional_directories
        if not entries:
            empty = QLabel("（未添加任何附加目录）")
            empty.setStyleSheet(
                f"font-size: 12px; color: {self.theme.colors['text_hint']};"
                "background: transparent; border: none;"
            )
            self._ext_lay.addWidget(empty)
            return
        for entry in entries:
            self._ext_lay.addWidget(self._ext_row(entry["name"], entry["path"]))

    def _ext_row(self, name: str, path: str) -> QWidget:
        c = self.theme.colors
        row = QFrame()
        row.setStyleSheet("QFrame { background: transparent; border: none; }")
        row.setFixedHeight(36)
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 2, 4, 2)
        h.setSpacing(8)
        lbl = QLabel(f"{name}  ·  {path}")
        lbl.setStyleSheet(
            f"font-size: 12px; color: {c['text']}; background: transparent; border: none;"
        )
        lbl.setToolTip(path)
        h.addWidget(lbl, stretch=1)
        rm = QPushButton("移除")
        rm.setFixedSize(56, 26)
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 5px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        rm.clicked.connect(lambda _=False, p=path: self._remove_ext_dir(p))
        h.addWidget(rm, alignment=Qt.AlignmentFlag.AlignRight)
        return row

    def _add_ext_dir(self):
        start = str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择要加入白名单的目录", start)
        if not path:
            return
        res = self.settings.add_additional_directory(Path(path).name, path)
        if res is None:
            _tip(self, self.theme, "该目录不存在或无权限，无法加入白名单。")
            return
        self._reload_ext_dirs()
        _tip(
            self,
            self.theme,
            f"已加入白名单：{res['path']}\nAgent 将可用 /ext/… 虚拟路径用文件工具访问。",
        )

    def _remove_ext_dir(self, path: str):
        if self.settings.remove_additional_directory(path):
            self._reload_ext_dirs()
        else:
            _tip(self, self.theme, "移除失败：该路径不在白名单中。")

    def _reload_models(self):
        self._model_combo.clear()
        self._model_combo.addItem("（未指定，使用厂商默认）", ("", ""))
        try:
            models = self._provider_store.list_selectable_models()
        except Exception:
            models = []
        current = (self.settings.default_provider, self.settings.default_model_id)
        select = 0
        for i, m in enumerate(models, start=1):
            label = f"{m.provider_name} / {m.model_id}"
            self._model_combo.addItem(label, (m.provider_id, m.model_id))
            if (m.provider_id, m.model_id) == current and current[1]:
                select = i
        self._model_combo.setCurrentIndex(select)

    def showEvent(self, event):
        super().showEvent(event)
        self._reload_models()

    def _on_save(self):
        root = self._ws_edit.text().strip()
        if not root:
            _tip(self, self.theme, "请先设置工作区根目录。")
            return
        try:
            Path(root).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _tip(self, self.theme, f"无法创建工作区目录：{e}")
            return

        self.settings.workspace_root = root
        self.settings.approval = flags_from_checks(self._approval_checks)
        self.settings.max_steps = self._max_steps.value()
        self.settings.max_parallel_tools = self._max_parallel.value()
        self.settings.max_pipeline_phases = self._max_phases.value()
        self.settings.ai_interval_ms = self._ai_interval.value()
        self.settings.tool_timeout_seconds = self._tool_timeout.value()
        self.settings.model_timeout_seconds = self._model_timeout.value()
        self.settings.enable_deepseek_search = self._enable_search.isChecked()
        pair = self._model_combo.currentData() or ("", "")
        self.settings.default_provider = pair[0]
        self.settings.default_model_id = pair[1]
        self.settings.save()
        _tip(self, self.theme, "WokBee 设置已保存。")
