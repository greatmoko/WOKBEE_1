"""工具结果截断、工具名/排序与工具前缀指纹（DeepSeek 前缀可达性稳定化）。"""

from __future__ import annotations

import hashlib
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Callable


logger = logging.getLogger("wokbee")


TOOL_RESULT_MAX_CHARS = 12_000
# 落盘完整结果时的文件名前缀
TOOL_RESULT_DUMP_PREFIX = "tool_result_"


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
            # 加纳秒级时间戳避免并发/多会话互相覆盖（同名多次调用不再复用同一文件）。
            path = dump_dir / f"{TOOL_RESULT_DUMP_PREFIX}{safe}_{time.time_ns()}.txt"
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

    def _hook(fn: Callable) -> Callable:
        """包装（可能异步的）原函数；异步函数保持异步，否则 ainvoke 会取回裸协程。"""
        if inspect.iscoroutinefunction(fn):
            async def _ahooked(*args, **kwargs):
                return _truncate(await fn(*args, **kwargs))
            return _ahooked

        def _hooked(*args, **kwargs):
            return _truncate(fn(*args, **kwargs))
        return _hooked

    # 异步入口（async @tool / MCP 等）必须一并包装，否则 ainvoke 走 coroutine 时绕过截断
    coro = getattr(tool, "coroutine", None)
    if inspect.iscoroutinefunction(coro):
        aw = _hook(coro)
        try:
            return tool.model_copy(update={"coroutine": aw})
        except Exception:
            try:
                object.__setattr__(tool, "coroutine", aw)
                return tool
            except Exception:
                return tool

    # StructuredTool / BaseTool：优先包 _run / func
    if hasattr(tool, "func") and callable(getattr(tool, "func")):
        orig = tool.func
        newfunc = _hook(orig)
        update = {"func": newfunc}
        if inspect.iscoroutinefunction(orig):
            # func 为协程时同步替换 coroutine，避免 ainvoke 返回裸协程
            update["coroutine"] = newfunc
        try:
            return tool.model_copy(update=update)
        except Exception:
            try:
                tool.func = newfunc  # type: ignore[attr-defined]
                if inspect.iscoroutinefunction(orig):
                    object.__setattr__(tool, "coroutine", newfunc)
                return tool
            except Exception:
                return tool

    if hasattr(tool, "_run") and callable(getattr(tool, "_run")):
        orig_run = tool._run
        if inspect.iscoroutinefunction(orig_run):
            # 异步 _run：优先包配套的异步 _arun（ainvoke 实际走它）
            arun = getattr(tool, "_arun", None)
            if inspect.iscoroutinefunction(arun):
                aw = _hook(arun)
                try:
                    object.__setattr__(tool, "_arun", aw)
                    return tool
                except Exception:
                    pass
            elif callable(arun):
                orig_arun = arun

                def _ahooked_run(*args, **kwargs):
                    return _truncate(orig_arun(*args, **kwargs))

                try:
                    object.__setattr__(tool, "_arun", _ahooked_run)
                    return tool
                except Exception:
                    pass
            return tool

        def hooked_run(*args, **kwargs):
            return _truncate(orig_run(*args, **kwargs))

        try:
            object.__setattr__(tool, "_run", hooked_run)
        except Exception:
            try:
                tool._run = hooked_run  # type: ignore[method-assign]
            except Exception:
                return tool

        # 同步 _run 分支同样包装配套的异步 _arun：ainvoke 优先走 _arun，
        # 若只包 _run，自定义 async 工具经 ainvoke 会绕过截断，把前缀撑大、命中率掉。
        arun = getattr(tool, "_arun", None)
        if inspect.iscoroutinefunction(arun):
            aw = _hook(arun)
            try:
                object.__setattr__(tool, "_arun", aw)
            except Exception:
                try:
                    tool._arun = aw  # type: ignore[method-assign]
                except Exception:
                    pass
        return tool

    return tool
