"""厂商设置工作区 — 我的厂商可自选添加；Ollama 与自定义本地分离。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, QThread, QTimer, QPoint
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame,
    QLabel, QPushButton, QScrollArea, QLineEdit,
    QDialog, QCheckBox, QListWidget, QListWidgetItem,
    QInputDialog, QComboBox, QStackedWidget, QSpinBox,
    QApplication,
)

from tokbee.ui.styles.theme import Theme
from tokbee.core.provider_store import ProviderStore, ProviderSettings, ProviderModel
from tokbee.core.provider import get_builtin
from tokbee.core.errors import AIError

logger = logging.getLogger("tokbee")

# 添加弹窗中「自定义本地 API」的特殊选项 id
_CUSTOM_OPTION = "__custom_local__"


class _ModelSettingsPopup(QFrame):
    """模型行设置浮层：上下文窗口 / 设为默认 / 删除。"""

    context_changed = Signal(str, int)   # model_id, context_window
    set_default = Signal(str)
    delete_model = Signal(str)

    def __init__(
        self,
        theme: Theme,
        model: ProviderModel,
        *,
        is_default: bool,
        parent=None,
    ):
        super().__init__(parent, Qt.WindowType.Popup)
        self.theme = theme
        self._model_id = model.model_id
        c = theme.colors
        self.setObjectName("modelSettingsPopup")
        self.setStyleSheet(f"""
            QFrame#modelSettingsPopup {{
                background: {c["content_bg"]};
                border: 1px solid {c["border"]};
                border-radius: 8px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        title = QLabel(model.nickname or model.model_id)
        title.setStyleSheet(f"font-size: 12px; font-weight: 600; color: {c['text']};")
        title.setWordWrap(True)
        layout.addWidget(title)

        ctx_row = QHBoxLayout()
        ctx_row.setSpacing(8)
        ctx_lbl = QLabel("上下文窗口")
        ctx_lbl.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']};")
        ctx_row.addWidget(ctx_lbl)
        self._ctx_spin = QSpinBox()
        self._ctx_spin.setRange(0, 10_000_000)
        self._ctx_spin.setSingleStep(1024)
        self._ctx_spin.setValue(int(model.context_window or 0))
        self._ctx_spin.setToolTip("tokens；0 表示未设置")
        self._ctx_spin.setMinimumWidth(120)
        self._ctx_spin.setStyleSheet(f"""
            QSpinBox {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 4px; padding: 4px 6px; color: {c["text"]}; font-size: 12px;
            }}
        """)
        self._ctx_spin.valueChanged.connect(self._on_ctx)
        ctx_row.addWidget(self._ctx_spin, stretch=1)
        layout.addLayout(ctx_row)

        hint = QLabel("单位 tokens")
        hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        layout.addWidget(hint)

        btn_style = f"""
            QPushButton {{
                background: {c["card_bg"]}; color: {c["text"]};
                border: 1px solid {c["border"]}; border-radius: 6px;
                padding: 6px 10px; font-size: 12px; text-align: left;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
            QPushButton:disabled {{ color: {c["text_hint"]}; }}
        """
        if is_default:
            def_btn = QPushButton("✓ 当前为默认模型")
            def_btn.setEnabled(False)
        else:
            def_btn = QPushButton("设为默认模型")
            def_btn.clicked.connect(self._on_default)
        def_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        def_btn.setStyleSheet(btn_style)
        layout.addWidget(def_btn)

        del_btn = QPushButton("删除此模型")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["danger"]};
                border: 1px solid {c["border"]}; border-radius: 6px;
                padding: 6px 10px; font-size: 12px; text-align: left;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        del_btn.clicked.connect(self._on_delete)
        layout.addWidget(del_btn)

        self.setFixedWidth(240)
        self.adjustSize()

    def _on_ctx(self, value: int):
        self.context_changed.emit(self._model_id, int(value))

    def _on_default(self):
        mid = self._model_id
        self.close()
        self.set_default.emit(mid)

    def _on_delete(self):
        mid = self._model_id
        self.close()
        self.delete_model.emit(mid)

    def popup_at(self, global_pos: QPoint):
        """在按钮附近弹出，必要时向左/上收拢以免出屏。"""
        self.adjustSize()
        screen = QApplication.screenAt(global_pos) or QApplication.primaryScreen()
        geo = screen.availableGeometry() if screen else None
        x, y = global_pos.x(), global_pos.y()
        if geo is not None:
            if x + self.width() > geo.right():
                x = geo.right() - self.width() - 4
            if y + self.height() > geo.bottom():
                y = global_pos.y() - self.height() - 4
            x = max(geo.left() + 4, x)
            y = max(geo.top() + 4, y)
        self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()


def _tip(parent: QWidget, theme: Theme, message: str):
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("提示")
    dlg.setFixedSize(360, 150)
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


def _confirm(parent: QWidget, theme: Theme, message: str, title: str = "确认") -> bool:
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(360, 150)
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
    cancel = QPushButton("取消")
    cancel.setFixedSize(72, 34)
    cancel.setCursor(Qt.CursorShape.PointingHandCursor)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: transparent; color: {c["text_secondary"]};
            border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["subnav_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    row.addWidget(cancel)
    ok = QPushButton("删除")
    ok.setFixedSize(72, 34)
    ok.setCursor(Qt.CursorShape.PointingHandCursor)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_primary"]}; color: #ffffff;
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(ok)
    layout.addLayout(row)
    return dlg.exec() == QDialog.DialogCode.Accepted

class _FetchModelsWorker(QThread):
    finished_ok = Signal(list)
    finished_err = Signal(str)

    def __init__(self, host: str, key: str, parent=None):
        super().__init__(parent)
        self._host = host
        self._key = key

    def run(self):
        try:
            models = ProviderStore.fetch_remote_models(self._host, self._key)
            self.finished_ok.emit(models)
        except AIError as e:
            self.finished_err.emit(str(e))
        except Exception as e:
            self.finished_err.emit(str(e))


class _AddProviderDialog(QDialog):
    """选择内置厂商，或填写名称添加自定义本地 API。"""

    def __init__(self, theme: Theme, store: ProviderStore, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store
        self.result_builtin_id: str | None = None
        self.result_custom: tuple[str, str, str] | None = None  # name, host, key
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setWindowTitle("添加厂商")
        self.setFixedSize(440, 420)
        self.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)

        lbl_style = f"font-size: 13px; font-weight: 600; color: {c['text']};"
        hint = f"font-size: 11px; color: {c['text_hint']};"
        inp = f"""
            QLineEdit, QComboBox {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 0 10px; color: {c["text"]}; font-size: 13px;
                min-height: 34px;
            }}
            QLineEdit:focus, QComboBox:focus {{ border-color: {c["input_focus_border"]}; }}
            QComboBox QAbstractItemView {{
                background: {c["content_bg"]};
                border: 1px solid {c["input_border"]};
                outline: none;
                selection-background-color: {c["btn_hover"]};
                selection-color: {c["text"]};
            }}
            QComboBox QAbstractItemView::item {{
                padding: 6px 10px; min-height: 26px;
                border: none; outline: none; color: {c["text"]};
            }}
            QComboBox QAbstractItemView::item:hover,
            QComboBox QAbstractItemView::item:selected,
            QComboBox QAbstractItemView::item:focus {{
                background: {c["btn_hover"]};
                border: none; outline: none;
            }}
        """

        t = QLabel("选择要添加的厂商")
        t.setStyleSheet(f"font-size: 16px; font-weight: bold; color: {c['text']};")
        layout.addWidget(t)

        tip = QLabel("内置厂商与「自定义本地 API」分开；多个本地服务请分别添加并填写厂商名称。")
        tip.setWordWrap(True)
        tip.setStyleSheet(hint)
        layout.addWidget(tip)

        type_lbl = QLabel("厂商类型")
        type_lbl.setStyleSheet(lbl_style)
        layout.addWidget(type_lbl)

        self._type_combo = QComboBox()
        self._type_combo.setStyleSheet(inp)
        from tokbee.ui.combo_style import apply_combo_popup_style
        apply_combo_popup_style(self._type_combo, c)
        for pid, name, icon, _f in self.store.list_addable_builtins():
            self._type_combo.addItem(f"{icon}  {name}", pid)
        self._type_combo.addItem("🖥️  自定义本地 API", _CUSTOM_OPTION)
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        layout.addWidget(self._type_combo)

        self._custom_box = QWidget()
        custom_l = QVBoxLayout(self._custom_box)
        custom_l.setContentsMargins(0, 8, 0, 0)
        custom_l.setSpacing(8)

        name_lbl = QLabel("厂商名称（必填）")
        name_lbl.setStyleSheet(lbl_style)
        custom_l.addWidget(name_lbl)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("例如：公司内网 vLLM / LM Studio-办公机")
        self._name_edit.setStyleSheet(inp)
        custom_l.addWidget(self._name_edit)

        host_lbl = QLabel("API Host")
        host_lbl.setStyleSheet(lbl_style)
        custom_l.addWidget(host_lbl)
        self._host_edit = QLineEdit()
        self._host_edit.setPlaceholderText("http://127.0.0.1:1234/v1")
        self._host_edit.setStyleSheet(inp)
        custom_l.addWidget(self._host_edit)

        key_lbl = QLabel("API Key（可选）")
        key_lbl.setStyleSheet(lbl_style)
        custom_l.addWidget(key_lbl)
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("本地服务通常可留空")
        self._key_edit.setStyleSheet(inp)
        custom_l.addWidget(self._key_edit)

        layout.addWidget(self._custom_box)
        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("取消")
        cancel.setFixedSize(72, 34)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)

        ok = QPushButton("添加")
        ok.setFixedSize(72, 34)
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        ok.clicked.connect(self._on_ok)
        btn_row.addWidget(ok)
        layout.addLayout(btn_row)

        self._on_type_changed()

    def _on_type_changed(self):
        is_custom = self._type_combo.currentData() == _CUSTOM_OPTION
        self._custom_box.setVisible(is_custom)
        self.setFixedSize(440, 420 if is_custom else 260)

    def _on_ok(self):
        pid = self._type_combo.currentData()
        if pid == _CUSTOM_OPTION:
            name = self._name_edit.text().strip()
            if not name:
                self._name_edit.setFocus()
                _tip(self, self.theme, "请填写厂商名称")
                return
            self.result_custom = (
                name,
                self._host_edit.text().strip(),
                self._key_edit.text().strip(),
            )
            self.accept()
            return
        if not pid:
            _tip(self, self.theme, "没有可添加的内置厂商")
            return
        self.result_builtin_id = str(pid)
        self.accept()


class ProviderSettingsWorkspace(QWidget):
    """厂商设置：左为我的厂商列表，右为详情。"""

    def __init__(self, theme: Theme, store: ProviderStore | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store or ProviderStore()
        self._current_id = ""
        self._model_checks: list = []  # (QCheckBox, ProviderModel)
        self._model_popup: _ModelSettingsPopup | None = None
        self._fetch_worker: _FetchModelsWorker | None = None
        self._loading = False
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(400)
        self._autosave_timer.timeout.connect(self._autosave_now)
        self._build()
        self._show_empty_detail()

    def _build(self):
        c = self.theme.colors
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        left = QFrame()
        left.setObjectName("providerSideList")
        left.setFixedWidth(220)
        left.setStyleSheet(f"""
            QFrame#providerSideList {{
                background: {c["subnav_bg"]};
                border: none;
                border-right: 1px solid {c["border"]};
            }}
        """)
        left_l = QVBoxLayout(left)
        left_l.setContentsMargins(8, 12, 8, 12)
        left_l.setSpacing(6)

        head_row = QHBoxLayout()
        head_row.setContentsMargins(4, 0, 4, 0)
        head = QLabel("我的厂商")
        head.setStyleSheet(
            f"font-size: 13px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        head_row.addWidget(head)
        head_row.addStretch()

        add_btn = QPushButton("+")
        add_btn.setToolTip("添加厂商")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setFixedSize(28, 28)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["accent"]};
                border: none; border-radius: 6px; font-size: 16px; font-weight: bold;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        add_btn.clicked.connect(self._on_add_provider)
        head_row.addWidget(add_btn)
        left_l.addLayout(head_row)

        self._list = QListWidget()
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: transparent; border: none; outline: none;
                color: {c["text"]}; font-size: 13px;
            }}
            QListWidget::item {{
                padding: 8px 10px; border-radius: 6px;
            }}
            QListWidget::item:selected {{
                background: {c["subnav_active"]}; color: {c["subnav_text_active"]};
            }}
            QListWidget::item:hover {{
                background: {c["subnav_hover"]};
            }}
        """)
        self._list.currentItemChanged.connect(self._on_list_changed)
        left_l.addWidget(self._list, stretch=1)

        empty_hint = QLabel("点击右上角 + 添加厂商")
        empty_hint.setWordWrap(True)
        empty_hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']}; padding: 4px 8px;")
        self._left_empty = empty_hint
        left_l.addWidget(empty_hint)

        root.addWidget(left)

        self._right_stack = QStackedWidget()
        # page 0: empty
        empty_page = QWidget()
        el = QVBoxLayout(empty_page)
        el.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_msg = QLabel("尚未选择厂商\n请从左侧添加或选择一个厂商进行配置")
        empty_msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_msg.setStyleSheet(f"font-size: 14px; color: {c['text_hint']};")
        el.addWidget(empty_msg)
        self._empty_default_lbl = QLabel("")
        self._empty_default_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_default_lbl.setStyleSheet(
            f"font-size: 12px; color: {c['text_secondary']}; margin-top: 12px;"
        )
        el.addWidget(self._empty_default_lbl)
        self._right_stack.addWidget(empty_page)

        # page 1: detail
        right = QWidget()
        right_l = QVBoxLayout(right)
        right_l.setContentsMargins(28, 24, 28, 24)
        right_l.setSpacing(12)

        self._title = QLabel("厂商设置")
        self._title.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {c['text']};")
        right_l.addWidget(self._title)

        self._notes = QLabel("")
        self._notes.setWordWrap(True)
        self._notes.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        right_l.addWidget(self._notes)

        self._default_hint = QLabel("")
        self._default_hint.setWordWrap(True)
        self._default_hint.setStyleSheet(
            f"font-size: 12px; color: {c['text_secondary']};"
        )
        right_l.addWidget(self._default_hint)

        lbl_style = f"font-size: 13px; font-weight: 600; color: {c['text']};"
        inp_style = f"""
            QLineEdit {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 0 10px; color: {c["text"]}; font-size: 13px;
            }}
            QLineEdit:focus {{ border-color: {c["input_focus_border"]}; }}
        """

        key_lbl = QLabel("API Key")
        key_lbl.setStyleSheet(lbl_style)
        right_l.addWidget(key_lbl)
        self._key_edit = QLineEdit()
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setFixedHeight(36)
        self._key_edit.setPlaceholderText("输入 API Key（本地可留空）")
        self._key_edit.setStyleSheet(inp_style)
        self._key_edit.textChanged.connect(self._schedule_autosave)
        self._key_edit.editingFinished.connect(self._autosave_now)
        right_l.addWidget(self._key_edit)

        host_lbl = QLabel("API Host")
        host_lbl.setStyleSheet(lbl_style)
        right_l.addWidget(host_lbl)
        self._host_edit = QLineEdit()
        self._host_edit.setFixedHeight(36)
        self._host_edit.setPlaceholderText("https://api.example.com/v1")
        self._host_edit.setStyleSheet(inp_style)
        self._host_edit.textChanged.connect(self._schedule_autosave)
        self._host_edit.editingFinished.connect(self._autosave_now)
        right_l.addWidget(self._host_edit)

        model_row = QHBoxLayout()
        model_lbl = QLabel("模型列表")
        model_lbl.setStyleSheet(lbl_style)
        model_row.addWidget(model_lbl)
        model_row.addStretch()

        self._query_btn = QPushButton("拉取远程模型")
        self._query_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._query_btn.setFixedHeight(30)
        self._query_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; padding: 0 12px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        self._query_btn.clicked.connect(self._on_fetch_models)
        model_row.addWidget(self._query_btn)

        add_model_btn = QPushButton("+ 添加")
        add_model_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_model_btn.setFixedHeight(30)
        add_model_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["accent"]};
                border: none; border-radius: 6px; padding: 0 12px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        add_model_btn.clicked.connect(self._on_add_model)
        model_row.addWidget(add_model_btn)
        right_l.addLayout(model_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        self._model_container = QWidget()
        self._model_layout = QVBoxLayout(self._model_container)
        self._model_layout.setContentsMargins(0, 0, 0, 0)
        self._model_layout.setSpacing(4)
        self._model_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._model_container)
        right_l.addWidget(scroll, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)

        reset_btn = QPushButton("恢复默认 Host")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setFixedHeight(34)
        reset_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: 1px solid {c["border"]}; border-radius: 6px; padding: 0 14px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        reset_btn.clicked.connect(self._on_reset)
        self._reset_btn = reset_btn
        btn_row.addWidget(reset_btn)

        del_btn = QPushButton("从列表移除")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setFixedHeight(34)
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["danger"]};
                border: 1px solid {c["border"]}; border-radius: 6px; padding: 0 14px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        del_btn.clicked.connect(self._on_remove)
        self._del_btn = del_btn
        btn_row.addWidget(del_btn)
        btn_row.addStretch()
        right_l.addLayout(btn_row)

        self._right_stack.addWidget(right)
        root.addWidget(self._right_stack, stretch=1)
        self._reload_list()

    def showEvent(self, event):
        super().showEvent(event)
        self._reload_list(keep_current=True)

    def _show_empty_detail(self):
        self._autosave_now()
        self._current_id = ""
        self._list.clearSelection()
        self._right_stack.setCurrentIndex(0)
        self._refresh_default_labels()

    def _refresh_default_labels(self):
        label = f"默认模型：{self.store.default_display_label()}"
        if hasattr(self, "_empty_default_lbl"):
            self._empty_default_lbl.setText(label)
        if hasattr(self, "_default_hint"):
            self._default_hint.setText(label)

    def _reload_list(self, keep_current: bool = False):
        cur = self._current_id if keep_current else ""
        self._list.blockSignals(True)
        self._list.clear()
        for pid, name, icon, _family in self.store.list_my_providers():
            item = QListWidgetItem(f"{icon}  {name}")
            item.setData(Qt.ItemDataRole.UserRole, pid)
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._left_empty.setVisible(self._list.count() == 0)

        if cur:
            for i in range(self._list.count()):
                if self._list.item(i).data(Qt.ItemDataRole.UserRole) == cur:
                    self._list.setCurrentRow(i)
                    self._refresh_default_labels()
                    return
        # 默认不选中任何厂商
        self._list.clearSelection()
        if not cur or cur not in {self._list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self._list.count())}:
            self._show_empty_detail()
        else:
            self._refresh_default_labels()

    def _on_list_changed(self, current: QListWidgetItem | None, _prev):
        if not current:
            self._show_empty_detail()
            return
        self._select_provider(current.data(Qt.ItemDataRole.UserRole))

    def _select_provider(self, provider_id: str):
        if self._current_id and self._current_id != provider_id:
            self._autosave_now()
        self._autosave_timer.stop()
        self._loading = True
        self._current_id = provider_id
        self._right_stack.setCurrentIndex(1)
        name = self.store.get_display_name(provider_id)
        self._title.setText(name)
        builtin = get_builtin(provider_id)
        if builtin:
            self._notes.setText(builtin.notes)
            self._reset_btn.setVisible(True)
        else:
            self._notes.setText("自定义 OpenAI 兼容本地 / 私有 API")
            self._reset_btn.setVisible(False)

        settings = self.store.get_settings(provider_id)
        self._key_edit.setText(settings.api_key)
        self._host_edit.setText(settings.api_host)
        self._render_models(settings.models)
        self._refresh_default_labels()
        self._loading = False

    def _render_models(self, models: list[ProviderModel]):
        self._close_model_popup()
        while self._model_layout.count():
            item = self._model_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        self._model_checks.clear()
        c = self.theme.colors
        if not models:
            empty = QLabel("暂无模型 — 请「拉取远程模型」或「+ 添加」")
            empty.setStyleSheet(f"color: {c['text_hint']}; font-size: 12px;")
            self._model_layout.addWidget(empty)
            return
        for m in models:
            row = QWidget()
            row_l = QHBoxLayout(row)
            row_l.setContentsMargins(0, 0, 0, 0)
            row_l.setSpacing(8)

            cb = QCheckBox(m.nickname or m.model_id)
            cb.setChecked(m.enabled)
            cb.setStyleSheet(f"font-size: 13px; color: {c['text']};")
            cb.toggled.connect(self._on_model_toggled)
            row_l.addWidget(cb, stretch=1)

            is_default = self.store.is_default_model(self._current_id, m.model_id)
            if is_default:
                badge = QLabel("默认")
                badge.setStyleSheet(f"""
                    QLabel {{
                        color: {c["accent"]}; font-size: 11px; font-weight: 600;
                        padding: 2px 8px; border: 1px solid {c["border"]};
                        border-radius: 4px;
                    }}
                """)
                row_l.addWidget(badge)

            gear = QPushButton("⚙")
            gear.setToolTip("模型设置")
            gear.setCursor(Qt.CursorShape.PointingHandCursor)
            gear.setFixedSize(28, 26)
            gear.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {c["text_secondary"]};
                    border: 1px solid {c["border"]}; border-radius: 4px;
                    font-size: 13px;
                }}
                QPushButton:hover {{ background: {c["subnav_hover"]}; color: {c["accent"]}; }}
            """)
            gear.clicked.connect(
                lambda _=False, model=m, btn=gear: self._open_model_settings(model, btn)
            )
            row_l.addWidget(gear)

            self._model_layout.addWidget(row)
            self._model_checks.append((cb, m))

    def _close_model_popup(self):
        if self._model_popup is not None:
            self._model_popup.close()
            self._model_popup.deleteLater()
            self._model_popup = None

    def _open_model_settings(self, model: ProviderModel, anchor: QWidget):
        self._close_model_popup()
        is_default = self.store.is_default_model(self._current_id, model.model_id)
        popup = _ModelSettingsPopup(
            self.theme, model, is_default=is_default, parent=self.window(),
        )
        popup.context_changed.connect(self._on_model_context_changed)
        popup.set_default.connect(self._on_set_default)
        popup.delete_model.connect(self._on_delete_model)
        self._model_popup = popup
        # 锚点右下角外侧弹出
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + 2))
        popup.popup_at(pos)

    def _on_model_context_changed(self, model_id: str, context_window: int):
        for _cb, m in self._model_checks:
            if m.model_id == model_id:
                m.context_window = int(context_window)
                break
        self._schedule_autosave()

    def _schedule_autosave(self, *_args):
        if self._loading or not self._current_id:
            return
        self._autosave_timer.start()

    def _autosave_now(self, *_args):
        if self._loading or not self._current_id:
            return
        self._autosave_timer.stop()
        self.store.update_settings(self._current_id, self._collect_settings())
        self._refresh_default_labels()

    def _on_model_toggled(self, *_args):
        self._autosave_now()

    def _on_delete_model(self, model_id: str):
        if not self._current_id or not model_id:
            return
        self._close_model_popup()
        if not _confirm(self, self.theme, f"确定删除模型「{model_id}」？"):
            return
        settings = self._collect_settings()
        settings.models = [m for m in settings.models if m.model_id != model_id]
        self.store.update_settings(self._current_id, settings)
        if self.store.is_default_model(self._current_id, model_id):
            self.store.clear_default_model()
        self._render_models(settings.models)
        self._refresh_default_labels()

    def _on_set_default(self, model_id: str):
        if not self._current_id or not model_id:
            return
        self._close_model_popup()
        for cb, m in self._model_checks:
            if m.model_id == model_id:
                cb.setChecked(True)
                break
        settings = self._collect_settings()
        if not (settings.api_host or "").strip():
            _tip(self, self.theme, "请先填写 API Host，再设为默认模型")
            return
        self.store.update_settings(self._current_id, settings)
        try:
            self.store.set_default_model(self._current_id, model_id)
        except ValueError as e:
            _tip(self, self.theme, str(e))
            return
        self._render_models(self.store.get_settings(self._current_id).models)
        self._refresh_default_labels()

    def _collect_settings(self) -> ProviderSettings:
        models: list[ProviderModel] = []
        for cb, m in self._model_checks:
            models.append(ProviderModel(
                model_id=m.model_id,
                nickname=m.nickname,
                capabilities=list(m.capabilities),
                context_window=int(m.context_window or 0),
                max_output=m.max_output,
                enabled=cb.isChecked(),
            ))
        return ProviderSettings(
            api_key=self._key_edit.text().strip(),
            api_host=self._host_edit.text().strip(),
            models=models,
        )

    def _on_add_provider(self):
        dlg = _AddProviderDialog(self.theme, self.store, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.result_builtin_id:
            ok = self.store.add_builtin_provider(dlg.result_builtin_id)
            if not ok:
                _tip(self, self.theme, "该厂商已在列表中")
                return
            new_id = dlg.result_builtin_id
        elif dlg.result_custom:
            name, host, key = dlg.result_custom
            try:
                info = self.store.add_custom_provider(name, host, key)
            except ValueError as e:
                _tip(self, self.theme, str(e))
                return
            new_id = info.id
        else:
            return

        self._reload_list()
        for i in range(self._list.count()):
            if self._list.item(i).data(Qt.ItemDataRole.UserRole) == new_id:
                self._list.setCurrentRow(i)
                break

    def _on_reset(self):
        if not self._current_id or self.store.is_custom(self._current_id):
            return
        self._autosave_timer.stop()
        self.store.reset_provider(self._current_id, keep_api_key=True)
        self._select_provider(self._current_id)
        _tip(self, self.theme, "已恢复默认 Host（保留 API Key；模型列表已清空，请重新拉取或添加）")

    def _on_remove(self):
        if not self._current_id:
            return
        self._autosave_timer.stop()
        self.store.remove_from_my_list(self._current_id)
        self._current_id = ""
        self._reload_list()
        self._show_empty_detail()

    def _on_add_model(self):
        if not self._current_id:
            return
        mid, ok = QInputDialog.getText(self, "添加模型", "模型 ID：")
        if not ok or not mid.strip():
            return
        settings = self._collect_settings()
        if any(m.model_id == mid.strip() for m in settings.models):
            _tip(self, self.theme, "模型已存在")
            return
        settings.models.append(ProviderModel(model_id=mid.strip(), enabled=False))
        self.store.update_settings(self._current_id, settings)
        self._render_models(settings.models)

    def _on_fetch_models(self):
        if not self._current_id:
            return
        host = self._host_edit.text().strip()
        if not host:
            _tip(self, self.theme, "请先填写 API Host")
            return
        self._autosave_now()
        self._query_btn.setEnabled(False)
        self._query_btn.setText("拉取中…")
        self._fetch_worker = _FetchModelsWorker(host, self._key_edit.text().strip(), self)
        self._fetch_worker.finished_ok.connect(self._on_fetch_ok)
        self._fetch_worker.finished_err.connect(self._on_fetch_err)
        self._fetch_worker.start()

    def _on_fetch_ok(self, model_ids: list):
        self._query_btn.setEnabled(True)
        self._query_btn.setText("拉取远程模型")
        settings = self._collect_settings()
        existing = {m.model_id for m in settings.models}
        for mid in model_ids:
            if mid not in existing:
                settings.models.append(ProviderModel(model_id=mid, enabled=False))
        self.store.update_settings(self._current_id, settings)
        self._render_models(settings.models)
        _tip(self, self.theme, f"已获取 {len(model_ids)} 个模型，请勾选需要启用的项")

    def _on_fetch_err(self, err: str):
        self._query_btn.setEnabled(True)
        self._query_btn.setText("拉取远程模型")
        _tip(self, self.theme, f"拉取失败：{err}")