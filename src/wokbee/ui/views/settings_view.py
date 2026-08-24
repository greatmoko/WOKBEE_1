"""设置页面 — 左侧二级导航 + 右侧工作区。"""

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame,
    QLabel, QPushButton, QStackedWidget, QScrollArea,
    QLineEdit, QDialog, QCheckBox, QComboBox, QTextEdit,
    QGridLayout, QSlider, QSpinBox, QDoubleSpinBox,
)

from wokbee.ui.styles.theme import Theme
from wokbee.core.model_config import ModelConfigManager, ModelEntry
from wokbee.core.model_presets import PROVIDER_PRESETS, DEFAULT_PARAMS, ProviderPreset
from wokbee.core.chat_manager import ChatManager


def _show_styled_tip(parent: QWidget, theme: "Theme", message: str):
    """统一的提示弹窗，替代 QMessageBox。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("提示")
    dlg.setFixedSize(340, 140)
    dlg.setStyleSheet(f"background: {c['content_bg']};")

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(16)

    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
    layout.addWidget(msg)

    layout.addStretch()

    btn_row = QHBoxLayout()
    btn_row.addStretch()
    ok_btn = QPushButton("知道了")
    ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    ok_btn.setFixedSize(80, 34)
    ok_btn.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    ok_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(ok_btn)
    layout.addLayout(btn_row)

    dlg.exec()


# ═══════════════════════════════════════════════════════════════
# 二级导航按钮
# ═══════════════════════════════════════════════════════════════

class _SubNavButton(QPushButton):
    """二级导航按钮：左侧图标 + 右侧文字。"""

    def __init__(self, icon_text: str, label: str, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._active = False
        self._icon_text = icon_text
        self._label = label
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(36)
        self.setText(f"  {icon_text}   {label}")
        self._apply_style()

    def _apply_style(self):
        c = self._theme.colors
        if self._active:
            bg = c["subnav_active"]
            tc = c["subnav_text_active"]
            fw = "bold"
        else:
            bg = "transparent"
            tc = c["subnav_text"]
            fw = "normal"
        self.setStyleSheet(f"""
            QPushButton {{
                background: {bg};
                color: {tc};
                font-weight: {fw};
                border: none;
                border-radius: 6px;
                padding: 0 12px;
                text-align: left;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {c["subnav_hover"]};
            }}
        """)

    def set_active(self, active: bool):
        self._active = active
        self._apply_style()


# ═══════════════════════════════════════════════════════════════
# 二级导航面板
# ═══════════════════════════════════════════════════════════════

class _SubNav(QFrame):
    """设置页的二级导航栏。"""

    nav_changed = Signal(str)

    ITEMS = [
        ("general", "⚙", "通用设置"),
    ]

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._buttons: dict[str, _SubNavButton] = {}
        self._current = ""
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setMinimumWidth(180)
        self.setMaximumWidth(220)
        self.setStyleSheet(f"""
            _SubNav {{
                background: {c["subnav_bg"]};
                border-right: 1px solid {c["border"]};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(2)

        for nav_id, icon, label in self.ITEMS:
            btn = _SubNavButton(icon, label, self.theme)
            btn.clicked.connect(lambda _, nid=nav_id: self._on_click(nid))
            layout.addWidget(btn)
            self._buttons[nav_id] = btn

        layout.addStretch()

    def _on_click(self, nav_id: str):
        if nav_id == self._current:
            return
        self._current = nav_id
        for nid, btn in self._buttons.items():
            btn.set_active(nid == nav_id)
        self.nav_changed.emit(nav_id)

    def select(self, nav_id: str):
        self._on_click(nav_id)


# ═══════════════════════════════════════════════════════════════
# 模型卡片
# ═══════════════════════════════════════════════════════════════

class _ModelCard(QFrame):
    """单个模型配置卡片。"""

    action_triggered = Signal(str, str)  # (action, model_id)

    def __init__(self, entry: ModelEntry, manager: ModelConfigManager,
                 theme: Theme, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.manager = manager
        self.theme = theme
        self._build()

    def _build(self):
        c = self.theme.colors
        e = self.entry

        self.setStyleSheet(f"""
            _ModelCard {{
                background: {c["card_bg"]};
                border-radius: 10px;
                border: 1px solid {c["border_light"]};
            }}
            _ModelCard:hover {{
                background: {c["card_hover"]};
                border-color: {c["border"]};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # ── 顶部行：提供商 + 模型名 + 操作按钮 ──
        top = QHBoxLayout()
        top.setSpacing(6)

        if e.is_primary:
            star = QLabel("⭐")
            star.setToolTip("主模型")
            star.setStyleSheet("font-size: 14px;")
            top.addWidget(star)

        provider_label = QLabel(e.provider)
        provider_label.setStyleSheet(f"""
            font-size: 14px; font-weight: bold; color: {c["text"]};
        """)
        top.addWidget(provider_label)

        name_label = QLabel(e.model_name)
        name_label.setStyleSheet(f"""
            font-size: 14px; font-weight: bold; color: {c["text_secondary"]};
        """)
        top.addWidget(name_label)
        top.addStretch()

        for text, action, color in [
            ("设为主模型" if not e.is_primary else "", "primary", c["accent"]),
            ("编辑", "edit", c["text_secondary"]),
            ("复制", "duplicate", c["text_secondary"]),
            ("删除", "delete", c["danger"]),
        ]:
            if not text:
                continue
            btn = QPushButton(text)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent;
                    color: {color};
                    border: none;
                    padding: 4px 10px;
                    border-radius: 4px;
                    font-size: 12px;
                }}
                QPushButton:hover {{
                    background: {c["btn_bg"]};
                }}
            """)
            btn.clicked.connect(
                lambda _, a=action, mid=e.id: self.action_triggered.emit(a, mid)
            )
            top.addWidget(btn)

        layout.addLayout(top)

        # ── 详情行 ──
        detail = QHBoxLayout()
        detail.setSpacing(20)

        for label, value in [
            ("地址", e.endpoint),
            ("Key", self.manager.mask_key(e.api_key)),
        ]:
            item = QHBoxLayout()
            item.setSpacing(4)
            lbl = QLabel(f"{label}:")
            lbl.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
            val = QLabel(value)
            val.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
            val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            item.addWidget(lbl)
            item.addWidget(val)
            detail.addLayout(item)

        if e.remark:
            item = QHBoxLayout()
            item.setSpacing(4)
            lbl = QLabel("备注:")
            lbl.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
            val = QLabel(e.remark)
            val.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
            item.addWidget(lbl)
            item.addWidget(val)
            detail.addLayout(item)

        detail.addStretch()
        layout.addLayout(detail)

        # ── 高级参数摘要（仅非默认值时显示） ──
        params_parts: list[str] = []
        if e.reasoning_effort in ("low", "high", "max"):
            params_parts.append(f"think={e.reasoning_effort}")
        elif e.disable_thinking:
            params_parts.append("no_think")
        if e.frequency_penalty != 0.0:
            params_parts.append(f"freq={e.frequency_penalty:.1f}")
        if e.presence_penalty != 0.0:
            params_parts.append(f"pres={e.presence_penalty:.1f}")
        if e.top_k > 0:
            params_parts.append(f"top_k={e.top_k}")
        if e.context_window > 0:
            params_parts.append(f"ctx={e.context_window}")

        if params_parts:
            params_label = QLabel("  ".join(params_parts))
            params_label.setStyleSheet(
                f"font-size: 11px; color: {c['text_hint']}; "
                f"background: {c['card_bg']}; padding: 2px 0;"
            )
            layout.addWidget(params_label)


# ═══════════════════════════════════════════════════════════════
# 模型编辑对话框
# ═══════════════════════════════════════════════════════════════

class _ModelEditDialog(QDialog):
    """添加 / 编辑模型的弹窗（含高级参数）。"""

    FIELDS = [
        ("provider", "AI 提供商", "例如：OpenRouter / DeepSeek"),
        ("model_name", "模型名称", "例如：deepseek-chat"),
        ("endpoint", "调用地址", "例如：https://api.deepseek.com/v1"),
        ("api_key", "API Key", "输入 API Key"),
        ("remark", "备注", "可选备注信息"),
    ]

    def __init__(self, theme: Theme, manager: ModelConfigManager,
                 entry: ModelEntry | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.manager = manager
        self.entry = entry
        self._inputs: dict[str, QLineEdit] = {}
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setWindowTitle("编辑模型" if self.entry else "添加模型")
        self.setFixedSize(520, 700)
        self.setStyleSheet(f"background: {c['content_bg']};")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 20)
        layout.setSpacing(0)

        title = QLabel("编辑模型" if self.entry else "添加模型")
        title.setStyleSheet(f"""
            font-size: 18px; font-weight: bold;
            color: {c["text"]}; margin-bottom: 16px;
        """)
        layout.addWidget(title)

        for field_id, label, placeholder in self.FIELDS:
            lbl = QLabel(label)
            lbl.setStyleSheet(f"""
                font-size: 13px; color: {c["text"]};
                margin-top: 10px; margin-bottom: 4px;
            """)
            layout.addWidget(lbl)

            inp = QLineEdit()
            inp.setPlaceholderText(placeholder)
            inp.setFixedHeight(36)
            if self.entry:
                inp.setText(getattr(self.entry, field_id, ""))
            self._inputs[field_id] = inp
            layout.addWidget(inp)

        # ── 思考模式设置 ──
        layout.addSpacing(12)
        think_lbl = QLabel("思考模式")
        think_lbl.setStyleSheet(f"font-size: 13px; color: {c['text']}; margin-bottom: 4px;")
        layout.addWidget(think_lbl)

        self._thinking_combo = QComboBox()
        self._thinking_combo.setFixedHeight(34)
        self._thinking_combo.setStyleSheet(f"""
            QComboBox {{
                background: {c["input_bg"]};
                border: 1px solid {c["input_border"]};
                border-radius: 6px;
                padding: 0 10px;
                color: {c["text"]};
                font-size: 13px;
            }}
            QComboBox:focus {{ border-color: {c["input_focus_border"]}; }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
            QComboBox::down-arrow {{ image: none; }}
            QComboBox QAbstractItemView {{
                background: {c["content_bg"]};
                border: 1px solid {c["border"]};
                selection-background-color: {c["subnav_hover"]};
                color: {c["text"]};
                font-size: 13px;
            }}
        """)
        self._thinking_combo.addItem("默认（不控制思考）", "default")
        self._thinking_combo.addItem("禁用思考", "disabled")
        self._thinking_combo.addItem("启用思考 — 低深度 (low)", "low")
        self._thinking_combo.addItem("启用思考 — 高深度 (high)", "high")
        self._thinking_combo.addItem("启用思考 — 最大深度 (max)", "max")
        self._thinking_combo.setToolTip(
            "DeepSeek 等模型支持思考模式控制：\n"
            "• 默认 — 不发送任何思考参数\n"
            "• 禁用 — 显式关闭思考\n"
            "• low/high/max — 启用思考并设置推理深度"
        )

        if self.entry:
            if self.entry.reasoning_effort in ("low", "high", "max"):
                idx = self._thinking_combo.findData(self.entry.reasoning_effort)
            elif self.entry.disable_thinking:
                idx = self._thinking_combo.findData("disabled")
            else:
                idx = 0
            self._thinking_combo.setCurrentIndex(max(idx, 0))

        layout.addWidget(self._thinking_combo)

        # ── 高级参数折叠区 ──
        layout.addSpacing(16)
        self._adv_toggle = QPushButton("▶ 高级参数")
        self._adv_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._adv_toggle.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: none; text-align: left; font-size: 13px;
                font-weight: bold; padding: 4px 0;
            }}
            QPushButton:hover {{ color: {c["text"]}; }}
        """)
        self._adv_toggle.clicked.connect(self._toggle_advanced)
        layout.addWidget(self._adv_toggle)

        self._adv_frame = QFrame()
        self._adv_frame.setVisible(False)
        adv_layout = QVBoxLayout(self._adv_frame)
        adv_layout.setContentsMargins(0, 8, 0, 0)
        adv_layout.setSpacing(10)

        self._freq_penalty = self._make_float_slider(
            adv_layout, "frequency_penalty", -2.0, 2.0,
            self.entry.frequency_penalty if self.entry else 0.0,
        )
        self._pres_penalty = self._make_float_slider(
            adv_layout, "presence_penalty", -2.0, 2.0,
            self.entry.presence_penalty if self.entry else 0.0,
        )

        # top_k
        topk_row = QHBoxLayout()
        topk_row.setSpacing(8)
        topk_lbl = QLabel("top_k (0=不启用)")
        topk_lbl.setStyleSheet(f"font-size: 12px; color: {c['text']};")
        topk_row.addWidget(topk_lbl)
        self._topk_spin = QSpinBox()
        self._topk_spin.setRange(0, 500)
        self._topk_spin.setValue(self.entry.top_k if self.entry else 0)
        self._topk_spin.setFixedSize(80, 30)
        self._topk_spin.setStyleSheet(f"""
            QSpinBox {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 4px; padding: 0 6px; color: {c["text"]}; font-size: 12px;
            }}
        """)
        topk_row.addWidget(self._topk_spin)
        topk_row.addStretch()
        adv_layout.addLayout(topk_row)

        # context_window
        ctx_row = QHBoxLayout()
        ctx_row.setSpacing(8)
        ctx_lbl = QLabel("上下文窗口 (参考值)")
        ctx_lbl.setStyleSheet(f"font-size: 12px; color: {c['text']};")
        ctx_row.addWidget(ctx_lbl)
        self._ctx_spin = QSpinBox()
        self._ctx_spin.setRange(0, 2000000)
        self._ctx_spin.setSingleStep(1024)
        self._ctx_spin.setValue(self.entry.context_window if self.entry else 0)
        self._ctx_spin.setFixedSize(100, 30)
        self._ctx_spin.setStyleSheet(f"""
            QSpinBox {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 4px; padding: 0 6px; color: {c["text"]}; font-size: 12px;
            }}
        """)
        ctx_row.addWidget(self._ctx_spin)
        ctx_row.addStretch()
        adv_layout.addLayout(ctx_row)

        # stop_sequences
        stop_lbl = QLabel("停止序列 (每行一个)")
        stop_lbl.setStyleSheet(f"font-size: 12px; color: {c['text']};")
        adv_layout.addWidget(stop_lbl)
        self._stop_edit = QTextEdit()
        self._stop_edit.setFixedHeight(60)
        self._stop_edit.setPlaceholderText("每行一个停止序列，留空则不设置")
        self._stop_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 6px; color: {c["text"]}; font-size: 12px;
            }}
        """)
        if self.entry and self.entry.stop_sequences:
            self._stop_edit.setPlainText(
                self.entry.stop_sequences.replace(",", "\n")
            )
        adv_layout.addWidget(self._stop_edit)

        # 重置按钮
        reset_btn = QPushButton("重置为默认值")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setFixedSize(110, 30)
        reset_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text_secondary"]};
                border: none; border-radius: 6px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        reset_btn.clicked.connect(self._reset_defaults)
        adv_layout.addWidget(reset_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        layout.addWidget(self._adv_frame)

        layout.addStretch()

        # ── 底部按钮 ──
        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch()

        cancel_btn = QPushButton("取  消")
        cancel_btn.setFixedSize(100, 36)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("保  存")
        save_btn.setFixedSize(100, 36)
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(save_btn)

        layout.addLayout(btn_row)
        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _make_float_slider(self, parent_layout, name: str, min_v: float, max_v: float,
                           current: float) -> QDoubleSpinBox:
        c = self.theme.colors
        row = QHBoxLayout()
        row.setSpacing(8)
        lbl = QLabel(name)
        lbl.setStyleSheet(f"font-size: 12px; color: {c['text']};")
        lbl.setFixedWidth(130)
        row.addWidget(lbl)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(int(min_v * 100), int(max_v * 100))
        slider.setValue(int(current * 100))
        slider.setFixedHeight(20)
        row.addWidget(slider, stretch=1)

        spin = QDoubleSpinBox()
        spin.setRange(min_v, max_v)
        spin.setSingleStep(0.1)
        spin.setDecimals(2)
        spin.setValue(current)
        spin.setFixedSize(70, 28)
        spin.setStyleSheet(f"""
            QDoubleSpinBox {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 4px; padding: 0 4px; color: {c["text"]}; font-size: 12px;
            }}
        """)
        row.addWidget(spin)

        slider.valueChanged.connect(lambda v: spin.setValue(v / 100.0))
        spin.valueChanged.connect(lambda v: slider.setValue(int(v * 100)))

        parent_layout.addLayout(row)
        return spin

    def _toggle_advanced(self):
        visible = not self._adv_frame.isVisible()
        self._adv_frame.setVisible(visible)
        self._adv_toggle.setText("▼ 高级参数" if visible else "▶ 高级参数")

    def _reset_defaults(self):
        self._freq_penalty.setValue(DEFAULT_PARAMS["frequency_penalty"])
        self._pres_penalty.setValue(DEFAULT_PARAMS["presence_penalty"])
        self._topk_spin.setValue(DEFAULT_PARAMS["top_k"])
        self._ctx_spin.setValue(DEFAULT_PARAMS["context_window"])
        self._stop_edit.clear()
        self._thinking_combo.setCurrentIndex(0)

    def _show_tip(self, message: str):
        _show_styled_tip(self, self.theme, message)

    def _on_save(self):
        vals = {fid: self._inputs[fid].text().strip() for fid, _, _ in self.FIELDS}

        required = ["provider", "model_name", "endpoint", "api_key"]
        missing = [fid for fid in required if not vals[fid]]
        if missing:
            self._show_tip("提供商、模型名称、调用地址和 API Key 为必填项")
            return

        thinking_val = self._thinking_combo.currentData()
        if thinking_val == "disabled":
            vals["disable_thinking"] = True
            vals["reasoning_effort"] = ""
        elif thinking_val in ("low", "high", "max"):
            vals["disable_thinking"] = False
            vals["reasoning_effort"] = thinking_val
        else:
            vals["disable_thinking"] = False
            vals["reasoning_effort"] = ""

        vals["frequency_penalty"] = self._freq_penalty.value()
        vals["presence_penalty"] = self._pres_penalty.value()
        vals["top_k"] = self._topk_spin.value()
        vals["context_window"] = self._ctx_spin.value()

        raw_stop = self._stop_edit.toPlainText().strip()
        vals["stop_sequences"] = ",".join(
            line.strip() for line in raw_stop.splitlines() if line.strip()
        )

        if self.entry:
            self.manager.update(self.entry.id, **vals)
        else:
            self.manager.add(ModelEntry(**vals))

        self.accept()


# ═══════════════════════════════════════════════════════════════
# 快速添加对话框（预设提供商选择）
# ═══════════════════════════════════════════════════════════════

class _QuickAddDialog(QDialog):
    """快速添加预设提供商 — 选择预设后仅需填入 API Key。"""

    _query_done_signal = Signal()

    def __init__(self, theme: Theme, manager: ModelConfigManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.manager = manager
        self._selected_preset: ProviderPreset | None = None
        self._pending_models: list[str] = []
        self._pending_error: str | None = None
        self._model_items: list[_QuickModelItem] = []
        self._query_done_signal.connect(self._on_query_done_main)
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setWindowTitle("快速添加模型")
        self.setFixedSize(600, 560)
        self.setStyleSheet(f"background: {c['content_bg']};")

        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(24, 20, 24, 16)
        self._outer_layout.setSpacing(0)

        self._page_stack = QStackedWidget()
        self._outer_layout.addWidget(self._page_stack)

        self._build_preset_page()
        self._build_form_page()
        self._page_stack.setCurrentIndex(0)

    def _build_preset_page(self):
        c = self.theme.colors
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("选择 AI 服务商")
        title.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {c['text']};")
        layout.addWidget(title)

        hint = QLabel("选择预设服务商，快速完成配置")
        hint.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; margin-bottom: 8px;")
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(10)

        for idx, preset in enumerate(PROVIDER_PRESETS):
            card = QPushButton()
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setFixedHeight(72)
            card.setStyleSheet(f"""
                QPushButton {{
                    background: {c["card_bg"]};
                    border: 1px solid {c["border_light"]};
                    border-radius: 10px;
                    text-align: left;
                    padding: 12px 14px;
                }}
                QPushButton:hover {{
                    background: {c["card_hover"]};
                    border-color: {c["border"]};
                }}
            """)

            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 6, 10, 6)
            card_layout.setSpacing(2)

            top_line = QLabel(f"{preset.icon}  {preset.name}")
            top_line.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {c['text']}; background: transparent;")
            top_line.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            card_layout.addWidget(top_line)

            desc = QLabel(preset.notes)
            desc.setStyleSheet(f"font-size: 11px; color: {c['text_hint']}; background: transparent;")
            desc.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            card_layout.addWidget(desc)

            card.clicked.connect(lambda _, p=preset: self._select_preset(p))
            row, col = divmod(idx, 3)
            grid.addWidget(card, row, col)

        layout.addLayout(grid)
        layout.addStretch()
        self._page_stack.addWidget(page)

    def _build_form_page(self):
        c = self.theme.colors
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 返回按钮 + 标题
        top_row = QHBoxLayout()
        back_btn = QPushButton("← 返回")
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: none; font-size: 13px;
            }}
            QPushButton:hover {{ color: {c["text"]}; }}
        """)
        back_btn.clicked.connect(lambda: self._page_stack.setCurrentIndex(0))
        top_row.addWidget(back_btn)
        top_row.addStretch()
        layout.addLayout(top_row)

        self._form_title = QLabel()
        self._form_title.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {c['text']};")
        layout.addWidget(self._form_title)

        # endpoint (只读，预填)
        lbl_ep = QLabel("调用地址")
        lbl_ep.setStyleSheet(f"font-size: 12px; color: {c['text']}; margin-top: 6px;")
        layout.addWidget(lbl_ep)
        self._form_endpoint = QLineEdit()
        self._form_endpoint.setFixedHeight(34)
        self._form_endpoint.setReadOnly(True)
        self._form_endpoint.setStyleSheet(f"""
            QLineEdit {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 0 10px; color: {c["text_secondary"]}; font-size: 12px;
            }}
        """)
        layout.addWidget(self._form_endpoint)

        # API Key
        lbl_key = QLabel("API Key")
        lbl_key.setStyleSheet(f"font-size: 12px; color: {c['text']}; margin-top: 6px;")
        layout.addWidget(lbl_key)
        self._form_key = QLineEdit()
        self._form_key.setPlaceholderText("输入 API Key")
        self._form_key.setFixedHeight(34)
        layout.addWidget(self._form_key)

        # 模型列表区域
        model_header = QHBoxLayout()
        model_header.setSpacing(8)
        self._model_lbl = QLabel("选择模型")
        self._model_lbl.setStyleSheet(f"font-size: 12px; color: {c['text']}; margin-top: 8px;")
        model_header.addWidget(self._model_lbl)
        model_header.addStretch()

        self._query_btn = QPushButton("🔍 查询可用模型")
        self._query_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._query_btn.setFixedHeight(28)
        self._query_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 4px; padding: 0 10px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        self._query_btn.clicked.connect(self._on_query_models)
        model_header.addWidget(self._query_btn)
        layout.addLayout(model_header)

        # 模型列表 scroll
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.setMinimumHeight(160)

        self._model_list_container = QWidget()
        self._model_list_container.setStyleSheet("background: transparent;")
        self._model_list_layout = QVBoxLayout(self._model_list_container)
        self._model_list_layout.setContentsMargins(0, 0, 0, 0)
        self._model_list_layout.setSpacing(2)
        self._model_list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        scroll.setWidget(self._model_list_container)
        layout.addWidget(scroll, stretch=1)

        # 底部确认
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(80, 34)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._confirm_btn = QPushButton("添加选中")
        self._confirm_btn.setFixedSize(100, 34)
        self._confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        self._confirm_btn.clicked.connect(self._on_confirm)
        btn_row.addWidget(self._confirm_btn)
        layout.addLayout(btn_row)

        self._page_stack.addWidget(page)

    def _select_preset(self, preset: ProviderPreset):
        self._selected_preset = preset
        c = self.theme.colors
        self._form_title.setText(f"{preset.icon} {preset.name}")
        self._form_endpoint.setText(preset.endpoint)
        self._form_key.clear()

        self._clear_model_list()
        self._model_items.clear()

        if preset.default_models:
            existing_names = {m.model_name for m in self.manager.list_all()}
            for model_id in preset.default_models:
                already = model_id in existing_names
                item = _QuickModelItem(model_id, already, self.theme)
                item.set_checked(not already)
                self._model_items.append(item)
                self._model_list_layout.addWidget(item)
        else:
            hint = QLabel("此服务商无预设模型，请点击「查询可用模型」获取列表")
            hint.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 20px 0;")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._model_list_layout.addWidget(hint)

        self._page_stack.setCurrentIndex(1)

    def _clear_model_list(self):
        while self._model_list_layout.count():
            w = self._model_list_layout.takeAt(0)
            if w.widget():
                w.widget().deleteLater()

    def _on_query_models(self):
        api_key = self._form_key.text().strip()
        if not api_key:
            _show_styled_tip(self, self.theme, "请先填入 API Key")
            return
        endpoint = self._form_endpoint.text().strip()
        self._clear_model_list()
        self._model_items.clear()
        c = self.theme.colors
        loading = QLabel("正在查询...")
        loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 20px 0;")
        self._model_list_layout.addWidget(loading)

        def _worker():
            try:
                models = self.manager.fetch_remote_models(endpoint, api_key)
                self._pending_models = models
                self._pending_error = None
            except Exception as e:
                self._pending_models = []
                self._pending_error = str(e)
            self._query_done_signal.emit()

        threading.Thread(target=_worker, daemon=True).start()

    def _on_query_done_main(self):
        c = self.theme.colors
        self._clear_model_list()
        self._model_items.clear()

        if self._pending_error:
            err = QLabel(f"查询失败: {self._pending_error}")
            err.setWordWrap(True)
            err.setStyleSheet(f"font-size: 12px; color: {c['danger']}; padding: 16px 0;")
            err.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._model_list_layout.addWidget(err)
            return

        if not self._pending_models:
            empty = QLabel("未查询到可用模型")
            empty.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 20px 0;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._model_list_layout.addWidget(empty)
            return

        existing_names = {m.model_name for m in self.manager.list_all()}
        for model_id in self._pending_models:
            already = model_id in existing_names
            item = _QuickModelItem(model_id, already, self.theme)
            self._model_items.append(item)
            self._model_list_layout.addWidget(item)

    def _on_confirm(self):
        if not self._selected_preset:
            return
        api_key = self._form_key.text().strip()
        if not api_key:
            _show_styled_tip(self, self.theme, "请填入 API Key")
            return

        selected = [it.model_id for it in self._model_items if it.is_checked()]
        if not selected:
            _show_styled_tip(self, self.theme, "请至少选择一个模型")
            return

        preset = self._selected_preset
        for model_name in selected:
            entry = ModelEntry(
                provider=preset.name,
                model_name=model_name,
                endpoint=preset.endpoint,
                api_key=api_key,
            )
            self.manager.add(entry)
        self.accept()


class _QuickModelItem(QFrame):
    """快速添加中的模型勾选项。"""

    def __init__(self, model_id: str, already_added: bool, theme: Theme, parent=None):
        super().__init__(parent)
        c = theme.colors
        self.model_id = model_id

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(8)

        self.checkbox = QCheckBox()
        self.checkbox.setStyleSheet(f"""
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {c["input_border"]};
                border-radius: 3px; background: {c["input_bg"]};
            }}
            QCheckBox::indicator:checked {{
                background: {c["accent"]};
                border-color: {c["accent"]};
            }}
        """)
        layout.addWidget(self.checkbox)

        label_text = f"{model_id}  (已添加)" if already_added else model_id
        label_color = c["text_hint"] if already_added else c["text"]
        label = QLabel(label_text)
        label.setStyleSheet(f"font-size: 12px; color: {label_color};")
        layout.addWidget(label, stretch=1)

        self.setStyleSheet(f"""
            _QuickModelItem {{
                background: transparent; border-radius: 4px;
            }}
            _QuickModelItem:hover {{ background: {c["subnav_hover"]}; }}
        """)

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    def set_checked(self, val: bool):
        self.checkbox.setChecked(val)


# ═══════════════════════════════════════════════════════════════
# 批量添加对话框
# ═══════════════════════════════════════════════════════════════

class _ModelCheckItem(QFrame):
    """模型列表中的单个勾选项。"""

    def __init__(self, model_id: str, already_added: bool, theme, parent=None):
        super().__init__(parent)
        c = theme.colors
        self.model_id = model_id
        self.already_added = already_added

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 6, 3)
        layout.setSpacing(8)

        self.checkbox = QCheckBox()
        self.checkbox.setStyleSheet(f"""
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {c["input_border"]};
                border-radius: 3px; background: {c["input_bg"]};
            }}
            QCheckBox::indicator:checked {{
                background: {c["accent"]};
                border-color: {c["accent"]};
            }}
        """)
        layout.addWidget(self.checkbox)

        label_text = f"{model_id}  (已添加)" if already_added else model_id
        label_color = c["text_hint"] if already_added else c["text"]
        label = QLabel(label_text)
        label.setStyleSheet(f"font-size: 12px; color: {label_color};")
        layout.addWidget(label, stretch=1)

        self.setStyleSheet(f"""
            _ModelCheckItem {{
                background: transparent;
                border-radius: 4px;
            }}
            _ModelCheckItem:hover {{
                background: {c["subnav_hover"]};
            }}
        """)

    def is_checked(self) -> bool:
        return self.checkbox.isChecked()

    def set_checked(self, val: bool):
        self.checkbox.setChecked(val)


class _BatchAddDialog(QDialog):
    """批量添加模型弹窗。"""

    _query_done_signal = Signal()

    def __init__(self, theme: Theme, manager: ModelConfigManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.manager = manager
        self._inputs: dict[str, QLineEdit] = {}
        self._model_items: list[_ModelCheckItem] = []
        self._all_model_ids: list[str] = []
        self._pending_models: list[str] = []
        self._pending_error: str | None = None
        self._providers = manager.get_unique_providers()
        self._query_done_signal.connect(self._on_query_done_main_thread)
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setWindowTitle("批量添加模型")
        self.setFixedSize(560, 620)
        self.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(0)

        # ── 标题 ──
        title = QLabel("批量添加模型")
        title.setStyleSheet(f"""
            font-size: 18px; font-weight: bold;
            color: {c["text"]}; margin-bottom: 12px;
        """)
        layout.addWidget(title)

        # ── 选择现有提供商 ──
        if self._providers:
            provider_row = QHBoxLayout()
            provider_row.setSpacing(8)

            lbl = QLabel("复制已有配置:")
            lbl.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
            provider_row.addWidget(lbl)

            self._provider_combo = QComboBox()
            self._provider_combo.setFixedHeight(30)
            self._provider_combo.setStyleSheet(f"""
                QComboBox {{
                    background: {c["input_bg"]};
                    border: 1px solid {c["input_border"]};
                    border-radius: 6px;
                    padding: 0 10px;
                    color: {c["text"]};
                    font-size: 12px;
                }}
                QComboBox:focus {{ border-color: {c["input_focus_border"]}; }}
                QComboBox::drop-down {{
                    border: none; width: 24px;
                }}
                QComboBox::down-arrow {{ image: none; }}
                QComboBox QAbstractItemView {{
                    background: {c["content_bg"]};
                    border: 1px solid {c["border"]};
                    selection-background-color: {c["subnav_hover"]};
                    color: {c["text"]};
                    font-size: 12px;
                }}
            """)
            self._provider_combo.addItem("不使用")
            for p, e, _ in self._providers:
                self._provider_combo.addItem(f"{p} ({e})")
            self._provider_combo.currentIndexChanged.connect(self._on_provider_change)
            provider_row.addWidget(self._provider_combo, stretch=1)

            layout.addLayout(provider_row)
            layout.addSpacing(8)

        # ── 表单字段 ──
        fields = [
            ("provider", "AI 提供商", "例如：OpenRouter"),
            ("endpoint", "调用地址", "例如：https://api.deepseek.com/v1"),
            ("api_key", "API Key", "输入 API Key"),
        ]
        for field_id, label, placeholder in fields:
            lbl = QLabel(label)
            lbl.setStyleSheet(f"font-size: 13px; color: {c['text']}; margin-top: 8px; margin-bottom: 3px;")
            layout.addWidget(lbl)

            inp = QLineEdit()
            inp.setPlaceholderText(placeholder)
            inp.setFixedHeight(34)
            if field_id == "api_key":
                pass  # API Key 明文显示
            self._inputs[field_id] = inp
            layout.addWidget(inp)

        if self._providers:
            self._on_provider_change(0)

        layout.addSpacing(8)

        # ── 模型列表区域 ──
        self._result_frame = QFrame()
        self._result_frame.setStyleSheet(f"""
            QFrame {{
                background: {c["card_bg"]};
                border: 1px solid {c["border_light"]};
                border-radius: 8px;
            }}
        """)
        result_layout = QVBoxLayout(self._result_frame)
        result_layout.setContentsMargins(10, 8, 10, 8)
        result_layout.setSpacing(4)

        # 顶部：全选 + 搜索 + 计数
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self._select_all_cb = QCheckBox("全选")
        self._select_all_cb.setStyleSheet(f"""
            QCheckBox {{ color: {c["text"]}; font-size: 12px; }}
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {c["input_border"]};
                border-radius: 3px; background: {c["input_bg"]};
            }}
            QCheckBox::indicator:checked {{
                background: {c["accent"]};
                border-color: {c["accent"]};
            }}
        """)
        self._select_all_cb.toggled.connect(self._on_select_all)
        top_bar.addWidget(self._select_all_cb)

        self._count_label = QLabel("")
        self._count_label.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        top_bar.addWidget(self._count_label)

        top_bar.addStretch()

        self._filter_input = QLineEdit()
        self._filter_input.setPlaceholderText("筛选模型...")
        self._filter_input.setFixedSize(160, 26)
        self._filter_input.setStyleSheet(f"""
            QLineEdit {{
                background: {c["content_bg"]};
                border: 1px solid {c["input_border"]};
                border-radius: 4px;
                padding: 0 8px;
                color: {c["text"]};
                font-size: 12px;
            }}
        """)
        self._filter_input.textChanged.connect(self._apply_filter)
        top_bar.addWidget(self._filter_input)

        result_layout.addLayout(top_bar)

        # 模型列表滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        scroll.setMinimumHeight(160)

        self._list_container = QWidget()
        self._list_container.setStyleSheet("background: transparent;")
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self._status_label = QLabel("请填写信息后点击「查询可用模型」")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 30px 0;")
        self._list_layout.addWidget(self._status_label)

        scroll.setWidget(self._list_container)
        result_layout.addWidget(scroll, stretch=1)

        layout.addWidget(self._result_frame, stretch=1)

        # ── 底部按钮 ──
        layout.addSpacing(12)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        query_btn = QPushButton("🔍 查询可用模型")
        query_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        query_btn.setFixedHeight(36)
        query_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px;
                padding: 0 16px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        query_btn.clicked.connect(self._on_query)
        btn_row.addWidget(query_btn)

        btn_row.addStretch()

        cancel_btn = QPushButton("取  消")
        cancel_btn.setFixedSize(100, 36)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        self._add_btn = QPushButton("添加选中模型")
        self._add_btn.setFixedSize(130, 36)
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        self._add_btn.clicked.connect(self._on_add_selected)
        btn_row.addWidget(self._add_btn)

        layout.addLayout(btn_row)

    def _on_provider_change(self, index: int):
        if index <= 0:
            return
        real_idx = index - 1
        if real_idx < len(self._providers):
            p, e, k = self._providers[real_idx]
            self._inputs["provider"].setText(p)
            self._inputs["endpoint"].setText(e)
            self._inputs["api_key"].setText(k)

    def _show_tip(self, message: str):
        _show_styled_tip(self, self.theme, message)

    def _on_query(self):
        endpoint = self._inputs["endpoint"].text().strip()
        api_key = self._inputs["api_key"].text().strip()

        if not endpoint or not api_key:
            self._show_tip("请填写调用地址和 API Key")
            return

        self._clear_model_list()
        c = self.theme.colors
        loading = QLabel("正在查询...")
        loading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        loading.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 30px 0;")
        self._list_layout.addWidget(loading)
        self._select_all_cb.setChecked(False)
        self._count_label.setText("")
        self._filter_input.clear()

        def do_query():
            try:
                models = self.manager.fetch_remote_models(endpoint, api_key)
                self._on_query_done(models, None)
            except RuntimeError as e:
                self._on_query_done([], str(e))

        threading.Thread(target=do_query, daemon=True).start()

    def _on_query_done(self, models: list[str], error: str | None):
        self._pending_models = models
        self._pending_error = error
        self._query_done_signal.emit()

    def _on_query_done_main_thread(self):
        self._populate_models(self._pending_models, self._pending_error)

    def _populate_models(self, models: list[str], error: str | None):
        c = self.theme.colors
        self._clear_model_list()
        self._model_items.clear()
        self._all_model_ids = models

        if error:
            err_label = QLabel(f"查询失败: {error}")
            err_label.setWordWrap(True)
            err_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            err_label.setStyleSheet(f"font-size: 12px; color: {c['danger']}; padding: 20px 0;")
            self._list_layout.addWidget(err_label)
            return

        if not models:
            empty = QLabel("未查询到可用模型")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"font-size: 12px; color: {c['text_hint']}; padding: 30px 0;")
            self._list_layout.addWidget(empty)
            return

        existing_names = {m.model_name for m in self.manager.list_all()}
        self._count_label.setText(f"共 {len(models)} 个模型")

        for model_id in models:
            already = model_id in existing_names
            item = _ModelCheckItem(model_id, already, self.theme)
            self._model_items.append(item)
            self._list_layout.addWidget(item)

    def _clear_model_list(self):
        while self._list_layout.count():
            w = self._list_layout.takeAt(0)
            if w.widget():
                w.widget().deleteLater()

    def _on_select_all(self, checked: bool):
        for item in self._model_items:
            if item.isVisible():
                item.set_checked(checked)

    def _apply_filter(self, keyword: str):
        kw = keyword.lower().strip()
        visible_count = 0
        for item in self._model_items:
            match = not kw or kw in item.model_id.lower()
            item.setVisible(match)
            if match:
                visible_count += 1
        total = len(self._model_items)
        if kw:
            self._count_label.setText(f"显示 {visible_count}/{total}")
        else:
            self._count_label.setText(f"共 {total} 个模型")

    def _on_add_selected(self):
        provider = self._inputs["provider"].text().strip()
        endpoint = self._inputs["endpoint"].text().strip()
        api_key = self._inputs["api_key"].text().strip()

        selected = [item.model_id for item in self._model_items if item.is_checked()]
        if not selected:
            self._show_tip("请至少选择一个模型")
            return
        if not provider or not endpoint or not api_key:
            self._show_tip("提供商、调用地址和 API Key 不能为空")
            return

        for model_name in selected:
            entry = ModelEntry(
                provider=provider,
                model_name=model_name,
                endpoint=endpoint,
                api_key=api_key,
            )
            self.manager.add(entry)

        self.accept()


# ═══════════════════════════════════════════════════════════════
# 厂商设置工作区
# ═══════════════════════════════════════════════════════════════

class _ModelConfigWorkspace(QWidget):
    """厂商设置的工作区。"""

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.manager = ModelConfigManager()
        self._build()

    def _build(self):
        c = self.theme.colors
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 20, 28, 20)
        outer.setSpacing(0)

        # ── 标题栏 ──
        header = QHBoxLayout()
        header.setSpacing(0)

        title = QLabel("厂商设置")
        title.setStyleSheet(f"""
            font-size: 20px; font-weight: bold; color: {c["text"]};
        """)
        header.addWidget(title)
        header.addStretch()

        quick_btn = QPushButton("⚡ 快速添加")
        quick_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        quick_btn.setFixedHeight(34)
        quick_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px;
                padding: 0 16px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        quick_btn.clicked.connect(self._on_quick_add)
        header.addWidget(quick_btn)
        header.addSpacing(8)

        batch_btn = QPushButton("批量添加")
        batch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        batch_btn.setFixedHeight(34)
        batch_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px;
                padding: 0 16px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        batch_btn.clicked.connect(self._on_batch_add)
        header.addWidget(batch_btn)
        header.addSpacing(8)

        add_btn = QPushButton("＋ 添加模型")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setFixedHeight(34)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px;
                padding: 0 18px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        add_btn.clicked.connect(self._on_add)
        header.addWidget(add_btn)

        outer.addLayout(header)
        outer.addSpacing(16)

        # ── 可滚动卡片区 ──
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self._card_container = QWidget()
        self._card_container.setStyleSheet("background: transparent;")
        self._card_layout = QVBoxLayout(self._card_container)
        self._card_layout.setContentsMargins(0, 0, 6, 0)
        self._card_layout.setSpacing(10)
        self._card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        scroll.setWidget(self._card_container)
        outer.addWidget(scroll, stretch=1)

        self._refresh()

    # ── 刷新卡片列表 ──

    def _refresh(self):
        while self._card_layout.count():
            item = self._card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        models = self.manager.list_all()
        models.sort(key=lambda m: (not m.is_primary, m.provider))

        if not models:
            self._show_empty()
            return

        for entry in models:
            card = _ModelCard(entry, self.manager, self.theme)
            card.action_triggered.connect(self._on_card_action)
            self._card_layout.addWidget(card)

    def _show_empty(self):
        c = self.theme.colors
        empty = QWidget()
        layout = QVBoxLayout(empty)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon = QLabel("🤖")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 40px;")
        layout.addWidget(icon)

        msg = QLabel("暂无模型配置")
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setStyleSheet(f"font-size: 16px; color: {c['text_secondary']}; margin-top: 12px;")
        layout.addWidget(msg)

        hint = QLabel("点击右上角「添加模型」开始配置")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"font-size: 13px; color: {c['text_hint']}; margin-top: 4px;")
        layout.addWidget(hint)

        self._card_layout.addWidget(empty)

    # ── 操作回调 ──

    def _on_quick_add(self):
        dlg = _QuickAddDialog(self.theme, self.manager, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh()

    def _on_add(self):
        dlg = _ModelEditDialog(self.theme, self.manager, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh()

    def _on_batch_add(self):
        dlg = _BatchAddDialog(self.theme, self.manager, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._refresh()

    def _on_card_action(self, action: str, model_id: str):
        if action == "edit":
            entry = self.manager.get(model_id)
            if entry:
                dlg = _ModelEditDialog(self.theme, self.manager, entry=entry, parent=self)
                if dlg.exec() == QDialog.DialogCode.Accepted:
                    self._refresh()

        elif action == "delete":
            if self._confirm_delete("确定要删除这个模型配置吗？"):
                self.manager.delete(model_id)
                self._refresh()

        elif action == "primary":
            self.manager.set_primary(model_id)
            self._refresh()

        elif action == "duplicate":
            self.manager.duplicate(model_id)
            self._refresh()

    def _confirm_delete(self, message: str) -> bool:
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("确认删除")
        dlg.setFixedSize(340, 150)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(16)

        msg = QLabel(message)
        msg.setWordWrap(True)
        msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
        layout.addWidget(msg)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(12)
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setFixedSize(72, 34)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        del_btn = QPushButton("删除")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setFixedSize(72, 34)
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["danger"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: #d32f2f; }}
        """)
        del_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(del_btn)

        layout.addLayout(btn_row)

        return dlg.exec() == QDialog.DialogCode.Accepted


# ═══════════════════════════════════════════════════════════════
# 通用设置工作区
# ═══════════════════════════════════════════════════════════════

class _GeneralWorkspace(QWidget):
    """通用设置页 — 聊天记录管理等。"""

    chats_cleared = Signal()

    def __init__(self, theme: Theme, chat_manager: ChatManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.manager = chat_manager
        self._build()

    def _build(self):
        c = self.theme.colors

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(20)

        title = QLabel("通用设置")
        title.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {c['text']};")
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {c['border_light']};")
        layout.addWidget(sep)

        group_label = QLabel("聊天记录管理")
        group_label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {c['text']}; margin-top: 4px;")
        layout.addWidget(group_label)

        row = QHBoxLayout()
        row.setSpacing(12)
        desc = QLabel("删除所有未置顶的聊天记录，置顶对话不受影响。")
        desc.setStyleSheet(f"font-size: 13px; color: {c['text_secondary']};")
        desc.setWordWrap(True)
        row.addWidget(desc, stretch=1)

        clear_btn = QPushButton("清空非置顶聊天")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setFixedHeight(34)
        clear_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["danger"]};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 0 16px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {c.get("danger_hover", "#c0392b")};
            }}
        """)
        clear_btn.clicked.connect(self._confirm_clear)
        row.addWidget(clear_btn)

        layout.addLayout(row)
        layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    def _confirm_clear(self):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("确认清空")
        dlg.setFixedSize(380, 170)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        msg = QLabel("确定要删除所有非置顶的聊天记录吗？\n\n此操作不可撤销，置顶对话不受影响。")
        msg.setWordWrap(True)
        msg.setStyleSheet(f"font-size: 13px; color: {c['text']};")
        layout.addWidget(msg)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(80, 32)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["card_bg"]};
                color: {c["text"]};
                border: 1px solid {c["border"]};
                border-radius: 6px;
                font-size: 13px;
            }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        confirm_btn = QPushButton("确认删除")
        confirm_btn.setFixedSize(100, 32)
        confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["danger"]};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                font-size: 13px;
            }}
            QPushButton:hover {{
                background: {c.get("danger_hover", "#c0392b")};
            }}
        """)
        confirm_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(confirm_btn)

        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            count = self.manager.delete_unpinned()
            self.chats_cleared.emit()
            self._show_result(count)

    def _show_result(self, count: int):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("提示")
        dlg.setFixedSize(300, 120)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        text = f"已清空 {count} 条非置顶聊天记录。" if count > 0 else "没有需要清空的聊天记录。"
        msg = QLabel(text)
        msg.setStyleSheet(f"font-size: 13px; color: {c['text']};")
        layout.addWidget(msg)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("知道了")
        ok_btn.setFixedSize(80, 32)
        ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                font-size: 13px;
            }}
        """)
        ok_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(ok_btn)
        layout.addLayout(btn_row)

        dlg.exec()


# ═══════════════════════════════════════════════════════════════
# 设置页面整体（二级导航 + 工作区）
# ═══════════════════════════════════════════════════════════════

class SettingsView(QWidget):
    """设置页：左侧二级导航 + 右侧工作区。"""

    chats_cleared = Signal()

    def __init__(self, theme: Theme, chat_manager: ChatManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._chat_manager = chat_manager
        self._pages: dict[str, QWidget] = {}
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._subnav = _SubNav(self.theme)
        self._subnav.nav_changed.connect(self._switch_page)
        layout.addWidget(self._subnav)

        self._stack = QStackedWidget()
        self._stack.setStyleSheet(f"background: {self.theme.colors['content_bg']};")
        layout.addWidget(self._stack, stretch=1)

        general_page = _GeneralWorkspace(self.theme, self._chat_manager)
        general_page.chats_cleared.connect(self.chats_cleared)
        self._pages["general"] = general_page
        self._stack.addWidget(general_page)

        self._subnav.select("general")

    def _switch_page(self, nav_id: str):
        page = self._pages.get(nav_id)
        if page:
            self._stack.setCurrentWidget(page)
