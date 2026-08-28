"""AI 配置 — MCP 服务器管理。"""

from __future__ import annotations

import json
import shlex

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QLineEdit, QDialog, QCheckBox, QComboBox,
    QTextEdit,
)

from tokbee.ui.styles.theme import Theme
from wokbee.core.mcp_store import McpServerConfig, McpStore
from wokbee.ui.dialogs import tip as _tip


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
        self.setObjectName("mcpCard")
        self.setStyleSheet(f"""
            QFrame#mcpCard {{
                background: {c["card_bg"]};
                border: 1px solid {c["border_light"]};
                border-radius: 8px;
            }}
            QFrame#mcpCard QLabel, QFrame#mcpCard QCheckBox {{
                background: transparent; border: none;
            }}
        """)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)

        info = QVBoxLayout()
        title = QLabel(server.name)
        title.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {c['text']}; background: transparent; border: none;")
        info.addWidget(title)
        if server.transport == "stdio":
            detail = f"stdio · {server.command} {' '.join(server.args)}".strip()
        else:
            detail = f"{server.transport} · {server.url}"
        desc = QLabel(detail)
        desc.setWordWrap(True)
        desc.setStyleSheet(f"font-size: 12px; color: {c['text_secondary']}; background: transparent; border: none;")
        info.addWidget(desc)
        lay.addLayout(info, stretch=1)

        from tokbee.ui.combo_style import checkbox_qss, secondary_btn_qss
        chk = QCheckBox("启用")
        chk.setChecked(server.enabled)
        chk.setStyleSheet(checkbox_qss(c))
        chk.toggled.connect(lambda v: self.toggled.emit(server.id, v))
        lay.addWidget(chk)

        for text, sig in (("测试", self.test_clicked), ("编辑", self.edit_clicked), ("删除", self.delete_clicked)):
            btn = QPushButton(text)
            btn.setFixedHeight(30)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setAutoDefault(False)
            btn.setDefault(False)
            if text == "删除":
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: transparent; color: {c["danger"]};
                        border: 1px solid {c["border"]}; border-radius: 6px;
                        padding: 0 10px; text-decoration: none;
                    }}
                    QPushButton:hover {{ background: #fff1f0; }}
                """)
            else:
                btn.setStyleSheet(secondary_btn_qss(c))
            btn.clicked.connect(lambda _, sid=server.id, s=sig: s.emit(sid))
            lay.addWidget(btn)


class _McpEditor(QDialog):
    def __init__(self, theme: Theme, server: McpServerConfig | None = None, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._server = server or McpServerConfig(name="新 MCP")
        self.setWindowTitle("编辑 MCP" if server else "添加 MCP")
        self.setFixedSize(520, 540)
        c = theme.colors
        self.setStyleSheet(f"background: {c['content_bg']};")
        from tokbee.ui.combo_style import (
            apply_combo_popup_style,
            rounded_lineedit_qss,
            secondary_btn_qss,
            DEFAULT_COMBO_WIDTH,
            DEFAULT_COMBO_HEIGHT,
        )
        inp = rounded_lineedit_qss(c)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 18)
        lay.setSpacing(8)

        name_lbl = QLabel("名称")
        name_lbl.setStyleSheet(f"font-size: 13px; color: {c['text']}; background: transparent; border: none;")
        lay.addWidget(name_lbl)
        self._name = QLineEdit(self._server.name)
        self._name.setFixedHeight(34)
        self._name.setStyleSheet(inp)
        lay.addWidget(self._name)

        transport_lbl = QLabel("传输方式")
        transport_lbl.setStyleSheet(f"font-size: 13px; color: {c['text']}; background: transparent; border: none;")
        lay.addWidget(transport_lbl)
        self._transport = QComboBox()
        self._transport.addItem("stdio（本地进程）", "stdio")
        self._transport.addItem("SSE", "sse")
        self._transport.addItem("Streamable HTTP", "streamable_http")
        idx = self._transport.findData(self._server.transport)
        self._transport.setCurrentIndex(idx if idx >= 0 else 0)
        self._transport.currentIndexChanged.connect(self._sync_fields)
        apply_combo_popup_style(
            self._transport, c, rounded=True,
            fixed_width=DEFAULT_COMBO_WIDTH, fixed_height=DEFAULT_COMBO_HEIGHT,
        )
        lay.addWidget(self._transport, alignment=Qt.AlignmentFlag.AlignLeft)

        self._cmd_label = QLabel("命令（如 npx / uvx / python）")
        self._cmd_label.setStyleSheet(f"font-size: 13px; color: {c['text']}; background: transparent; border: none;")
        lay.addWidget(self._cmd_label)
        self._command = QLineEdit(self._server.command)
        self._command.setFixedHeight(34)
        self._command.setStyleSheet(inp)
        lay.addWidget(self._command)

        self._args_label = QLabel("参数（空格分隔）")
        self._args_label.setStyleSheet(f"font-size: 13px; color: {c['text']}; background: transparent; border: none;")
        lay.addWidget(self._args_label)
        self._args = QLineEdit(shlex.join(self._server.args) if self._server.args else "")
        self._args.setFixedHeight(34)
        self._args.setStyleSheet(inp)
        lay.addWidget(self._args)

        self._url_label = QLabel("URL（SSE / HTTP）")
        self._url_label.setStyleSheet(f"font-size: 13px; color: {c['text']}; background: transparent; border: none;")
        lay.addWidget(self._url_label)
        self._url = QLineEdit(self._server.url)
        self._url.setFixedHeight(34)
        self._url.setStyleSheet(inp)
        lay.addWidget(self._url)

        env_lbl = QLabel("环境变量 JSON（可选，如 {\"KEY\":\"val\"}）")
        env_lbl.setStyleSheet(f"font-size: 13px; color: {c['text']}; background: transparent; border: none;")
        lay.addWidget(env_lbl)
        self._env = QTextEdit()
        self._env.setFixedHeight(80)
        self._env.setPlainText(json.dumps(self._server.env, ensure_ascii=False) if self._server.env else "")
        self._env.setStyleSheet(f"""
            QTextEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 6px;
                padding: 6px 8px; font-size: 13px;
            }}
            QTextEdit:focus {{ border: 1px solid {c["input_focus_border"]}; }}
        """)
        lay.addWidget(self._env)

        self._sync_fields()

        lay.addSpacing(12)
        lay.addStretch(1)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addStretch()
        cancel = QPushButton("取消")
        cancel.setFixedHeight(34)
        cancel.setMinimumWidth(72)
        cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel.setAutoDefault(False)
        cancel.setDefault(False)
        cancel.setFlat(False)
        cancel.setStyleSheet(secondary_btn_qss(c))
        cancel.clicked.connect(self.reject)
        ok = QPushButton("保存")
        ok.setFixedHeight(34)
        ok.setMinimumWidth(72)
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setAutoDefault(False)
        ok.setDefault(True)
        ok.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; padding: 0 16px; font-size: 13px;
                text-decoration: none;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
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
        self._test_workers: list[_McpTestWorker] = []  # 持有引用，避免后台测试线程被 GC
        self._build()
        self.refresh()

    def _build(self):
        c = self.theme.colors
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        header = QFrame()
        header.setStyleSheet(f"background: {c['content_bg']}; border: none;")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(28, 20, 28, 12)
        title = QLabel("MCP")
        title.setStyleSheet(
            f"font-size: 20px; font-weight: bold; color: {c['text']};"
            "background: transparent; border: none;"
        )
        hl.addWidget(title)
        tip = QLabel(
            "配置 Model Context Protocol 服务器。启用后，WokBee 运行时会加载其工具供 Agent 调用。"
            "stdio 需本机已安装对应命令（如 npx）。"
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
        bar.addStretch()
        add_btn = QPushButton("添加 MCP")
        add_btn.setFixedHeight(34)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; padding: 0 14px; font-size: 13px;
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
        # 后台线程测试连接，避免主线程 asyncio.run 无超时卡死窗口。
        worker = _McpTestWorker(server, parent=self)
        self._test_workers.append(worker)
        worker.result.connect(lambda msg: _tip(self, self.theme, msg))
        worker.result.connect(lambda *_: self._test_workers.remove(worker) if worker in self._test_workers else None)
        worker.finished.connect(worker.deleteLater)
        worker.start()


class _McpTestWorker(QThread):
    """后台测试 MCP 连接：带超时 via asyncio.wait_for，结果经信号回主线程。"""

    result = Signal(str)

    def __init__(self, server: McpServerConfig, parent=None):
        super().__init__(parent)
        self._server = server

    def run(self):
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
            import asyncio

            async def _test():
                conn = self._server.to_connection()
                client = MultiServerMCPClient({self._server.name or self._server.id: conn})
                # 超时保护：MCP 服务器无响应时不许拖死后台线程（进而占住 UI）。
                return await asyncio.wait_for(client.get_tools(), timeout=8)

            tools = asyncio.run(_test())
            names = ", ".join(getattr(t, "name", str(t)) for t in tools[:12])
            more = f" 等共 {len(tools)} 个" if len(tools) > 12 else f"（共 {len(tools)} 个）"
            self.result.emit(f"连接成功，工具：{names or '(无)'}{more}")
        except Exception as e:
            if isinstance(e, TimeoutError) or "Timeout" in type(e).__name__:
                self.result.emit("连接超时：服务器无响应（超过 8 秒）。")
            else:
                self.result.emit(f"连接失败：{e}")
