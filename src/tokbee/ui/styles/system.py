"""WokBee / TokBee / AutoBee 系统默认 UI 样式（权威入口）。

配色见 ``tokbee.ui.styles.theme.COLORS``。
控件 QSS / 应用函数见本模块。

新增界面请优先::

    from tokbee.ui.styles.system import (
        apply_form_combo, apply_lineedit, apply_checkbox,
        primary_btn_qss, secondary_btn_qss, checkbox_qss, ...
    )

仅当产品明确要求自定义外观时，才写局部 QSS。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QSize, QPoint
from PySide6.QtGui import QColor, QImage, QPainter, QPolygon, QPalette
from PySide6.QtWidgets import (
    QStyledItemDelegate, QStyle, QStyleOptionViewItem, QComboBox, QFrame,
    QStyleFactory, QApplication, QLineEdit, QCheckBox, QAbstractSpinBox,
    QPushButton, QTextEdit, QLabel, QMenu, QWidget,
)
from tokbee.ui.styles.theme import COLORS

# 资源目录：src/tokbee/resources
_RES = Path(__file__).resolve().parent.parent.parent / "resources"
_ARROW_PNG = _RES / "combo_down.png"

# ── 尺寸约定 ──────────────────────────────────────────────
RADIUS = 6
DEFAULT_COMBO_WIDTH = 300
DEFAULT_COMBO_HEIGHT = 40
DEFAULT_INPUT_HEIGHT = 34
DEFAULT_BTN_HEIGHT = 34


def _ensure_arrow_png() -> Path:
    """实心下三角 PNG（Windows 上 QSS SVG 不稳定）。"""
    if _ARROW_PNG.exists() and _ARROW_PNG.stat().st_size > 0:
        return _ARROW_PNG
    app = QApplication.instance()
    owns = False
    if app is None:
        app = QApplication([])
        owns = True
    img = QImage(24, 16, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#888888"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPolygon(QPolygon([QPoint(2, 2), QPoint(22, 2), QPoint(12, 14)]))
    painter.end()
    _RES.mkdir(parents=True, exist_ok=True)
    img.save(str(_ARROW_PNG))
    if owns:
        app.quit()
    return _ARROW_PNG


def _arrow_url() -> str:
    return f"file:///{_ensure_arrow_png().resolve().as_posix()}"


def _ensure_check_png() -> Path:
    path = _RES / "checkbox_check.png"
    if path.exists() and path.stat().st_size > 0:
        return path
    app = QApplication.instance()
    owns = False
    if app is None:
        app = QApplication([])
        owns = True
    img = QImage(16, 16, QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = painter.pen()
    pen.setColor(QColor("#ffffff"))
    pen.setWidth(2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawLine(3, 8, 6, 11)
    painter.drawLine(6, 11, 12, 4)
    painter.end()
    _RES.mkdir(parents=True, exist_ok=True)
    img.save(str(path))
    if owns:
        app.quit()
    return path


def _check_url() -> str:
    return f"file:///{_ensure_check_png().resolve().as_posix()}"


class _NoFocusDelegate(QStyledItemDelegate):
    """下拉选项自绘，去掉焦点黑框。"""

    def __init__(self, parent=None, *, hover_color: str = "#e5e5e5", text_color: str = "#1a1a1a"):
        super().__init__(parent)
        self._hover = QColor(hover_color)
        self._text = QColor(text_color)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        rect = opt.rect
        hovered = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        if hovered or selected:
            painter.fillRect(rect, self._hover)
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text is None:
            text = ""
        painter.save()
        painter.setPen(self._text)
        painter.drawText(
            rect.adjusted(10, 0, -10, 0),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            str(text),
        )
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), 28))


# ── QSS 生成 ──────────────────────────────────────────────

def underline_combo_qss(colors: dict) -> str:
    """扁平仅下边框下拉（特殊场景；表单默认用 rounded）。"""
    text = colors.get("text", "#1a1a1a")
    hint = colors.get("text_hint", "#b0b0b0")
    border = colors.get("input_border", "#e0e0e0")
    focus = colors.get("input_focus_border", colors.get("accent", "#07c160"))
    arrow = _arrow_url()
    return f"""
        QComboBox {{
            background: transparent; color: {text};
            border: none; border-bottom: 1px solid {border}; border-radius: 0;
            padding: 2px 28px 2px 4px; font-size: 13px; min-height: 34px;
        }}
        QComboBox:hover {{ border-bottom: 1px solid {focus}; }}
        QComboBox:focus {{ border-bottom: 1px solid {focus}; }}
        QComboBox:disabled {{ color: {hint}; }}
        QComboBox::drop-down {{ border: none; width: 28px; background: transparent; }}
        QComboBox::down-arrow {{ image: url("{arrow}"); width: 12px; height: 8px; }}
    """


def rounded_combo_qss(colors: dict) -> str:
    """默认圆角下拉：高 40，实心三角。"""
    text = colors.get("text", "#1a1a1a")
    hint = colors.get("text_hint", "#b0b0b0")
    bg = colors.get("input_bg", "#f5f5f5")
    border = colors.get("input_border", "#e0e0e0")
    focus = colors.get("input_focus_border", colors.get("accent", "#07c160"))
    arrow = _arrow_url()
    return f"""
        QComboBox {{
            background: {bg}; color: {text};
            border: 1px solid {border}; border-radius: {RADIUS}px;
            padding: 0 28px 0 12px; font-size: 13px;
            min-height: {DEFAULT_COMBO_HEIGHT}px; max-height: {DEFAULT_COMBO_HEIGHT}px;
        }}
        QComboBox:hover {{ border: 1px solid {focus}; }}
        QComboBox:focus {{ border: 1px solid {focus}; }}
        QComboBox:disabled {{ color: {hint}; }}
        QComboBox::drop-down {{
            subcontrol-origin: padding; subcontrol-position: center right;
            border: none; width: 28px; background: transparent;
        }}
        QComboBox::down-arrow {{ image: url("{arrow}"); width: 12px; height: 8px; }}
    """


def rounded_lineedit_qss(colors: dict) -> str:
    text = colors.get("text", "#1a1a1a")
    bg = colors.get("input_bg", "#f5f5f5")
    border = colors.get("input_border", "#e0e0e0")
    focus = colors.get("input_focus_border", colors.get("accent", "#07c160"))
    return f"""
        QLineEdit {{
            background: {bg}; color: {text};
            border: 1px solid {border}; border-radius: {RADIUS}px;
            padding: 0 10px; font-size: 13px; min-height: {DEFAULT_INPUT_HEIGHT}px;
        }}
        QLineEdit:focus {{ border: 1px solid {focus}; }}
    """


def rounded_textedit_qss(colors: dict) -> str:
    text = colors.get("text", "#1a1a1a")
    bg = colors.get("input_bg", "#f5f5f5")
    border = colors.get("input_border", "#e0e0e0")
    focus = colors.get("input_focus_border", colors.get("accent", "#07c160"))
    return f"""
        QTextEdit {{
            background: {bg}; color: {text};
            border: 1px solid {border}; border-radius: 8px;
            padding: 8px; font-size: 13px;
        }}
        QTextEdit:focus {{ border: 1px solid {focus}; }}
    """


def rounded_spin_qss(colors: dict) -> str:
    text = colors.get("text", "#1a1a1a")
    hint = colors.get("text_hint", "#b0b0b0")
    bg = colors.get("input_bg", "#f5f5f5")
    border = colors.get("input_border", "#e0e0e0")
    focus = colors.get("input_focus_border", colors.get("accent", "#07c160"))
    return f"""
        QSpinBox, QDoubleSpinBox {{
            background: {bg}; color: {text};
            border: 1px solid {border}; border-radius: {RADIUS}px;
            padding: 2px 6px; font-size: 13px; min-height: 32px;
        }}
        QSpinBox:focus, QDoubleSpinBox:focus {{ border: 1px solid {focus}; }}
        QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {hint}; }}
        QSpinBox::up-button, QSpinBox::down-button,
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
            width: 18px; border: none; background: transparent;
        }}
    """


def primary_btn_qss(colors: dict) -> str:
    """主按钮：绿底白字。"""
    return f"""
        QPushButton {{
            background: {colors.get("btn_primary", "#07c160")}; color: #ffffff;
            border: none; border-radius: {RADIUS}px;
            padding: 0 14px; font-size: 13px; text-decoration: none; outline: none;
        }}
        QPushButton:hover {{ background: {colors.get("btn_primary_hover", "#06ad56")}; }}
        QPushButton:disabled {{
            background: {colors.get("btn_bg", "#f0f0f0")};
            color: {colors.get("text_hint", "#b0b0b0")};
        }}
        QPushButton:focus {{ outline: none; }}
    """


def secondary_btn_qss(colors: dict) -> str:
    """次要按钮：灰底黑字。"""
    text = colors.get("text", "#1a1a1a")
    bg = colors.get("btn_bg", "#f0f0f0")
    hover = colors.get("btn_hover", "#e5e5e5")
    return f"""
        QPushButton {{
            background: {bg}; color: {text};
            border: none; border-radius: {RADIUS}px;
            padding: 0 14px; font-size: 13px; text-decoration: none; outline: none;
        }}
        QPushButton:hover {{ background: {hover}; }}
        QPushButton:focus {{ outline: none; border: none; }}
        QPushButton:pressed {{ background: {hover}; }}
        QPushButton:disabled {{ color: {colors.get("text_hint", "#b0b0b0")}; }}
    """


def danger_btn_qss(colors: dict) -> str:
    """危险操作：灰底红字。"""
    return f"""
        QPushButton {{
            background: {colors.get("btn_bg", "#f0f0f0")};
            color: {colors.get("danger", "#f56c6c")};
            border: none; border-radius: {RADIUS}px;
            padding: 0 14px; font-size: 13px; text-decoration: none; outline: none;
        }}
        QPushButton:hover {{ background: {colors.get("btn_hover", "#e5e5e5")}; }}
        QPushButton:disabled {{ color: {colors.get("text_hint", "#b0b0b0")}; }}
        QPushButton:focus {{ outline: none; }}
    """


def checkbox_qss(colors: dict) -> str:
    """勾选框：绿色选中 + 白勾。"""
    text = colors.get("text", "#1a1a1a")
    accent = colors.get("accent", "#07c160")
    border = colors.get("input_border", "#e0e0e0")
    bg = colors.get("input_bg", "#f5f5f5")
    check = _check_url()
    return f"""
        QCheckBox {{
            color: {text}; font-size: 13px;
            background: transparent; border: none; spacing: 6px;
        }}
        QCheckBox:focus {{ outline: none; border: none; }}
        QCheckBox::indicator {{
            width: 16px; height: 16px;
            border: 1px solid {border}; border-radius: 3px; background: {bg};
        }}
        QCheckBox::indicator:hover {{ border-color: {accent}; }}
        QCheckBox::indicator:checked {{
            background: {accent}; border-color: {accent};
            image: url("{check}");
        }}
        QCheckBox::indicator:disabled {{
            background: {border}; border-color: {border};
        }}
    """


def hint_label_qss(colors: dict) -> str:
    return (
        f"font-size: 11px; color: {colors.get('text_hint', '#b0b0b0')};"
        " background: transparent; border: none;"
    )


def title_label_qss(colors: dict, *, size: int = 20) -> str:
    return (
        f"font-size: {size}px; font-weight: bold; color: {colors.get('text', '#1a1a1a')};"
        " background: transparent; border: none;"
    )


def section_label_qss(colors: dict) -> str:
    return (
        f"font-size: 14px; font-weight: bold; color: {colors.get('text', '#1a1a1a')};"
        " background: transparent; border: none;"
    )


# ── 一键应用到控件 ────────────────────────────────────────

def apply_combo_popup_style(
    combo: QComboBox,
    colors: dict,
    *,
    underline: bool = False,
    rounded: bool = False,
    fixed_width: int | None = None,
    fixed_height: int | None = None,
):
    """应用下拉框样式与弹出列表样式。

    表单默认请用 ``apply_form_combo``（rounded + 300×40）。
    """
    hover = colors.get("btn_hover", "#e5e5e5")
    bg = colors.get("content_bg", "#ffffff")
    text = colors.get("text", "#1a1a1a")
    border = colors.get("input_border", "#e0e0e0")

    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        combo.setStyle(fusion)

    if underline:
        combo.setStyleSheet(underline_combo_qss(colors))
    elif rounded:
        combo.setStyleSheet(rounded_combo_qss(colors))

    if fixed_width is not None and fixed_height is not None:
        combo.setFixedSize(fixed_width, fixed_height)
    elif fixed_width is not None:
        combo.setFixedWidth(fixed_width)
    elif fixed_height is not None:
        combo.setFixedHeight(fixed_height)
    elif rounded:
        combo.setFixedHeight(DEFAULT_COMBO_HEIGHT)

    view = combo.view()
    view.setItemDelegate(_NoFocusDelegate(view, hover_color=hover, text_color=text))
    view.setFrameShape(QFrame.Shape.NoFrame)
    view.setMouseTracking(True)
    view.setStyleSheet(f"""
        QAbstractItemView, QListView {{
            background: {bg}; border: 1px solid {border}; outline: 0px;
        }}
        QAbstractItemView::item, QListView::item {{
            border: none; outline: 0px;
        }}
    """)


def apply_form_combo(combo: QComboBox, colors: dict, *, width: int = DEFAULT_COMBO_WIDTH):
    """表单默认下拉：圆角 + 固定宽高 + 实心三角。"""
    apply_combo_popup_style(
        combo, colors, rounded=True,
        fixed_width=width, fixed_height=DEFAULT_COMBO_HEIGHT,
    )


def apply_lineedit(edit: QLineEdit, colors: dict, *, height: int = DEFAULT_INPUT_HEIGHT):
    edit.setFixedHeight(height)
    edit.setStyleSheet(rounded_lineedit_qss(colors))


def apply_textedit(edit: QTextEdit, colors: dict):
    edit.setStyleSheet(rounded_textedit_qss(colors))


def apply_spin(spin: QAbstractSpinBox, colors: dict):
    spin.setStyleSheet(rounded_spin_qss(colors))


def apply_checkbox(chk: QCheckBox, colors: dict):
    chk.setStyleSheet(checkbox_qss(colors))


def apply_primary_btn(btn: QPushButton, colors: dict, *, height: int = DEFAULT_BTN_HEIGHT):
    btn.setFixedHeight(height)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(primary_btn_qss(colors))


def apply_secondary_btn(btn: QPushButton, colors: dict, *, height: int = DEFAULT_BTN_HEIGHT):
    btn.setFixedHeight(height)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoDefault(False)
    btn.setDefault(False)
    btn.setStyleSheet(secondary_btn_qss(colors))


def apply_danger_btn(btn: QPushButton, colors: dict, *, height: int = DEFAULT_BTN_HEIGHT):
    btn.setFixedHeight(height)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoDefault(False)
    btn.setDefault(False)
    btn.setStyleSheet(danger_btn_qss(colors))


def style_hint_label(lbl: QLabel, colors: dict):
    lbl.setWordWrap(True)
    lbl.setFrameShape(QFrame.Shape.NoFrame)
    lbl.setStyleSheet(hint_label_qss(colors))


def context_menu_qss(colors: dict) -> str:
    """右键菜单 QSS：白底黑字，避免跟随系统深色主题导致看不清。"""
    bg = colors.get("content_bg", "#ffffff")
    text = colors.get("text", "#1a1a1a")
    border = colors.get("border", "#e5e5e5")
    hover = colors.get("subnav_hover", "#f0f0f0")
    hint = colors.get("text_hint", "#b0b0b0")
    return f"""
        QMenu {{
            background-color: {bg};
            color: {text};
            border: 1px solid {border};
            border-radius: 6px;
            padding: 4px;
        }}
        QMenu::item {{
            background-color: transparent;
            color: {text};
            padding: 6px 20px;
            border-radius: 4px;
            font-size: 12px;
        }}
        QMenu::item:selected {{
            background-color: {hover};
            color: {text};
        }}
        QMenu::item:disabled {{
            color: {hint};
        }}
        QMenu::separator {{
            height: 1px;
            background: {border};
            margin: 4px 8px;
        }}
    """


def apply_context_menu(menu: QMenu, colors: dict) -> None:
    """应用统一的浅色右键菜单样式（QSS + Palette 双保险）。"""
    bg = colors.get("content_bg", "#ffffff")
    text = colors.get("text", "#1a1a1a")
    hover = colors.get("subnav_hover", "#f0f0f0")
    menu.setStyleSheet(context_menu_qss(colors))
    pal = menu.palette()
    pal.setColor(QPalette.ColorRole.Window, QColor(bg))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(text))
    pal.setColor(QPalette.ColorRole.Base, QColor(bg))
    pal.setColor(QPalette.ColorRole.Text, QColor(text))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(text))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(hover))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(text))
    menu.setPalette(pal)


def make_context_menu(parent: QWidget | None, colors: dict) -> QMenu:
    """创建已套用系统样式的右键菜单。"""
    menu = QMenu(parent)
    apply_context_menu(menu, colors)
    return menu


def exec_text_edit_context_menu(edit: QTextEdit, event, colors: dict) -> None:
    """QTextEdit 统一右键菜单：撤销/复制/粘贴等，白底黑字。"""
    menu = make_context_menu(edit, colors)
    undo_act = menu.addAction("撤销")
    undo_act.setEnabled(edit.document().isUndoAvailable())
    redo_act = menu.addAction("重做")
    redo_act.setEnabled(edit.document().isRedoAvailable())
    menu.addSeparator()
    has_sel = edit.textCursor().hasSelection()
    cut_act = menu.addAction("剪切")
    cut_act.setEnabled(has_sel and not edit.isReadOnly())
    copy_act = menu.addAction("复制")
    copy_act.setEnabled(has_sel)
    paste_act = menu.addAction("粘贴")
    paste_act.setEnabled(edit.canPaste() and not edit.isReadOnly())
    menu.addSeparator()
    select_all_act = menu.addAction("全选")
    action = menu.exec(event.globalPos())
    if action == undo_act:
        edit.undo()
    elif action == redo_act:
        edit.redo()
    elif action == cut_act:
        edit.cut()
    elif action == copy_act:
        edit.copy()
    elif action == paste_act:
        edit.paste()
    elif action == select_all_act:
        edit.selectAll()
    event.accept()


def bind_text_edit_context_menu(edit: QTextEdit, colors: dict) -> None:
    """为任意 QTextEdit 绑定统一右键菜单（无需改子类）。"""
    menu_colors = dict(colors)

    def _context_menu_event(event):
        exec_text_edit_context_menu(edit, event, menu_colors)

    edit.contextMenuEvent = _context_menu_event  # type: ignore[method-assign]
