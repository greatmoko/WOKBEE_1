"""项目元数据工具：供对话/Agent 读取与更新名称、目标。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import tool

from wokbee.core.project_store import ProjectStore
from wokbee.core.settings import WokBeeSettings
from wokbee.core.models import MAX_PROJECT_TITLE_LEN

EmitFn = Callable[[str, str, dict], None]


def build_project_meta_tools(
    *,
    project_id: str,
    settings: WokBeeSettings | None = None,
    emit: EmitFn | None = None,
) -> list[Any]:
    """构造绑定到指定项目的 get/update 工具。"""
    store = ProjectStore(settings or WokBeeSettings())
    pid = project_id

    def _notify(kind: str, content: str, meta: dict | None = None) -> None:
        if emit:
            try:
                emit(kind, content, meta or {})
            except Exception:
                pass

    @tool
    def get_project_info() -> str:
        """读取当前项目的名称、目标、状态与工作目录。用户问项目是什么、目标是什么时使用。"""
        p = store.get(pid)
        if not p:
            return "错误：项目不存在"
        root = store.path_for(pid)
        return (
            f"项目 ID: {p.id}\n"
            f"名称: {p.title}\n"
            f"目标: {p.goal or '（未设置）'}\n"
            f"状态: {p.status.value}\n"
            f"当前步骤: {p.current_step or '（无）'}\n"
            f"目录: {root}\n"
            f"模型: {p.provider}/{p.model_id}"
        )

    @tool
    def update_project_title(title: str) -> str:
        """更新项目名称（左侧列表与顶部标题）。名称须尽量简短，最多 15 个字。

        在用户要求改名、或根据对话总结后需要更新项目名称时调用。
        请自行压缩为不超过 15 字的短名（可含中文），不要用完整目标句当名称。
        """
        new_title = (title or "").replace("\x00", "").strip()
        if not new_title:
            return "错误：名称不能为空"
        if len(new_title) > MAX_PROJECT_TITLE_LEN:
            new_title = new_title[:MAX_PROJECT_TITLE_LEN]
        p = store.rename(pid, new_title)
        if not p:
            return "错误：项目不存在或更新失败"
        _notify(
            "info",
            f"已更新项目名称：{p.title}",
            {"project_meta": "title", "title": p.title},
        )
        return (
            f"成功：项目名称已更新为「{p.title}」"
            f"（上限 {MAX_PROJECT_TITLE_LEN} 字，超长已截断）"
        )

    @tool
    def update_project_goal(goal: str) -> str:
        """更新项目目标（上方「目标」字段）。

        在用户要求修改目标、或根据对话总结后需要重写目标时调用。
        """
        new_goal = (goal or "").strip()
        if not new_goal:
            return "错误：目标不能为空"
        p = store.update_goal(pid, new_goal)
        if not p:
            return "错误：项目不存在或更新失败"
        preview = new_goal if len(new_goal) <= 200 else new_goal[:200] + "…"
        _notify(
            "info",
            f"已更新项目目标：{preview}",
            {"project_meta": "goal"},
        )
        return f"成功：项目目标已更新。\n新目标：\n{new_goal}"

    return [get_project_info, update_project_title, update_project_goal]


PROJECT_META_TOOL_NAMES = (
    "get_project_info",
    "update_project_title",
    "update_project_goal",
)
