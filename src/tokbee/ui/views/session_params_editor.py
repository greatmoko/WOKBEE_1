"""可复用的对话参数编辑表单（全局默认 / 单会话共用）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QCheckBox,
)

from tokbee.ui.styles.theme import Theme
from tokbee.ui.combo_style import (
    apply_combo_popup_style,
    rounded_spin_qss,
    checkbox_qss,
    DEFAULT_COMBO_WIDTH,
    DEFAULT_COMBO_HEIGHT,
)
from tokbee.core.session_settings import SessionSettings, ProviderOptions
from tokbee.core.ai_role import AIRoleManager


class SessionParamsEditor(QWidget):
    """编辑 SessionSettings 的表单主体（不含底部按钮）。"""

    quick_create_clicked = Signal()

    def __init__(
        self,
        theme: Theme,
        *,
        family: str = "",
        role_manager: AIRoleManager | None = None,
        show_all_options: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self._family = family or ""
        self._show_all = show_all_options
        self._role_manager = role_manager
        self._base = SessionSettings()
        self._reason_combo: QComboBox | None = None
        self._think_combo: QComboBox | None = None
        self._g_level: QComboBox | None = None
        self._g_budget: QSpinBox | None = None
        self._compat_reason: QComboBox | None = None
        self._build()

    def _style_combo(self, combo: QComboBox) -> None:
        """表单下拉：走全局圆角样式，固定 300×40，右侧实心三角。"""
        apply_combo_popup_style(
            combo,
            self.theme.colors,
            rounded=True,
            fixed_width=DEFAULT_COMBO_WIDTH,
            fixed_height=DEFAULT_COMBO_HEIGHT,
        )

    def _build(self):
        c = self.theme.colors
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._lbl = f"font-size: 13px; font-weight: bold; color: {c['text']};"
        self._hint = f"font-size: 11px; color: {c['text_hint']}; margin-bottom: 2px;"
        self._spin_style = rounded_spin_qss(c)
        self._chk_style = checkbox_qss(c)

        role_lbl = QLabel("系统角色设定")
        role_lbl.setStyleSheet(self._lbl)
        layout.addWidget(role_lbl)
        role_hint = QLabel("从已有角色选择，或手动编辑下方内容")
        role_hint.setStyleSheet(self._hint)
        layout.addWidget(role_hint)

        role_row = QHBoxLayout()
        self.role_combo = QComboBox()
        self._style_combo(self.role_combo)
        self._reload_roles()
        role_row.addWidget(self.role_combo, alignment=Qt.AlignmentFlag.AlignLeft)
        if self._role_manager is not None:
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
            quick_btn.clicked.connect(self.quick_create_clicked.emit)
            role_row.addWidget(quick_btn)
        layout.addLayout(role_row)

        self.sys_input = QTextEdit()
        self.sys_input.setFixedHeight(140)
        self.sys_input.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 6px 8px; color: {c["text"]}; font-size: 12px;
            }}
            QTextEdit:focus {{ border-color: {c["input_focus_border"]}; }}
        """)
        layout.addWidget(self.sys_input)
        self.role_combo.currentIndexChanged.connect(self._on_role_selected)

        ctx_lbl = QLabel("上下文消息数")
        ctx_lbl.setStyleSheet(self._lbl)
        layout.addWidget(ctx_lbl)
        ctx_hint = QLabel("携带最近多少条消息（soft limit）；设很大表示尽量不按条数截断")
        ctx_hint.setStyleSheet(self._hint)
        layout.addWidget(ctx_hint)
        self.hist_box = QSpinBox()
        self.hist_box.setRange(0, 100000)
        self.hist_box.setSingleStep(2)
        self.hist_box.setFixedWidth(180)
        self.hist_box.setStyleSheet(self._spin_style)
        layout.addWidget(self.hist_box, alignment=Qt.AlignmentFlag.AlignLeft)

        thr_lbl = QLabel("上下文压缩触发比例")
        thr_lbl.setStyleSheet(self._lbl)
        layout.addWidget(thr_lbl)
        thr_hint = QLabel(
            "用量超过「可用窗口 × 该比例」时，发送前先摘要旧消息（默认 60%，对齐 Chatbox）"
        )
        thr_hint.setWordWrap(True)
        thr_hint.setStyleSheet(self._hint)
        layout.addWidget(thr_hint)
        thr_row = QHBoxLayout()
        self.thr_box = QDoubleSpinBox()
        self.thr_box.setRange(0.10, 0.95)
        self.thr_box.setSingleStep(0.05)
        self.thr_box.setDecimals(2)
        self.thr_box.setSuffix("  （比例）")
        self.thr_box.setFixedWidth(180)
        self.thr_box.setStyleSheet(self._spin_style)
        thr_row.addWidget(self.thr_box)
        self.thr_pct = QLabel("")
        self.thr_pct.setStyleSheet(self._hint)
        thr_row.addWidget(self.thr_pct)
        thr_row.addStretch()
        layout.addLayout(thr_row)
        self.thr_box.valueChanged.connect(self._on_thr_changed)

        self.auto_compact_chk = QCheckBox("启用自动上下文压缩（先摘要再发）")
        self.auto_compact_chk.setStyleSheet(self._chk_style)
        layout.addWidget(self.auto_compact_chk)

        enable_tip = "勾选后随请求发送"
        params_row = QHBoxLayout()
        params_row.setSpacing(16)

        self.temp_enable = QCheckBox("启用")
        self.temp_enable.setToolTip(enable_tip)
        self.temp_enable.setStyleSheet(self._chk_style)
        self.temp_box = QDoubleSpinBox()
        self.temp_box.setRange(0.0, 2.0)
        self.temp_box.setSingleStep(0.1)
        self.temp_box.setDecimals(2)
        self.temp_box.setFixedWidth(180)
        self.temp_box.setStyleSheet(self._spin_style)
        self.temp_enable.toggled.connect(self.temp_box.setEnabled)
        temp_col = QVBoxLayout()
        tl = QLabel("Temperature")
        tl.setStyleSheet(self._lbl)
        temp_col.addWidget(tl)
        temp_col.addWidget(self.temp_enable)
        temp_col.addWidget(self.temp_box)
        params_row.addLayout(temp_col)

        self.topp_enable = QCheckBox("启用")
        self.topp_enable.setToolTip(enable_tip)
        self.topp_enable.setStyleSheet(self._chk_style)
        self.topp_box = QDoubleSpinBox()
        self.topp_box.setRange(0.0, 1.0)
        self.topp_box.setSingleStep(0.05)
        self.topp_box.setDecimals(2)
        self.topp_box.setFixedWidth(180)
        self.topp_box.setStyleSheet(self._spin_style)
        self.topp_enable.toggled.connect(self.topp_box.setEnabled)
        topp_col = QVBoxLayout()
        pl = QLabel("Top P")
        pl.setStyleSheet(self._lbl)
        topp_col.addWidget(pl)
        topp_col.addWidget(self.topp_enable)
        topp_col.addWidget(self.topp_box)
        params_row.addLayout(topp_col)
        params_row.addStretch()
        layout.addLayout(params_row)

        self.mt_enable = QCheckBox("启用")
        self.mt_enable.setToolTip(enable_tip)
        self.mt_enable.setStyleSheet(self._chk_style)
        self.max_tok_box = QSpinBox()
        self.max_tok_box.setRange(1, 256000)
        self.max_tok_box.setSingleStep(256)
        self.max_tok_box.setFixedWidth(180)
        self.max_tok_box.setStyleSheet(self._spin_style)
        self.mt_enable.toggled.connect(self.max_tok_box.setEnabled)
        mt_lbl = QLabel("Max Output Tokens")
        mt_lbl.setStyleSheet(self._lbl)
        layout.addWidget(mt_lbl)
        layout.addWidget(self.mt_enable)
        layout.addWidget(self.max_tok_box, alignment=Qt.AlignmentFlag.AlignLeft)

        self.stream_chk = QCheckBox("启用流式输出")
        self.stream_chk.setStyleSheet(self._chk_style)
        layout.addWidget(self.stream_chk)

        self._build_provider_options(layout)
        layout.addStretch()

    def _add_section(self, layout: QVBoxLayout, title: str):
        sep = QLabel(title)
        sep.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {self.theme.colors['text']}; margin-top: 8px;"
        )
        layout.addWidget(sep)

    def _build_provider_options(self, layout: QVBoxLayout):
        c = self.theme.colors
        family = self._family
        show_openai = self._show_all or family == "openai"
        show_gemini = self._show_all or family == "gemini"
        show_think = self._show_all or family in (
            "deepseek", "qwen", "glm", "kimi", "openai_compat",
        )
        # 无厂商信息时（全局默认页以外）也展示兼容思考项
        if not family and not self._show_all:
            show_think = True

        if show_openai:
            self._add_section(layout, "推理强度（OpenAI Reasoning Effort）")
            oh = QLabel("适用于 o 系列 / GPT-5 等推理模型")
            oh.setStyleSheet(self._hint)
            layout.addWidget(oh)
            self._reason_combo = QComboBox()
            self._style_combo(self._reason_combo)
            for label, val in [
                ("默认（不发送）", ""),
                ("低 low", "low"),
                ("中 medium", "medium"),
                ("高 high", "high"),
            ]:
                self._reason_combo.addItem(label, val)
            layout.addWidget(self._reason_combo, alignment=Qt.AlignmentFlag.AlignLeft)

        if show_gemini:
            self._add_section(layout, "思考配置（Gemini）")
            gl = QLabel("Thinking Level")
            gl.setStyleSheet(self._lbl)
            layout.addWidget(gl)
            self._g_level = QComboBox()
            self._style_combo(self._g_level)
            for label, val in [
                ("默认", ""),
                ("minimal", "minimal"),
                ("low", "low"),
                ("medium", "medium"),
                ("high", "high"),
            ]:
                self._g_level.addItem(label, val)
            layout.addWidget(self._g_level, alignment=Qt.AlignmentFlag.AlignLeft)
            gb = QLabel("Thinking Budget（可选，0=关闭）")
            gb.setStyleSheet(self._lbl)
            layout.addWidget(gb)
            self._g_budget = QSpinBox()
            self._g_budget.setRange(-1, 24576)
            self._g_budget.setSpecialValueText("不指定")
            self._g_budget.setFixedWidth(180)
            self._g_budget.setStyleSheet(self._spin_style)
            layout.addWidget(self._g_budget, alignment=Qt.AlignmentFlag.AlignLeft)

        if show_think:
            self._add_section(layout, "思考模式（DeepSeek / 兼容）")
            th = QLabel(
                "对应 DeepSeek：thinking.type；强度为 reasoning_effort（low/high/max）"
            )
            th.setWordWrap(True)
            th.setStyleSheet(self._hint)
            layout.addWidget(th)

            self._think_combo = QComboBox()
            self._style_combo(self._think_combo)
            for label, val in [
                ("默认（开启）", ""),
                ("开启思考", "on"),
                ("关闭思考", "off"),
            ]:
                self._think_combo.addItem(label, val)
            layout.addWidget(self._think_combo, alignment=Qt.AlignmentFlag.AlignLeft)

            el = QLabel("思考强度（reasoning_effort）")
            el.setStyleSheet(self._lbl)
            layout.addWidget(el)
            self._compat_reason = QComboBox()
            self._style_combo(self._compat_reason)
            for label, val in [
                ("默认（high）", ""),
                ("低 low", "low"),
                ("高 high", "high"),
                ("最大 max", "max"),
            ]:
                self._compat_reason.addItem(label, val)
            layout.addWidget(self._compat_reason, alignment=Qt.AlignmentFlag.AlignLeft)

            tip = QLabel("提示：DeepSeek 开启思考时 Temperature / Top P 不会生效")
            tip.setWordWrap(True)
            tip.setStyleSheet(self._hint)
            layout.addWidget(tip)

            def _sync():
                on = self._think_combo.currentData() != "off"
                self._compat_reason.setEnabled(on)

            self._think_combo.currentIndexChanged.connect(lambda _i: _sync())
            _sync()

    def _on_thr_changed(self, value: float):
        self.thr_pct.setText(f"≈ {int(round(value * 100))}%")

    def _reload_roles(self):
        self.role_combo.blockSignals(True)
        self.role_combo.clear()
        self.role_combo.addItem("— 自定义 —", "")
        if self._role_manager:
            for r in self._role_manager.list_all():
                self.role_combo.addItem(r.name, r.id)
        self.role_combo.blockSignals(False)

    def _on_role_selected(self, index: int):
        if not self._role_manager:
            return
        role_id = self.role_combo.itemData(index)
        if role_id:
            role = self._role_manager.get(role_id)
            if role:
                self.sys_input.setPlainText(role.description)

    def load(self, settings: SessionSettings):
        self._base = SessionSettings.from_dict(settings.to_dict())
        p = self._base
        self.sys_input.setPlainText(p.system_prompt)
        self.hist_box.setValue(p.max_context_message_count)
        self.thr_box.setValue(p.compaction_threshold)
        self._on_thr_changed(p.compaction_threshold)
        self.auto_compact_chk.setChecked(p.auto_compaction)
        self.stream_chk.setChecked(p.stream)

        self.temp_enable.setChecked(p.temperature is not None)
        self.temp_box.setValue(0.7 if p.temperature is None else p.temperature)
        self.temp_box.setEnabled(p.temperature is not None)

        self.topp_enable.setChecked(p.top_p is not None)
        self.topp_box.setValue(1.0 if p.top_p is None else p.top_p)
        self.topp_box.setEnabled(p.top_p is not None)

        self.mt_enable.setChecked(p.max_tokens is not None)
        self.max_tok_box.setValue(8192 if p.max_tokens is None else p.max_tokens)
        self.max_tok_box.setEnabled(p.max_tokens is not None)

        opt = p.provider_options
        if self._reason_combo is not None:
            self._reason_combo.setCurrentIndex(
                max(0, self._reason_combo.findData(opt.openai_reasoning_effort))
            )
        if self._g_level is not None:
            self._g_level.setCurrentIndex(
                max(0, self._g_level.findData(opt.google_thinking_level))
            )
        if self._g_budget is not None:
            self._g_budget.setValue(
                -1 if opt.google_thinking_budget is None else opt.google_thinking_budget
            )
        if self._think_combo is not None:
            self._think_combo.setCurrentIndex(
                max(0, self._think_combo.findData(opt.thinking_enabled))
            )
        if self._compat_reason is not None:
            effort = opt.openai_reasoning_effort
            if effort == "medium":
                effort = "high"
            idx = self._compat_reason.findData(effort)
            self._compat_reason.setCurrentIndex(idx if idx >= 0 else 0)

        self.role_combo.setCurrentIndex(0)

    def collect(self) -> SessionSettings:
        openai_effort = ""
        if self._compat_reason is not None:
            openai_effort = self._compat_reason.currentData() or ""
        if not openai_effort and self._reason_combo is not None:
            openai_effort = self._reason_combo.currentData() or ""
        # 仅有 OpenAI 区时用其值
        if self._compat_reason is None and self._reason_combo is not None:
            openai_effort = self._reason_combo.currentData() or ""

        opt = ProviderOptions(
            openai_reasoning_effort=openai_effort,
            google_thinking_level=(
                (self._g_level.currentData() or "") if self._g_level else ""
            ),
            google_thinking_budget=(
                None if (self._g_budget is None or self._g_budget.value() < 0)
                else self._g_budget.value()
            ),
            thinking_enabled=(
                (self._think_combo.currentData() or "") if self._think_combo else ""
            ),
        )
        return SessionSettings(
            provider=self._base.provider,
            model_id=self._base.model_id,
            system_prompt=self.sys_input.toPlainText().strip(),
            temperature=self.temp_box.value() if self.temp_enable.isChecked() else None,
            top_p=self.topp_box.value() if self.topp_enable.isChecked() else None,
            max_tokens=self.max_tok_box.value() if self.mt_enable.isChecked() else None,
            max_context_message_count=self.hist_box.value(),
            stream=self.stream_chk.isChecked(),
            compaction_threshold=float(self.thr_box.value()),
            auto_compaction=self.auto_compact_chk.isChecked(),
            provider_options=opt,
        )
