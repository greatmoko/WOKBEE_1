"""AI 配置 — Skills 管理。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QLineEdit, QDialog, QCheckBox, QFileDialog,
)

from tokbee.ui.styles.theme import Theme
from wokbee.core.skills_store import SkillsStore, SkillInfo
from wokbee.ui.dialogs import open_path as _open_path, tip as _tip


class _SkillCard(QFrame):
    toggled = Signal(str, bool)
    open_clicked = Signal(str)
    delete_clicked = Signal(str)

    def __init__(self, skill: SkillInfo, theme: Theme, parent=None):
        super().__init__(parent)
        self.skill = skill
        self.theme = theme
        c = theme.colors
        self.setObjectName("skillCard")
        self.setStyleSheet(f"""
            QFrame#skillCard {{
                background: {c["card_bg"]};
                border: 1px solid {c["border_light"]};
                border-radius: 8px;
            }}
            QFrame#skillCard QLabel, QFrame#skillCard QCheckBox {{
                background: transparent; border: none;
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(10)

        info = QVBoxLayout()
        title = QLabel(skill.name)
        title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {c['text']}; background: transparent; border: none;")
        info.addWidget(title)
        desc = QLabel(skill.description or skill.path.name)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']}; background: transparent; border: none;")
        info.addWidget(desc)
        path = QLabel(str(skill.path))
        path.setStyleSheet(f"font-size: 11px; color: {c['text_hint']}; background: transparent; border: none;")
        info.addWidget(path)
        lay.addLayout(info, stretch=1)

        from tokbee.ui.combo_style import checkbox_qss, secondary_btn_qss
        self._chk = QCheckBox("启用")
        self._chk.setChecked(skill.enabled)
        self._chk.setStyleSheet(checkbox_qss(c))
        self._chk.toggled.connect(lambda v: self.toggled.emit(skill.path.name, v))
        lay.addWidget(self._chk)

        open_btn = QPushButton("打开")
        open_btn.setFixedHeight(30)
        open_btn.setMinimumWidth(56)
        open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        open_btn.setAutoDefault(False)
        open_btn.setStyleSheet(secondary_btn_qss(c))
        open_btn.clicked.connect(lambda: self.open_clicked.emit(skill.path.name))
        lay.addWidget(open_btn)

        del_btn = QPushButton("删除")
        del_btn.setFixedHeight(30)
        del_btn.setMinimumWidth(56)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setAutoDefault(False)
        del_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {c["danger"]};
                border: 1px solid {c["border"]}; border-radius: 6px;
                padding: 0 10px; text-decoration: none;
            }}
            QPushButton:hover {{ background: #fff1f0; }}
            QPushButton:focus {{ outline: none; }}
        """)
        del_btn.clicked.connect(lambda: self.delete_clicked.emit(skill.path.name))
        lay.addWidget(del_btn)

class SkillsWorkspace(QWidget):
    def __init__(self, theme: Theme, store: SkillsStore | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store or SkillsStore()
        self._build()
        self.refresh()

    def _build(self):
        c = self.theme.colors
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(f"background: {c['content_bg']}; border: none;")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(28, 20, 28, 12)
        title = QLabel("Skills")
        title.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        hl.addWidget(title)
        tip = QLabel(
            "技能以文件夹 + SKILL.md 形式存放在全局目录（默认 ~/.wokbee/skills）。"
            "启用后，WokBee 运行时通过 /skills/ 挂载该目录（不复制到每个项目）；"
            "Agent 可用 read_file/edit_file 直接编辑（走写操作审批）。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(
            f"font-size: 12px; color: {c['text_hint']};"
            "background: transparent; border: none;"
        )
        hl.addWidget(tip)
        root.addWidget(header)

        bar = QHBoxLayout()
        bar.setContentsMargins(28, 12, 28, 8)
        bar.setSpacing(8)
        self._root_edit = QLineEdit(str(self.store.root))
        self._root_edit.setFixedHeight(34)
        self._root_edit.setReadOnly(True)
        from tokbee.ui.combo_style import rounded_lineedit_qss, secondary_btn_qss
        self._root_edit.setStyleSheet(rounded_lineedit_qss(c))
        bar.addWidget(self._root_edit, stretch=1)
        browse = QPushButton("目录…")
        browse.setFixedHeight(34)
        browse.setMinimumWidth(72)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.setAutoDefault(False)
        browse.setStyleSheet(secondary_btn_qss(c))
        browse.clicked.connect(self._browse_root)
        bar.addWidget(browse)
        add_btn = QPushButton("新建 Skill")
        add_btn.setFixedHeight(34)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; padding: 0 14px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        add_btn.clicked.connect(self._on_create)
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

    def refresh(self):
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        skills = self.store.list_skills()
        c = self.theme.colors
        if not skills:
            empty = QLabel("暂无 Skill。点击「新建 Skill」开始。")
            empty.setStyleSheet(f"color: {c['text_hint']}; padding: 20px;")
            self._list_layout.addWidget(empty)
            return
        for s in skills:
            card = _SkillCard(s, self.theme)
            card.toggled.connect(self._on_toggle)
            card.open_clicked.connect(self._on_open)
            card.delete_clicked.connect(self._on_delete)
            self._list_layout.addWidget(card)
        self._root_edit.setText(str(self.store.root))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def _browse_root(self):
        path = QFileDialog.getExistingDirectory(self, "选择 Skills 根目录", str(self.store.root))
        if path:
            self.store.set_root(path)
            self.refresh()

    def _on_create(self):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("新建 Skill")
        dlg.setFixedSize(400, 180)
        dlg.setStyleSheet(f"background: {c['content_bg']};")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.addWidget(QLabel("名称（文件夹名）"))
        name = QLineEdit()
        name.setFixedHeight(32)
        lay.addWidget(name)
        lay.addWidget(QLabel("描述"))
        desc = QLineEdit()
        desc.setFixedHeight(32)
        lay.addWidget(desc)
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dlg.reject)
        ok = QPushButton("创建")
        ok.clicked.connect(dlg.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if not name.text().strip():
            _tip(self, self.theme, "请填写名称")
            return
        skill = self.store.create(name.text().strip(), desc.text().strip())
        _open_path(skill.path / "SKILL.md")
        self.refresh()

    def _on_toggle(self, folder: str, enabled: bool):
        self.store.set_enabled(folder, enabled)

    def _on_open(self, folder: str):
        _open_path(self.store.root / folder)

    def _on_delete(self, folder: str):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("删除 Skill")
        dlg.setFixedSize(380, 140)
        dlg.setStyleSheet(f"background: {c['content_bg']};")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.addWidget(QLabel(f"确定删除技能「{folder}」？此操作不可恢复。"))
        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(dlg.reject)
        ok = QPushButton("删除")
        ok.clicked.connect(dlg.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.store.delete(folder)
            self.refresh()
