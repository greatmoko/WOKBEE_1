"""Deep Agents 运行器：目标 → 执行 → 审批门 → lesson。"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.types import Command

from tokbee.core.provider_store import ProviderStore, ResolvedModel

from wokbee.core.models import ApprovalFlags, Project, MAX_PROJECT_TITLE_LEN
from wokbee.core.paths import (
    ensure_project_layout,
    list_deliverable_names,
    memory_dir,
    workspace_sandbox,
)
from wokbee.core.settings import WokBeeSettings
from wokbee.engine.approval_policy import (
    build_interrupt_on,
    risk_label_for_tool,
)
from wokbee.engine.archive_guard import ArchiveDeniedBackend
from wokbee.engine.lessons import (
    Lesson,
    LessonStore,
    build_lesson_digest,
    collect_scripts_context,
    summarize_lesson_with_ai,
)
from wokbee.engine.runtime_env import build_runtime_env_block
from wokbee.engine.model_factory import build_chat_model
from wokbee.engine.network_tools import NETWORK_TOOLS
from wokbee.engine.cache_prefix import (
    CacheHitTracker,
    PrefixGuard,
    ai_reply_suggests_pending_action,
    build_session_context_block,
    compose_user_with_context,
    prefix_fingerprint,
    sort_tools_by_name,
    static_system_prompt,
    tool_name_of,
    wrap_tools_truncate_results,
)
from wokbee.engine.ask_user import (
    build_ask_user_tool,
    is_ask_user_interrupt,
    normalize_ask_user_value,
)
from wokbee.engine.project_tools import PROJECT_META_TOOL_NAMES, build_project_meta_tools
from wokbee.engine.script_factory import (
    apply_ai_authored_scripts,
    apply_ai_pipeline_steps,
    solidify_scripts,
)
from wokbee.engine.script_runner import (
    build_user_message_for_ai_phase,
    run_pipeline_until_ai_or_end,
)
from wokbee.core.skills_store import SkillsStore
from wokbee.core.mcp_store import McpStore

logger = logging.getLogger("wokbee")

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


def _reset_run_state(project_id: str) -> InMemorySaver:
    """新开「运行」时清空线程状态，避免继承上次卡死的空 AIMessage / 半截对话。"""
    with _LOCK:
        _CHECKPOINTERS[project_id] = InMemorySaver()
        _AGENTS.pop(project_id, None)
        return _CHECKPOINTERS[project_id]


def _ensure_memory_files(project_root: Path, project: Project) -> None:
    mem = memory_dir(project_root)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "experiences").mkdir(parents=True, exist_ok=True)
    agents_md = mem / "AGENTS.md"
    content = (
        f"# Project {project.title}\n\n"
        f"- id: `{project.id}`\n"
        f"- goal: {project.goal or '(未设置)'}\n"
        f"- approval: {project.approval.summary()}\n\n"
        "你是一个能力强大的AI工作助手,具备完整联网能力,读写本地文件,并且可以执行本机命令。\n"
        "使用 `web_search` / `http_get` 获取实时信息；需要时可用 `execute` 跑本机命令。\n"
        "如果配置了deepseek的api key并且deepseek搜索工具可用,则使用DeepSeek服务端搜索工具进行联网搜索。\n"
        "工作文件放 `workspace/`；最终交付物放 `deliverables/`；"
        "用户上传文件在 `uploads/`（可直接读取调用；"
        "若有名称或内容相近的多份文件，默认以修改时间最新的一份为准）；"
        "参考材料放 `references/`（第三方代码/登录/环境参数/用到的 Skills 快照，归档时不清理）。\n"
        "当你使用外部软件/服务、需登录、或依赖环境参数/密钥时，请把可复用的第三方代码、配置、环境参数与登录信息保存到 `references/`，并在 `references/MANIFEST.md` 登记，确保下次能稳定复跑；"
        "这些敏感信息仅供本机使用，勿外发。\n"
        "**禁止**访问 `archives/`：归档文档与归档数据不得作为本轮数据来源。\n"
        "经验位于 `memory/experiences/exp_时间戳.md`；每次总结新建一份；"
        "运行时只加载**最新一份**（实现步骤/执行顺序/环境/注意事项），不要依赖旧经验或结果正文。\n"
        "可用工具更新项目名称（update_project_title）与目标（update_project_goal）；"
        f"名称尽量简短，最多 {MAX_PROJECT_TITLE_LEN} 字。\n"
        "总结时会把可确定性步骤固化到 scripts/（不归档）；下次运行优先本地执行脚本，"
        "脚本 callback 写入 workspace/script_callback_<脚本名>.md，"
        "后续 AI 步骤必须先读这些文件再继续；仅脚本失败/数据不对/需创作时才唤 AI。\n"
        "全局 Skills 位于本机公共目录，通过 /skills/ 只读挂载，不复制进本项目；"
        "用到的 Skills 会在经验总结时快照到 references/skills/。\n"
    )
    agents_md.write_text(content, encoding="utf-8")
    from wokbee.engine.lessons import LessonStore

    LessonStore(project_root).rebuild_index()


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                # 兼容 text / output_text 等块
                if block.get("type") in ("text", "output_text", "input_text") or "text" in block:
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "reasoning":
                    continue
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return str(content).strip()


def _reasoning_text(msg: Any) -> str:
    """读取厂商附带的思考/推理文本（如 DeepSeek 的 reasoning_content）。

    标准 ChatOpenAI 解析流式 delta 时丢弃该字段，须先经模型层保留才有值；否则恒空。
    """
    ak = getattr(msg, "additional_kwargs", None)
    if isinstance(msg, dict):
        ak = msg.get("additional_kwargs")
    if not isinstance(ak, dict):
        return ""
    rc = ak.get("reasoning_content") or ""
    if isinstance(rc, list):
        rc = "".join(str(x) for x in rc)
    return str(rc).strip()


def _is_ai_message(msg: Any) -> bool:
    cls = msg.__class__.__name__ if not isinstance(msg, dict) else str(msg.get("type") or "")
    role = getattr(msg, "type", None) or (
        msg.get("role") if isinstance(msg, dict) else None
    ) or cls
    role_s = str(role or "")
    return "AI" in cls or role_s in ("ai", "assistant", "AIMessage")


def _extract_text(messages: list) -> str:
    """取最近一条**非空** AI 正文（跳过空 content / Tool / Human）。

    续跑后模型常回一条 content=\"\" 的 AIMessage；若只看 messages[-1] 会误判
    「已无待办」从而中断自动续跑、立刻 incomplete。
    """
    if not messages:
        return ""
    for msg in reversed(list(messages)):
        if not _is_ai_message(msg):
            continue
        content = (
            getattr(msg, "content", None)
            if not isinstance(msg, dict)
            else msg.get("content")
        )
        text = _message_text(content)
        if text:
            return text
        rc = _reasoning_text(msg)
        if rc:
            return rc
    return ""


def _msg_id(msg: Any) -> str:
    mid = getattr(msg, "id", None)
    if mid:
        return str(mid)
    if isinstance(msg, dict) and msg.get("id"):
        return str(msg["id"])
    return ""


def _msg_fingerprint(msg: Any) -> str:
    """稳定去重键：优先消息 id / tool_call_id，否则内容指纹。

    LangGraph stream 后若再遍历 get_state 全量 messages，无 id 时会重复写入时间线。
    """
    mid = _msg_id(msg)
    if mid:
        return f"id:{mid}"

    tcid = getattr(msg, "tool_call_id", None)
    if not tcid and isinstance(msg, dict):
        tcid = msg.get("tool_call_id")
    if tcid:
        return f"toolmsg:{tcid}"

    tc_ids: list[str] = []
    for tc in _tool_calls_of(msg):
        if isinstance(tc, dict):
            tid = str(tc.get("id") or tc.get("tool_call_id") or "").strip()
        else:
            tid = str(getattr(tc, "id", "") or getattr(tc, "tool_call_id", "") or "").strip()
        if tid:
            tc_ids.append(tid)
    if tc_ids:
        return "tc:" + ",".join(tc_ids)

    cls = msg.__class__.__name__ if not isinstance(msg, dict) else str(msg.get("type", "dict"))
    role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else "") or cls
    name = getattr(msg, "name", None) or (msg.get("name") if isinstance(msg, dict) else "") or ""
    text = _message_text(
        getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
    )[:800]
    # 含 tool_calls 摘要，避免仅文本相同的不同调用被误去重
    if _tool_calls_of(msg):
        bits = []
        for tc in _tool_calls_of(msg)[:6]:
            n, a = _tool_call_parts(tc)
            bits.append(f"{n}:{json.dumps(a, ensure_ascii=False, sort_keys=True)[:120]}")
        text = text + "|" + ";".join(bits)
    raw = f"{role}|{name}|{text}"
    import hashlib

    return "fp:" + hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:20]


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


def _tool_call_parts(tc: Any) -> tuple[str, dict]:
    if isinstance(tc, dict):
        name = str(tc.get("name") or tc.get("function", {}).get("name") or "tool")
        args = tc.get("args")
        if args is None and isinstance(tc.get("function"), dict):
            args = tc["function"].get("arguments")
    else:
        name = str(getattr(tc, "name", "tool") or "tool")
        args = getattr(tc, "args", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"raw": args[:500]}
    if not isinstance(args, dict):
        args = {"raw": args}
    return name, args


def _tool_call_id(tc: Any) -> str:
    """取工具调用的 id，用于 call ↔ callback 配对（兼容 dict/对象）。"""
    if isinstance(tc, dict):
        return str(tc.get("id") or tc.get("tool_call_id") or "").strip()
    return str(getattr(tc, "id", "") or getattr(tc, "tool_call_id", "") or "").strip()


def _format_tool_call(tc: Any) -> str:
    name, args = _tool_call_parts(tc)
    args_s = json.dumps(args, ensure_ascii=False)
    if len(args_s) > 400:
        args_s = args_s[:400] + "…"
    return f"{name}({args_s})"


def _format_arg_preview(key: str, value: Any, *, max_chars: int = 240) -> str:
    """把单个工具参数格式化为可读多行文本（避免整段 JSON 挤成一行）。"""
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        raw = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        raw = str(value)
    # 还原字面 \\n，便于阅读 write_file content
    if key in ("content", "command", "text", "body", "code") and "\\n" in raw and "\n" not in raw[:200]:
        raw = raw.replace("\\n", "\n").replace("\\t", "\t")
    raw = raw.strip()
    if len(raw) > max_chars:
        return raw[:max_chars].rstrip() + f"\n…（共 {len(raw)} 字，已截断）"
    return raw


def format_tool_call_for_timeline(name: str, args: dict | None) -> str:
    """时间线展示：多行 Markdown，避免 call 挤成一行。"""
    name = (name or "tool").strip() or "tool"
    args = args if isinstance(args, dict) else {}
    lines = [f"**call:** `{name}`"]
    if not args:
        lines.append("- （无参数）")
        return "\n".join(lines)
    # 优先展示路径类字段，content 放后并截断
    preferred = (
        "file_path",
        "path",
        "command",
        "url",
        "query",
        "method",
        "content",
        "body",
        "text",
    )
    keys = [k for k in preferred if k in args] + [
        k for k in args.keys() if k not in preferred
    ]
    for k in keys[:12]:
        preview = _format_arg_preview(str(k), args.get(k))
        if "\n" in preview:
            lines.append(f"- **{k}:**")
            for ln in preview.splitlines():
                lines.append(f"  {ln}")
        else:
            lines.append(f"- **{k}:** {preview}")
    if len(args) > 12:
        lines.append(f"- …另有 {len(args) - 12} 个参数未展示")
    return "\n".join(lines)


def format_tool_callback_for_timeline(name: str, body: str) -> str:
    name = (name or "tool").strip() or "tool"
    text = (body or "").strip()
    if len(text) > 2000:
        text = text[:2000].rstrip() + f"\n…（已截断，共约 {len(body or '')} 字）"
    if not text:
        return f"**callback:** `{name}`\n\n（无输出）"
    return f"**callback:** `{name}`\n\n```\n{text}\n```"


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
            steps.append(f"{len(steps) + 1}. call: {_format_tool_call(tc)}")

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
                steps.append(f"{len(steps) + 1}. callback: {name} — {body}")
            else:
                steps.append(f"{len(steps) + 1}. callback: {name}")

    if not steps:
        return ""
    return "\n".join(steps)


def _emit_message_events(
    emit: EventCallback,
    msg: Any,
    seen: set[str],
    *,
    cache_tracker: CacheHitTracker | None = None,
) -> None:
    """把单条消息转成时间线事件（跳过已发过的 id/指纹）。"""
    key = _msg_fingerprint(msg)
    if key in seen:
        return
    seen.add(key)

    cls = msg.__class__.__name__ if not isinstance(msg, dict) else msg.get("type", "")
    role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else "") or cls

    # ToolMessage
    if "Tool" in cls or role in ("tool", "ToolMessage"):
        name = getattr(msg, "name", None) or (msg.get("name") if isinstance(msg, dict) else "") or "tool"
        body = _message_text(
            getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
        )
        tcid = getattr(msg, "tool_call_id", None)
        if not tcid and isinstance(msg, dict):
            tcid = msg.get("tool_call_id")
        status = "success"
        if isinstance(msg, dict):
            status = str(msg.get("status") or "success").lower()
        else:
            status = str(getattr(msg, "status", "success") or "success").lower()
        emit(
            "tool",
            format_tool_callback_for_timeline(str(name), body),
            {
                "tool": name,
                "phase": "callback",
                "tool_call_id": str(tcid or ""),
                "status": status,
            },
        )
        return

    # AIMessage / assistant
    if "AI" in cls or role in ("ai", "assistant", "AIMessage"):
        if cache_tracker is not None:
            try:
                cache_tracker.observe_message(msg)
            except Exception:
                logger.exception("cache hit 观测失败")
        tcs = _tool_calls_of(msg)
        text = _message_text(
            getattr(msg, "content", None) if not isinstance(msg, dict) else msg.get("content")
        )
        reasoning = _reasoning_text(msg)
        if reasoning:
            emit("agent", reasoning, {"phase": "reasoning"})
        # AI 正文总是发射：有工具调用时作为「旁白/指挥」先于 call 展示，
        # 无工具调用时作为「AI 回答」→ 顺序: reasoning → 旁白 → call1..N → callback1..N
        if text:
            emit("agent", text, {"phase": "narration" if tcs else "answer"})
        for tc in tcs:
            name, args = _tool_call_parts(tc)
            emit(
                "tool",
                format_tool_call_for_timeline(name, args),
                {
                    "phase": "call",
                    "tool": name,
                    "args": args,
                    "tool_call_id": _tool_call_id(tc),
                },
            )
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


def _iter_interrupt_values(agent, config: dict):
    """遍历 checkpoint 中未处理的 interrupt 值。"""
    try:
        state = agent.get_state(config)
    except Exception as e:
        logger.warning("get_state 失败: %s", e)
        return
    tasks = getattr(state, "tasks", None) or ()
    for task in tasks:
        interrupts = getattr(task, "interrupts", None) or ()
        for intr in interrupts:
            yield getattr(intr, "value", intr)


def _first_ask_user_payload(agent, config: dict) -> dict | None:
    for value in _iter_interrupt_values(agent, config):
        if is_ask_user_interrupt(value):
            return normalize_ask_user_value(value)
        # 兼容：仅含 questions 的载荷
        if isinstance(value, dict) and value.get("questions") and not (
            value.get("action_requests") or value.get("actions")
        ):
            return normalize_ask_user_value({**value, "type": "ask_user"})
    return None


def _pending_from_state(agent, config: dict) -> list[dict]:
    """从 checkpoint state 解析待审批动作（跳过 ask_user 澄清中断）。"""
    pending: list[dict] = []
    for value in _iter_interrupt_values(agent, config):
        if is_ask_user_interrupt(value):
            continue
        if isinstance(value, dict) and value.get("questions") and not (
            value.get("action_requests") or value.get("actions")
        ):
            continue

        action_requests = None
        if isinstance(value, dict):
            action_requests = value.get("action_requests") or value.get("actions")
        else:
            action_requests = getattr(value, "action_requests", None)

        if not action_requests:
            # 兜底：整包当作一条（非 ask_user）
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
        settings: WokBeeSettings | None = None,
        provider_store: ProviderStore | None = None,
    ):
        self.settings = settings or WokBeeSettings()
        self.provider_store = provider_store or ProviderStore()
        self._cancel = threading.Event()
        self._approval_event = threading.Event()
        self._approval_decisions: list[dict] | None = None
        self._ask_user_event = threading.Event()
        self._ask_user_answers: dict | None = None
        self._run_events: list[Any] = []
        self._cache_tracker = CacheHitTracker()
        self._prefix_guard: PrefixGuard | None = None
        self._session_context_block: str = ""
        self._context_injected: bool = False
        self.on_event: EventCallback | None = None
        self.on_approval_needed: ApprovalCallback | None = None
        self.on_ask_user_needed: Callable[[dict], None] | None = None

    def request_cancel(self) -> None:
        self._cancel.set()
        # 若卡在审批/澄清，解开等待
        self.resolve_approval([{"type": "reject", "message": "用户取消运行"}])
        self.resolve_ask_user({"cancelled": True})

    def resolve_approval(self, decisions: list[dict]) -> None:
        self._approval_decisions = decisions
        self._approval_event.set()

    def resolve_ask_user(self, answers: dict) -> None:
        self._ask_user_answers = answers if isinstance(answers, dict) else {"cancelled": True}
        self._ask_user_event.set()

    def _emit(self, kind: str, content: str, meta: dict | None = None) -> None:
        meta = meta or {}
        # 本轮内存轨迹：供首次自动总结固化脚本（避免等 UI 写盘竞态）
        self._run_events.append(
            SimpleNamespace(kind=kind, content=content or "", meta=dict(meta))
        )
        if self.on_event:
            try:
                self.on_event(kind, content, meta)
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

    def _wait_ask_user(self, payload: dict) -> dict:
        self._ask_user_event.clear()
        self._ask_user_answers = None
        if self.on_ask_user_needed:
            self.on_ask_user_needed(payload)
        while not self._ask_user_event.wait(timeout=0.5):
            if self._cancel.is_set():
                return {"cancelled": True}
        return self._ask_user_answers or {"cancelled": True}

    def build_agent(self, req: RunRequest, *, mode: str = "run"):
        """构建完整能力 Agent。

        mode=run：按经验管线推进项目目标。
        mode=chat：同等完整能力（文件/联网/execute/MCP/Skills），但不跑经验管线；
                   提问可与目标无关，并可改项目名称/目标。
        """
        ensure_project_layout(req.project_root)
        workspace_sandbox(req.project_root).mkdir(parents=True, exist_ok=True)
        _ensure_memory_files(req.project_root, req.project)

        model = build_chat_model(req.resolved)
        project_inner = ArchiveDeniedBackend(
            root_dir=str(req.project_root),
            virtual_mode=True,
            timeout=180,
            inherit_env=True,
        )
        project_backend = project_inner
        interrupt_on = build_interrupt_on(req.approval)
        if req.approval.skip_routine:
            for n in PROJECT_META_TOOL_NAMES:
                interrupt_on.pop(n, None)

        # chat 与 run 共用项目 checkpointer，但 thread_id 不同，状态不串
        checkpointer = _get_checkpointer(req.project.id)

        skills_paths: list[str] = []
        routes: dict = {}
        skills_extra_lines: list[str] = []
        try:
            skills_store = SkillsStore()
            skills_store.cleanup_project_copies(req.project_root)
            skills_paths = skills_store.global_skills_paths()
            enabled_skills = [s.name for s in skills_store.list_enabled()]
            if skills_paths:
                skills_inner = FilesystemBackend(
                    root_dir=str(skills_store.root),
                    virtual_mode=True,
                )
                routes["/skills/"] = skills_inner
                skills_extra_lines = [
                    f"- 全局 Skills 目录（真实路径，execute 可用）：{skills_store.root}",
                    "- 全局 Skills 虚拟路径：/skills/<技能名>/SKILL.md（read/edit/write 可用）",
                    f"- 已启用 Skills：{', '.join(enabled_skills) or '（无）'}",
                ]
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

        mcp_tools: list = []
        try:
            mcp_store = McpStore()
            if mcp_store.list_enabled():
                self._emit("info", "正在连接 MCP 服务器…")
                mcp_tools = mcp_store.load_tools()
                names = [getattr(t, "name", str(t)) for t in mcp_tools]
                if names:
                    self._emit(
                        "info",
                        f"已加载 MCP 工具：{', '.join(names[:20])}"
                        + (f" 等 {len(names)} 个" if len(names) > 20 else ""),
                    )
                    if not req.approval.skip_routine:
                        for n in names:
                            interrupt_on[str(n)] = True
                else:
                    self._emit("info", "MCP 已启用但未返回工具")
        except Exception as e:
            logger.exception("加载 MCP 失败")
            self._emit("error", f"MCP 加载失败：{e}")

        lesson_store = LessonStore(req.project_root)
        experience_digest = lesson_store.prompt_digest()
        if not lesson_store.is_empty():
            latest = lesson_store.latest_path()
            self._emit(
                "info",
                f"已注入最新项目经验：{latest.name if latest else 'experiences/'}"
                f"（历史经验不注入；禁止使用 archives/）",
            )

        # Reasonix ImmutablePrefix：system 静态；易变态进【会话上下文】user 块
        system_prompt = static_system_prompt(mode=mode)
        runtime_env_block = build_runtime_env_block(
            project_root=str(req.project_root),
            model=f"{req.resolved.provider_name}/{req.resolved.model_id}",
            policy=req.approval.summary(),
            settings=self.settings,
        )
        context_extra: list[str] = list(skills_extra_lines)

        self._session_context_block = build_session_context_block(
            title=req.project.title,
            goal=req.project.goal or "",
            approval_summary=req.approval.summary(),
            max_steps=req.max_steps if mode != "chat" else None,
            experience_digest=experience_digest,
            mode=mode,
            runtime_env_block=runtime_env_block,
            extra_lines=context_extra or None,
        )

        project_tools = build_project_meta_tools(
            project_id=req.project.id,
            settings=self.settings,
            emit=self._emit,
        )
        # DeepSeek 服务端搜索：包成工具给 Agent 用（开关在设置 enable_deepseek_search；
        # 需官方 DeepSeek Key 才真正注册，主模型可是本地模型）。
        deepseek_search = None
        if getattr(self.settings, "enable_deepseek_search", True):
            try:
                from wokbee.engine.deepseek_search import build_deepseek_search_tool

                has_ds_key = bool(
                    getattr(
                        self.provider_store.get_settings("deepseek"),
                        "api_key",
                        "",
                    ).strip()
                )
                if has_ds_key:
                    deepseek_search = build_deepseek_search_tool(self.provider_store)
                    self._emit(
                        "info",
                        "已挂载 DeepSeek 服务端搜索工具：deepseek_web_search（检索质量更高，多轮+引用）。",
                    )
                else:
                    self._emit(
                        "info",
                        "已开启 DeepSeek 服务端搜索，但未配置官方 DeepSeek 的 API Key；"
                        "deepseek_web_search 暂不生效，去「厂商设置」填官方 Key 即可。",
                    )
            except Exception:
                logger.exception("构建 DeepSeek 搜索工具失败")
                deepseek_search = None

        tools = sort_tools_by_name(
            list(NETWORK_TOOLS)
            + list(project_tools)
            + [build_ask_user_tool()]
            + ([deepseek_search] if deepseek_search is not None else [])
            + list(mcp_tools)
        )
        tools = wrap_tools_truncate_results(tools, project_root=req.project_root)
        tool_names = [tool_name_of(t) for t in tools]
        fp = prefix_fingerprint(system_prompt, tool_names)

        def _on_cache_update(payload: dict) -> None:
            phase = payload.get("phase")
            if phase == "pin":
                self._emit(
                    "info",
                    f"缓存前缀已钉死（DeepSeek prefix-cache）：fp={payload.get('prefix_fp')}，"
                    f"tools={payload.get('tool_count')}。"
                    " system 本会话不变；项目态在用户消息【会话上下文】。",
                    {"cache": True, **payload},
                )
                return
            tag = self._cache_tracker.format_tag()
            self._emit(
                "cache",
                tag,
                {"cache": True, **payload},
            )

        self._cache_tracker = CacheHitTracker(on_update=_on_cache_update)
        self._cache_tracker.note_prefix(fp, len(tool_names))

        # Reasonix 前缀护栏：只对 append-only 破坏告警，正常追加不打扰。
        def _on_prefix_drift(payload: dict) -> None:
            drift = payload.get("drift")
            if not isinstance(drift, dict):
                # 非漂移载荷（如发现点信息）不进改写告警，避免误报。
                return
            self._emit(
                "error",
                "缓存前缀被改写（append-only 破坏，DeepSeek 前缀缓存将在该点失效）：\n"
                f"位置 #{drift.get('index')} 类型 {drift.get('kind') or 'rewrite'} "
                f"role={drift.get('role') or '?'}\n"
                f"内容：{drift.get('content') or '（空）'}",
                {"cache": True, **payload},
            )

        self._prefix_guard = PrefixGuard(on_drift=_on_prefix_drift)
        self._prefix_guard.note_static(fp, len(tool_names))

        # ask_user 在工具内 interrupt，绝不能再套一层 interrupt_on
        interrupt_on.pop("ask_user", None)

        agent_name = (
            f"wokbee-chat-{req.project.id}"
            if mode == "chat"
            else f"wokbee-{req.project.id}"
        )
        agent = create_deep_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            backend=backend,
            interrupt_on=interrupt_on or None,
            # 经验只注入首条 user 的【会话上下文】（Reasonix：记忆写盘不改本会话 system），
            # 不再把 memory= 传给 create_deep_agent，避免 MemoryMiddleware 每次请求
            # 重新加载经验进 system——经验一变化即破坏 DeepSeek 前缀缓存。
            skills=skills_paths or None,
            checkpointer=checkpointer,
            name=agent_name,
        )
        if mode == "run":
            with _LOCK:
                _AGENTS[req.project.id] = agent
        return agent

    def _with_session_context(self, user_message: str) -> str:
        return compose_user_with_context(user_message, self._session_context_block)

    def _inject_session_context_once(self, payload: Any) -> Any:
        """仅首条用户消息注入【会话上下文】，保持后续轮次 append-only 前缀稳定。"""
        if self._context_injected or not isinstance(payload, dict):
            return payload
        msgs = payload.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return payload
        out_msgs = []
        injected = False
        for m in msgs:
            if (
                not injected
                and isinstance(m, dict)
                and (m.get("role") or "") == "user"
            ):
                content = str(m.get("content") or "")
                out_msgs.append(
                    {**m, "content": self._with_session_context(content)}
                )
                injected = True
            else:
                out_msgs.append(m)
        if injected:
            self._context_injected = True
            return {**payload, "messages": out_msgs}
        return payload

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
                    _emit_message_events(
                        self._emit,
                        msg,
                        seen,
                        cache_tracker=self._cache_tracker,
                    )
        except GraphInterrupt:
            pass
        self._check_prefix_guard(agent, config)

    def _check_prefix_guard(self, agent, config: dict) -> None:
        """轮次结束后校验消息历史 append-only；发现改写则归因到具体消息。"""
        if self._prefix_guard is None:
            return
        try:
            state = agent.get_state(config)
            values = getattr(state, "values", None) or {}
            messages = values.get("messages") if isinstance(values, dict) else None
            if not messages:
                return
            self._prefix_guard.check(messages)
        except Exception:
            logger.exception("前缀护栏检查失败")

    def _emit_script_items(self, items: list) -> None:
        for item in items:
            if item.ok:
                preview = (item.output or "")[:600]
                self._emit(
                    "tool",
                    f"callback: 本地脚本 `{item.path}` 成功：\n{preview}",
                    {"script": item.path, "step_id": item.step_id},
                )
            else:
                self._emit(
                    "error",
                    f"本地脚本 `{item.path}` 失败：{item.error or (item.output or '')[:400]}",
                    {"script": item.path, "step_id": item.step_id},
                )

    def _agent_last_ai_text(self, agent, config: dict) -> str:
        try:
            state = agent.get_state(config)
            values = getattr(state, "values", None) or {}
            messages = values.get("messages") if isinstance(values, dict) else None
            if messages:
                return _extract_text(list(messages))
        except Exception:
            pass
        return ""

    def _tool_events_since(self, start: int) -> int:
        return sum(
            1
            for e in self._run_events[start:]
            if getattr(e, "kind", "") == "tool"
        )

    def _drain_pending_interrupts(
        self,
        agent,
        config: dict,
        seen_msg_ids: set[str],
        req: RunRequest,
        *,
        allow_auto_lesson: bool,
    ) -> RunResult | None:
        """处理 ask_user / 工具审批中断，直到无 pending 或需外部等待。"""
        guard = 0
        while _has_pending(agent, config) and guard < 50:
            guard += 1
            if self._cancel.is_set():
                lesson = None
                if allow_auto_lesson:
                    lesson = self._maybe_auto_write_lesson(
                        req, "cancelled", "用户取消", ""
                    )
                return RunResult(
                    ok=False,
                    outcome="cancelled",
                    error="已取消",
                    lesson_id=lesson.id if lesson else "",
                )

            ask_payload = _first_ask_user_payload(agent, config)
            if ask_payload:
                n = len(ask_payload.get("questions") or [])
                self._emit(
                    "info",
                    f"AI 需要你澄清意图（{n} 题），请在弹窗中作答…",
                    {"ask_user": ask_payload},
                )
                answers = self._wait_ask_user(ask_payload)
                if self._cancel.is_set():
                    lesson = None
                    if allow_auto_lesson:
                        lesson = self._maybe_auto_write_lesson(
                            req, "cancelled", "用户取消", ""
                        )
                    return RunResult(
                        ok=False,
                        outcome="cancelled",
                        error="已取消",
                        lesson_id=lesson.id if lesson else "",
                    )
                if answers.get("cancelled"):
                    self._emit("info", "你取消了澄清提问。")
                else:
                    self._emit("info", "已收到你的澄清回答，继续执行…")
                self._stream_until_pause(
                    agent,
                    Command(resume=answers),
                    config,
                    seen_msg_ids,
                )
                continue

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
                lesson = None
                if allow_auto_lesson:
                    lesson = self._maybe_auto_write_lesson(
                        req, "cancelled", "用户取消", ""
                    )
                return RunResult(
                    ok=False,
                    outcome="cancelled",
                    error="已取消",
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
        return None

    def _nudge_user_text(self, nudge_i: int, last_text: str) -> str:
        """按续跑轮次升级指令；英文模型常忽略中文软提示。"""
        low = (last_text or "").lower()
        wants_html = any(
            k in low
            for k in (
                "href",
                "raw html",
                "rawhtml",
                "curl",
                "link structure",
                "html links",
                "原始 html",
                "链接结构",
            )
        )
        html_hint = (
            "若需要页面链接/href：立即调用 http_get 或 http_request，并设 preserve_html=True"
            "（不要只说要用 curl）。\n"
            "If you need href/links: call http_get/http_request with preserve_html=True NOW "
            "(do not only talk about curl).\n"
            if wants_html
            else ""
        )
        levels = [
            (
                "【系统续跑 / SYSTEM CONTINUE】你描述了下一步但还没调用工具。\n"
                "You described the next step but did NOT call any tool.\n"
                f"{html_hint}"
                "请立刻发出 tool call（http_get / http_request / execute / read_file 等），"
                "禁止只回复文字计划。\n"
                "Emit a real tool call now. Do NOT reply with plan-only text."
            ),
            (
                "【系统续跑 强制 / HARD REQUIREMENT】上轮你又只输出了文字、零工具调用。\n"
                "Last turn was text-only with ZERO tool calls — that is invalid.\n"
                f"{html_hint}"
                "下一回合必须包含至少一次 function/tool call；纯文本视为失败。\n"
                "Your next response MUST include at least one function/tool call."
            ),
            (
                "【最后通牒 / FINAL】连续空转。现在唯一允许的动作是调用工具。\n"
                "Stop narrating. Call a tool immediately.\n"
                f"{html_hint}"
                "推荐：http_get(url=..., preserve_html=True, max_chars=30000) "
                "或 execute(command=curl ...)。完成后写入 deliverables/。"
            ),
        ]
        return levels[min(nudge_i, len(levels) - 1)]

    def _maybe_nudge_agent_continue(
        self,
        agent,
        config: dict,
        seen_msg_ids: set[str],
        req: RunRequest,
        *,
        allow_auto_lesson: bool,
        max_nudges: int = 5,
    ) -> RunResult | None:
        """最后一条 AI 回复仍在承诺下一步、却未再调工具时，自动续跑。

        注意：同轮里先前已有 grep/ls 等工具时也要续跑——常见失败是
        「先搜了一下 → 文字说接下来用 curl → 直接结束」。
        """
        for nudge_i in range(max_nudges):
            if _has_pending(agent, config):
                return None
            last_text = self._agent_last_ai_text(agent, config)
            suggests = ai_reply_suggests_pending_action(last_text)
            if not suggests:
                # 空正文不能当成「已收工」：续跑后模型常回 content="" 的 AIMessage
                if (last_text or "").strip():
                    return None
                if nudge_i == 0 and self._tool_events_since(0) == 0:
                    return None
                # 继续用升级文案强制下一轮
                last_text = last_text or "(empty)"
            tools_before = self._tool_events_since(0)
            # 仅统计本函数调用后新增的工具；用绝对起点会在长会话里失真，
            # 这里用「续跑前后差」判断是否真的动手了。
            event_mark = len(self._run_events)
            nudge_text = self._nudge_user_text(nudge_i, last_text)
            self._emit(
                "info",
                f"检测到模型仍在描述下一步、未继续调工具，自动续跑（{nudge_i + 1}/{max_nudges}）…",
            )
            self._stream_until_pause(
                agent,
                {"messages": [{"role": "user", "content": nudge_text}]},
                config,
                seen_msg_ids,
            )
            early = self._drain_pending_interrupts(
                agent,
                config,
                seen_msg_ids,
                req,
                allow_auto_lesson=allow_auto_lesson,
            )
            if early is not None:
                return early
            tools_after = self._tool_events_since(0)
            new_tools = self._tool_events_since(event_mark)
            if tools_after > tools_before:
                # 已动手：若最新回复仍是「计划口吻」则再续；否则结束
                continue
            if new_tools == 0:
                # 本轮续跑零工具，再试下一轮（文案已升级）
                continue
        if ai_reply_suggests_pending_action(self._agent_last_ai_text(agent, config)):
            self._emit(
                "info",
                "模型仍未调用工具完成操作；请检查审核策略（execute 是否需审批）或换更强模型后重试。"
                "若需页面链接，请用 http_get/http_request 并设 preserve_html=True。",
            )
        return None

    def _run_agent_turn(
        self,
        agent,
        config: dict,
        seen_msg_ids: set[str],
        req: RunRequest,
        *,
        payload: Any,
        first: bool,
        allow_auto_lesson: bool = True,
        start_hint: str = "",
    ) -> RunResult | None:
        """跑一轮流式 + 审批。返回 None 表示可继续；返回 RunResult 表示应立刻结束。"""
        if first:
            self._emit(
                "agent",
                start_hint or "开始本阶段 AI 执行（过程将实时显示）…",
                {"phase": "hint"},
            )
            self._stream_until_pause(
                agent,
                self._inject_session_context_once(payload),
                config,
                seen_msg_ids,
            )
        else:
            self._emit("agent", "继续下一阶段 AI 执行…", {"phase": "hint"})
            self._stream_until_pause(
                agent,
                self._inject_session_context_once(payload),
                config,
                seen_msg_ids,
            )

        early = self._drain_pending_interrupts(
            agent,
            config,
            seen_msg_ids,
            req,
            allow_auto_lesson=allow_auto_lesson,
        )
        if early is not None:
            return early

        early = self._maybe_nudge_agent_continue(
            agent,
            config,
            seen_msg_ids,
            req,
            allow_auto_lesson=allow_auto_lesson,
        )
        if early is not None:
            return early

        if self._cancel.is_set():
            lesson = None
            if allow_auto_lesson:
                lesson = self._maybe_auto_write_lesson(
                    req, "cancelled", "用户取消", ""
                )
            return RunResult(
                ok=False,
                outcome="cancelled",
                error="已取消",
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

    def run_chat(self, req: RunRequest) -> RunResult:
        """非运行期对话：回答提问（可与目标无关），可读写项目名称/目标；不跑经验管线。"""
        self._cancel.clear()
        self._run_events = []
        self._context_injected = False
        thread_id = f"wokbee-chat-{req.project.id}"
        config = {"configurable": {"thread_id": thread_id}}
        seen_msg_ids: set[str] = set()

        question = (req.user_message or "").strip()
        if not question:
            return RunResult(ok=False, outcome="failed", error="提问内容为空")

        try:
            agent = self.build_agent(req, mode="chat")
        except Exception as e:
            logger.exception("创建交互 Agent 失败")
            return RunResult(ok=False, outcome="failed", error=str(e))

        self._emit(
            "agent",
            f"交互模式（完整能力，不跑经验管线）。"
            f"模型：{req.resolved.provider_name}/{req.resolved.model_id}\n"
            "可用：联网 / 文件 / execute / Skills / MCP / 项目名称与目标工具。",
            {"phase": "hint"},
        )

        # 附带近期对话，便于「总结对话后改目标/名称」
        recent = self._recent_events_digest(req.project_root, limit=40)
        user_content = question
        if recent:
            user_content = (
                f"{question}\n\n"
                "——\n【近期时间线摘录（供参考，回答不必复述全文）】\n"
                f"{recent}"
            )

        try:
            early = self._run_agent_turn(
                agent,
                config,
                seen_msg_ids,
                req,
                payload={"messages": [{"role": "user", "content": user_content}]},
                first=True,
                allow_auto_lesson=False,
                start_hint="Agent 处理中…",
            )
            if early:
                # 对话模式：取消/失败原样返回；审批等待也返回
                return early

            final_text = ""
            try:
                state = agent.get_state(config)
                values = getattr(state, "values", None) or {}
                messages = values.get("messages") if isinstance(values, dict) else None
                if messages:
                    # 只取最终文本；流式阶段已写入时间线，禁止把 checkpoint 全量历史再刷一遍
                    final_text = _extract_text(list(messages))
            except Exception:
                pass

            self._emit("info", "本轮回复已完成")
            had_tool = any(
                getattr(e, "kind", "") == "tool" for e in self._run_events
            )
            if not had_tool and final_text and ai_reply_suggests_pending_action(final_text):
                self._emit(
                    "info",
                    "模型未调用工具；可回复「继续」让它继续推进。",
                )
            return RunResult(ok=True, outcome="success", final_text=final_text)
        except Exception as e:
            logger.exception("交互失败")
            self._emit("error", f"交互失败：{e}")
            return RunResult(ok=False, outcome="failed", error=str(e))

    @staticmethod
    def _recent_events_digest(project_root: Path, *, limit: int = 40) -> str:
        try:
            from wokbee.core.context_usage import (
                events_as_messages,
                load_context_state,
            )
            from wokbee.core.models import ProjectEvent
            from wokbee.core.paths import events_path
            from tokbee.core import context_manager as ctxman

            ep = events_path(project_root)
            if not ep.exists():
                return ""
            events: list = []
            with ep.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(ProjectEvent.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, TypeError, KeyError):
                        continue
            messages = events_as_messages(events)
            state = load_context_state(project_root)
            summary, active, _ = ctxman.slice_after_compaction(
                messages, state.get("compaction_points") or [],
            )
            rows: list[str] = []
            if summary:
                body = summary.strip().replace("\n", " ")
                if len(body) > 400:
                    body = body[:400] + "…"
                rows.append(f"- [summary] {body}")
            for msg in active:
                kind = msg.get("kind") or msg.get("role") or "?"
                body = (msg.get("content") or "").strip().replace("\n", " ")
                if not body:
                    continue
                if len(body) > 220:
                    body = body[:220] + "…"
                rows.append(f"- [{kind}] {body}")
            if not rows:
                return ""
            return "\n".join(rows[-limit:])
        except Exception:
            logger.exception("读取近期时间线失败")
            return ""

    def run(self, req: RunRequest, *, resume: bool = False) -> RunResult:
        self._cancel.clear()
        self._run_events = []
        self._context_injected = False
        thread_id = f"wokbee-{req.project.id}"
        config = {"configurable": {"thread_id": thread_id}}
        seen_msg_ids: set[str] = set()

        base_message = (
            req.user_message.strip()
            or req.project.goal
            or "请根据项目目标推进工作。"
        )

        try:
            # 非 resume 必须清空 checkpoint，否则会继承上次空 AIMessage / 半截计划而秒退
            if not resume:
                _reset_run_state(req.project.id)
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
            "执行策略：读取经验「执行顺序」与 `scripts/pipeline.json` 的 steps，"
            "按顺序一路推进（本地脚本步骤不耗 Token；遇到 AI 步骤再唤模型）。",
            {"phase": "hint"},
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
                # ── 有序管线：按 pipeline.json steps 顺序推进（可为连续脚本/连续 AI）──
                phase_idx = 0
                context_parts: list[str] = []
                ai_turn = 0
                max_phases = max(1, int(getattr(self.settings, "max_pipeline_phases", 64) or 64))

                self._emit(
                    "info",
                    "按经验执行顺序推进（最新 experiences/exp_*.md + pipeline.json steps；"
                    f"阶段上限 {max_phases}）…",
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
                            # 流式已写时间线；此处只取文本，避免把历史 messages 重复落盘
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

            # 收尾：再取一次最终文本（不重复刷时间线）
            try:
                state = agent.get_state(config)
                values = getattr(state, "values", None) or {}
                messages = values.get("messages") if isinstance(values, dict) else None
                if messages:
                    trajectory_messages = list(messages)
                    final_text = _extract_text(trajectory_messages) or final_text
            except Exception:
                pass

            # 末轮若仍在「计划口吻」，再给一次续跑机会（覆盖管线阶段刚结束的情况）
            if ai_reply_suggests_pending_action(final_text):
                early = self._maybe_nudge_agent_continue(
                    agent,
                    config,
                    seen_msg_ids,
                    req,
                    allow_auto_lesson=False,
                    max_nudges=4,
                )
                if early:
                    return early
                try:
                    state = agent.get_state(config)
                    values = getattr(state, "values", None) or {}
                    messages = (
                        values.get("messages") if isinstance(values, dict) else None
                    )
                    if messages:
                        trajectory_messages = list(messages)
                        final_text = _extract_text(trajectory_messages) or final_text
                except Exception:
                    pass

            success_path = build_success_path_from_messages(trajectory_messages)
            arts = list_deliverable_names(req.project_root)
            still_planning = ai_reply_suggests_pending_action(final_text)

            # 末次定向续跑：仅当「看起来仍在计划」且「尚无交付物」时给一次强制落盘机会，
            # 避免因模型只是忘了落盘/只描述未动手就被判未完成。
            if still_planning and not arts:
                finalize = (
                    "【收尾要求 / FINALIZE】请把本任务最终成果写入 deliverables/ 下的一个文件"
                    "（用 write_file，path=deliverables/…，内容为最终报告/数据），"
                    "写完后用一句话说明成果文件路径。\n"
                    "若确实无需落盘成果，用 ask_user 询问用户后再结束。\n"
                    "Write the final result to a file under deliverables/ now, then reply in one line. "
                    "If no artifact is needed, ask the user via ask_user."
                )
                self._emit(
                    "info",
                    "模型仍停留在描述阶段且无交付物；已发出强制落盘要求（最后机会）。",
                )
                self._stream_until_pause(
                    agent,
                    {"messages": [{"role": "user", "content": finalize}]},
                    config,
                    seen_msg_ids,
                )
                final_early = self._drain_pending_interrupts(
                    agent,
                    config,
                    seen_msg_ids,
                    req,
                    allow_auto_lesson=False,
                )
                if final_early is not None:
                    return final_early
                # 复评：重新取文本 / 交付物 / 是否仍在计划
                try:
                    state = agent.get_state(config)
                    values = getattr(state, "values", None) or {}
                    messages = (
                        values.get("messages") if isinstance(values, dict) else None
                    )
                    if messages:
                        trajectory_messages = list(messages)
                        final_text = _extract_text(trajectory_messages) or final_text
                except Exception:
                    pass
                success_path = build_success_path_from_messages(trajectory_messages)
                arts = list_deliverable_names(req.project_root)
                still_planning = ai_reply_suggests_pending_action(final_text)

            incomplete = still_planning and not arts
            if incomplete:
                reasons = []
                if still_planning:
                    reasons.append("模型仍在描述下一步、未真正收尾")
                if not arts:
                    reasons.append("deliverables/ 无交付物")
                reason = "；".join(reasons)
                self._emit(
                    "info",
                    f"任务似乎未完成（{reason}）。"
                    "已跳过自动经验总结，避免把未完成流程固化进 scripts/pipeline.json。"
                    "可再次点「运行」继续，或确认完成后点「总结」。",
                )
                self._emit("info", "运行结束：未完成")
                return RunResult(
                    ok=False,
                    outcome="incomplete",
                    error="",
                    final_text=final_text,
                )

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
        """仅在尚无任何经验时自动总结；已有经验则跳过并提示手动发起。"""
        store = LessonStore(req.project_root)
        if not store.is_empty():
            self._emit(
                "info",
                "已有历史经验，本次未自动总结。需要时请点击「总结经验」"
                "（将结合上一份经验、本次日志与脚本，由 AI 新建一份带时间戳的经验）。",
            )
            return None
        if outcome == "cancelled":
            self._emit("info", "已取消；尚无经验，未写入取消类经验。")
            return None
        return self._write_lesson(
            req,
            outcome,
            summary,
            errors,
            success_path=success_path,
            notes=notes,
            artifacts="",
            events=list(self._run_events),
            use_ai=True,
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
        """人工发起经验总结：始终新建一份；优先用 AI（上一份经验+日志+脚本）。"""
        return self._write_lesson(
            req,
            outcome,
            summary,
            errors,
            success_path=success_path,
            notes=notes,
            artifacts="",
            events=events,
            use_ai=True,
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
        use_ai: bool = True,
    ) -> Lesson | None:
        try:
            store = LessonStore(req.project_root)
            # 固化用的「原始工具轨迹」：必须保留，不能被 AI 散文覆盖
            trace_for_scripts = (success_path or "").strip()

            # 事件优先：调用方传入 > 本轮内存缓冲 > 磁盘 events.jsonl
            if events is None:
                events = list(self._run_events) if self._run_events else None
            if not events:
                try:
                    from wokbee.core.models import ProjectEvent
                    from wokbee.core.paths import events_path

                    ep = events_path(req.project_root)
                    loaded: list = []
                    if ep.exists():
                        with ep.open("r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    loaded.append(ProjectEvent.from_dict(json.loads(line)))
                                except (json.JSONDecodeError, TypeError, KeyError):
                                    continue
                    events = loaded[-400:] if loaded else []
                except Exception:
                    logger.exception("读取运行日志失败，AI 总结将缺少日志上下文")
                    events = []
            # 内存缓冲有工具调用时并入（补磁盘竞态缺口）
            if self._run_events:
                events = list(events or []) + [
                    e
                    for e in self._run_events
                    if getattr(e, "kind", "") == "tool"
                    and (getattr(e, "meta", None) or {}).get("phase") == "call"
                ]

            previous_text = store.read_latest_text(max_chars=8000)
            run_log = build_lesson_digest(events)
            scripts_ctx = collect_scripts_context(req.project_root)
            env = build_runtime_env_block(
                project_root=str(req.project_root),
                model=f"{req.resolved.provider_name}/{req.resolved.model_id}",
                policy=req.approval.summary(),
                settings=self.settings,
            )

            self._emit(
                "info",
                "准备经验总结上下文：\n"
                f"- 上一份经验：{len(previous_text or '')} 字\n"
                f"- 运行日志：{len(run_log or '')} 字"
                f"（事件约 {len(events or [])} 条）\n"
                f"- 脚本/pipeline：{len(scripts_ctx or '')} 字",
            )

            ai_fields: dict[str, str] = {}
            if use_ai:
                try:
                    # ResolvedModel 才调模型；占位 SimpleNamespace 跳过
                    if getattr(req.resolved, "api_key", None) and getattr(
                        req.resolved, "api_host", None
                    ):
                        model_label = (
                            f"{req.resolved.provider_name}/{req.resolved.model_id}"
                        )
                        self._emit(
                            "info",
                            f"正在调用 AI 总结经验…\n"
                            f"模型：{model_label}\n"
                            f"输入：上一份经验 + 运行日志 + 脚本",
                        )
                        chat = build_chat_model(req.resolved, temperature=0.2)
                        ai_fields = summarize_lesson_with_ai(
                            model=chat,
                            goal=req.project.goal or req.user_message,
                            outcome=outcome,
                            previous_experience=previous_text,
                            run_log=run_log or summary,
                            scripts_context=scripts_ctx,
                            environment_hint=env,
                        )
                        # 结束后再展示完整结果（生成过程不刷进度气泡）
                        preview_parts = []
                        if ai_fields.get("summary"):
                            preview_parts.append(
                                f"**摘要**\n{ai_fields['summary'][:800]}"
                            )
                        if ai_fields.get("order_section"):
                            preview_parts.append(
                                f"**执行顺序**\n{ai_fields['order_section'][:1200]}"
                            )
                        if ai_fields.get("script_section"):
                            preview_parts.append(
                                f"**脚本步骤**\n{ai_fields['script_section'][:800]}"
                            )
                        if ai_fields.get("ai_section"):
                            preview_parts.append(
                                f"**需 AI 的步骤**\n{ai_fields['ai_section'][:600]}"
                            )
                        if ai_fields.get("notes"):
                            preview_parts.append(
                                f"**注意事项**\n{ai_fields['notes'][:600]}"
                            )
                        ai_scripts = ai_fields.get("script_files") or []
                        if ai_scripts:
                            names = [
                                str(x.get("filename") or "?")
                                for x in ai_scripts
                                if isinstance(x, dict)
                            ]
                            preview_parts.append(
                                f"**AI 手写脚本**\n" + "、".join(names[:12])
                            )
                        if preview_parts:
                            self._emit(
                                "agent",
                                "【AI 经验总结结果】\n\n" + "\n\n".join(preview_parts),
                                {"phase": "lesson"},
                            )
                        self._emit("info", "AI 经验总结完成，开始写入经验与脚本…")
                    else:
                        self._emit("info", "无可用模型密钥，改用规则回退总结经验")
                except Exception as e:
                    logger.exception("AI 总结经验失败，回退规则总结")
                    self._emit("info", f"AI 总结失败，改用规则回退：{e}")

            # 合并：AI 优先，否则用调用方/规则回退；绝不写入产物
            summary_f = (ai_fields.get("summary") or summary or "").strip()
            path_f = (ai_fields.get("success_path") or success_path or "").strip()
            order_f = (ai_fields.get("order_section") or "").strip()
            script_sec = (ai_fields.get("script_section") or "").strip()
            ai_sec = (ai_fields.get("ai_section") or "").strip()
            env_f = (ai_fields.get("environment") or env).strip()
            notes_f = (ai_fields.get("notes") or notes or "").strip()

            if not path_f:
                if outcome == "success":
                    path_f = (
                        "1. 明确目标与约束\n"
                        "2. 使用联网工具或读取 uploads/ 获取真实数据（禁止用 archives/；"
                        "同名或相近文件以最新修改时间为准）\n"
                        "3. 在 workspace/ 起草，最终写入 deliverables/\n"
                        "4. 用中文说明过程与数据来源（经验中不记录结果正文）"
                    )
                else:
                    path_f = (
                        "本次未形成完整成功路径。建议下次：\n"
                        "- 先读最新 memory/experiences/exp_*.md\n"
                        "- 优先复用已验证数据源、本地脚本与工具顺序\n"
                        f"- 关注失败原因：{errors or summary_f}"
                    )

            if not notes_f:
                notes_parts = []
                if errors:
                    notes_parts.append(f"- 错误线索：{errors[:500]}")
                notes_parts.append("- 需要实时数据时必须联网，禁止凭记忆编造。")
                notes_parts.append("- 禁止访问 archives/ 归档数据。")
                notes_parts.append("- 高危 execute / 写文件是否免审取决于项目审核策略。")
                notes_parts.append("- 确定性拉取步骤尽量固化到 scripts/；scripts 不参与归档。")
                notes_parts.append(
                    "- 脚本 callback 必须写入 workspace/script_callback_*.md，"
                    "后续 AI 步骤优先读取，禁止编造。"
                )
                notes_f = "\n".join(notes_parts)

            if not summary_f:
                summary_f = "本轮流程经验（方法向，不含结果/产物）。"

            lesson = Lesson(
                project_id=req.project.id,
                goal=req.project.goal or req.user_message,
                outcome=outcome,
                summary=summary_f,
                success_path=path_f,
                environment=env_f,
                notes=notes_f,
                artifacts="",  # 明确不写产物
                errors=errors,
                model=f"{req.resolved.provider_name}/{req.resolved.model_id}",
                policy=req.approval.summary(),
                order_section=order_f,
                script_section=script_sec,
                ai_section=ai_sec,
            )

            # 固化本地脚本：用原始工具轨迹 + 事件，勿只用 AI 改写后的散文路径
            try:
                solidify_path = "\n".join(
                    x for x in (trace_for_scripts, path_f) if x
                )
                solid = solidify_scripts(
                    req.project_root,
                    lesson_id=lesson.id,
                    goal=lesson.goal,
                    summary=summary_f,
                    success_path=solidify_path,
                    events=events,
                )
                # 总结 AI 手写的 .bat/.json/.py 等一并写入 scripts/ 并并入 pipeline
                ai_script_files = []
                if isinstance(ai_fields, dict):
                    raw_files = ai_fields.get("script_files")
                    if isinstance(raw_files, list):
                        ai_script_files = raw_files
                ai_written = apply_ai_authored_scripts(
                    req.project_root,
                    lesson_id=lesson.id,
                    script_files=ai_script_files,
                )
                ai_pipeline = []
                if isinstance(ai_fields, dict):
                    raw_pipe = ai_fields.get("pipeline_steps")
                    if isinstance(raw_pipe, list):
                        ai_pipeline = raw_pipe
                applied_order = apply_ai_pipeline_steps(
                    req.project_root,
                    lesson_id=lesson.id,
                    goal=lesson.goal,
                    pipeline_steps=ai_pipeline,
                )
                # AI 未给出清单时用固化结果补全；有脚本时以固化章节为准（更准确）
                if solid.script_steps or ai_written:
                    lesson.script_section = solid.script_section_md or lesson.script_section
                    if ai_written:
                        extra = "\n".join(
                            f"- `{s.rel_path}` — {s.description}（AI 手写）"
                            for s in ai_written
                        )
                        if lesson.script_section and "无可固化" not in lesson.script_section:
                            lesson.script_section = (
                                lesson.script_section.rstrip() + "\n" + extra
                            )
                        else:
                            lesson.script_section = (
                                extra
                                + "\n\n约定：脚本输出写入 workspace/script_callback_*.md。"
                            )
                    lesson.order_section = solid.order_section_md or lesson.order_section
                    if solid.ai_section_md:
                        lesson.ai_section = solid.ai_section_md
                else:
                    if not lesson.script_section:
                        lesson.script_section = solid.script_section_md
                    if not lesson.ai_section:
                        lesson.ai_section = solid.ai_section_md
                    if not lesson.order_section:
                        lesson.order_section = solid.order_section_md
                lesson.scripts = [s.rel_path for s in solid.script_steps] + [
                    s.rel_path for s in ai_written
                ]
                # 去重保序
                seen_sp: set[str] = set()
                uniq_scripts: list[str] = []
                for p in lesson.scripts:
                    if p not in seen_sp:
                        seen_sp.add(p)
                        uniq_scripts.append(p)
                lesson.scripts = uniq_scripts
                lesson.pipeline = solid.pipeline_rel
                total_scripts = len(lesson.scripts)
                if total_scripts or solid.ai_steps or applied_order:
                    order_note = (
                        "（已采用 AI 给出的 pipeline_steps 顺序）"
                        if applied_order
                        else "（自动固化默认顺序：脚本…→AI…→收尾脚本）"
                    )
                    self._emit(
                        "info",
                        f"已写入有序执行步骤到 scripts/pipeline.json"
                        f"（脚本 {total_scripts} + AI {len(solid.ai_steps)}），"
                        f"其中 AI 手写脚本 {len(ai_written)} 个{order_note}；"
                        f"下次按 steps 一路执行；scripts/ 不参与归档",
                        {"scripts": lesson.scripts},
                    )
                elif not total_scripts:
                    self._emit(
                        "info",
                        "本轮未识别到可固化脚本，且 AI 未手写 script_files；"
                        "故 scripts/ 无新脚本。下次若有 execute/.py/.bat 或 AI 手写，"
                        "总结时会写入。",
                    )
            except Exception:
                logger.exception("固化脚本失败（经验仍会写入）")

            # 保存本次用到的 Skills 快照与参考材料到 references/（归档不清理）
            try:
                from wokbee.core.references import (
                    snapshot_used_skills,
                    write_reference_manifest,
                )

                used_skills: list[str] = []
                mats: list[dict] = []
                if isinstance(ai_fields, dict):
                    raw_skills = ai_fields.get("used_skills")
                    if isinstance(raw_skills, list):
                        used_skills = [str(s) for s in raw_skills if str(s).strip()]
                    raw_mats = ai_fields.get("reference_materials")
                    if isinstance(raw_mats, list):
                        mats = [
                            m
                            for m in raw_mats
                            if isinstance(m, dict)
                            and (str(m.get("path") or "").strip() or str(m.get("note") or "").strip())
                        ]
                written = snapshot_used_skills(
                    req.project_root,
                    used_skills,
                )
                manifest_path = write_reference_manifest(
                    req.project_root,
                    used_skills=used_skills,
                    materials=mats,
                    goal=lesson.goal or "",
                )
                snap_msg = f"已保存 {len(written)} 个 Skill 快照到 references/skills/"
                if manifest_path:
                    try:
                        mrel = manifest_path.relative_to(req.project_root).as_posix()
                    except ValueError:
                        mrel = str(manifest_path)
                    snap_msg += f"，并登记 {mrel}"
                if written or manifest_path:
                    self._emit("info", snap_msg + "（references/ 不会被归档）")
            except Exception:
                logger.exception("保存 references/ 材料失败（经验仍会写入）")

            # 清理过期脚本与 Skill 快照：丢到 archives/（可逆，不进入下次运行/上下文）
            try:
                from wokbee.engine.script_factory import quarantine_obsolete_scripts
                from wokbee.engine.script_runner import load_pipeline
                from wokbee.core.references import quarantine_obsolete_skill_snapshots

                used_skills_cur: list[str] = []
                if isinstance(ai_fields, dict):
                    raw_skills = ai_fields.get("used_skills")
                    if isinstance(raw_skills, list):
                        used_skills_cur = [
                            str(s) for s in raw_skills if str(s).strip()
                        ]

                # kept = 当前 pipeline 引用的脚本 ∪ 本轮 lesson.scripts（保守超集，宁可多留）
                kept_paths: list[str] = []
                pipe = load_pipeline(req.project_root) or {}
                steps = pipe.get("steps") if isinstance(pipe.get("steps"), list) else []
                for s in steps:
                    if isinstance(s, dict) and s.get("type") == "script":
                        p = str(s.get("path") or "").strip()
                        if p:
                            kept_paths.append(p)
                for sp in lesson.scripts:
                    rel = sp if str(sp).startswith("scripts/") else f"scripts/{Path(sp).name}"
                    kept_paths.append(rel)

                moved_scripts, script_dest = quarantine_obsolete_scripts(
                    req.project_root,
                    kept_paths=kept_paths,
                    lesson_id=lesson.id,
                )
                if moved_scripts:
                    self._emit(
                        "info",
                        f"已将 {len(moved_scripts)} 个过期脚本移入 {script_dest}，"
                        "下次运行不再读取，避免浪费 token；可回收。",
                        {"moved_scripts": moved_scripts},
                    )

                moved_skills, skill_dest = quarantine_obsolete_skill_snapshots(
                    req.project_root,
                    used_skills=used_skills_cur,
                )
                if moved_skills:
                    self._emit(
                        "info",
                        f"已将 {len(moved_skills)} 个过期 Skill 快照移入 {skill_dest}，"
                        "仅清理不再用到的快照，未动导入的材料文件。",
                        {"moved_skills": moved_skills},
                    )
            except Exception:
                logger.exception("清理过期脚本/Skill 快照失败（经验仍会写入）")

            path = store.save(lesson)
            try:
                rel = path.relative_to(req.project_root).as_posix()
            except ValueError:
                rel = str(path)
            self._emit(
                "lesson",
                f"已新建经验：{rel}\n"
                f"（多份并存，运行只加载最新；内容不含结果/产物）",
                {"lesson_id": lesson.id, "path": str(path)},
            )
            return lesson
        except Exception:
            logger.exception("写入 lesson 失败")
            return None


def resolve_model_for_project(
    project: Project,
    settings: WokBeeSettings,
    provider_store: ProviderStore | None = None,
) -> ResolvedModel:
    """解析项目模型：项目绑定 → 厂商默认模型 → WokBee 设置默认 → 列表第一个。"""
    store = provider_store or ProviderStore()
    # 1) 项目已绑定
    provider = (project.provider or "").strip()
    model_id = (project.model_id or "").strip()
    if provider and model_id:
        resolved = store.resolve(provider, model_id)
        if resolved:
            return resolved
    # 2) 厂商设置里的「默认」徽章（用户认知上的默认模型）
    default = store.resolve_default()
    if default:
        return default
    # 3) WokBee 设置页可选覆盖
    wp = (settings.default_provider or "").strip()
    wm = (settings.default_model_id or "").strip()
    if wp and wm:
        resolved = store.resolve(wp, wm)
        if resolved:
            return resolved
    first = store.first_resolved()
    if not first:
        raise ValueError("没有可用模型，请先在「AI配置 → 厂商设置」中启用模型并填写 Key/Host。")
    return first
