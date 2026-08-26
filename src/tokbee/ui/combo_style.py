"""兼容层：请改用 ``tokbee.ui.styles.system``。

本模块再导出系统默认样式，避免旧 import 失效。
"""

from __future__ import annotations

from tokbee.ui.styles.system import (  # noqa: F401
    DEFAULT_COMBO_WIDTH,
    DEFAULT_COMBO_HEIGHT,
    apply_combo_popup_style,
    apply_form_combo,
    rounded_combo_qss,
    rounded_lineedit_qss,
    rounded_spin_qss,
    rounded_textedit_qss,
    underline_combo_qss,
    primary_btn_qss,
    secondary_btn_qss,
    danger_btn_qss,
    checkbox_qss,
    hint_label_qss,
    title_label_qss,
    section_label_qss,
    apply_lineedit,
    apply_textedit,
    apply_spin,
    apply_checkbox,
    apply_primary_btn,
    apply_secondary_btn,
    apply_danger_btn,
    style_hint_label,
)
