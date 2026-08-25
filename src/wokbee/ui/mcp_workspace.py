"""AI 配置 — MCP 服务器管理。"""

from __future__ import annotations

import json
import shlex

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QLineEdit, QDialog, QCheckBox, QComboBox,
    QTextEdit,
)

from tokbee.ui.styles.theme import Theme
from wokbee.core.mcp_store import McpServerConfig, McpStore


def _tip(parent: QWidget, theme: Theme, message: str):
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("提示")
    dlg.setFixedSize(420, 180)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    lay = QVBoxLayout(dlg)
    lay.setContentsMargins(24, 20, 24, 18)
    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 13px; color: {c['text']};")
    lay.addWidget(msg)
    lay.addStretch()
    row = QHBoxLayout()
    row.addStretch()
    ok = QPushButton("知道了")
    ok.setFixedSize(80, 34)
    ok.setStyleSheet(f"""
        QPushButton {{
            background: {c["btn_bg"]}; color: {c["text"]};
            border: none; border-radius: 6px;
        }}
        QPushButton:hover {{ background: {c["btn_hover"]}; }}
    """)
    ok.clicked.connect(dlg.accept)
    row.addWidget(ok)
    lay.addLayout(row)
    dlg.exec()


class _McpCard(QFrame):
    toggled = Signal(str, bool)
    edit_clicked = Signal(str)
    delete_clicked = Signal(str)
    test_clicked = Signal(str)

    def __init__(self, server: McpServerConfig, theme: Theme, parent=None):
        super().__init__(parent)
        self.server = server
        self.theme = theme
        c = theme.colors
        self.setStyleSheet(f"""
            _McpCard {{
                background: {c["card_bg"]};
                border: 1px solid {c["border_light"]};
                border-radius: 8px;
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)

        info = QVBoxLayout()
        title = QLabel(server.name)
        title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {c['text']}; background: transparent;")
        info.addWidget(title)
        if server.transport == "stdio":
            detail = f"stdio · {server.command} {' '.join(server.args)}".strip()
        else:
            detail = f"{server.transport} · {server.url}"
        desc = QLabel(detail)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']}; background: transparent;")
        info.addWidget(desc)
        lay.addLayout(info, stretch=1)

        chk = QCheckBox("启用")
        chk.setChecked(server.enabled)
        chk.toggled.connect(lambda v: self.toggled.emit(server.id, v))
        lay.addWidget(chk)

        for text, sig in (("测试", self.test_clicked), ("编辑", self.edit_clicked), ("删除", self.delete_clicked)):
            btn = QPushButton(text)
            btn.setFixedHeight(30)
            if text == "删除":
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent; color: {c["danger"]};
                        border: 1px solid {c["border"]}; border-radius: 6px; padding: 0 10px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {c["btn_bg"]}; color: {c["text"]};
                        border: none; border-radius: 6px; padding: 0 10px;
                    }}
                    QPushButton:hover {{ background: {c["btn_hover"]}; }}
                """)
            btn.clicked.connect(lambda _, sid=server.id, s=sig: s.emit(sid))
            lay.addWidget(btn)


class _McpEditor(QDialog):
    def __init__(self, theme: Theme, server: McpServerConfig | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._server = server or McpServerConfig(name="新 MCP")
        self.setWindowTitle("编辑 MCP" if server else "添加 MCP")
        self.setFixedSize(520, 420)
        c = theme.colors
        self.setStyleSheet(f"background: {c['content_bg']};")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(8)

        lay.addWidget(QLabel("名称"))
        self._name = QLineEdit(self._server.name)
        self._name.setFixedHeight(32)
        lay.addWidget(self._name)

        lay.addWidget(QLabel("传输方式"))
        self._transport = QComboBox()
        self._transport.addItem("stdio（本地进程）", "stdio")
        self._transport.addItem("SSE", "sse")
        self._transport.addItem("Streamable HTTP", "streamable_http")
        idx = self._transport.findData(self._server.transport)
        self._transport.setCurrentIndex(idx if idx >= 0 else 0)
        self._transport.currentIndexChanged.connect(self._sync_fields)
        lay.addWidget(self._transport)

        self._cmd_label = QLabel("命令（如 npx / uvx / python）")
        lay.addWidget(self._cmd_label)
        self._command = QLineEdit(self._server.command)
        self._command.setFixedHeight(32)
        lay.addWidget(self._command)

        self._args_label = QLabel("参数（空格分隔）")
        lay.addWidget(self._args_label)
        self._args = QLineEdit(shlex.join(self._server.args) if self._server.args else "")
        self._args.setFixedHeight(32)
        lay.addWidget(self._args)

        self._url_label = QLabel("URL（SSE / HTTP）")
        lay.addWidget(self._url_label)
        self._url = QLineEdit(self._server.url)
        self._url.setFixedHeight(32)
        lay.addWidget(self._url)

        lay.addWidget(QLabel("环境变量 JSON（可选，如 {\"KEY\":\"val\"}）"))
        self._env = QTextEdit()
        self._env.setFixedHeight(70)
        self._env.setPlainText(json.dumps(self._server.env, ensure_ascii=False) if self._server.env else "")
        lay.addWidget(self._env)

        self._sync_fields()

        row = QHBoxLayout()
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; padding: 6px 16px;
            }}
        """)
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)

    def _sync_fields(self):
        t = self._transport.currentData()
        is_stdio = t == "stdio"
        for w in (self._cmd_label, self._command, self._args_label, self._args):
            w.setVisible(is_stdio)
        for w in (self._url_label, self._url):
            w.setVisible(not is_stdio)

    def result_server(self) -> McpServerConfig | None:
        name = self._name.text().strip()
        if not name:
            return None
        t = self._transport.currentData() or "stdio"
        env = {}
        raw_env = self._env.toPlainText().strip()
        if raw_env:
            try:
                parsed = json.loads(raw_env)
                if isinstance(parsed, dict):
                    env = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                return None
        args_text = self._args.text().strip()
        args = shlex.split(args_text, posix=False) if args_text else []
        return McpServerConfig(
            id=self._server.id,
            name=name,
            enabled=self._server.enabled,
            transport=str(t),
            command=self._command.text().strip(),
            args=args,
            url=self._url.text().strip(),
            env=env,
            cwd=self._server.cwd,
        )


class McpWorkspace(QWidget):
    def __init__(self, theme: Theme, store: McpStore | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.store = store or McpStore()
        self._build()
        self.refresh()

    def _build(self):
        c = self.theme.colors
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        header = QFrame()
        header.setStyleSheet(f"background: {c['content_bg']}; border-bottom: 1px solid {c['border']};")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(28, 20, 28, 12)
        title = QLabel("MCP")
        title.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {c['text']};")
        hl.addWidget(title)
        tip = QLabel(
            "配置 Model Context Protocol 服务器。启用后，WokBee 运行时会加载其工具供 Agent 调用。"
            "stdio 需本机已安装对应命令（如 npx）。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        hl.addWidget(tip)
        root.addWidget(header)

        bar = QHBoxLayout()
        bar.setContentsMargins(28, 12, 28, 8)
        bar.addStretch()
        add_btn = QPushButton("添加 MCP")
        add_btn.setFixedHeight(32)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; padding: 0 14px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
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

    def refresh(self):
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        servers = self.store.list_servers()
        c = self.theme.colors
        if not servers:
            empty = QLabel("暂无 MCP。可添加例如：npx -y @modelcontextprotocol/server-filesystem <目录>")
            empty.setWordWrap(True)
            empty.setStyleSheet(f"color: {c['text_hint']}; padding: 20px;")
            self._list_layout.addWidget(empty)
            return
        for s in servers:
            card = _McpCard(s, self.theme)
            card.toggled.connect(self._on_toggle)
            card.edit_clicked.connect(self._on_edit)
            card.delete_clicked.connect(self._on_delete)
            card.test_clicked.connect(self._on_test)
            self._list_layout.addWidget(card)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def _on_add(self):
        dlg = _McpEditor(self.theme, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        server = dlg.result_server()
        if not server:
            _tip(self, self.theme, "名称不能为空，或环境变量 JSON 无效。")
            return
        if not server.to_connection():
            _tip(self, self.theme, "请完整填写命令（stdio）或 URL（SSE/HTTP）。")
            return
        self.store.upsert(server)
        self.refresh()

    def _on_edit(self, server_id: str):
        server = next((s for s in self.store.list_servers() if s.id == server_id), None)
        if not server:
            return
        dlg = _McpEditor(self.theme, server, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        updated = dlg.result_server()
        if not updated or not updated.to_connection():
            _tip(self, self.theme, "配置不完整或环境变量 JSON 无效。")
            return
        self.store.upsert(updated)
        self.refresh()

    def _on_toggle(self, server_id: str, enabled: bool):
        self.store.set_enabled(server_id, enabled)

    def _on_delete(self, server_id: str):
        c = self.theme.colors
        dlg = QDialog(self)
        dlg.setWindowTitle("删除 MCP")
        dlg.setFixedSize(360, 130)
        dlg.setStyleSheet(f"background: {c['content_bg']};")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.addWidget(QLabel("确定删除该 MCP 配置？"))
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
            self.store.delete(server_id)
            self.refresh()

    def _on_test(self, server_id: str):
        server = next((s for s in self.store.list_servers() if s.id == server_id), None)
        if not server:
            return
        conn = server.to_connection()
        if not conn:
            _tip(self, self.theme, "配置不完整，无法连接。")
            return
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
            import asyncio

            async def _test():
                client = MultiServerMCPClient({server.name or server.id: conn})
                tools = await client.get_tools()
                return tools

            tools = asyncio.run(_test())
            names = ", ".join(getattr(t, "name", str(t)) for t in tools[:12])
            more = f" 等共 {len(tools)} 个" if len(tools) > 12 else f"（共 {len(tools)} 个）"
            _tip(self, self.theme, f"连接成功，工具：{names or '(无)'}{more}")
        except Exception as e:
            _tip(self, self.theme, f"连接失败：{e}")
