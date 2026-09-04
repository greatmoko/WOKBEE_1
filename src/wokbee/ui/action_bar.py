"""WokBee 下段操作栏：输入、运行/暂停、审批条、模型切换、上下文用量环。"""

from __future__ import annotations

import random

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from tokbee.core.provider_store import ProviderStore
from tokbee.ui.combo_style import apply_combo_popup_style
from tokbee.ui.styles.system import bind_text_edit_context_menu
from tokbee.ui.styles.theme import Theme
from tokbee.ui.widgets.context_ring import ContextUsageRing

# 运行中：运行按钮音频均衡器动效 —— 5 根细竖条自绘，底部对齐，随机跃升 + 指数回落
_RUN_EQ_BARS = 5
_RUN_EQ_BAR_W = 3        # 竖条宽度（细）
_RUN_EQ_GAP = 4          # 竖条间距
_RUN_EQ_PAD_TOP = 6
_RUN_EQ_PAD_BOTTOM = 6
_RUN_EQ_INTERVAL = 70    # 帧间隔（毫秒）
_RUN_EQ_DECAY = 0.82     # 每帧回落系数（真实均衡器风格）
_RUN_EQ_PEAK = 0.35      # 高于该值的随机量才触发跃升


class _RunButton(QPushButton):
    """运行按钮：空闲显示「运行」，运行中自绘底部对齐的音频均衡器动画。"""

    def __init__(self, theme: Theme, parent=None):
        super().__init__("运行", parent)
        self._theme = theme
        self._spinning = False
        self._levels = [0.0] * _RUN_EQ_BARS
        self._eq_timer = QTimer(self)
        self._eq_timer.setInterval(_RUN_EQ_INTERVAL)
        self._eq_timer.timeout.connect(self._tick)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(59, 34)
        self.setStyleSheet(f"""
            QPushButton {{
                background: #faad14; color: white;
                border: none; border-radius: 6px; font-size: 13px; font-weight: bold;
            }}
            QPushButton:hover {{ background: #e69500; }}
            QPushButton:pressed {{ background: #faad14; }}
        """)

    def set_spinning(self, spinning: bool):
        self._spinning = spinning
        if spinning:
            self._levels = [0.0] * _RUN_EQ_BARS
            self._eq_timer.start()
        else:
            self._eq_timer.stop()
            self.setText("运行")
        self.setEnabled(not spinning)
        self.update()

    def _tick(self):
        # 真实均衡器：随机跃升 + 指数回落
        for i in range(_RUN_EQ_BARS):
            peak = random.random()
            if peak > _RUN_EQ_PEAK:
                self._levels[i] = max(self._levels[i], peak)
            self._levels[i] *= _RUN_EQ_DECAY
        self.update()

    def paintEvent(self, event):
        if not self._spinning:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()
        painter.setBrush(QColor("#f5f5f5"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 6, 6)
        self._draw_equalizer(painter, rect)
        painter.end()

    def _draw_equalizer(self, painter: QPainter, rect):
        inner_h = (rect.height() - _RUN_EQ_PAD_TOP - _RUN_EQ_PAD_BOTTOM) * 0.8
        base_y = rect.y() + rect.height() - _RUN_EQ_PAD_BOTTOM
        total_w = _RUN_EQ_BARS * _RUN_EQ_BAR_W + (_RUN_EQ_BARS - 1) * _RUN_EQ_GAP
        start_x = rect.x() + (rect.width() - total_w) // 2
        lit = QColor("#07c160")
        dim = QColor("#f5f5f5")
        for i in range(_RUN_EQ_BARS):
            bar_h = round(self._levels[i] * inner_h)
            x = start_x + i * (_RUN_EQ_BAR_W + _RUN_EQ_GAP)
            painter.setBrush(dim)
            painter.drawRoundedRect(
                x, base_y - inner_h, _RUN_EQ_BAR_W, inner_h, 1, 1
            )
            if bar_h > 0:
                painter.setBrush(lit)
                painter.drawRoundedRect(
                    x, base_y - bar_h, _RUN_EQ_BAR_W, bar_h, 1, 1
                )

class _ActionBar(QFrame):
    run_clicked = Signal()
    pause_clicked = Signal()
    open_folder_clicked = Signal()
    archive_clicked = Signal()
    upload_clicked = Signal()
    open_deliverables_clicked = Signal()
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
        bind_text_edit_context_menu(self._input, c)
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
            ("📦", "打开交付物目录", self.open_deliverables_clicked.emit),
            ("⬆️", "上传文件", self.upload_clicked.emit),
            ("🗄️", "归档（仅保留最新经验）", self.archive_clicked.emit),
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
        self._model_combo.setToolTip("切换本项目模型")
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
        self._ctx_ring.setToolTip("上下文用量")
        self._ctx_ring.compress_clicked.connect(self.compress_clicked.emit)
        row.addWidget(self._ctx_ring)

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

        self._run_btn = _RunButton(self.theme)
        self._run_btn.clicked.connect(self.run_clicked.emit)
        row.addWidget(self._run_btn)

        send_btn = QPushButton("发送")
        send_btn.setFixedSize(72, 34)
        send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        send_btn.setStyleSheet("""
            QPushButton {
                background: #07c160; color: white;
                border: none; border-radius: 6px; font-size: 13px; font-weight: bold;
            }
            QPushButton:hover { background: #06ad56; }
            QPushButton:pressed { background: #07c160; }
        """)
        send_btn.clicked.connect(self._on_send)
        row.addWidget(send_btn)
        layout.addLayout(row)

        self.reload_models()

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
        self._run_btn.set_spinning(running)
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
        """缓存命中信息不再单独展示，合并进用量环的悬停提示。"""
        if text or tooltip:
            parts = [p for p in (text or "", tooltip or "") if p.strip()]
            self._ctx_ring.set_cache_info("\n".join(parts))
        else:
            self._ctx_ring.set_cache_info("")

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