"""Deep Agents 运行器：目标 → 执行 → 审批门 → lesson。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, LocalShellBackend
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from wokbee.core.provider_store import ProviderStore, ResolvedModel

from autobee.core.models import ApprovalFlags, Project
from autobee.core.paths import ensure_project_layout, memory_dir, workspace_sandbox
from autobee.core.settings import AutoBeeSettings
from autobee.engine.approval_policy import (
    build_interrupt_on,
    risk_label_for_tool,
)
from autobee.engine.lessons import (
    Lesson,
    LessonStore,
    build_environment_block,
)
from autobee.engine.model_factory import build_chat_model
from autobee.engine.network_tools import NETWORK_TOOLS
from autobee.engine.script_factory import solidify_scripts
from autobee.engine.script_runner import (
    build_user_message_for_ai_phase,
    run_pipeline_until_ai_or_end,
)
from autobee.core.skills_store import SkillsStore
from autobee.core.mcp_store import McpStore

logger = logging.getLogger("autobee")

EventCallback = Callable[[str, str, dict], None]  # kind, content, meta
ApprovalCallback = Callable[[list[dict]], None]  # pending action summaries


@dataclass
class RunRequest:
    project: Project
    project_root: Path
    user_message: str
    resolved: ResolvedModel
    approval: ApprovalFlags
    max_steps: int = 40


@dataclass
class RunResult:
    ok: bool
    outcome: str  # success | failed | cancelled | awaiting_approval
    final_text: str = ""
    error: str = ""
    lesson_id: str = ""
    pending_actions: list[dict] = field(default_factory=list)


# 进程内 checkpointer，保证同一项目可 resume
_CHECKPOINTERS: dict[str, InMemorySaver] = {}
_AGENTS: dict[str, Any] = {}
_LOCK = threading.Lock()


def _get_checkpointer(project_id: str) -> InMemorySaver:
    with _LOCK:
        if project_id not in _CHECKPOINTERS:
            _CHECKPOINTERS[project_id] = InMemorySaver()
        return _CHECKPOINTERS[project_id]


def _ensure_memory_files(project_root: Path, project: Project) -> None:
    mem = memory_dir(project_root)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "experiences").mkdir(parents=True, exist_ok=True)  # 兼容旧路径
    agents_md = mem / "AGENTS.md"
    content = (
        f"# Project {project.title}\n\n"
        f"- id: `{project.id}`\n"
        f"- goal: {project.goal or '(未设置)'}\n"
        f"- approval: {project.approval.summary()}\n\n"
        "你是具备完整联网能力的本地工作助手（非离线沙箱）。\n"
        "优先使用 `web_search` / `http_get` 获取实时信息；需要时可用 `execute` 跑本机命令。\n"
        "工作文件放 `workspace/`；最终交付物放 `deliverables/`；"
        "用户上传文件在 `uploads/`（可直接读取调用）。\n"
        "经验总结：仅当 memory/EXPERIENCE.md 尚不存在/为空时，运行结束后自动写入；"
        "已有经验后请由用户点击「总结经验」覆盖更新同一文件。\n"
        "总结时会把可确定性步骤固化到 scripts/（不归档）；下次运行优先本地执行脚本，"
        "仅脚本失败/数据不对/需创作时才唤 AI。\n"
        "全局 Skills 位于本机公共目录，通过 /skills/ 只读挂载，不复制进本项目。\n"
        "再次执行时，必须先阅读 memory/EXPERIENCE.md 中的「成功实现路径 / 运行环境 / 注意事项」。\n"
    )
    agents_md.write_text(content, encoding="utf-8")
    # 确保经验索引存在
    from autobee.engine.lessons import LessonStore

    LessonStore(project_root).rebuild_index()


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return str(content).strip()


def _extract_text(messages: list) -> str:
    if not messages:
        return ""
    return _message_text(
        getattr(messages[-1], "content", None)
        if not isinstance(messages[-1], dict)
        else messages[-1].get("content")
    )


def _msg_id(msg: Any) -> str:
    mid = getattr(msg, "id", None)
    if mid:
        return str(mid)
    if isinstance(msg, dict) and msg.get("id"):
        return str(msg["id"])
    return ""


def _tool_calls_of(msg: Any) -> list:
    tcs = getattr(msg, "tool_calls", None)
    if tcs:
        return list(tcs)
    if isinstance(msg, dict):
        return list(msg.get("tool_calls") or [])
    additional = getattr(msg, "additional_kwargs", None) or {}
    if isinstance(additional, dict):
        return list(additional.get("tool_calls") or [])
    return []


def _format_tool_call(tc: Any) -> str:
    if isinstance(tc, dict):
        name = tc.get("name") or tc.get("function", {}).get("name") or "tool"
        args = tc.get("args")
        if args is None and isinstance(tc.get("function"), dict):
            args = tc["function"].get("arguments")
    else:
        name = getattr(tc, "name", "tool")
        args = getattr(tc, "args", {})
    args_s = str(args)
    if len(args_s) > 400:
        args_s = args_s[:400] + "…"
    return f"{name}({args_s})"


def build_success_path_from_messages(messages: list, *, limit: int = 40) -> str:
    """从本轮消息中的工具调用轨迹提炼「成功实现路径」。"""
    steps: list[str] = []
    for msg in messages or []:
        if len(steps) >= limit:
            break
        cls = msg.__class__.__name__ if not isinstance(msg, dict) else str(msg.get("type", ""))
        role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else "") or cls

        for tc in _tool_calls_of(msg):
            if len(steps) >= limit:
                break
            steps.append(f"{len(steps) + 1}. 调用工具：{_format_tool_call(tc)}")

        if "Tool" in cls or role in ("tool", "ToolMessage"):
            name = (
                getattr(msg, "name", None)
                or (msg.get("name") if isinstance(msg, dict) else None)
                or "tool"
            )
            body = _message_text(
                getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
            )
            if len(body) > 220:
                body = body[:220] + "…"
            if body:
                steps.append(f"{len(steps) + 1}. 工具 `{name}` 返回：{body}")
            else:
                steps.append(f"{len(steps) + 1}. 工具 `{name}` 完成")

    if not steps:
        return ""
    return "\n".join(steps)


def _emit_message_events(emit: EventCallback, msg: Any, seen: set[str]) -> None:
    """把单条消息转成时间线事件（跳过已发过的 id）。"""
    mid = _msg_id(msg)
    if mid and mid in seen:
        return
    if mid:
        seen.add(mid)

    cls = msg.__class__.__name__ if not isinstance(msg, dict) else msg.get("type", "")
    role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else "") or cls

    # ToolMessage
    if "Tool" in cls or role in ("tool", "ToolMessage"):
        name = getattr(msg, "name", None) or (msg.get("name") if isinstance(msg, dict) else "") or "tool"
        body = _message_text(
            getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
        )
        if len(body) > 1200:
            body = body[:1200] + "\n…(已截断)"
        emit("tool", f"⟵ {name} 返回：\n{body}", {"tool": name})
        return

    # AIMessage / assistant
    if "AI" in cls or role in ("ai", "assistant", "AIMessage"):
        tcs = _tool_calls_of(msg)
        for tc in tcs:
            emit("tool", f"⟶ 调用工具：{_format_tool_call(tc)}", {"phase": "call"})
        text = _message_text(
            getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
        )
        if text:
            emit("agent", text, {})
        return

    # Human / other — 一般不回显用户消息（UI 已有）
    return


def _collect_messages_from_update(update: Any) -> list:
    msgs: list = []
    if isinstance(update, dict):
        if "messages" in update:
            raw = update["messages"]
            if isinstance(raw, list):
                msgs.extend(raw)
            else:
                msgs.append(raw)
        else:
            # node_name -> payload
            for v in update.values():
                if isinstance(v, dict) and "messages" in v:
                    raw = v["messages"]
                    if isinstance(raw, list):
                        msgs.extend(raw)
                    else:
                        msgs.append(raw)
                elif isinstance(v, list):
                    msgs.extend(v)
    return msgs


def _pending_from_state(agent, config: dict) -> list[dict]:
    """从 checkpoint state 解析待审批动作。"""
    pending: list[dict] = []
    try:
        state = agent.get_state(config)
    except Exception as e:
        logger.warning("get_state 失败: %s", e)
        return pending

    tasks = getattr(state, "tasks", None) or ()
    for task in tasks:
        interrupts = getattr(task, "interrupts", None) or ()
        for intr in interrupts:
            value = getattr(intr, "value", intr)
            action_requests = None
            if isinstance(value, dict):
                action_requests = value.get("action_requests") or value.get("actions")
            else:
                action_requests = getattr(value, "action_requests", None)

            if not action_requests:
                # 兜底：整包当作一条
                pending.append(
                    {
                        "name": "tool",
                        "args": {},
                        "description": str(value)[:500],
                        "risk": "操作",
                    }
                )
                continue

            for action in action_requests:
                if isinstance(action, dict):
                    name = action.get("name") or action.get("tool") or "tool"
                    args = action.get("args") or action.get("arguments") or {}
                else:
                    name = getattr(action, "name", None) or "tool"
                    args = getattr(action, "args", {}) or {}
                pending.append(
                    {
                        "name": str(name),
                        "args": args if isinstance(args, dict) else {"raw": str(args)},
                        "description": f"{name}({args})"[:400],
                        "risk": risk_label_for_tool(str(name)),
                    }
                )
    return pending


def _has_pending(agent, config: dict) -> bool:
    try:
        state = agent.get_state(config)
        return bool(getattr(state, "next", None))
    except Exception:
        return False


class AgentRunner:
    """同步运行（应在后台线程调用）。"""

    def __init__(
        self,
        settings: AutoBeeSettings | None = None,
        provider_store: ProviderStore | None = None,
    ):
        self.settings = settings or AutoBeeSettings()
        self.provider_store = provider_store or ProviderStore()
        self._cancel = threading.Event()
        self._approval_event = threading.Event()
        self._approval_decisions: list[dict] | None = None
        self.on_event: EventCallback | None = None
        self.on_approval_needed: ApprovalCallback | None = None

    def request_cancel(self) -> None:
        self._cancel.set()
        # 若卡在审批，视为拒绝以解开等待
        self.resolve_approval([{"type": "reject", "message": "用户取消运行"}])

    def resolve_approval(self, decisions: list[dict]) -> None:
        self._approval_decisions = decisions
        self._approval_event.set()

    def _emit(self, kind: str, content: str, meta: dict | None = None) -> None:
        if self.on_event:
            try:
                self.on_event(kind, content, meta or {})
            except Exception:
                logger.exception("on_event 回调失败")

    def _wait_approval(self, pending: list[dict]) -> list[dict]:
        self._approval_event.clear()
        self._approval_decisions = None
        if self.on_approval_needed:
            self.on_approval_needed(pending)
        # 最长等待 1 小时
        while not self._approval_event.wait(timeout=0.5):
            if self._cancel.is_set():
                return [{"type": "reject", "message": "用户取消"} for _ in pending]
        decisions = self._approval_decisions or [
            {"type": "reject", "message": "无审批结果"} for _ in pending
        ]
        # 数量对齐
        if len(decisions) < len(pending):
            decisions = list(decisions) + [
                {"type": "reject", "message": "未提供决策"}
                for _ in range(len(pending) - len(decisions))
            ]
        return decisions[: len(pending) or 1]

    def build_agent(self, req: RunRequest):
        ensure_project_layout(req.project_root)
        workspace_sandbox(req.project_root).mkdir(parents=True, exist_ok=True)
        _ensure_memory_files(req.project_root, req.project)

        model = build_chat_model(req.resolved)
        # 项目目录为默认后端；全局 Skills 只读挂载到 /skills/（不复制进项目）
        project_backend = LocalShellBackend(
            root_dir=str(req.project_root),
            virtual_mode=True,
            timeout=180,
            inherit_env=True,
        )
        interrupt_on = build_interrupt_on(req.approval)
        checkpointer = _get_checkpointer(req.project.id)

        skills_paths: list[str] = []
        routes: dict = {}
        try:
            skills_store = SkillsStore()
            skills_store.cleanup_project_copies(req.project_root)
            skills_paths = skills_store.global_skills_paths()
            enabled_skills = [s.name for s in skills_store.list_enabled()]
            if skills_paths:
                routes["/skills/"] = FilesystemBackend(
                    root_dir=str(skills_store.root),
                    virtual_mode=True,
                )
                self._emit(
                    "info",
                    f"已挂载全局 Skills（不复制到项目）：{skills_store.root}\n"
                    f"启用：{', '.join(enabled_skills) or '（无）'}",
                )
        except Exception as e:
            logger.exception("加载 Skills 失败")
            self._emit("error", f"Skills 加载失败：{e}")

        backend = (
            CompositeBackend(default=project_backend, routes=routes)
            if routes
            else project_backend
        )

        # MCP：加载已启用服务器工具
        mcp_tools: list = []
        try:
            mcp_store = McpStore()
            if mcp_store.list_enabled():
                self._emit("info", "正在连接 MCP 服务器…")
                mcp_tools = mcp_store.load_tools()
                names = [getattr(t, "name", str(t)) for t in mcp_tools]
                if names:
                    self._emit("info", f"已加载 MCP 工具：{', '.join(names[:20])}"
                               + (f" 等 {len(names)} 个" if len(names) > 20 else ""))
                    # 未勾选「常规免审」时，MCP 工具也走审批
                    if not req.approval.skip_routine:
                        for n in names:
                            interrupt_on[str(n)] = True
                else:
                    self._emit("info", "MCP 已启用但未返回工具")
        except Exception as e:
            logger.exception("加载 MCP 失败")
            self._emit("error", f"MCP 加载失败：{e}")

        # 经验记忆：单文件 memory/EXPERIENCE.md
        lesson_store = LessonStore(req.project_root)
        memory_paths = lesson_store.virtual_memory_paths()
        experience_digest = lesson_store.prompt_digest()
        if not lesson_store.is_empty():
            self._emit(
                "info",
                "已注入项目经验 memory/EXPERIENCE.md（单文件，总结时覆盖更新）",
            )

        system_prompt = (
            "你是 AutoBee——运行在用户本机上的工作助手，具备完整网络与本机执行能力。\n"
            "这不是离线沙箱：你可以使用 web_search、http_get、http_request 访问公网，"
            "也可以用 execute 运行本机命令（curl/python 等）。\n"
            "全局 Skills 只读挂载在 /skills/（来自本机公共 Skills 目录，未复制进本项目）；"
            "需要时请读取 /skills/<技能名>/SKILL.md 并遵循。\n"
            "若已加载 MCP 工具，可直接调用它们完成外部系统操作。\n"
            "需要实时信息（天气、新闻、资料）时，必须先联网查询，禁止凭空编造数据；"
            "若查询失败再说明原因并给出备选方案。\n"
            "项目经验唯一文件：memory/EXPERIENCE.md；其中「执行顺序」定义脚本↔AI 交错步骤。\n"
            "开干前先对照执行顺序 / 成功路径 / 注意事项；主机按序跑脚本，仅在 AI 步骤唤你。\n"
            f"项目根目录为工作根；工作区：workspace/；交付物：deliverables/；"
            f"用户上传：uploads/；记忆：memory/。\n"
            f"当前审核策略：{req.approval.summary()}。\n"
            "执行过程中用中文简要说明你在做什么；最终成果写入 deliverables/；"
            "若 uploads/ 有用户文件请优先读取使用。\n"
            "经验总结：仅当 EXPERIENCE.md 为空时自动写入；之后由用户「总结经验」覆盖更新。\n"
            "若存在 scripts/pipeline.json：优先本地跑脚本；仅失败、数据异常或需创作时再调用模型。\n"
            f"步数上限约 {req.max_steps}，请聚焦目标。"
        )
        if experience_digest:
            system_prompt = system_prompt + "\n\n" + experience_digest

        tools = list(NETWORK_TOOLS) + list(mcp_tools)

        agent = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            backend=backend,
            interrupt_on=interrupt_on or None,
            memory=memory_paths,
            skills=skills_paths or None,
            checkpointer=checkpointer,
            name=f"autobee-{req.project.id}",
        )
        with _LOCK:
            _AGENTS[req.project.id] = agent
        return agent

    def _stream_until_pause(self, agent, input_payload, config: dict, seen: set[str]) -> None:
        """流式执行，边跑边把消息推到时间线；遇 interrupt 正常返回。"""
        try:
            for chunk in agent.stream(
                input_payload,
                config=config,
                stream_mode="updates",
            ):
                if self._cancel.is_set():
                    break
                for msg in _collect_messages_from_update(chunk):
                    _emit_message_events(self._emit, msg, seen)
        except GraphInterrupt:
            return

    def _emit_script_items(self, items: list) -> None:
        for item in items:
            if item.ok:
                preview = (item.output or "")[:600]
                self._emit(
                    "tool",
                    f"⟵ 本地脚本 `{item.path}` 成功：\n{preview}",
                    {"script": item.path, "step_id": item.step_id},
                )
            else:
                self._emit(
                    "error",
                    f"本地脚本 `{item.path}` 失败：{item.error or (item.output or '')[:400]}",
                    {"script": item.path, "step_id": item.step_id},
                )

    def _run_agent_turn(
        self,
        agent,
        config: dict,
        seen_msg_ids: set[str],
        req: RunRequest,
        *,
        payload: Any,
        first: bool,
    ) -> RunResult | None:
        """跑一轮流式 + 审批。返回 None 表示可继续；返回 RunResult 表示应立刻结束。"""
        if first:
            self._emit("agent", "开始本阶段 AI 执行（过程将实时显示）…")
            self._stream_until_pause(agent, payload, config, seen_msg_ids)
        else:
            self._emit("agent", "继续下一阶段 AI 执行…")
            self._stream_until_pause(agent, payload, config, seen_msg_ids)

        guard = 0
        while _has_pending(agent, config) and guard < 50:
            guard += 1
            if self._cancel.is_set():
                lesson = self._maybe_auto_write_lesson(
                    req, "cancelled", "用户取消", ""
                )
                return RunResult(
                    ok=False,
                    outcome="cancelled",
                    error="已取消",
                    lesson_id=lesson.id if lesson else "",
                )

            pending = _pending_from_state(agent, config)
            if not pending:
                break

            lines = []
            for i, act in enumerate(pending, 1):
                lines.append(
                    f"{i}. [{act.get('risk')}] {act.get('name')}: {act.get('description')}"
                )
            self._emit(
                "approval",
                "需要审批以下操作：\n" + "\n".join(lines),
                {"pending": pending},
            )

            decisions = self._wait_approval(pending)
            if self._cancel.is_set():
                lesson = self._maybe_auto_write_lesson(
                    req, "cancelled", "用户取消", ""
                )
                return RunResult(
                    ok=False,
                    outcome="cancelled",
                    lesson_id=lesson.id if lesson else "",
                )

            approved = sum(1 for d in decisions if d.get("type") == "approve")
            rejected = len(decisions) - approved
            self._emit(
                "approval",
                f"审批结果：通过 {approved}，拒绝 {rejected}",
                {"decisions": decisions},
            )

            self._stream_until_pause(
                agent,
                Command(resume={"decisions": decisions}),
                config,
                seen_msg_ids,
            )

        if self._cancel.is_set():
            lesson = self._maybe_auto_write_lesson(
                req, "cancelled", "用户取消", ""
            )
            return RunResult(
                ok=False,
                outcome="cancelled",
                lesson_id=lesson.id if lesson else "",
            )

        if _has_pending(agent, config):
            pending = _pending_from_state(agent, config)
            return RunResult(
                ok=False,
                outcome="awaiting_approval",
                pending_actions=pending,
            )
        return None

    def run(self, req: RunRequest, *, resume: bool = False) -> RunResult:
        self._cancel.clear()
        thread_id = f"autobee-{req.project.id}"
        config = {"configurable": {"thread_id": thread_id}}
        seen_msg_ids: set[str] = set()

        base_message = (
            req.user_message.strip()
            or req.project.goal
            or "请根据项目目标推进工作。"
        )

        try:
            agent = self.build_agent(req)
        except Exception as e:
            logger.exception("创建 Agent 失败")
            return RunResult(ok=False, outcome="failed", error=str(e))

        self._emit(
            "agent",
            f"引擎已启动（Deep Agents + 联网工具）。"
            f"模型：{req.resolved.provider_name}/{req.resolved.model_id}\n"
            f"策略：{req.approval.summary()}；目录：{req.project_root}\n"
            "可用：web_search / http_get / http_request / 文件工具 / execute\n"
            "执行策略：读取经验「执行顺序」，按 script↔AI 交错推进（脚本本地跑，AI 仅在对应步骤介入）。",
        )

        final_text = ""
        trajectory_messages: list = []

        try:
            if resume:
                early = self._run_agent_turn(
                    agent,
                    config,
                    seen_msg_ids,
                    req,
                    payload={
                        "messages": [
                            {
                                "role": "user",
                                "content": "请继续未完成的流程（审批后或中断后续）。",
                            }
                        ]
                    },
                    first=True,
                )
                if early:
                    return early
            else:
                # ── 有序管线：脚本阶段 ↔ AI 阶段交错 ──
                phase_idx = 0
                context_parts: list[str] = []
                ai_turn = 0
                max_phases = 32

                self._emit(
                    "info",
                    "按经验执行顺序推进（先读 EXPERIENCE.md / pipeline.json steps）…",
                )

                for _ in range(max_phases):
                    pipe = run_pipeline_until_ai_or_end(
                        req.project_root,
                        start_phase=phase_idx,
                        prior_context=context_parts,
                    )

                    if not pipe.ran or not pipe.phases:
                        self._emit("info", f"未使用有序管线：{pipe.reason}")
                        early = self._run_agent_turn(
                            agent,
                            config,
                            seen_msg_ids,
                            req,
                            payload={
                                "messages": [
                                    {"role": "user", "content": base_message}
                                ]
                            },
                            first=True,
                        )
                        if early:
                            return early
                        break

                    if pipe.items:
                        self._emit(
                            "info",
                            f"有序管线：{pipe.reason}",
                            {
                                "phase": pipe.next_phase_index,
                                "need_ai": pipe.need_ai,
                                "ok": pipe.ok,
                            },
                        )
                        self._emit_script_items(pipe.items)

                    context_parts = list(pipe.context_parts or [])

                    if pipe.ok and not pipe.need_ai:
                        art_dir = Path(req.project_root) / "deliverables"
                        art_dir.mkdir(parents=True, exist_ok=True)
                        out_file = art_dir / "script_result.md"
                        body = (
                            f"# 有序脚本执行结果\n\n"
                            f"目标：{req.project.goal or base_message}\n\n"
                            f"{pipe.combined_output or '（无输出）'}\n"
                        )
                        out_file.write_text(body, encoding="utf-8")
                        self._emit(
                            "agent",
                            "有序管线均为脚本且已成功，跳过模型以节省 Token。\n"
                            f"结果已写入 `deliverables/{out_file.name}`。",
                        )
                        lesson = self._maybe_auto_write_lesson(
                            req,
                            "success",
                            (pipe.combined_output or "脚本执行成功")[:800],
                            "",
                            success_path="\n".join(
                                f"{i+1}. 本地脚本 `{it.path}`"
                                for i, it in enumerate(pipe.items)
                            ),
                            notes=(
                                "- 本轮按执行顺序由本地脚本完成，未调用模型。\n"
                                "- scripts/ 不参与归档。"
                            ),
                            artifacts=f"- `deliverables/{out_file.name}`",
                        )
                        self._emit("info", "运行结束：成功（纯脚本有序管线）")
                        return RunResult(
                            ok=True,
                            outcome="success",
                            final_text=(pipe.combined_output or "")[:2000],
                            lesson_id=lesson.id if lesson else "",
                        )

                    user_message = build_user_message_for_ai_phase(
                        original_message=base_message,
                        pipeline=pipe,
                    )
                    ai_turn += 1
                    early = self._run_agent_turn(
                        agent,
                        config,
                        seen_msg_ids,
                        req,
                        payload={
                            "messages": [
                                {"role": "user", "content": user_message}
                            ]
                        },
                        first=True,
                    )
                    if early:
                        return early

                    segment_text = ""
                    try:
                        state = agent.get_state(config)
                        values = getattr(state, "values", None) or {}
                        messages = (
                            values.get("messages") if isinstance(values, dict) else None
                        )
                        if messages:
                            trajectory_messages = list(messages)
                            for msg in trajectory_messages:
                                _emit_message_events(self._emit, msg, seen_msg_ids)
                            segment_text = _extract_text(trajectory_messages)
                            final_text = segment_text
                    except Exception:
                        pass

                    if segment_text:
                        context_parts.append(
                            f"## 阶段 {pipe.next_phase_index + 1}（AI）产出\n"
                            f"{segment_text[:4000]}"
                        )

                    phase_idx = pipe.next_phase_index + 1
                    if phase_idx >= len(pipe.phases):
                        break
                else:
                    self._emit("info", "有序管线阶段次数达到上限，结束循环")

            # 收尾：再取一次最终文本
            try:
                state = agent.get_state(config)
                values = getattr(state, "values", None) or {}
                messages = values.get("messages") if isinstance(values, dict) else None
                if messages:
                    trajectory_messages = list(messages)
                    for msg in trajectory_messages:
                        _emit_message_events(self._emit, msg, seen_msg_ids)
                    final_text = _extract_text(trajectory_messages) or final_text
            except Exception:
                pass

            success_path = build_success_path_from_messages(trajectory_messages)
            lesson = self._maybe_auto_write_lesson(
                req,
                "success",
                final_text[:800] or "任务执行完成",
                "",
                success_path=success_path,
            )
            self._emit("info", "运行结束：成功")
            return RunResult(
                ok=True,
                outcome="success",
                final_text=final_text,
                lesson_id=lesson.id if lesson else "",
            )

        except Exception as e:
            logger.exception("Agent 运行失败")
            self._emit("error", f"执行失败：{e}")
            fail_path = ""
            try:
                state = agent.get_state(config)
                values = getattr(state, "values", None) or {}
                messages = values.get("messages") if isinstance(values, dict) else None
                if messages:
                    fail_path = build_success_path_from_messages(list(messages))
            except Exception:
                pass
            lesson = self._maybe_auto_write_lesson(
                req,
                "failed",
                str(e)[:500],
                str(e),
                success_path=fail_path,
            )
            return RunResult(
                ok=False,
                outcome="failed",
                error=str(e),
                lesson_id=lesson.id if lesson else "",
            )

    def _maybe_auto_write_lesson(
        self,
        req: RunRequest,
        outcome: str,
        summary: str,
        errors: str,
        *,
        success_path: str = "",
        notes: str = "",
        artifacts: str = "",
    ) -> Lesson | None:
        """仅在经验文档为空时自动总结；已有经验则跳过并提示手动发起。"""
        store = LessonStore(req.project_root)
        if not store.is_empty():
            self._emit(
                "info",
                "已有经验文档，本次未自动总结。需要时请点击「总结经验」手动发起。",
            )
            return None
        # 取消场景不作为首条自动经验（价值低）
        if outcome == "cancelled":
            self._emit("info", "已取消；经验目录为空，未写入取消类经验。")
            return None
        return self._write_lesson(
            req,
            outcome,
            summary,
            errors,
            success_path=success_path,
            notes=notes,
            artifacts=artifacts,
        )

    def write_lesson_manual(
        self,
        req: RunRequest,
        outcome: str,
        summary: str,
        errors: str = "",
        *,
        success_path: str = "",
        notes: str = "",
        artifacts: str = "",
        events: list | None = None,
    ) -> Lesson | None:
        """人工发起经验总结（无论是否已有经验都写入）。"""
        return self._write_lesson(
            req,
            outcome,
            summary,
            errors,
            success_path=success_path,
            notes=notes,
            artifacts=artifacts,
            events=events,
        )

    def _write_lesson(
        self,
        req: RunRequest,
        outcome: str,
        summary: str,
        errors: str,
        *,
        success_path: str = "",
        notes: str = "",
        artifacts: str = "",
        events: list | None = None,
    ) -> Lesson | None:
        try:
            store = LessonStore(req.project_root)
            # 自动扫产物目录
            if not artifacts:
                from autobee.core.paths import list_deliverable_names

                files = list_deliverable_names(req.project_root, limit=12)
                if files:
                    artifacts = "\n".join(f"- `deliverables/{n}`" for n in files)

            if not success_path:
                if outcome == "success":
                    success_path = (
                        "（本轮未捕获到工具调用轨迹）\n"
                        "1. 明确目标与约束\n"
                        "2. 使用联网工具或 MCP 获取真实数据（或读取 uploads/）\n"
                        "3. 在 workspace/ 起草，将最终结果写入 deliverables/\n"
                        "4. 用中文交付结论并标注数据来源\n\n"
                        f"本轮摘要：\n{summary[:600]}"
                    )
                else:
                    success_path = (
                        "本次未形成完整成功路径。建议下次：\n"
                        "- 先读 memory/EXPERIENCE.md\n"
                        "- 优先复用已验证数据源、本地脚本与工具顺序\n"
                        f"- 关注失败原因：{errors or summary}"
                    )
            elif outcome != "success" and errors:
                success_path = (
                    "【本轮已执行步骤（未完全成功）】\n"
                    f"{success_path}\n\n"
                    f"失败原因：{errors}"
                )

            if not notes:
                notes_parts = []
                if errors:
                    notes_parts.append(f"- 错误：{errors}")
                notes_parts.append("- 需要实时数据时必须联网，禁止凭记忆编造。")
                notes_parts.append("- 高危 execute / 写文件是否免审取决于项目审核策略。")
                notes_parts.append(
                    "- 确定性拉取步骤已尽量固化到 scripts/；scripts 不参与归档。"
                )
                notes = "\n".join(notes_parts)

            env = build_environment_block(
                model=f"{req.resolved.provider_name}/{req.resolved.model_id}",
                policy=req.approval.summary(),
                project_root=str(req.project_root),
            )
            lesson = Lesson(
                project_id=req.project.id,
                goal=req.project.goal or req.user_message,
                outcome=outcome,
                summary=summary,
                success_path=success_path,
                environment=env,
                notes=notes,
                artifacts=artifacts,
                errors=errors,
                model=f"{req.resolved.provider_name}/{req.resolved.model_id}",
                policy=req.approval.summary(),
            )

            # 固化本地脚本（不调用 AI）
            try:
                solid = solidify_scripts(
                    req.project_root,
                    lesson_id=lesson.id,
                    goal=lesson.goal,
                    summary=summary,
                    success_path=success_path,
                    events=events,
                )
                lesson.script_section = solid.script_section_md
                lesson.ai_section = solid.ai_section_md
                lesson.order_section = solid.order_section_md
                lesson.scripts = [s.rel_path for s in solid.script_steps]
                lesson.pipeline = solid.pipeline_rel
                if solid.script_steps or solid.ai_steps:
                    self._emit(
                        "info",
                        f"已写入有序执行步骤到 scripts/pipeline.json"
                        f"（脚本 {len(solid.script_steps)} + AI {len(solid.ai_steps)}），"
                        f"下次按序交错执行；scripts/ 不参与归档",
                        {"scripts": lesson.scripts},
                    )
            except Exception:
                logger.exception("固化脚本失败（经验仍会写入）")

            path = store.save(lesson)
            self._emit(
                "lesson",
                f"经验已更新：memory/EXPERIENCE.md\n"
                f"（单文件覆盖更新；可脚本步骤见 scripts/pipeline.json）",
                {"lesson_id": lesson.id, "path": str(path)},
            )
            return lesson
        except Exception:
            logger.exception("写入 lesson 失败")
            return None


def resolve_model_for_project(
    project: Project,
    settings: AutoBeeSettings,
    provider_store: ProviderStore | None = None,
) -> ResolvedModel:
    store = provider_store or ProviderStore()
    provider = project.provider or settings.default_provider
    model_id = project.model_id or settings.default_model_id
    if provider and model_id:
        resolved = store.resolve(provider, model_id)
        if resolved:
            return resolved
    first = store.first_resolved()
    if not first:
        raise ValueError("没有可用模型，请先在「AI配置 → 厂商设置」中启用模型并填写 Key/Host。")
    return first
