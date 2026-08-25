"""澄清意图弹窗：单选 / 多选 + 末尾自定义选项。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QLineEdit,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from tokbee.ui.styles.theme import Theme

CUSTOM_LABEL = "其他（请填写）"


class _QuestionBlock(QFrame):
    def __init__(self, question: dict[str, Any], theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.question = question
        self.qid = str(question.get("id") or "q")
        self.mode = str(question.get("mode") or "single")
        self.allow_custom = bool(question.get("allow_custom", True))
        self._radios: list[QRadioButton] = []
        self._checks: list[QCheckBox] = []
        self._group: QButtonGroup | None = None
        self._custom_edit: QLineEdit | None = None
        self._custom_toggle: QCheckBox | QRadioButton | None = None
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setStyleSheet(
            f"QFrame {{ background: {c.get('card_bg', '#fff')}; border: 1px solid "
            f"{c.get('border_light', '#e5e7eb')}; border-radius: 8px; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(8)

        title = QLabel(str(self.question.get("prompt") or "请选择"))
        title.setWordWrap(True)
        title.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {c['text']}; "
            f"background: transparent; border: none;"
        )
        lay.addWidget(title)

        hint = "可多选" if self.mode == "multi" else "单选"
        tip = QLabel(hint)
        tip.setStyleSheet(
            f"font-size: 11px; color: {c['text_hint']}; background: transparent; border: none;"
        )
        lay.addWidget(tip)

        options = [str(x) for x in (self.question.get("options") or []) if str(x).strip()]
        if self.mode == "multi":
            for opt in options:
                cb = QCheckBox(opt)
                cb.setStyleSheet(f"color: {c['text']}; background: transparent;")
                self._checks.append(cb)
                lay.addWidget(cb)
            if self.allow_custom:
                self._custom_toggle = QCheckBox(CUSTOM_LABEL)
                self._custom_toggle.setStyleSheet(
                    f"color: {c['text']}; background: transparent;"
                )
                self._custom_toggle.toggled.connect(self._on_custom_toggled)
                lay.addWidget(self._custom_toggle)
        else:
            self._group = QButtonGroup(self)
            self._group.setExclusive(True)
            for i, opt in enumerate(options):
                rb = QRadioButton(opt)
                rb.setStyleSheet(f"color: {c['text']}; background: transparent;")
                self._group.addButton(rb, i)
                self._radios.append(rb)
                lay.addWidget(rb)
            if self.allow_custom:
                self._custom_toggle = QRadioButton(CUSTOM_LABEL)
                self._custom_toggle.setStyleSheet(
                    f"color: {c['text']}; background: transparent;"
                )
                self._group.addButton(self._custom_toggle, len(options))
                self._custom_toggle.toggled.connect(self._on_custom_toggled)
                lay.addWidget(self._custom_toggle)
            if self._radios:
                self._radios[0].setChecked(True)

        if self.allow_custom:
            self._custom_edit = QLineEdit()
            self._custom_edit.setPlaceholderText("请输入自定义内容…")
            self._custom_edit.setEnabled(False)
            self._custom_edit.setStyleSheet(
                f"QLineEdit {{ background: {c['input_bg']}; color: {c['text']}; "
                f"border: 1px solid {c['input_border']}; border-radius: 6px; "
                f"padding: 6px 8px; }}"
            )
            lay.addWidget(self._custom_edit)

    def _on_custom_toggled(self, checked: bool):
        if self._custom_edit is not None:
            self._custom_edit.setEnabled(bool(checked))
            if checked:
                self._custom_edit.setFocus()

    def collect(self) -> dict[str, Any] | None:
        selected: list[str] = []
        custom = ""
        if self.mode == "multi":
            for cb in self._checks:
                if cb.isChecked():
                    selected.append(cb.text())
            if self._custom_toggle and self._custom_toggle.isChecked():
                custom = (self._custom_edit.text() if self._custom_edit else "").strip()
                if not custom:
                    return None  # 勾了自定义但没填
        else:
            chosen = None
            if self._group:
                btn = self._group.checkedButton()
                if btn is not None:
                    chosen = btn
            if chosen is None:
                return None
            if chosen is self._custom_toggle:
                custom = (self._custom_edit.text() if self._custom_edit else "").strip()
                if not custom:
                    return None
            else:
                selected.append(chosen.text())
        if not selected and not custom:
            return None
        return {"id": self.qid, "selected": selected, "custom": custom}


class AskUserDialog(QDialog):
    """展示 AI 澄清问题，返回 answers 字典。"""

    def __init__(self, payload: dict[str, Any], theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.payload = payload if isinstance(payload, dict) else {}
        self._blocks: list[_QuestionBlock] = []
        self._result: dict[str, Any] = {"cancelled": True}
        self.setWindowTitle("需要你确认一下")
        self.setModal(True)
        self.setMinimumWidth(420)
        self.setMaximumWidth(560)
        self._build()

    def _build(self):
        c = self.theme.colors
        self.setStyleSheet(f"QDialog {{ background: {c.get('content_bg', '#fafafa')}; }}")
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        head = QLabel("AI 需要你澄清意图后才能继续")
        head.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {c['text']}; border: none;"
        )
        root.addWidget(head)
        sub = QLabel("请完成下列选择题（含「其他」时可自行填写）。")
        sub.setWordWrap(True)
        sub.setStyleSheet(
            f"font-size: 12px; color: {c['text_hint']}; border: none;"
        )
        root.addWidget(sub)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 8, 0)
        bl.setSpacing(10)

        for q in self.payload.get("questions") or []:
            if not isinstance(q, dict):
                continue
            block = _QuestionBlock(q, self.theme, parent=body)
            self._blocks.append(block)
            bl.addWidget(block)
        bl.addStretch(1)
        scroll.setWidget(body)
        scroll.setMinimumHeight(min(420, 120 + 110 * max(1, len(self._blocks))))
        root.addWidget(scroll, stretch=1)

        self._err = QLabel("")
        self._err.setStyleSheet(f"color: {c.get('danger', '#e11d48')}; font-size: 12px;")
        self._err.setVisible(False)
        root.addWidget(self._err)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认提交")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_accept(self):
        answers: list[dict[str, Any]] = []
        for block in self._blocks:
            item = block.collect()
            if item is None:
                self._err.setText("请完成所有题目；若选「其他」请填写内容。")
                self._err.setVisible(True)
                return
            answers.append(item)
        self._result = {"cancelled": False, "answers": answers}
        self.accept()

    def result_payload(self) -> dict[str, Any]:
        return dict(self._result)
