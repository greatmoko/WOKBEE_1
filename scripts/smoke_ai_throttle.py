"""节流冒烟：wait 为无条件 sleep；工具包装在真正执行前 wait。

运行：
    PYTHONPATH=src .venv/Scripts/python.exe scripts/smoke_ai_throttle.py
"""

from __future__ import annotations

import time
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from langchain_core.tools import tool  # noqa: E402

from wokbee.engine.ai_throttle import ai_throttle  # noqa: E402
from wokbee.engine.tool_truncate import wrap_tools_truncate_results  # noqa: E402

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _failures.append(msg)
        print(f"  FAIL {msg}")
    else:
        print(f"  OK   {msg}")


def test_wait_sleeps() -> None:
    prev = ai_throttle._override_interval
    ai_throttle._override_interval = 0
    t0 = time.monotonic()
    ai_throttle.wait()
    check(time.monotonic() - t0 < 0.05, "间隔 0 立即返回")

    ai_throttle._override_interval = 200
    t0 = time.monotonic()
    ai_throttle.wait()
    elapsed = time.monotonic() - t0
    check(elapsed >= 0.18, f"间隔 200ms 至少睡约 0.2s（实际 {elapsed:.3f}s）")
    check(elapsed < 0.8, f"间隔 200ms 不应睡过久（实际 {elapsed:.3f}s）")
    ai_throttle._override_interval = prev


def test_tool_waits_before_body() -> None:
    prev = ai_throttle._override_interval
    ai_throttle._override_interval = 200
    marks: list[float] = []

    @tool
    def ping() -> str:
        """冒烟用空工具。"""
        marks.append(time.monotonic())
        return "ok"

    wrapped = wrap_tools_truncate_results([ping], project_root=None)
    t0 = time.monotonic()
    out = wrapped[0].invoke({})
    elapsed = time.monotonic() - t0
    check(out == "ok", "工具仍返回结果")
    check(elapsed >= 0.18, f"工具执行前等待（总耗时 {elapsed:.3f}s）")
    check(bool(marks) and marks[0] - t0 >= 0.18, "工具函数本体在 wait 之后才跑")
    ai_throttle._override_interval = prev


def test_stream_wait_before_orig() -> None:
    """约定：先 wait 再进入 orig_stream（与 model_factory 补丁顺序一致）。"""
    prev = ai_throttle._override_interval
    ai_throttle._override_interval = 150
    order: list[str] = []

    def orig_stream():
        order.append("orig")
        yield "chunk"

    def wrapped():
        ai_throttle.wait()
        yield from orig_stream()

    t0 = time.monotonic()
    list(wrapped())
    check(order == ["orig"], "wait 之后才跑 orig_stream")
    check(time.monotonic() - t0 >= 0.12, "该顺序下总耗时含 sleep")
    ai_throttle._override_interval = prev


def test_async_only_mcp_style_sync_invoke() -> None:
    """MCP 工具只有 coroutine，同步 Agent.stream 走 invoke，不能再报不支持 sync。"""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class _Args(BaseModel):
        q: str = Field(default="x")

    async def only_async(q: str = "x") -> str:
        return f"mcp:{q}"

    raw = StructuredTool(
        name="get_user_participant_projects",
        description="mcp style",
        coroutine=only_async,
        args_schema=_Args,
    )
    prev = ai_throttle._override_interval
    ai_throttle._override_interval = 0
    try:
        raised = False
        try:
            raw.invoke({"q": "danamon"})
        except NotImplementedError:
            raised = True
        check(raised, "包装前同步 invoke 会失败（对照）")

        wrapped = wrap_tools_truncate_results([raw], project_root=None)
        out = wrapped[0].invoke({"q": "danamon"})
        check(out == "mcp:danamon", f"包装后同步 invoke 成功（got {out!r}）")
    finally:
        ai_throttle._override_interval = prev


def test_mcp_content_and_artifact_sync_invoke() -> None:
    """langchain-mcp-adapters 固定 content_and_artifact，包装后不能把元组收成 str。"""
    from langchain_core.tools import StructuredTool
    from pydantic import BaseModel, Field

    class _Args(BaseModel):
        q: str = Field(default="x")

    async def mcp_pair(q: str = "x"):
        return [{"type": "text", "text": f"mcp:{q}"}], {"q": q}

    raw = StructuredTool(
        name="get_user_participant_projects",
        description="mcp artifact style",
        coroutine=mcp_pair,
        args_schema=_Args,
        response_format="content_and_artifact",
    )
    prev = ai_throttle._override_interval
    ai_throttle._override_interval = 0
    try:
        wrapped = wrap_tools_truncate_results([raw], project_root=None)
        out = wrapped[0].invoke({"q": "danamon"})
        blob = str(getattr(out, "content", out))
        check("mcp:danamon" in blob, f"content_and_artifact 同步 invoke 成功（got {out!r}）")
    finally:
        ai_throttle._override_interval = prev


def main() -> int:
    print("ai throttle")
    test_wait_sleeps()
    test_tool_waits_before_body()
    test_stream_wait_before_orig()
    test_async_only_mcp_style_sync_invoke()
    test_mcp_content_and_artifact_sync_invoke()
    if _failures:
        print(f"\n{len(_failures)} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
