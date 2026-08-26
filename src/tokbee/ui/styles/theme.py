"""主题与配色方案。

控件默认样式见 ``tokbee.ui.styles.system``（权威入口）。
"""

from __future__ import annotations

COLORS = {
    "bg": "#ffffff",
    "sidebar_bg": "#f7f7f7",
    "sidebar_hover": "#f0f0f0",
    "sidebar_active": "#e8f5e9",
    "sidebar_text": "#999999",
    "sidebar_text_active": "#07c160",
    "subnav_bg": "#fafafa",
    "subnav_hover": "#f0f0f0",
    "subnav_active": "#e8f5e9",
    "subnav_text": "#555555",
    "subnav_text_active": "#07c160",
    "content_bg": "#ffffff",
    "card_bg": "#f8f8f8",
    "card_hover": "#f2f2f2",
    "text": "#1a1a1a",
    "text_secondary": "#666666",
    "text_hint": "#b0b0b0",
    "accent": "#07c160",
    "accent_hover": "#06ad56",
    "accent_light": "#e8f5e9",
    "border": "#e5e5e5",
    "border_light": "#f0f0f0",
    "btn_bg": "#f0f0f0",
    "btn_hover": "#e5e5e5",
    "btn_primary": "#07c160",
    "btn_primary_hover": "#06ad56",
    "danger": "#f56c6c",
    "danger_hover": "#e04b4b",
    "success": "#07c160",
    "warning": "#faad14",
    "input_bg": "#f5f5f5",
    "input_border": "#e0e0e0",
    "input_focus_border": "#07c160",
    "scrollbar": "#d0d0d0",
    "scrollbar_hover": "#b0b0b0",
    "tag_bg": "#f0f0f0",
    "tooltip_bg": "#ffffff",
    "tooltip_text": "#1a1a1a",
    "tooltip_border": "#e0e0e0",
}


class Theme:
    """WokBee 主题管理器。"""

    def __init__(self, **_kw):
        self.colors: dict[str, str] = dict(COLORS)

    def stylesheet(self) -> str:
        """返回全局 QSS 样式表。"""
        c = self.colors
        return f"""
            * {{
                font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
                font-size: 13px;
            }}
            QMainWindow {{
                background-color: {c["bg"]};
            }}

            /* --- 滚动条 --- */
            QScrollArea {{
                border: none;
                background: transparent;
            }}
            QScrollBar:vertical {{
                width: 6px;
                background: transparent;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {c["scrollbar"]};
                min-height: 30px;
                border-radius: 3px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {c["scrollbar_hover"]};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical,
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                height: 0; background: none;
            }}

            /* --- 输入框 --- */
            QLineEdit {{
                background: {c["input_bg"]};
                border: 1px solid {c["input_border"]};
                border-radius: 6px;
                padding: 6px 10px;
                color: {c["text"]};
                font-size: 13px;
            }}
            QLineEdit:focus {{
                border-color: {c["input_focus_border"]};
            }}
            QLineEdit[readOnly="true"] {{
                color: {c["text_hint"]};
            }}

            /* --- 工具提示：黑字白底；带边框以便圆角尽量生效 --- */
            QToolTip {{
                background-color: {c["tooltip_bg"]};
                color: {c["tooltip_text"]};
                border: 1px solid {c["tooltip_border"]};
                border-radius: 6px;
                padding: 6px 10px;
                font-size: 12px;
            }}
        """
