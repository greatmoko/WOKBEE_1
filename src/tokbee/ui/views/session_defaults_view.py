"""AI 配置 — TokBee 全局默认对话参数。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QDialog, QLineEdit, QTextEdit, QFrame,
)

from tokbee.ui.styles.theme import Theme
from tokbee.ui.views.session_params_editor import SessionParamsEditor
from tokbee.core.session_settings import SessionSettings, GlobalSessionDefaults
from tokbee.core.ai_role import AIRole, AIRoleManager


def _tip(parent: QWidget, theme: Theme, message: str):
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("提示")
    dlg.setFixedSize(360, 140)
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


class SessionDefaultsWorkspace(QWidget):
    """管理新建对话时拷贝的全局默认参数。"""

    def __init__(
        self,
        theme: Theme,
        role_manager: AIRoleManager | None = None,
        defaults: GlobalSessionDefaults | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self._role_manager = role_manager or AIRoleManager()
        self._defaults = defaults or GlobalSessionDefaults()
        self._build()
        self._editor.load(self._defaults.get())

    def _build(self):
        c = self.theme.colors
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(f"background: {c['content_bg']}; border: none;")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(28, 20, 28, 12)
        title = QLabel("TokBee 设置")
        title.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        hl.addWidget(title)
        tip = QLabel("新建对话会复制这里的会话参数；模型调用参数统一在「厂商设置」的模型配置中管理。")
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
        self._editor = SessionParamsEditor(
            self.theme,
            role_manager=self._role_manager,
            show_all_options=True,
        )
        self._editor.quick_create_clicked.connect(self._on_quick_create)
        bl.addWidget(self._editor)
        scroll.setWidget(body)
        root.addWidget(scroll, stretch=1)

        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(28, 10, 28, 18)
        btn_bar.setSpacing(10)

        reset_btn = QPushButton("恢复出厂默认")
        reset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reset_btn.setFixedHeight(34)
        reset_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["text_secondary"]};
                border: 1px solid {c["border"]}; border-radius: 6px; padding: 0 14px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
        """)
        reset_btn.clicked.connect(self._on_factory_reset)
        btn_bar.addWidget(reset_btn)
        btn_bar.addStretch()

        save_btn = QPushButton("保存为全局默认")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setFixedHeight(34)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; padding: 0 18px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        save_btn.clicked.connect(self._on_save)
        btn_bar.addWidget(save_btn)
        root.addLayout(btn_bar)

    def showEvent(self, event):
        super().showEvent(event)
        self._editor._reload_roles()
        self._editor.load(self._defaults.get())

    def _on_save(self):
        settings = self._editor.collect()
        # 全局默认不绑定具体厂商/模型
        settings.provider = ""
        settings.model_id = ""
        self._defaults.save(settings)
        _tip(self, self.theme, "已保存全局默认；之后新建的对话将使用这套参数")

    def _on_factory_reset(self):
        self._editor.load(SessionSettings())
        _tip(self, self.theme, "已填入出厂默认值，请点击「保存为全局默认」生效")

    def _on_quick_create(self):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("快速创建角色")
        dlg.setFixedSize(420, 300)
        dlg.setStyleSheet(f"background: {c['content_bg']};")
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(10)

        name_lbl = QLabel("角色名称")
        name_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {c['text']};")
        layout.addWidget(name_lbl)
        name_input = QLineEdit()
        name_input.setPlaceholderText("例如：翻译助手")
        layout.addWidget(name_input)

        desc_lbl = QLabel("角色描述")
        desc_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {c['text']};")
        layout.addWidget(desc_lbl)
        desc_input = QTextEdit()
        desc_input.setPlaceholderText("系统提示词内容…")
        layout.addWidget(desc_input, stretch=1)

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.setFixedSize(72, 34)
        cancel.clicked.connect(dlg.reject)
        row.addWidget(cancel)
        ok = QPushButton("创建")
        ok.setFixedSize(72, 34)
        ok.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
        """)
        row.addWidget(ok)
        layout.addLayout(row)

        def _create():
            name = name_input.text().strip()
            desc = desc_input.toPlainText().strip()
            if not name or not desc:
                _tip(dlg, self.theme, "请填写名称和描述")
                return
            role = AIRole(name=name, description=desc)
            self._role_manager.add(role)
            self._editor._reload_roles()
            idx = self._editor.role_combo.findData(role.id)
            if idx >= 0:
                self._editor.role_combo.setCurrentIndex(idx)
            self._editor.sys_input.setPlainText(desc)
            dlg.accept()

        ok.clicked.connect(_create)
        dlg.exec()
