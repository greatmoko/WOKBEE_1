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

from wokbee.core.models import ApprovalFlags
from wokbee.core.settings import WokBeeSettings


def _tip(parent: QWidget, theme: Theme, message: str):
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("提示")
    dlg.setFixedSize(380, 150)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
    layout.addWidget(msg)
    layout.addStretch()
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


def build_approval_checkboxes(
    theme: Theme,
    parent: QWidget | None = None,
) -> tuple[QWidget, dict[str, QCheckBox]]:
    """创建四个审核勾选控件，返回容器与 checkbox 字典。"""
    c = theme.colors
    box = QWidget(parent)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)

    defs = [
        ("skip_read", "读免审", "读操作免审"),
        ("skip_write", "写免审", "写操作免审"),
        ("skip_routine", "常规操作免审", "常规操作免审"),
        ("skip_high_risk", "高危操作免审", "高危操作免审"),
    ]
    checks: dict[str, QCheckBox] = {}
    cb_qss = checkbox_qss(c)
    for key, label, tip in defs:
        cb = QCheckBox(label)
        cb.setToolTip(tip)
        cb.setStyleSheet(cb_qss)
        lay.addWidget(cb)
        checks[key] = cb

    return box, checks


def apply_flags_to_checks(checks: dict[str, QCheckBox], flags: ApprovalFlags) -> None:
    checks["skip_read"].setChecked(flags.skip_read)
    checks["skip_write"].setChecked(flags.skip_write)
    checks["skip_routine"].setChecked(flags.skip_routine)
    checks["skip_high_risk"].setChecked(flags.skip_high_risk)


def flags_from_checks(checks: dict[str, QCheckBox]) -> ApprovalFlags:
    return ApprovalFlags(
        skip_read=checks["skip_read"].isChecked(),
        skip_write=checks["skip_write"].isChecked(),
        skip_routine=checks["skip_routine"].isChecked(),
        skip_high_risk=checks["skip_high_risk"].isChecked(),
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
        self._enable_search.setChecked(self.settings.enable_deepseek_search)
        self._reload_models()

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
        self.settings.enable_deepseek_search = self._enable_search.isChecked()
        pair = self._model_combo.currentData() or ("", "")
        self.settings.default_provider = pair[0]
        self.settings.default_model_id = pair[1]
        self.settings.save()
        _tip(self, self.theme, "WokBee 设置已保存。")
