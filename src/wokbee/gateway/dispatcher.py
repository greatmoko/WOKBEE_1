"""无头分发 worker：把一条手机消息交给 WokBee 项目 Agent，取回文本回复。

这是 `autobee/engine/executor.py::_run_wokbee` 的进程内移植 —— 复用同一套
内存 checkpointer（`thread_id = wokbee-chat-{project.id}`），手机转轮上下文连续。
必须在非 UI 线程调用（引擎import契约），由 `GatewayManager` 的线程池驱动。
"""

from __future__ import annotations

import logging

from wokbee.core.models import ApprovalFlags, Project, ProjectEvent
from wokbee.core.project_store import ProjectStore
from wokbee.core.settings import WokBeeSettings
from tokbee.core.provider_store import ProviderStore

logger = logging.getLogger("wokbee")


class GatewayDispatcher:
    def __init__(
        self,
        settings: WokBeeSettings | None = None,
        provider_store: ProviderStore | None = None,
        project_store: ProjectStore | None = None,
    ):
        self.settings = settings or WokBeeSettings()
        self.provider_store = provider_store or ProviderStore()
        self.project_store = project_store or ProjectStore()
        # 每条落盘事件回调（由 GatewayManager 注入）→ 转发给 UI 做实时时间线刷新。
        # 置为 ``None`` 时不转发（测试可用假 dispatcher 无此属性）。
        self.event_sink = None

    def run_chat(self, project: Project, text: str):
        """在后台线程跑一轮交互对话，返回 RunResult。"""
        # 无人值守线程内按需加载引擎（deepagents 栈），避免拖慢启动
        from wokbee.engine import ensure_engine_warm
        from wokbee.engine.runner import AgentRunner, RunRequest, resolve_model_for_project

        ensure_engine_warm()
        resolved = resolve_model_for_project(project, self.settings, self.provider_store)

        runner = AgentRunner(self.settings, self.provider_store)
        self._wire_callbacks(runner, project)
        req = RunRequest(
            project=project,
            project_root=self.project_store.path_for(project.id),
            user_message=text,
            resolved=resolved,
            approval=ApprovalFlags(
                skip_read=True, skip_write=True,
                skip_routine=True, skip_high_risk=True,  # 无人值守自动放行
            ),
            max_steps=self.settings.max_steps,
        )
        return runner.run_chat(req)

    @staticmethod
    def reply_for(result) -> str:
        """把 RunResult 变成要发回手机的文本。"""
        if result is None:
            return "（引擎无响应）"
        ok = getattr(result, "ok", False)
        text = (getattr(result, "final_text", "") or "").strip()
        if ok:
            return text or "（已处理，无文字回复）"
        err = (getattr(result, "error", "") or "").strip()
        return err or (getattr(result, "outcome", "failed") or "failed")

    def _wire_callbacks(self, runner, project: Project) -> None:
        """代理引擎事件 → 写入项目时间线；无人值守自动审批/自动澄清。"""

        def _on_event(kind: str, content: str, meta: dict | None):
            if kind == "cache":
                # 与桌面运行一致：cache 统计不入时间线，也不单独提醒
                return
            try:
                self.project_store.append_event(
                    project.id,
                    ProjectEvent(kind=kind, content=content, meta=dict(meta or {})),
                )
            except Exception:
                logger.exception("写入项目事件失败")
            if self.event_sink is not None:
                try:
                    self.event_sink(project.id, kind, content, dict(meta or {}))
                except Exception:
                    logger.exception("转发网关事件失败")

        def _on_approval(pending: list):
            # 无人值守：自动放行
            try:
                runner.resolve_approval([{"type": "approve"}] * (len(pending) or 16))
            except Exception:
                logger.exception("自动审批失败")

        def _on_ask_user(payload: dict):
            # 无人值守：自动选择第一项，避免无限等待
            questions = payload.get("questions") or []
            answers = [
                {"id": str(q.get("id") or ""), "selected": [str(q["options"][0])]}
                for q in questions if isinstance(q, dict) and q.get("options")
            ]
            try:
                runner.resolve_ask_user({"answers": answers})
            except Exception:
                logger.exception("自动回答澄清失败")

        runner.on_event = _on_event
        runner.on_approval_needed = _on_approval
        runner.on_ask_user_needed = _on_ask_user
