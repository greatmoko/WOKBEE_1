"""WokBee 主视图：项目列表 + 三段式工作区。

本文件已按职责拆分：时间线/工具步骤行 → timeline.py，操作栏 → action_bar.py，
侧栏/项目要素/整体工作区/后台 worker → workspace.py。这里保留顶层容器 WokBeeView，
并作为既有导入的兼容聚合点（STATUS_* / TITLE_FROM_GOAL_LEN / 各内部类）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QWidget

from tokbee.ui.styles.theme import Theme

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


class WokBeeView(QWidget):
    """wokbee 主页面：中栏项目列表 + 右栏三段工作区。"""

    def __init__(
        self,
        theme: Theme,
        store: ProjectStore | None = None,
        settings: WokBeeSettings | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.theme = theme
        self.settings = settings or WokBeeSettings()
        self.store = store or ProjectStore(self.settings)
        self._build()

    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._sidebar = _ProjectSidebar(self.theme, self.store)
        self._sidebar.project_selected.connect(self._on_selected)
        self._sidebar.project_changed.connect(self._on_list_changed)
        layout.addWidget(self._sidebar)

        self._workspace = _ProjectWorkspace(self.theme, self.store)
        self._workspace.status_changed.connect(self._on_workspace_status_changed)
        layout.addWidget(self._workspace, stretch=1)

        projects = self.store.list_projects()
        if projects:
            self._sidebar.select(projects[0].id)

    def _on_selected(self, project_id: str):
        self._workspace.load_project(project_id)

    def _on_list_changed(self):
        # 删除后无项目时清空工作区
        if not self.store.list_projects():
            self._workspace.show_welcome()

    def _on_workspace_status_changed(self):
        """工作区状态变更时刷新侧栏，保持「完成/运行中」一致。"""
        self._sidebar.refresh()

    def shutdown(self):
        """退出前收尾：转发给内部工作区取消/等待所有在途 worker。"""
        self._workspace.shutdown()

    def showEvent(self, event):
        super().showEvent(event)
        self._sidebar.refresh()
        # 同一项目不要反复 load_project/整表重绘，否则总结经验或弹窗关闭后会跳到旧记录
        sel = self._sidebar._selected_id
        if not sel:
            return
        if self._workspace._project_id != sel:
            self._workspace.load_project(sel)
        else:
            self._workspace._refresh_essentials()
