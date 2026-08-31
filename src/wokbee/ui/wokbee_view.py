"""WokBee 主视图：项目列表 + 三段式工作区。

本文件已按职责拆分：时间线/工具步骤行 → timeline.py，操作栏 → action_bar.py，
侧栏/项目要素/整体工作区/后台 worker → workspace.py。这里保留顶层容器 WokBeeView，
并作为既有导入的兼容聚合点（STATUS_* / TITLE_FROM_GOAL_LEN / 各内部类）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QWidget

from tokbee.ui.styles.theme import Theme

from wokbee.core.models import ProjectEvent
from wokbee.core.project_store import ProjectStore
from wokbee.core.settings import WokBeeSettings

from wokbee.ui.action_bar import _ActionBar
from wokbee.ui.timeline import _Timeline
from wokbee.ui.workspace import (
    STATUS_COLOR_KEY,
    STATUS_LABEL,
    TITLE_FROM_GOAL_LEN,
    _CompactWorker,
    _ProjectEssentials,
    _ProjectItem,
    _ProjectSidebar,
    _ProjectWorkspace,
    _RefineMetaWorker,
)

__all__ = [
    "WokBeeView",
    "STATUS_LABEL",
    "STATUS_COLOR_KEY",
    "TITLE_FROM_GOAL_LEN",
    "_CompactWorker",
    "_RefineMetaWorker",
    "_ProjectItem",
    "_ProjectSidebar",
    "_ProjectEssentials",
    "_ProjectWorkspace",
    "_Timeline",
    "_ActionBar",
]


# 无项目时的「欢迎」占位 tab 键；真实项目键为其 project_id
_WELCOME_TAB = "__welcome__"


class WokBeeView(QWidget):
    """wokbee 主页面：左侧项目列表 + 右侧单工作区（无独立 tab 条）。

    项目切换全由左侧栏驱动，右侧只显示当前选中项目的工作区。每个项目各有独立的
    _ProjectWorkspace 与 worker；被切走时其工作区仅隐藏（不销毁），任务仍在后台线程继续运行，
    切回即见当次运行的实时进度。多个项目可**同时运行**（引擎本就按 project_id 隔离）。
    """

    def __init__(
        self,
        theme: Theme,
        store: ProjectStore | None = None,
        settings: WokBeeSettings | None = None,
        gateway_manager=None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.settings = settings or WokBeeSettings()
        self.store = store or ProjectStore(self.settings)
        self._gateway_manager = gateway_manager
        self._workspaces: dict[str, _ProjectWorkspace] = {}
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._sidebar = _ProjectSidebar(self.theme, self.store)
        self._sidebar.project_selected.connect(self._on_selected)
        self._sidebar.project_changed.connect(self._on_list_changed)
        layout.addWidget(self._sidebar)
        # 手机来消息：网关在后台线程逐条落盘事件 → 实时刷新（不等跑完）；跑完后再做一次终态刷新。
        if self._gateway_manager is not None:
            self._gateway_manager.notifier.message_done.connect(self._on_gateway_done)
            self._gateway_manager.notifier.event_written.connect(self._on_gateway_event)

        self._stack = QStackedWidget()
        # 右侧不强加 tab 条：项目切换由左侧栏驱动；切走的项目工作区仅隐藏不销毁，任务后台继续跑。
        layout.addWidget(self._stack, stretch=1)

        # 不在 __init__ 自动选中项目：默认 tab 是「对话」，本视图不可见；
        # 项目加载/标签创建推迟到 showEvent（首次真正显示时）再做，启动更快。
        self._ensure_welcome_tab()

    # ── 工作区标签管理 ────────────────────────────────────────────

    def _make_workspace(self) -> _ProjectWorkspace:
        ws = _ProjectWorkspace(self.theme, self.store, parent=self._stack)
        ws.status_changed.connect(self._on_workspace_status_changed)
        return ws

    def _ws_key(self, ws) -> str | None:
        for key, w in self._workspaces.items():
            if w is ws:
                return key
        return None

    def _add_ws(self, ws: _ProjectWorkspace) -> int:
        # 只入栈不显示：切换到该项目时才由 _on_selected 设为当前页
        return self._stack.addWidget(ws)

    # 无右侧 tab 条：项目「运行中/待审批」状态由侧栏状态点展示，无需维护标签标题。
    def _ensure_welcome_tab(self) -> None:
        if _WELCOME_TAB in self._workspaces:
            return
        ws = self._make_workspace()
        self._workspaces[_WELCOME_TAB] = ws
        self._add_ws(ws)

    def _remove_welcome_tab(self) -> None:
        ws = self._workspaces.pop(_WELCOME_TAB, None)
        if ws is None:
            return
        idx = self._stack.indexOf(ws)
        if idx >= 0:
            self._stack.removeWidget(ws)
        ws.deleteLater()

    def _workspace_for(self, project_id: str) -> _ProjectWorkspace:
        ws = self._workspaces.get(project_id)
        if ws is not None:
            return ws
        ws = self._make_workspace()
        self._workspaces[project_id] = ws
        ws.load_project(project_id)
        self._add_ws(ws)
        return ws

    def _on_selected(self, project_id: str):
        # 左侧栏驱动：切换到该项目工作区；其 worker 在后台线程继续运行，不受切换影响。
        if project_id == "":
            self._ensure_welcome_tab()
            self._stack.setCurrentWidget(self._workspaces[_WELCOME_TAB])
            return
        if not project_id:
            return
        self._remove_welcome_tab()
        ws = self._workspace_for(project_id)
        if ws._project_id != project_id:
            ws.load_project(project_id)
        else:
            ws._refresh_essentials()  # 同项目：只刷新要素，保留在跑的实时时间线
        self._stack.setCurrentWidget(ws)

    def _on_list_changed(self):
        # 项目已删除 → 关闭对应工作区；无项目时保证有「欢迎」占位
        for key in list(self._workspaces.keys()):
            if key == _WELCOME_TAB:
                continue
            if self.store.get(key) is None:
                ws = self._workspaces.pop(key)
                ws.shutdown()  # 取消/等待在途 worker，再销毁视图
                idx = self._stack.indexOf(ws)
                if idx >= 0:
                    self._stack.removeWidget(ws)
                ws.deleteLater()
        if self.store.list_projects():
            self._remove_welcome_tab()
        else:
            self._ensure_welcome_tab()

    def _on_workspace_status_changed(self):
        """工作区状态变更时刷新侧栏状态点（运行/待审批在侧栏即可看出）。"""
        self._sidebar.refresh()

    def _on_gateway_done(self, project_id: str, sender_id: str, reply_brief: str):
        """手机来消息并已被网关处理完（事件已落盘）→ 实时刷新该项目时间线。

        网关在后台线程里 `append_event`，UI 不主动刷新就看不到；此处经 `message_done`
        信号（QueuedConnection 回主线程）触发，让桌面时间线与手机对话同步。
        同时收起网关运行期间显示的「正在执行…」状态条，让时间线回到空闲
        （否则会一直挂在「运行中」——issue 3）。
        """
        if project_id and project_id in self._workspaces:
            ws = self._workspaces[project_id]
            if ws._project_id == project_id:
                # 该项目的桌面 worker 若在同时运行，不打断它的运行态；否则收掉网关的运行态
                worker_running = ws._worker is not None and ws._worker.isRunning()
                if not worker_running:
                    ws._timeline.end_run()
                # force_timeline: 已渲染过也重绘，追加「手机」相关事件
                ws.load_project(project_id, force_timeline=True)
        # 刷新左侧状态点；列表顺序不再跟 updated_at 走
        self._sidebar.refresh()

    def _on_gateway_event(self, project_id: str, kind: str, content: str, meta):
        """网关后台线程逐条落盘事件时，实时同步到已打开的项目时间线（不等跑完）。

        复用 `_Timeline.append_event` 的增量追加（含工具 call/callback 原位更新），
        让手机上的对话流向桌面实时呈现；终态仍由 `_on_gateway_done` 全量重绘兜底。
        """
        if not project_id or project_id not in self._workspaces:
            return
        ws = self._workspaces[project_id]
        if ws._project_id != project_id:
            return
        meta_d = meta if isinstance(meta, dict) else {}
        ws._timeline.append_event(ProjectEvent(kind=kind, content=content, meta=meta_d))
        if kind == "agent":
            ws._timeline._status(
                "正在思考…" if str(meta_d.get("phase") or "") == "reasoning" else "正在执行…"
            )
        elif kind == "error":
            ws._timeline._status("出现错误")
        elif kind == "approval":
            ws._timeline._status("等待审批…", pulse=False)
        # 防抖刷新顶栏要素与左侧状态点（archived/运行态可能变化）
        ws._schedule_essentials_refresh()

    def shutdown(self):
        """退出前收尾：转发给各工作区取消/等待所有在途 worker。"""
        for ws in self._workspaces.values():
            if ws is not None:
                ws.shutdown()

    def showEvent(self, event):
        super().showEvent(event)
        self._sidebar.refresh()
        sel = self._sidebar._selected_id
        if not sel:
            # 首次显示本视图时才自动选中第一个项目（启动时默认在「对话」，不进这里）
            projects = self.store.list_projects()
            if projects:
                self._sidebar.select(projects[0].id)
            return
        # 同项目不反复 load_project/整表重绘，否则总结经验或弹窗关闭后会跳到旧记录
        self._on_selected(sel)
