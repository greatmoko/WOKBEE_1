"""工具结果截断、工具名/排序与工具前缀指纹（DeepSeek 前缀可达性稳定化）。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, create_model


logger = logging.getLogger("wokbee")

# 单个工具执行超时：超过时限立即返回失败结果，交给 Agent 接管。
# 人在环工具（ask_user 在工具体内 interrupt()）与子代理工具（task）不套硬超时，
# 否则会吞掉 graph interrupt 或误杀长时间合法的子代理运行。
TOOL_TIMEOUT_EXEMPT = frozenset({"ask_user", "task", "request_access"})

# AI 逐调用覆盖默认超时的保留参数名：调用工具时传 `timeout_seconds`，未传则用全局默认值。
PER_CALL_TIMEOUT_KEY = "timeout_seconds"

# 供同步工具超时使用的共享线程池（进程级、跨运行复用）。
_TOOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(8, min(16, (os.cpu_count() or 2) + 4)),
    thread_name_prefix="wokbee-tool",
)


def tool_timeout_result(tool_name: str, timeout: float) -> str:
    return (
        f"工具 `{tool_name}` 执行超时（> {timeout:g} 秒），已被终止并交由 AI 接管。"
        "请拆成更小的步骤，或改用更轻量的替代方式。"
    )


def _resolve_tool_timeout(kwargs: dict, default_timeout: float | None) -> float | None:
    """从调用 kwargs 里读出该次调用的生效超时（秒）。

    规则：AI 显式传 `timeout_seconds` 则优先生效（>0 用之；<=0 视为"不限定"，返回 None）；
    传了但非法（非数字）回落默认；未传则回落默认；默认 <=0 表示禁用超时。
    同时把 `timeout_seconds` 从 kwargs 弹出，避免透传给真正的工具函数（其签名不认识它）。
    """
    base = None if (default_timeout is None or default_timeout <= 0) else float(default_timeout)
    per_call = kwargs.pop(PER_CALL_TIMEOUT_KEY, None)
    if per_call is None:
        return base
    try:
        value = float(per_call)
    except (TypeError, ValueError):
        return base
    return value if value > 0 else None


def _inject_timeout_schema(schema: Any) -> Any:
    """给工具 args_schema 注入可选的 `timeout_seconds` 字段，让模型看得到、传得了。

    通过为原 schema 生成一个带新增字段的子类实现；若 schema 不是 pydantic BaseModel、
    或注入失败，则原样返回（优雅降级：该工具无法用 timeout_seconds，仅影响覆盖面）。
    """
    if schema is None or not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        return schema
    if PER_CALL_TIMEOUT_KEY in getattr(schema, "model_fields", {}):
        return schema
    try:
        return create_model(
            f"{schema.__name__}_WokBeeTimeout",
            __base__=schema,
            timeout_seconds=(
                Optional[float],
                Field(
                    default=None,
                    ge=1,
                    description=(
                        "覆盖此工具调用的默认超时（秒）。下载/长时任务可传较大值以延长等待，"
                        "未传则用全局默认。"
                    ),
                ),
            ),
        )
    except Exception:  # noqa: BLE001
        return schema


def _callable_with_timeout(
    fn: Callable, tool_name: str, default_timeout: float | None
) -> Callable:
    """给工具执行挂上超时：超时返回失败结果，其余异常照常抛出。

    `default_timeout` 是全局默认；AI 可在调用时传 `timeout_seconds` 覆盖（见
    `_resolve_tool_timeout`），仅当两者都未提供/无效时才不超时。

    - 异步 coroutine 用 asyncio.wait_for，可真正取消。
    - 同步函数用共享线程池跑 + result(timeout)：超时后主线程返回失败。
      被放弃的线程会继续到其自身收尾（execute/网络工具自带子进程与连接超时，
      因而不会被无限占用）；纯 Python 线程无法在进程内强杀，此为已知边界。
    """
    if tool_name in TOOL_TIMEOUT_EXEMPT:
        return fn
    if inspect.iscoroutinefunction(fn):
        async def _aw(*args, **kwargs):
            timeout = _resolve_tool_timeout(kwargs, default_timeout)
            if not timeout:
                return await fn(*args, **kwargs)
            try:
                return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout)
            except asyncio.TimeoutError:
                return tool_timeout_result(tool_name, timeout)
            except asyncio.CancelledError:
                return tool_timeout_result(tool_name, timeout)

        _aw.__name__ = getattr(fn, "__name__", "_aw")
        return _aw

    def _sw(*args, **kwargs):
        timeout = _resolve_tool_timeout(kwargs, default_timeout)
        if not timeout:
            return fn(*args, **kwargs)
        future = _TOOL_EXECUTOR.submit(fn, *args, **kwargs)
        try:
            return future.result(timeout=timeout)
        except _FutureTimeout:
            return tool_timeout_result(tool_name, timeout)

    _sw.__name__ = getattr(fn, "__name__", "_sw")
    return _sw


TOOL_RESULT_MAX_CHARS = 12_000
# 落盘完整结果时的文件名前缀
TOOL_RESULT_DUMP_PREFIX = "tool_result_"
# 含密钥的工具结果不得写入 workspace dump
NEVER_DUMP_TOOLS = frozenset({"get_credential"})


def tool_name_of(tool: Any) -> str:
    name = getattr(tool, "name", None)
    if name:
        return str(name)
    return str(tool)


def sort_tools_by_name(tools: list) -> list:
    """稳定排序，避免 MCP 返回顺序抖动破坏 tools 前缀。

    顶层按小写名排序；同名（含大小写不一致）再用原名作二级键兜底，
    保证不同轮次即便输入顺序不同，也得到完全一致的排序。
    """
    return sorted(
        list(tools or []),
        key=lambda t: (tool_name_of(t).lower(), tool_name_of(t)),
    )


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
    if tool_name in NEVER_DUMP_TOOLS:
        dump_dir = None
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
    tool_timeout: float | None = None,
) -> list:
    """包装工具返回值：超长截断 + 单工具超时。"""
    dump_dir = None
    if project_root is not None:
        dump_dir = Path(project_root) / "workspace"

    wrapped: list = []
    for tool in tools or []:
        try:
            wrapped.append(
                _wrap_one_tool(
                    tool,
                    dump_dir=dump_dir,
                    max_chars=max_chars,
                    tool_timeout=tool_timeout,
                )
            )
        except Exception:
            logger.exception("包装工具失败，使用原工具：%s", tool_name_of(tool))
            wrapped.append(tool)
    return wrapped


def _wrap_one_tool(
    tool: Any,
    *,
    dump_dir: Path | None,
    max_chars: int,
    tool_timeout: float | None = None,
) -> Any:
    name = tool_name_of(tool)
    # 往 tools schema 注入可选 `timeout_seconds`，让模型能控制该调用超时；
    # 豁免工具不注入，避免在人机中断工具上误导模型。
    timeout_schema = (
        None
        if name in TOOL_TIMEOUT_EXEMPT
        else _inject_timeout_schema(getattr(tool, "args_schema", None))
    )

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
        aw = _hook(_callable_with_timeout(coro, name, tool_timeout))
        try:
            update = {"coroutine": aw}
            if timeout_schema is not None:
                update["args_schema"] = timeout_schema
            return tool.model_copy(update=update)
        except Exception:
            try:
                object.__setattr__(tool, "coroutine", aw)
                if timeout_schema is not None:
                    object.__setattr__(tool, "args_schema", timeout_schema)
                return tool
            except Exception:
                return tool

    # StructuredTool / BaseTool：优先包 _run / func
    if hasattr(tool, "func") and callable(getattr(tool, "func")):
        orig = tool.func
        newfunc = _hook(_callable_with_timeout(orig, name, tool_timeout))
        update = {"func": newfunc}
        if inspect.iscoroutinefunction(orig):
            # func 为协程时同步替换 coroutine，避免 ainvoke 返回裸协程
            update["coroutine"] = newfunc
        if timeout_schema is not None:
            update["args_schema"] = timeout_schema
        try:
            return tool.model_copy(update=update)
        except Exception:
            try:
                tool.func = newfunc  # type: ignore[attr-defined]
                if inspect.iscoroutinefunction(orig):
                    object.__setattr__(tool, "coroutine", newfunc)
                if timeout_schema is not None:
                    object.__setattr__(tool, "args_schema", timeout_schema)
                return tool
            except Exception:
                return tool

    if hasattr(tool, "_run") and callable(getattr(tool, "_run")):
        if timeout_schema is not None:
            try:
                object.__setattr__(tool, "args_schema", timeout_schema)
            except Exception:
                pass
        orig_run = tool._run
        if inspect.iscoroutinefunction(orig_run):
            # 异步 _run：优先包配套的异步 _arun（ainvoke 实际走它）
            arun = getattr(tool, "_arun", None)
            if inspect.iscoroutinefunction(arun):
                aw = _hook(_callable_with_timeout(arun, name, tool_timeout))
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

        hooked_run = _hook(_callable_with_timeout(orig_run, name, tool_timeout))
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
            aw = _hook(_callable_with_timeout(arun, name, tool_timeout))
            try:
                object.__setattr__(tool, "_arun", aw)
            except Exception:
                try:
                    tool._arun = aw  # type: ignore[method-assign]
                except Exception:
                    pass
        return tool

    return tool
