"""共享 UI 小对话框：提示框与「在资源管理器/访达打开路径」。

各 workspace（wokbee_view / mcp_workspace / skills_workspace / settings_workspace）
此前各带一份几乎相同的 `_tip` 与 `_open_path`，收敛到这里便于统一维护。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from tokbee.ui.combo_style import checkbox_qss
from tokbee.ui.styles.system import bind_text_edit_context_menu
from tokbee.ui.styles.theme import Theme

from wokbee.core.models import ApprovalFlags, MAX_PROJECT_TITLE_LEN


def tip(parent: QWidget, theme: Theme, message: str, title: str = "提示"):
    """模态提示框（「知道了」按钮）。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumSize(400, 160)
    dlg.resize(420, 200)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
    layout.addWidget(msg, stretch=1)
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


def open_path(path):
    """用系统默认方式打开路径（文件→其所在文件夹由调用方决定）。"""
    path = str(path)
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


# ─── 审核勾选控件（settings_workspace 与 wokbee 工作区共用） ───

def build_approval_checkboxes(
    theme: Theme,
    parent: QWidget | None = None,
) -> tuple[QWidget, dict[str, QCheckBox]]:
    """创建四个审核勾选控件，返回容器与 checkbox 字典。"""
    c = theme.colors
    box = QWidget(parent)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)

    defs = [
        ("skip_read", "读免审", "读操作免审"),
        ("skip_write", "写免审", "写操作免审"),
        ("skip_routine", "常规操作免审", "常规操作免审"),
        ("skip_high_risk", "高危操作免审", "本机命令、挂载外目录、读取凭据密码"),
    ]
    checks: dict[str, QCheckBox] = {}
    cb_qss = checkbox_qss(c)
    for key, label, tip in defs:
        cb = QCheckBox(label)
        cb.setToolTip(tip)
        cb.setStyleSheet(cb_qss)
        lay.addWidget(cb)
        checks[key] = cb

    return box, checks


def apply_flags_to_checks(checks: dict[str, QCheckBox], flags: ApprovalFlags) -> None:
    checks["skip_read"].setChecked(flags.skip_read)
    checks["skip_write"].setChecked(flags.skip_write)
    checks["skip_routine"].setChecked(flags.skip_routine)
    checks["skip_high_risk"].setChecked(flags.skip_high_risk)


def flags_from_checks(checks: dict[str, QCheckBox]) -> ApprovalFlags:
    return ApprovalFlags(
        skip_read=checks["skip_read"].isChecked(),
        skip_write=checks["skip_write"].isChecked(),
        skip_routine=checks["skip_routine"].isChecked(),
        skip_high_risk=checks["skip_high_risk"].isChecked(),
    )


# ─── 主题化小对话框：名称/目标/多行输入、审批表单、确认框 ───

def _title_from_goal(goal: str, max_len: int = MAX_PROJECT_TITLE_LEN) -> str:
    s = " ".join((goal or "").split())
    if not s:
        return "未命名项目"
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def _ask_text(
    parent: QWidget,
    theme: Theme,
    title: str,
    label: str,
    default: str = "",
    *,
    max_length: int | None = None,
) -> str | None:
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(400, 180)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(12)
    layout.addWidget(QLabel(label))
    inp = QLineEdit(default)
    inp.setFixedHeight(34)
    if max_length is not None and max_length > 0:
        inp.setMaxLength(max_length)
    inp.setStyleSheet(f"""
        QLineEdit {{
            background: {c["input_bg"]}; color: {c["text"]};
            border: 1px solid {c["input_border"]}; border-radius: 6px; padding: 0 10px;
        }}
    """)
    layout.addWidget(inp)
    layout.addStretch()
    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(72, 34)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("确定")
    ok.setFixedSize(72, 34)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_primary"]}; color: white;
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return inp.text().strip()
    return None


def _textedit_qss(theme: Theme) -> str:
    c = theme.colors
    return f"""
        QTextEdit {{
            background: {c["input_bg"]}; color: {c["text"]};
            border: 1px solid {c["input_border"]}; border-radius: 6px;
            padding: 8px; font-size: 13px;
        }}
        QTextEdit:focus {{ border: 1px solid {c["input_focus_border"]}; }}
    """


def _ask_multiline(
    parent: QWidget,
    theme: Theme,
    title: str,
    label: str,
    default: str = "",
    *,
    min_lines: int = 5,
) -> str | None:
    """多行文本输入（至少约 min_lines 行，超出滚动）。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setMinimumSize(480, 320)
    dlg.resize(520, 360)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)
    tip = QLabel(label)
    tip.setWordWrap(True)
    tip.setStyleSheet(f"font-size: 13px; color: {c['text']};")
    layout.addWidget(tip)
    inp = QTextEdit()
    inp.setPlainText(default or "")
    bind_text_edit_context_menu(inp, c)
    # ~22px/行 + padding
    inp.setMinimumHeight(max(5, min_lines) * 22 + 16)
    inp.setStyleSheet(_textedit_qss(theme))
    layout.addWidget(inp, stretch=1)
    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(72, 34)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("确定")
    ok.setFixedSize(72, 34)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_primary"]}; color: white;
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)
    if dlg.exec() == QDialog.DialogCode.Accepted:
        return inp.toPlainText().strip()
    return None


def _default_project_title(when: datetime | None = None) -> str:
    """默认名称：项目 + 创建时间（不超过名称上限）。"""
    dt = when or datetime.now()
    raw = f"项目{dt.strftime('%m-%d %H:%M')}"
    return raw[:MAX_PROJECT_TITLE_LEN]


def _prompt_approval_flags(
    parent: QWidget,
    theme: Theme,
    current: ApprovalFlags,
    title: str = "项目审核策略",
) -> ApprovalFlags | None:
    """弹窗编辑四个审核勾选项。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(420, 320)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(10)

    tip = QLabel(
        "勾选表示该级别免审；未勾选则执行时需要人工审批。仅影响当前项目。"
    )
    tip.setWordWrap(True)
    tip.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
    layout.addWidget(tip)

    box, checks = build_approval_checkboxes(theme)
    apply_flags_to_checks(checks, current)
    layout.addWidget(box)
    layout.addStretch()

    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(72, 34)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("保存")
    ok.setFixedSize(72, 34)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_primary"]}; color: white;
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)

    if dlg.exec() == QDialog.DialogCode.Accepted:
        return flags_from_checks(checks)
    return None


def _confirm(parent: QWidget, theme: Theme, title: str, message: str) -> bool:
    """主题化确认框，避免原生 QMessageBox 在 Windows 上发黑。"""
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setFixedSize(400, 170)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    layout.setSpacing(12)
    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
    layout.addWidget(msg)
    layout.addStretch()
    row = QHBoxLayout()
    row.addStretch()
    cancel = QPushButton("取消")
    cancel.setFixedSize(80, 34)
    cancel.setCursor(Qt.CursorShape.PointingHandCursor)
    cancel.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    cancel.clicked.connect(dlg.reject)
    ok = QPushButton("确定")
    ok.setFixedSize(80, 34)
    ok.setCursor(Qt.CursorShape.PointingHandCursor)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["danger"]}; color: white;
            border: none; border-radius: 6px; font-size: 13px;
        }}
        QPushButton:hover {{ background: {c["danger_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(cancel)
    row.addWidget(ok)
    layout.addLayout(row)
    return dlg.exec() == QDialog.DialogCode.Accepted
