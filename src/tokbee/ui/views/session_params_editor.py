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
from tokbee.core.session_settings import SessionSettings
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

        layout.addStretch()

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
        self.role_combo.setCurrentIndex(0)

    def collect(self) -> SessionSettings:
        return SessionSettings(
            provider=self._base.provider,
            model_id=self._base.model_id,
            system_prompt=self.sys_input.toPlainText().strip(),
            max_context_message_count=self.hist_box.value(),
            compaction_threshold=float(self.thr_box.value()),
            auto_compaction=self.auto_compact_chk.isChecked(),
        )
