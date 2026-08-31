"""AI 配置 — 本机凭据库。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from tokbee.ui.styles.system import (
    apply_checkbox,
    apply_danger_btn,
    apply_lineedit,
    apply_primary_btn,
    apply_secondary_btn,
    apply_textedit,
    style_hint_label,
)
from tokbee.ui.styles.theme import Theme
from wokbee.core.credential_store import (
    CredentialRecord,
    CredentialStore,
    CredentialVaultError,
)
from wokbee.ui.dialogs import tip as _tip


class _CredentialCard(QFrame):
    def __init__(
        self,
        rec: CredentialRecord,
        theme: Theme,
        *,
        on_edit,
        on_copy,
        on_delete,
        parent=None,
    ):
        super().__init__(parent)
        self.rec = rec
        c = theme.colors
        self.setObjectName("credentialCard")
        self.setStyleSheet(f"""
            QFrame#credentialCard {{
                background: {c["card_bg"]};
                border: 1px solid {c["border_light"]};
                border-radius: 8px;
            }}
            QFrame#credentialCard QLabel {{
                background: transparent; border: none;
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)

        info = QVBoxLayout()
        title = QLabel(rec.title or rec.alias)
        title.setStyleSheet(
            f"font-size: 14px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        info.addWidget(title)
        bits = [f"别名 {rec.alias}"]
        if rec.username:
            bits.append(rec.username)
        if rec.url:
            bits.append(rec.url)
        sub = QLabel(" · ".join(bits))
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"font-size: 12px; color: {c['text_secondary']};"
            "background: transparent; border: none;"
        )
        info.addWidget(sub)
        lay.addLayout(info, stretch=1)

        copy_btn = QPushButton("复制密码")
        apply_secondary_btn(copy_btn, c, height=30)
        copy_btn.setMinimumWidth(84)
        copy_btn.clicked.connect(lambda: on_copy(rec))
        lay.addWidget(copy_btn)

        edit_btn = QPushButton("编辑")
        apply_secondary_btn(edit_btn, c, height=30)
        edit_btn.setMinimumWidth(56)
        edit_btn.clicked.connect(lambda: on_edit(rec))
        lay.addWidget(edit_btn)

        del_btn = QPushButton("删除")
        apply_danger_btn(del_btn, c, height=30)
        del_btn.setMinimumWidth(56)
        del_btn.clicked.connect(lambda: on_delete(rec))
        lay.addWidget(del_btn)


class _CredentialDialog(QDialog):
    def __init__(self, theme: Theme, rec: CredentialRecord | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._rec = rec
        editing = rec is not None
        self.setWindowTitle("编辑凭据" if editing else "添加凭据")
        self.setMinimumSize(460, 460)
        c = theme.colors
        self.setStyleSheet(f"background: {c['content_bg']};")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(10)

        def _label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setStyleSheet(
                f"font-size: 13px; font-weight: 600; color: {c['text']};"
                "background: transparent; border: none;"
            )
            return lbl

        lay.addWidget(_label("别名（Agent 检索用，唯一）"))
        self._alias = QLineEdit()
        self._alias.setPlaceholderText("例如 gitlab 或 company-oa")
        apply_lineedit(self._alias, c)
        lay.addWidget(self._alias)

        lay.addWidget(_label("标题"))
        self._title = QLineEdit()
        self._title.setPlaceholderText("显示名称，可与别名相同")
        apply_lineedit(self._title, c)
        lay.addWidget(self._title)

        lay.addWidget(_label("网址"))
        self._url = QLineEdit()
        self._url.setPlaceholderText("https://…（可选）")
        apply_lineedit(self._url, c)
        lay.addWidget(self._url)

        lay.addWidget(_label("用户名 / 账号"))
        self._user = QLineEdit()
        apply_lineedit(self._user, c)
        lay.addWidget(self._user)

        lay.addWidget(_label("密码"))
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        if editing:
            self._password.setPlaceholderText("留空则不修改原密码")
        apply_lineedit(self._password, c)
        lay.addWidget(self._password)

        self._show_pwd = QCheckBox("显示密码")
        apply_checkbox(self._show_pwd, c)
        self._show_pwd.toggled.connect(self._toggle_password)
        lay.addWidget(self._show_pwd)

        lay.addWidget(_label("备注（可选）"))
        self._notes = QTextEdit()
        self._notes.setFixedHeight(80)
        apply_textedit(self._notes, c)
        lay.addWidget(self._notes)

        if rec:
            self._alias.setText(rec.alias)
            self._title.setText(rec.title)
            self._url.setText(rec.url)
            self._user.setText(rec.username)
            self._notes.setPlainText(rec.notes)

        lay.addStretch()
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        apply_secondary_btn(cancel, c)
        cancel.setMinimumWidth(80)
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        save = QPushButton("保存")
        apply_primary_btn(save, c)
        save.setMinimumWidth(80)
        save.clicked.connect(self._on_save)
        row.addWidget(save)
        lay.addLayout(row)

    def _toggle_password(self, on: bool):
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        self._password.setEchoMode(mode)

    def _on_save(self):
        if not self._alias.text().strip():
            self._alias.setFocus()
            return
        self.accept()

    def get_data(self) -> dict:
        return {
            "rec_id": self._rec.id if self._rec else "",
            "alias": self._alias.text().strip(),
            "title": self._title.text().strip(),
            "url": self._url.text().strip(),
            "username": self._user.text().strip(),
            "password": self._password.text(),
            "notes": self._notes.toPlainText(),
        }


class CredentialWorkspace(QWidget):
    def __init__(self, theme: Theme, store: CredentialStore | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store or CredentialStore()
        self._build()
        self.refresh()

    def _build(self):
        c = self.theme.colors
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("credentialHeader")
        header.setStyleSheet(f"QFrame#credentialHeader {{ background: {c['content_bg']}; border: none; }}")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(28, 20, 28, 12)
        title = QLabel("凭据库")
        title.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        hl.addWidget(title)
        tip = QLabel(
            "账号密码加密保存在本机（AES-256-GCM），主密钥放在 Windows 凭据管理器，"
            "不写入 config.json。同一 Windows 用户下的其它程序理论上仍可能读取；"
            "防的是磁盘被拷走或误提交。Agent 只能拿到环境变量名（如 WOKBEE_CRED_GOOGLE_PASSWORD），"
            "execute 时由本机注入明文；对话、时间线和命令展示都会隐藏账号密码。"
        )
        style_hint_label(tip, c)
        hl.addWidget(tip)
        root.addWidget(header)

        bar = QHBoxLayout()
        bar.setContentsMargins(28, 8, 28, 8)
        bar.addStretch()
        add_btn = QPushButton("添加凭据")
        apply_primary_btn(add_btn, c)
        add_btn.setMinimumWidth(96)
        add_btn.clicked.connect(self._on_add)
        bar.addWidget(add_btn)
        root.addLayout(bar)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        self._list = QWidget()
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(28, 8, 28, 20)
        self._list_layout.setSpacing(8)
        self._list_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(self._list)
        root.addWidget(scroll, stretch=1)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        c = self.theme.colors
        try:
            records = self.store.list_records()
        except CredentialVaultError as e:
            err = QLabel(str(e))
            err.setWordWrap(True)
            err.setStyleSheet(f"color: {c['danger']}; padding: 20px;")
            self._list_layout.addWidget(err)
            return
        if not records:
            empty = QLabel("暂无凭据。点击「添加凭据」录入账号密码，供 Agent 登录其它系统。")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {c['text_hint']}; padding: 20px;")
            self._list_layout.addWidget(empty)
            return
        for rec in records:
            card = _CredentialCard(
                rec,
                self.theme,
                on_edit=self._on_edit,
                on_copy=self._on_copy,
                on_delete=self._on_delete,
            )
            self._list_layout.addWidget(card)

    def _on_add(self):
        dlg = _CredentialDialog(self.theme, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._save_from_dialog(dlg.get_data())

    def _on_edit(self, rec: CredentialRecord):
        dlg = _CredentialDialog(self.theme, rec, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._save_from_dialog(dlg.get_data())

    def _save_from_dialog(self, data: dict):
        try:
            self.store.upsert(**data)
        except CredentialVaultError as e:
            _tip(self, self.theme, str(e))
            return
        self.refresh()

    def _on_copy(self, rec: CredentialRecord):
        latest = self.store.get(rec.alias)
        pwd = latest.password if latest else rec.password
        clip = QApplication.clipboard()
        if clip is None:
            _tip(self, self.theme, "无法访问剪贴板")
            return
        clip.setText(pwd or "")
        _tip(self, self.theme, "已复制密码到剪贴板")

    def _on_delete(self, rec: CredentialRecord):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("删除凭据")
        dlg.setFixedSize(380, 150)
        dlg.setStyleSheet(f"background: {c['content_bg']};")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 18)
        msg = QLabel(f"确定删除「{rec.title}」（{rec.alias}）？此操作不可恢复。")
        msg.setWordWrap(True)
        msg.setStyleSheet(f"font-size: 13px; color: {c['text']};")
        lay.addWidget(msg)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        apply_secondary_btn(cancel, c)
        cancel.setMinimumWidth(80)
        cancel.clicked.connect(dlg.reject)
        row.addWidget(cancel)
        ok = QPushButton("删除")
        apply_danger_btn(ok, c)
        ok.setMinimumWidth(80)
        ok.clicked.connect(dlg.accept)
        row.addWidget(ok)
        lay.addLayout(row)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            try:
                self.store.delete(rec.id)
            except CredentialVaultError as e:
                _tip(self, self.theme, str(e))
                return
            self.refresh()
