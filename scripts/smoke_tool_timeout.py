"""离屏冒烟：工具超时支持 AI 逐调用 `timeout_seconds` 覆盖默认。

覆盖点：
- `_callable_with_timeout`：async/sync 两条路径，未传用默认、传了用 per-call、非法回落、
  豁免工具不套超时。
- `_inject_timeout_schema`：给 `@tool` 生成的 StructuredTool 注入 `timeout_seconds` 字段。
- `_wrap_one_tool` / `wrap_tools_truncate_results`：经整条包装链后，工具 schema 含
  `timeout_seconds`，且调用时 per-call 生效。

运行：
    PYTHONPATH=src QT_QPA_PLATFORM=offscreen venv/Scripts/python.exe scripts/smoke_tool_timeout.py
"""

from __future__ import annotations

import asyncio
import time

from langchain_core.tools import tool

from wokbee.engine.tool_truncate import (
    _callable_with_timeout,
    _inject_timeout_schema,
    _resolve_tool_timeout,
    _wrap_one_tool,
    wrap_tools_truncate_results,
)


# ---------- 辅助：用来标记断言失败 ----------
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _failures.append(msg)
        print(f"  ✗ {msg}")
    else:
        print(f"  ✓ {msg}")


async def _a_slow() -> str:
    await asyncio.sleep(3.0)
    return "ok-async"


def _s_slow() -> str:
    time.sleep(3.0)
    return "ok-sync"


def _s_fast() -> str:
    return "ok-fast"


@tool
def slow_tool(delay: int = 1) -> str:
    """一个模拟慢速/下载风格的工具（sleep delay 秒）。"""
    time.sleep(delay)
    return f"done:{delay}"


async def _run_async() -> None:
    print("== async 路径：_callable_with_timeout ==")
    # 未传 timeout_seconds → 用默认 1s，应超时
    slow_wrap = _callable_with_timeout(_a_slow, "download_tool", 1.0)
    out = await slow_wrap()
    check("执行超时" in out, "async 未传 per-call → 用默认 1s 超时")
    check("1 秒" in out and "接管" in out, "async 超时消息含耗时与接管文案")

    # 传 timeout_seconds=5 → 覆盖默认，应正常返回
    slow_wrap2 = _callable_with_timeout(_a_slow, "download_tool", 1.0)
    out2 = await slow_wrap2(timeout_seconds=5)
    check(out2 == "ok-async", "async 传 timeout_seconds=5 → 覆盖默认并正常返回")


def _run_sync() -> None:
    print("== sync 路径：_callable_with_timeout ==")
    # 未传 → 用默认 1s，应超时
    slow_wrap = _callable_with_timeout(_s_slow, "download_tool", 1.0)
    out = slow_wrap()
    check("执行超时" in out, "sync 未传 per-call → 用默认 1s 超时")

    # 传 timeout_seconds=5 → 覆盖默认，正常返回
    slow_wrap2 = _callable_with_timeout(_s_slow, "download_tool", 1.0)
    out2 = slow_wrap2(timeout_seconds=5)
    check(out2 == "ok-sync", "sync 传 timeout_seconds=5 → 覆盖默认并正常返回")

    # 传 timeout_seconds=0 → 显式不限时，直接跑完
    slow_wrap3 = _callable_with_timeout(_s_slow, "download_tool", 1.0)
    out3 = slow_wrap3(timeout_seconds=0)
    check(out3 == "ok-sync", "sync 传 timeout_seconds=0 → 视为不限时")

    # 传非法值 → 回落默认（1s），应超时
    slow_wrap4 = _callable_with_timeout(_s_slow, "download_tool", 1.0)
    out4 = slow_wrap4(timeout_seconds="abc")
    check("执行超时" in out4, "sync 传非法 timeout_seconds → 回落默认 1s 超时")


def _run_exempt_and_resolver() -> None:
    print("== 豁免与 _resolve_tool_timeout ==")
    # 豁免工具：即使传 timeout_seconds 也不套超时（直接返回原函数）
    exempt = _callable_with_timeout(_s_fast, "ask_user", 1.0)
    check(exempt is _s_fast, "豁免工具（ask_user）返回原函数，不套超时")
    check(_s_fast() == "ok-fast", "豁免工具可直接调用")

    # _resolve_tool_timeout 弹出参数且不泄漏进 kwargs
    kwargs = {"query": "x", "timeout_seconds": 60}
    eff = _resolve_tool_timeout(kwargs, 1.0)
    check(eff == 60.0, "_resolve_tool_timeout 读取 per-call=60")
    check("timeout_seconds" not in kwargs, "_resolve_tool_timeout 已弹出 timeout_seconds")
    check(kwargs == {"query": "x"}, "其余 kwargs 原样保留")

    check(_resolve_tool_timeout({}, 1.0) == 1.0, "未传 → 用默认 1.0")
    check(_resolve_tool_timeout({"timeout_seconds": 0}, 1.0) is None, "传 0 → 不限时")
    check(_resolve_tool_timeout({"timeout_seconds": -3}, 1.0) is None, "传负 → 不限时")
    check(_resolve_tool_timeout({}, 0) is None, "全局默认 0 → 禁用超时")


def _run_schema_and_wrap() -> None:
    print("== _inject_timeout_schema 与 _wrap_one_tool ==")
    # 注入进一个真正的 @tool（StructuredTool）
    injected = _inject_timeout_schema(slow_tool.args_schema)
    check(injected is not slow_tool.args_schema, "注入产生新 schema（子类化）")
    check("timeout_seconds" in injected.model_fields, "新 schema 含 timeout_seconds 字段")
    check("delay" in injected.model_fields, "原字段保留（delay）")

    # 幂等：再次注入返回原 schema
    again = _inject_timeout_schema(injected)
    check(again is injected, "重复注入幂等，返回同一 schema")

    # 经 wrap 链后：工具 schema 含 timeout_seconds，且 per-call 生效
    wrapped = _wrap_one_tool(slow_tool, dump_dir=None, max_chars=12000, tool_timeout=1)
    s = getattr(wrapped, "args_schema", None)
    check(s is not None and "timeout_seconds" in getattr(s, "model_fields", {}),
          "wrap 后工具 args_schema 注入 timeout_seconds")

    # 调 wrap_tools_truncate_results 整链（只验证不抛错 + schema 注入）
    tools = wrap_tools_truncate_results(
        [slow_tool], project_root=None, max_chars=12000, tool_timeout=1
    )
    check(len(tools) == 1, "wrap_tools_truncate_results 返回同数量工具")
    ws = getattr(tools[0], "args_schema", None)
    check("timeout_seconds" in getattr(ws, "model_fields", {}), "整链后 schema 含 timeout_seconds")

    # 非模型 schema（如 None / 非 BaseModel）注入应原样返回，不抛错
    check(_inject_timeout_schema(None) is None, "schema 为 None → 原样返回")
    check(_inject_timeout_schema("not-a-model") == "not-a-model", "schema 非 BaseModel → 原样返回")


def main() -> None:
    print(r"=== smoke_tool_timeout ===")
    asyncio.run(_run_async())
    _run_sync()
    _run_exempt_and_resolver()
    _run_schema_and_wrap()
    if _failures:
        print(f"\nFAIL：{len(_failures)} 个断言失败：")
        for f in _failures:
            print(f"  - {f}")
        raise SystemExit(1)
    print("\nPASS：全部通过")


if __name__ == "__main__":
    main()
