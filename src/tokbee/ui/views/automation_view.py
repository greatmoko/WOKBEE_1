"""AI 配置页面 — AI 角色 / 厂商设置 / TokBee 设置。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QFrame,
    QLabel, QPushButton, QStackedWidget, QScrollArea,
    QLineEdit, QDialog, QComboBox, QTextEdit,
)

from tokbee.ui.styles.theme import Theme
from tokbee.core.provider_store import ProviderStore, ResolvedModel
from tokbee.core.ai_role import AIRole, AIRoleManager
from tokbee.core.ai_client import AIClient
from tokbee.core.config import Config

logger = logging.getLogger("tokbee")


class _SubNavButton(QPushButton):
    def __init__(self, icon_text: str, label: str, theme: Theme, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._active = False
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
                background: {bg}; color: {tc}; font-weight: {fw};
                border: none; border-radius: 6px;
                padding: 0 12px; text-align: left; font-size: 13px;
                text-decoration: none; outline: none;
            }}
            QPushButton:hover {{ background: {c["subnav_hover"]}; }}
            QPushButton:focus {{ outline: none; border: none; }}
        """)

    def set_active(self, active: bool):
        self._active = active
        self._apply_style()


class _SubNav(QFrame):
    nav_changed = Signal(str)

    ITEMS = [
        ("ai_roles", "🤖", "AI 角色"),
        ("model_config", "🧠", "厂商设置"),
        ("session_defaults", "⚙", "TokBee 设置"),
        ("wokbee_settings", "🐝", "WokBee 设置"),
        ("skills", "📚", "Skills"),
        ("mcp", "🔌", "MCP"),
        ("gateway", "📡", "消息网关"),
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
        self.setObjectName("aiConfigSubNav")
        self.setStyleSheet(f"""
            QFrame#aiConfigSubNav {{
                background: {c["subnav_bg"]};
                border: none;
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
# AI 角色管理
# ═══════════════════════════════════════════════════════════════

class _AIRoleCard(QFrame):
    """单条 AI 角色卡片。"""

    action_triggered = Signal(str, str)  # (action, role_id)

    def __init__(self, role: AIRole, theme: Theme, parent=None):
        super().__init__(parent)
        self.role = role
        self.theme = theme
        self._build()

    def _build(self):
        c = self.theme.colors
        r = self.role

        self.setStyleSheet(f"""
            _AIRoleCard {{
                background: {c["card_bg"]};
                border-radius: 8px;
                border: 1px solid {c["border_light"]};
            }}
            _AIRoleCard:hover {{
                background: {c["card_hover"]};
                border-color: {c["border"]};
            }}
        """)
        self.setFixedHeight(72)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(3)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_label = QLabel(r.name)
        name_label.setStyleSheet(f"background: transparent; font-size: 14px; font-weight: bold; color: {c['text']};")
        name_row.addWidget(name_label)

        if r.is_default:
            tag = QLabel("默认")
            tag.setStyleSheet(f"""
                font-size: 10px; color: {c["accent"]};
                background: {c["accent_light"]}; border-radius: 3px;
                padding: 1px 5px;
            """)
            name_row.addWidget(tag)

        name_row.addStretch()
        info.addLayout(name_row)

        preview = r.description[:80].replace("\n", " ") if r.description else "(无描述)"
        desc_label = QLabel(preview)
        desc_label.setStyleSheet(f"background: transparent; font-size: 12px; color: {c['text_secondary']};")
        desc_label.setWordWrap(False)
        desc_label.setMinimumWidth(0)
        from PySide6.QtWidgets import QSizePolicy
        desc_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        info.addWidget(desc_label)

        layout.addLayout(info, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        edit_btn = QPushButton("编辑")
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.setFixedSize(52, 26)
        edit_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text_secondary"]};
                border: none; border-radius: 4px; font-size: 11px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """)
        edit_btn.clicked.connect(lambda: self.action_triggered.emit("edit", self.role.id))
        btn_row.addWidget(edit_btn)

        if not r.is_default:
            del_btn = QPushButton("删除")
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.setFixedSize(52, 26)
            del_btn.setStyleSheet(f"""
                QPushButton {{
                    background: {c["btn_bg"]}; color: {c["danger"]};
                    border: none; border-radius: 4px; font-size: 11px;
                }}
                QPushButton:hover {{ background: {c["btn_hover"]}; }}
            """)
            del_btn.clicked.connect(lambda: self.action_triggered.emit("delete", self.role.id))
            btn_row.addWidget(del_btn)

        layout.addLayout(btn_row)


class _RoleGenWorker(QThread):
    """后台线程：调用 AI 生成角色的 System Prompt（非流式）。"""

    done = Signal(str)   # content
    error = Signal(str)

    _DEFAULT_PROMPT = (
        "你是一个 AI 角色 System Prompt 专家。根据用户给出的角色名称，"
        "撰写一份专业、详细的 System Prompt，定义该角色的身份、能力范围、"
        "行为准则和输出风格。直接输出 Prompt 内容，不要加标题或解释。"
    )

    def __init__(self, model: ResolvedModel, role_name: str,
                 system_prompt: str = "", parent=None):
        super().__init__(parent)
        self._model = model
        self._role_name = role_name
        self._system_prompt = system_prompt or self._DEFAULT_PROMPT

    def run(self):
        try:
            client = AIClient(
                self._model.api_host, self._model.api_key, self._model.model_id,
                family=self._model.family,
            )
            messages = [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": f"角色名称：{self._role_name}"},
            ]
            resp = client.chat(messages, temperature=0.7, max_tokens=2048)
            content = resp.content.strip()
            if content:
                self.done.emit(content)
            else:
                self.error.emit("AI 返回了空内容")
        except Exception as e:
            logger.warning("AI 生成角色描述失败: %s", e)
            self.error.emit(str(e))


class _AIRoleDialog(QDialog):
    """AI 角色新建 / 编辑弹窗。"""

    def __init__(self, theme: Theme, role: AIRole | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._role = role
        self._gen_worker: _RoleGenWorker | None = None
        self._provider_store = ProviderStore()
        self._role_manager = AIRoleManager()
        self._config = Config()
        self._build()

    def _build(self):
        c = self.theme.colors
        editing = self._role is not None

        self.setWindowTitle("编辑角色" if editing else "新建角色")
        self.setFixedSize(460, 620)
        self.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(10)

        combo_style = f"""
            QComboBox {{
                background: {c["input_bg"]};
                border: 1px solid {c["input_border"]};
                border-radius: 6px;
                padding: 4px 28px 4px 8px;
                color: {c["text"]};
                font-size: 12px;
            }}
            QComboBox:hover {{ border-color: {c["input_focus_border"]}; }}
            QComboBox::drop-down {{
                subcontrol-origin: padding;
                subcontrol-position: center right;
                width: 24px; border: none;
            }}
            QComboBox::down-arrow {{
                image: none; width: 0; height: 0;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {c["text_secondary"]};
            }}
            QComboBox QAbstractItemView {{
                background: {c["content_bg"]};
                border: 1px solid {c["input_border"]};
                border-radius: 4px; padding: 4px; outline: none;
                selection-background-color: {c["btn_hover"]};
                selection-color: {c["text"]};
            }}
            QComboBox QAbstractItemView::item {{
                padding: 4px 8px; border: none; outline: none; color: {c["text"]};
            }}
            QComboBox QAbstractItemView::item:hover,
            QComboBox QAbstractItemView::item:selected,
            QComboBox QAbstractItemView::item:focus {{
                background: {c["btn_hover"]};
                border: none;
                outline: none;
            }}
        """

        # ── 角色名称 + AI 创作按钮 ──
        name_lbl = QLabel("角色名称")
        name_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {c['text']};")
        layout.addWidget(name_lbl)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("例如：翻译助手")
        if editing:
            self._name_input.setText(self._role.name)
        name_row.addWidget(self._name_input, stretch=1)

        self._ai_btn = QPushButton("✨ AI 创作")
        self._ai_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ai_btn.setFixedHeight(30)
        self._ai_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; padding: 0 14px; font-size: 12px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
            QPushButton:disabled {{ background: {c["border"]}; color: {c["text_hint"]}; }}
        """)
        self._ai_btn.clicked.connect(self._on_ai_generate)
        name_row.addWidget(self._ai_btn)

        layout.addLayout(name_row)

        # ── AI 创作设置：模型选择 + 角色选择 ──
        ai_settings_row = QHBoxLayout()
        ai_settings_row.setSpacing(12)

        model_col = QVBoxLayout()
        model_col.setSpacing(2)
        model_lbl = QLabel("AI 模型")
        model_lbl.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        model_col.addWidget(model_lbl)

        self._model_combo = QComboBox()
        self._model_combo.setStyleSheet(combo_style)
        self._model_combo.setFixedHeight(30)
        from tokbee.ui.combo_style import apply_combo_popup_style
        apply_combo_popup_style(self._model_combo, c)
        models = self._provider_store.list_selectable_models()
        last_key = self._config.get("role_gen.model_key", "")
        select_idx = 0
        for i, m in enumerate(models):
            key = f"{m.provider_id}|{m.model_id}"
            self._model_combo.addItem(f"{m.provider_name} / {m.model_id}", key)
            if last_key and key == last_key:
                select_idx = i
        if models:
            self._model_combo.setCurrentIndex(select_idx)
        model_col.addWidget(self._model_combo)
        ai_settings_row.addLayout(model_col, stretch=1)

        role_col = QVBoxLayout()
        role_col.setSpacing(2)
        role_lbl = QLabel("创作角色")
        role_lbl.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        role_col.addWidget(role_lbl)

        self._role_combo = QComboBox()
        self._role_combo.setStyleSheet(combo_style)
        apply_combo_popup_style(self._role_combo, c)
        self._role_combo.setFixedHeight(30)
        self._role_combo.addItem("默认（Prompt 专家）", "")
        roles = self._role_manager.list_all()
        last_role_id = self._config.get("role_gen.role_id", "")
        role_select_idx = 0
        for i, r in enumerate(roles):
            self._role_combo.addItem(r.name, r.id)
            if last_role_id and r.id == last_role_id:
                role_select_idx = i + 1
        self._role_combo.setCurrentIndex(role_select_idx)
        role_col.addWidget(self._role_combo)
        ai_settings_row.addLayout(role_col, stretch=1)

        layout.addLayout(ai_settings_row)

        # ── 角色描述 ──
        desc_lbl = QLabel("角色描述")
        desc_lbl.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {c['text']};")
        layout.addWidget(desc_lbl)

        hint = QLabel("即 System Prompt，用于定义 AI 的角色和行为方式")
        hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        layout.addWidget(hint)

        self._desc_input = QTextEdit()
        self._desc_input.setPlaceholderText("例如：你是一个专业的中英翻译助手，请将用户输入的内容翻译成对应语言...")
        self._desc_input.setMinimumHeight(260)
        self._desc_input.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; border: 1px solid {c["input_border"]};
                border-radius: 6px; padding: 6px 10px; color: {c["text"]}; font-size: 13px;
            }}
            QTextEdit:focus {{ border-color: {c["input_focus_border"]}; }}
        """)
        if editing:
            self._desc_input.setPlainText(self._role.description)
        layout.addWidget(self._desc_input)

        layout.addStretch()

        # ── 底部按钮 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.setFixedSize(80, 34)
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["card_bg"]}; color: {c["text"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
        """)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        save_btn = QPushButton("保存")
        save_btn.setFixedSize(80, 34)
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

    def _get_selected_model(self) -> ResolvedModel | None:
        key = self._model_combo.currentData()
        if not key or "|" not in str(key):
            return None
        pid, mid = str(key).split("|", 1)
        return self._provider_store.resolve(pid, mid)

    def _get_selected_role_prompt(self) -> str:
        role_id = self._role_combo.currentData()
        if not role_id:
            return ""
        role = self._role_manager.get(role_id)
        return role.description if role else ""

    def _on_ai_generate(self):
        role_name = self._name_input.text().strip()
        if not role_name:
            self._name_input.setFocus()
            return

        model = self._get_selected_model()
        if not model or not model.api_host:
            self._desc_input.setPlainText("请先在「AI配置 → 厂商设置」中配置模型")
            return

        if self._gen_worker and self._gen_worker.isRunning():
            self._gen_worker.wait(3000)

        self._config.set("role_gen.model_key", self._model_combo.currentData() or "")
        self._config.set("role_gen.role_id", self._role_combo.currentData() or "")
        self._config.save()

        self._desc_input.clear()
        self._desc_input.setPlaceholderText("AI 正在创作中，请稍候...")
        self._ai_btn.setEnabled(False)
        self._ai_btn.setText("生成中…")
        self._name_input.setEnabled(False)

        system_prompt = self._get_selected_role_prompt()
        self._gen_worker = _RoleGenWorker(model, role_name, system_prompt, parent=self)
        self._gen_worker.done.connect(self._on_gen_done)
        self._gen_worker.error.connect(self._on_gen_error)
        self._gen_worker.start()

    def _on_gen_done(self, content: str):
        self._desc_input.setPlainText(content)
        self._desc_input.setPlaceholderText("例如：你是一个专业的中英翻译助手，请将用户输入的内容翻译成对应语言...")
        self._ai_btn.setEnabled(True)
        self._ai_btn.setText("✨ AI 创作")
        self._name_input.setEnabled(True)

    def _on_gen_error(self, msg: str):
        self._ai_btn.setEnabled(True)
        self._ai_btn.setText("✨ AI 创作")
        self._name_input.setEnabled(True)
        self._desc_input.setPlaceholderText("例如：你是一个专业的中英翻译助手，请将用户输入的内容翻译成对应语言...")
        if not self._desc_input.toPlainText().strip():
            self._desc_input.setPlainText(f"生成失败: {msg}")

    def _on_save(self):
        if not self._name_input.text().strip():
            self._name_input.setFocus()
            return
        if self._gen_worker and self._gen_worker.isRunning():
            self._gen_worker.wait(3000)
        self.accept()

    def reject(self):
        if self._gen_worker and self._gen_worker.isRunning():
            self._gen_worker.wait(3000)
        super().reject()

    def get_data(self) -> dict:
        return {
            "name": self._name_input.text().strip(),
            "description": self._desc_input.toPlainText().strip(),
        }


class _AIRoleWorkspace(QWidget):
    """AI 角色管理工作区。"""

    def __init__(self, theme: Theme, role_manager: AIRoleManager, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.role_manager = role_manager
        self._build()

    def _build(self):
        c = self.theme.colors

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QFrame()
        header.setFixedHeight(56)
        header.setStyleSheet(f"background: {c['content_bg']}; border: none;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 0, 24, 0)

        title = QLabel("AI 角色")
        title.setStyleSheet(
            f"font-size: 18px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        header_layout.addWidget(title)
        header_layout.addStretch()

        add_btn = QPushButton("+ 新建角色")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setFixedHeight(34)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: #ffffff;
                border: none; border-radius: 6px; padding: 0 16px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        add_btn.clicked.connect(self._on_add)
        header_layout.addWidget(add_btn)

        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(24, 16, 24, 16)
        self._list_layout.setSpacing(8)
        self._list_layout.addStretch()

        scroll.setWidget(self._list_container)
        outer.addWidget(scroll)

        self._refresh()

    def _refresh(self):
        while self._list_layout.count() > 0:
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        roles = self.role_manager.list_all()
        if not roles:
            c = self.theme.colors
            empty = QLabel("暂无 AI 角色")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet(f"color: {c['text_hint']}; font-size: 14px; padding: 40px;")
            self._list_layout.addWidget(empty)
        else:
            for role in roles:
                card = _AIRoleCard(role, self.theme)
                card.action_triggered.connect(self._on_card_action)
                self._list_layout.addWidget(card)

        self._list_layout.addStretch()

    def _on_card_action(self, action: str, role_id: str):
        if action == "edit":
            role = self.role_manager.get(role_id)
            if not role:
                return
            dlg = _AIRoleDialog(self.theme, role, self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                self.role_manager.update(role_id, **dlg.get_data())
                self._refresh()
        elif action == "delete":
            self._confirm_delete(role_id)

    def _on_add(self):
        dlg = _AIRoleDialog(self.theme, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            role = AIRole(**data)
            self.role_manager.add(role)
            self._refresh()

    def _confirm_delete(self, role_id: str):
        c = self.theme.colors
        role = self.role_manager.get(role_id)
        if not role:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("确认删除")
        dlg.setFixedSize(340, 150)
        dlg.setStyleSheet(f"background: {c['content_bg']};")

        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(12)

        msg = QLabel(f"确定要删除角色「{role.name}」吗？\n此操作不可撤销。")
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
                background: {c["card_bg"]}; color: {c["text"]};
                border: 1px solid {c["border"]}; border-radius: 6px; font-size: 13px;
            }}
        """)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

        confirm_btn = QPushButton("删除")
        confirm_btn.setFixedSize(80, 32)
        confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["danger"]}; color: #ffffff;
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["danger_hover"]}; }}
        """)
        confirm_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(confirm_btn)

        layout.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.role_manager.delete(role_id)
            self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh()


class AutomationView(QWidget):
    """AI 配置页：左侧二级导航 + 右侧工作区。"""

    def __init__(
        self,
        theme: Theme,
        role_manager: AIRoleManager | None = None,
        wokbee_settings=None,
        gateway_manager=None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self._role_manager = role_manager or AIRoleManager()
        self._wokbee_settings = wokbee_settings
        self._gateway_manager = gateway_manager
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

        role_page = _AIRoleWorkspace(self.theme, self._role_manager)
        self._pages["ai_roles"] = role_page
        self._stack.addWidget(role_page)

        from tokbee.ui.views.provider_view import ProviderSettingsWorkspace
        model_page = ProviderSettingsWorkspace(self.theme)
        self._pages["model_config"] = model_page
        self._stack.addWidget(model_page)

        from tokbee.ui.views.session_defaults_view import SessionDefaultsWorkspace
        defaults_page = SessionDefaultsWorkspace(self.theme, self._role_manager)
        self._pages["session_defaults"] = defaults_page
        self._stack.addWidget(defaults_page)

        from wokbee.ui.settings_workspace import WokBeeSettingsWorkspace
        from wokbee.core.settings import WokBeeSettings
        ab_settings = self._wokbee_settings or WokBeeSettings()
        ab_page = WokBeeSettingsWorkspace(self.theme, ab_settings)
        self._pages["wokbee_settings"] = ab_page
        self._stack.addWidget(ab_page)

        from wokbee.ui.skills_workspace import SkillsWorkspace
        from wokbee.core.skills_store import SkillsStore
        skills_page = SkillsWorkspace(self.theme, SkillsStore())
        self._pages["skills"] = skills_page
        self._stack.addWidget(skills_page)

        from wokbee.ui.mcp_workspace import McpWorkspace
        from wokbee.core.mcp_store import McpStore
        mcp_page = McpWorkspace(self.theme, McpStore())
        self._pages["mcp"] = mcp_page
        self._stack.addWidget(mcp_page)

        from wokbee.ui.gateway_workspace import GatewayWorkspace
        gateway_page = GatewayWorkspace(self.theme, self._gateway_manager)
        self._pages["gateway"] = gateway_page
        self._stack.addWidget(gateway_page)

        self._subnav.select("ai_roles")

    def _switch_page(self, nav_id: str):
        page = self._pages.get(nav_id)
        if page:
            self._stack.setCurrentWidget(page)
