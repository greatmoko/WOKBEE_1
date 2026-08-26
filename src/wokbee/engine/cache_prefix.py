"""DeepSeek 前缀缓存友好：ImmutablePrefix / 工具钉死 / hit 观测 / 结果裁剪。

借鉴 Reasonix Cache-First Loop：静态前缀不变、历史只追加、易变内容不上 system、
超长 tool 结果截断，并暴露 prompt_cache_hit/miss。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from wokbee.core.models import MAX_PROJECT_TITLE_LEN

logger = logging.getLogger("wokbee")

# 进入模型上下文的 tool 结果上限（字符，约合数千 tokens）
TOOL_RESULT_MAX_CHARS = 12_000
# 落盘完整结果时的文件名前缀
TOOL_RESULT_DUMP_PREFIX = "tool_result_"


def static_system_prompt(*, mode: str) -> str:
    """会话级不可变 system：不含项目名/目标/审核/经验/步数。"""
    if mode == "chat":
        return (
            "你是 WokBee——运行在用户本机上的工作助手，具备**完整**网络与本机执行能力。\n"
            "当前是**交互模式**（用户点「发送」）：不自动跑经验/脚本有序管线，"
            "但你仍可自由使用全部能力完成用户请求。\n"
            "用户提问**可以与项目目标无关**；请正常回答并在需要时调用工具。\n"
            "可用能力：web_search / deepseek_web_search / http_get / http_request、文件读写/搜索、"
            "execute 本机命令、Skills（/skills/）、MCP（若已加载）、"
            "get_project_info / update_project_title / update_project_goal、ask_user（澄清意图）。\n"
            "联网：需要高质量、最新、多轮检索且带引用的资料时优先 deepseek_web_search；"
            "普通快捷查询用 web_search/http_get 即可。\n"
            "意图不清时必须先调用 ask_user 弹窗提问（单选/多选），禁止擅自猜测关键选择。\n"
            "**严禁**访问 archives/。\n"
            "当用户要求改名、改目标，或「根据对话总结后更新目标/名称」时，"
            "请先理解对话再调用项目工具，并用中文确认。\n"
            f"项目名称须尽量简短，最多 {MAX_PROJECT_TITLE_LEN} 字，禁止用整句目标当名称。\n"
            "工作区：workspace/；交付物：deliverables/；上传：uploads/（同名或相近以最新为准）；记忆：memory/；"
            "参考材料：references/。\n"
            "当你使用外部软件/服务、需登录、或依赖环境参数/密钥时，请把可复用的第三方代码、"
            "配置、环境参数与登录信息保存到 references/，并在 references/MANIFEST.md 登记，"
            "确保下次能稳定复跑；references/ 不会被归档。这些敏感信息仅供本机使用，勿外发。\n"
            "本轮具体项目态（名称、目标、审核、经验摘要等）见用户消息中的"
            "【会话上下文】；勿假设 system 会随轮次改写。\n"
            "用中文说明你在做什么；需要落盘的结果可写入 workspace/ 或 deliverables/。\n"
            "完整自动化管线（按 pipeline 有序执行）请用户点击「运行」。"
        )
    return (
        "你是 WokBee——运行在用户本机上的工作助手，具备完整网络与本机执行能力。\n"
        "这不是离线沙箱：你可以使用 web_search、http_get、http_request 访问公网，"
        "也可以用 execute 运行本机命令（curl/python 等）。\n"
        "联网：需要高质量、最新、多轮检索且带引用的资料时优先 deepseek_web_search；"
        "普通快捷查询用 web_search/http_get 即可。\n"
        "**严禁**读取、列举、搜索或通过 shell 访问 `archives/`；"
        "归档文档与归档数据不得作为当前运行的数据来源。\n"
        "全局 Skills 只读挂载在 /skills/（来自本机公共 Skills 目录，未复制进本项目）；"
        "需要时请读取 /skills/<技能名>/SKILL.md 并遵循。\n"
        "若已加载 MCP 工具，可直接调用它们完成外部系统操作。\n"
        "需要实时信息（天气、新闻、资料）时，必须先联网查询，禁止凭空编造数据；"
        "若查询失败再说明原因并给出备选方案。\n"
        "意图不清或有多种做法时：先调用 ask_user 弹窗向用户提问（单选/多选），再继续。\n"
        "项目经验在 memory/experiences/；关注实现步骤 / 执行顺序 / 运行环境 / 注意事项，"
        "忽略结果与产物描述。具体是否注入最新经验见用户消息【会话上下文】。\n"
        "开干前先对照执行顺序 / 成功路径 / 注意事项；主机按 pipeline.json 的 steps 顺序推进，"
        "仅在 type=ai 的步骤唤你。\n"
        "脚本 callback 已落盘到 workspace/script_callback_*.md；"
        "你做提取/创作时必须先读这些文件，禁止凭空编造脚本未提供的事实。\n"
        "项目根目录为工作根；工作区：workspace/；交付物：deliverables/；"
        "用户上传：uploads/（同名或相近以最新为准）；记忆：memory/；参考材料：references/。\n"
        "当你使用外部软件/服务、需登录、或依赖环境参数/密钥时，请把可复用的第三方代码、"
        "配置、环境参数与登录信息保存到 references/，并在 references/MANIFEST.md 登记，"
        "确保下次能稳定复跑；references/ 不会被归档。这些敏感信息仅供本机使用，勿外发。\n"
        "执行过程中用中文简要说明你在做什么；最终成果写入 deliverables/；"
        "若 uploads/ 有用户文件请优先读取使用；"
        "同名或内容相近的多份文件默认以修改时间最新的一份为准，勿混用旧版。\n"
        "可用项目工具：get_project_info / update_project_title / update_project_goal；"
        "用户要求改名称或目标、或总结对话后更新时请调用它们。"
        f"名称尽量简短，最多 {MAX_PROJECT_TITLE_LEN} 字。\n"
        "经验总结：无经验时运行结束可自动总结；之后由用户「总结经验」新建带时间戳文档。\n"
        "若存在 scripts/pipeline.json：优先本地跑脚本；仅失败、数据异常或需创作时再调用模型。\n"
        "本轮具体项目态（名称、目标、审核、步数上限、经验摘要）见用户消息【会话上下文】；"
        "system 在本会话内保持字节级稳定以利于 DeepSeek 前缀缓存。"
    )


def build_session_context_block(
    *,
    title: str,
    goal: str,
    approval_summary: str,
    max_steps: int | None = None,
    experience_digest: str = "",
    mode: str = "run",
) -> str:
    """易变内容：拼进首条/当轮 user，不进 system。"""
    lines = [
        "【会话上下文】（本块可能随项目变更；勿写入对 system 稳定性的假设）",
        f"- 模式：{'交互' if mode == 'chat' else '运行'}",
        f"- 项目名称：{title or '未命名项目'}",
        f"- 目标：{goal or '（未设置）'}",
        f"- 审核策略：{approval_summary or '（未设置）'}",
    ]
    if max_steps is not None and int(max_steps) > 0:
        lines.append(f"- 步数上限约：{int(max_steps)}（请聚焦目标）")
    if experience_digest.strip():
        lines.append("")
        lines.append(experience_digest.strip())
    return "\n".join(lines)


def compose_user_with_context(user_message: str, context_block: str) -> str:
    user_message = (user_message or "").strip()
    context_block = (context_block or "").strip()
    if not context_block:
        return user_message
    if not user_message:
        return context_block
    return f"{context_block}\n\n——\n{user_message}"


def tool_name_of(tool: Any) -> str:
    name = getattr(tool, "name", None)
    if name:
        return str(name)
    return str(tool)


def sort_tools_by_name(tools: list) -> list:
    """稳定排序，避免 MCP 返回顺序抖动破坏 tools 前缀。"""
    return sorted(list(tools or []), key=lambda t: tool_name_of(t).lower())


def prefix_fingerprint(system_prompt: str, tool_names: list[str]) -> str:
    h = hashlib.sha256()
    h.update((system_prompt or "").encode("utf-8"))
    h.update(b"\0")
    h.update("\n".join(tool_names).encode("utf-8"))
    return h.hexdigest()[:12]


def truncate_tool_result(
    text: str,
    *,
    max_chars: int = TOOL_RESULT_MAX_CHARS,
    dump_dir: Path | None = None,
    tool_name: str = "tool",
) -> str:
    """截断进入模型上下文的 tool 结果；完整内容可落盘。"""
    raw = text if isinstance(text, str) else str(text or "")
    if len(raw) <= max_chars:
        return raw
    note = ""
    if dump_dir is not None:
        try:
            dump_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in (tool_name or "tool"))[:40]
            path = dump_dir / f"{TOOL_RESULT_DUMP_PREFIX}{safe}.txt"
            path.write_text(raw, encoding="utf-8")
            note = f"\n（完整结果已写入 `{path.as_posix()}`，需要时请再读取）"
        except OSError as e:
            logger.warning("落盘超长 tool 结果失败: %s", e)
            note = "\n（完整结果过长，落盘失败，仅保留摘要）"
    head_budget = max(64, max_chars - 180)
    head = raw[:head_budget].rstrip()
    return (
        f"{head}\n\n…（已截断，原约 {len(raw)} 字，上限 {max_chars}）"
        f"{note}"
    )


def wrap_tools_truncate_results(
    tools: list,
    *,
    project_root: Path | None = None,
    max_chars: int = TOOL_RESULT_MAX_CHARS,
) -> list:
    """包装工具返回值，避免超长 callback 污染后续前缀增长。"""
    dump_dir = None
    if project_root is not None:
        dump_dir = Path(project_root) / "workspace"

    wrapped: list = []
    for tool in tools or []:
        try:
            wrapped.append(_wrap_one_tool(tool, dump_dir=dump_dir, max_chars=max_chars))
        except Exception:
            logger.exception("包装工具失败，使用原工具：%s", tool_name_of(tool))
            wrapped.append(tool)
    return wrapped


def _wrap_one_tool(tool: Any, *, dump_dir: Path | None, max_chars: int) -> Any:
    name = tool_name_of(tool)

    def _truncate(result: Any) -> Any:
        if result is None:
            return result
        if isinstance(result, (dict, list)):
            import json

            text = json.dumps(result, ensure_ascii=False)
            if len(text) <= max_chars:
                return result
            return truncate_tool_result(
                text, max_chars=max_chars, dump_dir=dump_dir, tool_name=name
            )
        text = result if isinstance(result, str) else str(result)
        return truncate_tool_result(
            text, max_chars=max_chars, dump_dir=dump_dir, tool_name=name
        )

    # StructuredTool / BaseTool：优先包 _run / func
    if hasattr(tool, "func") and callable(getattr(tool, "func")):
        orig = tool.func

        def hooked(*args, **kwargs):
            return _truncate(orig(*args, **kwargs))

        try:
            return tool.model_copy(update={"func": hooked})
        except Exception:
            try:
                tool.func = hooked  # type: ignore[attr-defined]
                return tool
            except Exception:
                return tool

    if hasattr(tool, "_run") and callable(getattr(tool, "_run")):
        orig_run = tool._run

        def hooked_run(*args, **kwargs):
            return _truncate(orig_run(*args, **kwargs))

        try:
            object.__setattr__(tool, "_run", hooked_run)
        except Exception:
            try:
                tool._run = hooked_run  # type: ignore[method-assign]
            except Exception:
                return tool
        return tool

    return tool


def extract_cache_tokens(msg: Any) -> tuple[int, int]:
    """从 AIMessage 解析 (hit, miss)；无数据则 (0, 0)。"""
    hit = 0
    miss = 0

    um = getattr(msg, "usage_metadata", None)
    if isinstance(um, dict):
        details = um.get("input_token_details") or {}
        if isinstance(details, dict):
            hit = int(details.get("cache_read") or details.get("cache_read_tokens") or 0)
        inp = int(um.get("input_tokens") or 0)
        if inp and hit:
            miss = max(0, inp - hit)
        elif inp and not hit:
            miss = inp

    rm = getattr(msg, "response_metadata", None) or {}
    if not isinstance(rm, dict):
        rm = {}
    usage = rm.get("token_usage") or rm.get("usage") or {}
    if isinstance(usage, dict):
        if not hit:
            hit = int(
                usage.get("prompt_cache_hit_tokens")
                or usage.get("cache_hit_tokens")
                or 0
            )
        ds_miss = usage.get("prompt_cache_miss_tokens")
        if ds_miss is not None:
            miss = int(ds_miss)
        elif not miss:
            prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            if prompt:
                miss = max(0, prompt - hit)

    if isinstance(msg, dict):
        usage = msg.get("usage") or {}
        if isinstance(usage, dict):
            if not hit:
                hit = int(usage.get("prompt_cache_hit_tokens") or 0)
            if usage.get("prompt_cache_miss_tokens") is not None:
                miss = int(usage["prompt_cache_miss_tokens"])

    return max(0, hit), max(0, miss)


@dataclass
class CacheHitTracker:
    """会话级累计 hit/miss（Reasonix：本轮% + 会话 avg%）。"""

    hit_total: int = 0
    miss_total: int = 0
    last_hit: int = 0
    last_miss: int = 0
    turns: int = 0
    prefix_fp: str = ""
    on_update: Callable[[dict], None] | None = field(default=None, repr=False)

    def note_prefix(self, fp: str, tool_count: int) -> None:
        self.prefix_fp = fp
        self._emit_update(extra={"prefix_fp": fp, "tool_count": tool_count, "phase": "pin"})

    def observe_message(self, msg: Any) -> dict | None:
        hit, miss = extract_cache_tokens(msg)
        if hit <= 0 and miss <= 0:
            return None
        self.last_hit = hit
        self.last_miss = miss
        self.hit_total += hit
        self.miss_total += miss
        self.turns += 1
        payload = self.as_dict()
        self._emit_update(extra={"phase": "turn"})
        return payload

    def as_dict(self) -> dict:
        last_den = self.last_hit + self.last_miss
        sess_den = self.hit_total + self.miss_total
        return {
            "last_hit": self.last_hit,
            "last_miss": self.last_miss,
            "hit_total": self.hit_total,
            "miss_total": self.miss_total,
            "turns": self.turns,
            "now_pct": round(100.0 * self.last_hit / last_den) if last_den else None,
            "avg_pct": round(100.0 * self.hit_total / sess_den) if sess_den else None,
            "prefix_fp": self.prefix_fp,
        }

    def format_tag(self) -> str:
        d = self.as_dict()
        now_s = f"{d['now_pct']}%" if d["now_pct"] is not None else "—"
        avg_s = f"{d['avg_pct']}%" if d["avg_pct"] is not None else "—"
        return f"cache {now_s} · avg {avg_s}"

    def _emit_update(self, *, extra: dict | None = None) -> None:
        if not self.on_update:
            return
        payload = self.as_dict()
        if extra:
            payload.update(extra)
        try:
            self.on_update(payload)
        except Exception:
            logger.exception("cache tracker on_update 失败")


# --------------------------------------------------------------------------- #
# 前缀护栏（Prefix Guard）：Reasonix 式 append-only 检测
# --------------------------------------------------------------------------- #
# DeepSeek 自动前缀缓存不认 cache_control，只认「本请求前缀与上一请求逐字节一致」。
# 命中率低的根源几乎都是「静态前缀或消息历史被改写」，而非没开缓存。
# 本护栏在每次模型轮次结束后，对整条消息历史做 stable-JSON + SHA-256 逐下标哈希，
# 只在「旧前缀被改写」时告警（正常 append 不告警），并定位到具体消息。
# 对齐社区 pi-deepseek-cache 的 P2 前缀护栏：append-only、只在改写时告警。


def _message_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or msg.get("type") or "")
    return str(getattr(msg, "type", None) or "")


def _message_content_text(content: Any) -> str:
    """把 str / list[block] / dict 统一成纯文本，稳定参与哈希。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type")
                if t == "text":
                    parts.append(str(block.get("text") or ""))
                elif t == "image":
                    parts.append("[image]")
                elif isinstance(block.get("content"), (str, list)):
                    parts.append(_message_content_text(block.get("content")))
                else:
                    parts.append(str(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if isinstance(content, dict):
        return _message_content_text(content.get("content") or content)
    return str(content)


def _message_fingerprint(msg: Any) -> str:
    """单条消息 → stable-JSON 哈希。只取会上行模型的关键字段，不含 metadata。"""
    if isinstance(msg, dict):
        role = _message_role(msg)
        payload = {
            "role": role,
            "content": _message_content_text(msg.get("content")),
        }
        if msg.get("name"):
            payload["name"] = str(msg["name"])
        if msg.get("tool_call_id"):
            payload["tool_call_id"] = str(msg["tool_call_id"])
        tcs = msg.get("tool_calls")
        if tcs:
            payload["tool_calls"] = [
                {
                    "id": str(tc.get("id") or ""),
                    "name": str(tc.get("name") or ""),
                    "args": _stable_json(tc.get("args") or tc.get("function") or {}),
                }
                for tc in tcs
            ]
    else:
        payload = {
            "role": _message_role(msg),
            "content": _message_content_text(getattr(msg, "content", None)),
        }
        name = getattr(msg, "name", None)
        if name:
            payload["name"] = str(name)
        tcid = getattr(msg, "tool_call_id", None)
        if tcid:
            payload["tool_call_id"] = str(tcid)
        tcs = getattr(msg, "tool_calls", None) or getattr(msg, "additional_kwargs", {}).get("tool_calls")
        if tcs:
            normalized = []
            for tc in tcs:
                if hasattr(tc, "get"):
                    function = tc.get("function") or {}
                    normalized.append(
                        {
                            "id": str(tc.get("id") or ""),
                            "name": str(tc.get("name") or function.get("name") or ""),
                            "args": _stable_json(tc.get("args") or function.get("arguments") or {}),
                        }
                    )
                else:
                    normalized.append({"id": str(getattr(tc, "id", "") or ""), "name": str(getattr(tc, "name", "") or "")})
            payload["tool_calls"] = normalized
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _stable_json(obj: Any) -> Any:
    """尽力把 args/function 归一化成可稳定序列化的 JSON 值。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_stable_json(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _stable_json(v) for k, v in obj.items()}
    return str(obj)


@dataclass
class PrefixGuard:
    """会话级前缀护栏：检测消息历史 append-only 是否被破坏。

    复用 Reasonix 的「turn-end cap + 只追加」不变量：正常往尾部追加新消息
    不会触发告警；一旦某个已出现下标的前缀哈希改变（改写/重排/压缩/就地修改），
    即上报改写点，用于归因命中率骤降。
    """

    _seen: dict[int, str] = field(default_factory=dict)  # index -> 前缀哈希
    _count: int = 0
    static_fp: str = ""
    on_drift: Callable[[dict], None] | None = field(default=None, repr=False)

    def note_static(self, fp: str, tool_count: int) -> None:
        # 只存静态指纹；钉死/前缀信息由 CacheHitTracker 的 pin 事件展示，
        # 不把「无 drift」的载荷送进 on_drift，避免误报改写。
        self.static_fp = fp

    def check(self, messages: list) -> dict:
        """对整条消息历史做 append-only 校验。

        返回 {"ok": bool, "drift": dict|None, "count": int}；发现改写时先上报再返回。
        """
        if not messages:
            return {"ok": True, "drift": None, "count": self._count}

        fprs = [_message_fingerprint(m) for m in messages]
        hasher = hashlib.sha256()
        current: dict[int, str] = {}
        drift: dict | None = None

        for i, fp in enumerate(fprs):
            hasher.update(fp.encode("utf-8"))
            current[i] = hasher.hexdigest()[:20]
            if drift is None and i in self._seen and self._seen[i] != current[i]:
                drift = {
                    "index": i,
                    "role": _message_role(messages[i]),
                    "content": _message_content_text(
                        messages[i].get("content") if isinstance(messages[i], dict)
                        else getattr(messages[i], "content", "")
                    )[:160],
                    "kind": "rewrite",
                }

        # 历史缩短：原地删/切中间消息也算破坏前缀
        if drift is None and messages and len(fprs) < self._count:
            drift = {
                "index": len(fprs),
                "role": "eof",
                "content": f"消息数由 {self._count} 缩为 {len(fprs)}",
                "kind": "truncated",
            }

        self._seen = current
        self._count = len(fprs)

        result = {"ok": drift is None, "drift": drift, "count": len(fprs)}
        if drift is not None:
            self._report_extra({"phase": "drift", **result})
        return result

    def _report_extra(self, extra: dict) -> None:
        if not self.on_drift:
            return
        try:
            self.on_drift(extra)
        except Exception:
            logger.exception("prefix guard on_drift 失败")
