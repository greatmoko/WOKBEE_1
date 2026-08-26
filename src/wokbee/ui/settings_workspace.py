"""WokBee UI — 设置页（挂在 AI 配置二级导航）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QLineEdit, QComboBox, QCheckBox,
    QSpinBox, QFileDialog, QDialog,
)

from tokbee.ui.styles.theme import Theme
from tokbee.core.provider_store import ProviderStore

from wokbee.core.models import ApprovalFlags
from wokbee.core.settings import WokBeeSettings


def _tip(parent: QWidget, theme: Theme, message: str):
    c = theme.colors
    dlg = QDialog(parent)
    dlg.setWindowTitle("提示")
    dlg.setFixedSize(380, 150)
    dlg.setStyleSheet(f"background: {c['content_bg']};")
    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(24, 20, 24, 18)
    msg = QLabel(message)
    msg.setWordWrap(True)
    msg.setStyleSheet(f"font-size: 14px; color: {c['text']};")
    layout.addWidget(msg)
    layout.addStretch()
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
        ("skip_read", "读免审", "读取文件、列目录、检索等只读操作无需审批"),
        ("skip_write", "写免审", "创建/修改文件、写类 API 等无需审批"),
        ("skip_routine", "常规操作免审", "常规命令、非破坏性工具调用无需审批"),
        ("skip_high_risk", "高危操作免审", "删除、安装依赖、高危系统命令等无需审批"),
    ]
    checks: dict[str, QCheckBox] = {}
    for key, label, tip in defs:
        cb = QCheckBox(label)
        cb.setToolTip(tip)
        cb.setStyleSheet(f"color: {c['text']}; font-size: 13px;")
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


class WokBeeSettingsWorkspace(QWidget):
    """WokBee 必要配置。"""

    def __init__(
        self,
        theme: Theme,
        settings: WokBeeSettings | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.settings = settings or WokBeeSettings()
        self._provider_store = ProviderStore()
        self._build()
        self._load()

    def _build(self):
        c = self.theme.colors
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setStyleSheet(
            f"background: {c['content_bg']}; border-bottom: 1px solid {c['border']};"
        )
        hl = QVBoxLayout(header)
        hl.setContentsMargins(28, 20, 28, 12)
        title = QLabel("WokBee 设置")
        title.setStyleSheet(f"font-size: 20px; font-weight: bold; color: {c['text']};")
        hl.addWidget(title)
        tip = QLabel(
            "配置工作区根目录、默认审核勾选与模型。"
            "新建项目会拷贝此处的审核策略，之后可在项目内单独修改。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"font-size: 12px; color: {c['text_hint']};")
        hl.addWidget(tip)
        root.addWidget(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(28, 16, 28, 16)
        bl.setSpacing(18)

        bl.addWidget(self._section_label("工作区根目录"))
        ws_row = QHBoxLayout()
        self._ws_edit = QLineEdit()
        self._ws_edit.setFixedHeight(34)
        self._ws_edit.setPlaceholderText("选择 WokBee 工作区根目录…")
        self._style_input(self._ws_edit)
        ws_row.addWidget(self._ws_edit, stretch=1)
        browse = QPushButton("浏览…")
        browse.setFixedSize(72, 34)
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.setStyleSheet(self._secondary_btn_qss())
        browse.clicked.connect(self._browse_workspace)
        ws_row.addWidget(browse)
        bl.addLayout(ws_row)
        hint = QLabel(
            "新建项目将创建：memory|workspace|deliverables|uploads|runs|archives|scripts…；"
            "交付物→deliverables/；上传→uploads/（均可归档）；"
            "经验→memory/experiences/exp_时间戳.md（只加载最新）；"
            "Agent 禁止访问 archives/；Skills 全局挂载不复制"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        bl.addWidget(hint)

        bl.addWidget(self._section_label("默认审核策略（勾选 = 免审）"))
        approval_box, self._approval_checks = build_approval_checkboxes(self.theme)
        bl.addWidget(approval_box)
        ap_hint = QLabel(
            "未勾选的级别在执行时需要人工审批。"
            "新项目会继承这些勾选，互不影响；改全局不会自动改已有项目。"
        )
        ap_hint.setWordWrap(True)
        ap_hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        bl.addWidget(ap_hint)

        bl.addWidget(self._section_label("默认模型（OpenAI 风格 API）"))
        self._model_combo = QComboBox()
        self._model_combo.setFixedHeight(34)
        self._model_combo.setMinimumWidth(360)
        bl.addWidget(self._model_combo, alignment=Qt.AlignmentFlag.AlignLeft)
        model_hint = QLabel(
            "新建项目优先使用「厂商设置」里带「默认」徽章的模型；"
            "此处仅在未设置厂商默认时作为回退。Key / Host 仍在厂商设置中维护。"
        )
        model_hint.setWordWrap(True)
        model_hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        bl.addWidget(model_hint)

        bl.addWidget(self._section_label("执行上限"))
        limits = QHBoxLayout()
        limits.setSpacing(16)
        limits.addWidget(QLabel("最大步数"))
        self._max_steps = QSpinBox()
        self._max_steps.setRange(1, 500)
        self._max_steps.setFixedWidth(90)
        self._max_steps.setToolTip("单次 Agent 对话/推理相关步数参考上限")
        limits.addWidget(self._max_steps)
        limits.addWidget(QLabel("最大并行工具"))
        self._max_parallel = QSpinBox()
        self._max_parallel.setRange(1, 16)
        self._max_parallel.setFixedWidth(90)
        limits.addWidget(self._max_parallel)
        limits.addWidget(QLabel("管线阶段上限"))
        self._max_phases = QSpinBox()
        self._max_phases.setRange(1, 500)
        self._max_phases.setFixedWidth(90)
        self._max_phases.setToolTip(
            "运行时按 pipeline.json 推进的阶段次数上限"
            "（连续同类步骤算一阶段；如 script→AI→script 为 3 阶段）"
        )
        limits.addWidget(self._max_phases)
        limits.addStretch()
        bl.addLayout(limits)
        phase_hint = QLabel(
            "管线阶段按总结写入的执行顺序一路推进（可为 脚本×N → AI×N → 脚本…），"
            "不是强制「脚本、AI」一一交错。"
        )
        phase_hint.setWordWrap(True)
        phase_hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        bl.addWidget(phase_hint)

        bl.addWidget(self._section_label("AI 调用节流"))
        ai_int_row = QHBoxLayout()
        ai_int_row.setSpacing(10)
        ai_int_row.addWidget(QLabel("调用最小间隔（毫秒）"))
        self._ai_interval = QSpinBox()
        self._ai_interval.setRange(0, 60000)
        self._ai_interval.setSingleStep(500)
        self._ai_interval.setFixedWidth(110)
        self._ai_interval.setSuffix(" ms")
        self._ai_interval.setToolTip(
            "两次调用 AI 接口「发起时间」的最小间隔；0 = 不限制。"
            "用于本地模型短时调用限流。"
        )
        ai_int_row.addWidget(self._ai_interval)
        ai_int_row.addStretch()
        bl.addLayout(ai_int_row)
        ai_int_hint = QLabel(
            "间隔按「发起时间」计算（下一次 ≥ 上一次发起 + 间隔），而非响应结束时间。"
            "设 0 表示关掉节流、完全无额外开销；设为如 2000 则每次 AI 调用至少相隔 2 秒。"
        )
        ai_int_hint.setWordWrap(True)
        ai_int_hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        bl.addWidget(ai_int_hint)

        ds_row = QHBoxLayout()
        ds_row.setSpacing(12)
        self._enable_search = QCheckBox("启用 DeepSeek 服务端搜索工具")
        self._enable_search.setToolTip(
            "开启后在工具列表注册 deepseek_web_search；"
            "需在「厂商设置」里配置官方 DeepSeek 的 API Key 才能成功调用。"
        )
        ds_row.addWidget(self._enable_search)
        ds_hint = QLabel(
            "把 DeepSeek 官方联网搜索包成工具给 Agent 用（多轮检索+引用，主模型可为本地模型）。"
        )
        ds_hint.setWordWrap(True)
        ds_hint.setStyleSheet(f"font-size: 11px; color: {c['text_hint']};")
        ds_row.addWidget(ds_hint, stretch=1)
        bl.addLayout(ds_row)

        bl.addStretch()
        scroll.setWidget(body)
        root.addWidget(scroll, stretch=1)

        btn_bar = QHBoxLayout()
        btn_bar.setContentsMargins(28, 10, 28, 18)
        btn_bar.addStretch()
        save_btn = QPushButton("保存 WokBee 设置")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setFixedHeight(34)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: {c["btn_primary"]}; color: white;
                border: none; border-radius: 6px; padding: 0 18px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_primary_hover"]}; }}
        """)
        save_btn.clicked.connect(self._on_save)
        btn_bar.addWidget(save_btn)
        root.addLayout(btn_bar)

    def _section_label(self, text: str) -> QLabel:
        c = self.theme.colors
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size: 14px; font-weight: bold; color: {c['text']};")
        return lbl

    def _style_input(self, edit: QLineEdit):
        c = self.theme.colors
        edit.setStyleSheet(f"""
            QLineEdit {{
                background: {c["input_bg"]}; color: {c["text"]};
                border: 1px solid {c["input_border"]}; border-radius: 6px;
                padding: 0 10px; font-size: 13px;
            }}
            QLineEdit:focus {{ border: 1px solid {c["input_focus_border"]}; }}
        """)

    def _secondary_btn_qss(self) -> str:
        c = self.theme.colors
        return f"""
            QPushButton {{
                background: {c["btn_bg"]}; color: {c["text"]};
                border: none; border-radius: 6px; font-size: 13px;
            }}
            QPushButton:hover {{ background: {c["btn_hover"]}; }}
        """

    def _browse_workspace(self):
        start = self._ws_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择 WokBee 工作区根目录", start)
        if path:
            self._ws_edit.setText(path)

    def _load(self):
        self._ws_edit.setText(str(self.settings.workspace_root))
        apply_flags_to_checks(self._approval_checks, self.settings.approval)
        self._max_steps.setValue(self.settings.max_steps)
        self._max_parallel.setValue(self.settings.max_parallel_tools)
        self._max_phases.setValue(self.settings.max_pipeline_phases)
        self._ai_interval.setValue(self.settings.ai_interval_ms)
        self._enable_search.setChecked(self.settings.enable_deepseek_search)
        self._reload_models()

    def _reload_models(self):
        self._model_combo.clear()
        self._model_combo.addItem("（未指定，使用厂商默认）", ("", ""))
        try:
            models = self._provider_store.list_selectable_models()
        except Exception:
            models = []
        current = (self.settings.default_provider, self.settings.default_model_id)
        select = 0
        for i, m in enumerate(models, start=1):
            label = f"{m.provider_name} / {m.model_id}"
            self._model_combo.addItem(label, (m.provider_id, m.model_id))
            if (m.provider_id, m.model_id) == current and current[1]:
                select = i
        self._model_combo.setCurrentIndex(select)

    def showEvent(self, event):
        super().showEvent(event)
        self._reload_models()

    def _on_save(self):
        root = self._ws_edit.text().strip()
        if not root:
            _tip(self, self.theme, "请先设置工作区根目录。")
            return
        try:
            Path(root).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            _tip(self, self.theme, f"无法创建工作区目录：{e}")
            return

        self.settings.workspace_root = root
        self.settings.approval = flags_from_checks(self._approval_checks)
        self.settings.max_steps = self._max_steps.value()
        self.settings.max_parallel_tools = self._max_parallel.value()
        self.settings.max_pipeline_phases = self._max_phases.value()
        self.settings.ai_interval_ms = self._ai_interval.value()
        self.settings.enable_deepseek_search = self._enable_search.isChecked()
        pair = self._model_combo.currentData() or ("", "")
        self.settings.default_provider = pair[0]
        self.settings.default_model_id = pair[1]
        self.settings.save()
        _tip(self, self.theme, "WokBee 设置已保存。")
