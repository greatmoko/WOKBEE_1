"""工具循环检测：相同参数重复调用且结果不变时短路，避免 Agent 空转。"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
from typing import Any

from wokbee.engine.cache_prefix import tool_name_of

logger = logging.getLogger("wokbee")

_LOOP_MSG = (
    "【循环检测】已用相同参数多次调用 `{tool}`，返回内容无明显变化。"
    "请停止重复请求：换其他 URL/关键词、说明目标数据可能尚未发布、"
    "或使用 ask_user 向用户确认是否更换日期或策略。"
)

# 截断态不代表结果真的相同（大型文件/页面每次都被截到同一前缀），不算循环
_TRUNC_MARKERS = ("…（已截断", "Output truncated at", "已截断")


def _call_key(name: str, args: tuple, kwargs: dict) -> str:
    payload: Any
    if kwargs:
        payload = kwargs
    elif len(args) == 1:
        payload = args[0]
    else:
        payload = {"args": args}
    try:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        blob = str(payload)
    return f"{name}:{blob}"


def _result_fp(result: Any) -> str | None:
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(result)
    # 截断态（被 wrap_tools_truncate_results 裁过的长结果）每次都是同一前缀，
    # 无法证明「真实结果无变化」，禁止据此判循环，免得大型文件重读被卡死。
    if any(m in text for m in _TRUNC_MARKERS):
        return None
    return hashlib.sha256(text[:4000].encode("utf-8")).hexdigest()[:16]


class ToolLoopGuard:
    """单次 Agent 运行内的工具重复调用检测。"""

    def __init__(self, *, max_identical: int = 3):
        # 连续相同结果达到 max_identical 次后，下一次同参调用直接拒绝
        self.max_identical = max(1, int(max_identical))
        self._history: dict[str, list[str]] = {}

    def preflight(self, name: str, args: tuple, kwargs: dict) -> str | None:
        key = _call_key(name, args, kwargs)
        fps = [f for f in self._history.get(key, []) if f]
        if len(fps) >= self.max_identical:
            tail = fps[-self.max_identical :]
            if len(set(tail)) == 1:
                return _LOOP_MSG.format(tool=name)
        return None

    def record(self, name: str, args: tuple, kwargs: dict, result: Any) -> None:
        key = _call_key(name, args, kwargs)
        fp = _result_fp(result)
        if fp is None:
            return
        hist = self._history.setdefault(key, [])
        hist.append(fp)
        if len(hist) > self.max_identical + 2:
            del hist[: -self.max_identical - 2]


def wrap_tools_loop_guard(tools: list, *, guard: ToolLoopGuard | None = None) -> list:
    """包装工具：检测同参重复且结果不变的循环调用。"""
    g = guard or ToolLoopGuard()
    wrapped: list = []
    for tool in tools or []:
        try:
            wrapped.append(_wrap_one_tool_loop(tool, g))
        except Exception:
            logger.exception("循环检测包装失败，使用原工具：%s", tool_name_of(tool))
            wrapped.append(tool)
    return wrapped


def _wrap_one_tool_loop(tool: Any, guard: ToolLoopGuard) -> Any:
    name = tool_name_of(tool)

    def _preflight(args: tuple, kwargs: dict) -> str | None:
        return guard.preflight(name, args, kwargs)

    def _record(args: tuple, kwargs: dict, result: Any) -> Any:
        guard.record(name, args, kwargs, result)
        return result

    def _hook(fn: Any) -> Any:
        """包装（可能异步的）原函数；异步函数保持异步，否则 ainvoke 会取回裸协程。"""
        if inspect.iscoroutinefunction(fn):
            async def _ahooked(*args, **kwargs):
                blocked = _preflight(args, kwargs)
                if blocked:
                    return blocked
                return _record(args, kwargs, await fn(*args, **kwargs))
            return _ahooked

        def _hooked(*args, **kwargs):
            blocked = _preflight(args, kwargs)
            if blocked:
                return blocked
            return _record(args, kwargs, fn(*args, **kwargs))
        return _hooked

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

    if hasattr(tool, "func") and callable(getattr(tool, "func")):
        orig = tool.func
        hooked = _hook(orig)
        update = {"func": hooked}
        if inspect.iscoroutinefunction(orig):
            update["coroutine"] = hooked
        try:
            return tool.model_copy(update=update)
        except Exception:
            try:
                tool.func = hooked  # type: ignore[attr-defined]
                if inspect.iscoroutinefunction(orig):
                    object.__setattr__(tool, "coroutine", hooked)
                return tool
            except Exception:
                return tool

    if hasattr(tool, "_run") and callable(getattr(tool, "_run")):
        orig_run = tool._run
        if inspect.iscoroutinefunction(orig_run):
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
                    blocked = _preflight(args, kwargs)
                    if blocked:
                        return blocked
                    return _record(args, kwargs, orig_arun(*args, **kwargs))

                try:
                    object.__setattr__(tool, "_arun", _ahooked_run)
                    return tool
                except Exception:
                    pass
            return tool

        def hooked_run(*args, **kwargs):
            blocked = _preflight(args, kwargs)
            if blocked:
                return blocked
            return _record(args, kwargs, orig_run(*args, **kwargs))

        try:
            object.__setattr__(tool, "_run", hooked_run)
        except Exception:
            try:
                tool._run = hooked_run  # type: ignore[method-assign]
            except Exception:
                return tool
        return tool

    return tool
