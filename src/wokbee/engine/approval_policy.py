"""根据 ApprovalFlags 生成 Deep Agents 的 interrupt_on。

说明：带命令执行的 LocalShellBackend 不能同时使用 FilesystemPermission
（Deep Agents 尚未实现 execute 的 tool-level permissions），因此审批只走 interrupt_on。
"""

from __future__ import annotations

from wokbee.core.models import ApprovalFlags


# 内置工具名（Deep Agents 默认）
READ_TOOLS = ("ls", "read_file", "glob", "grep")
WRITE_TOOLS = ("write_file", "edit_file")
HIGH_RISK_TOOLS = ("execute", "request_access")
ROUTINE_TOOLS = ("task", "web_search", "http_get", "http_request")


def build_interrupt_on(flags: ApprovalFlags) -> dict[str, bool]:
    """勾选免审 → 不中断；未勾选 → 工具调用前 interrupt。"""
    interrupt_on: dict[str, bool] = {}
    if not flags.skip_read:
        for name in READ_TOOLS:
            interrupt_on[name] = True
    if not flags.skip_write:
        for name in WRITE_TOOLS:
            interrupt_on[name] = True
    if not flags.skip_routine:
        for name in ROUTINE_TOOLS:
            interrupt_on[name] = True
    if not flags.skip_high_risk:
        for name in HIGH_RISK_TOOLS:
            interrupt_on[name] = True
    return interrupt_on


def risk_label_for_tool(tool_name: str) -> str:
    if tool_name in READ_TOOLS:
        return "读"
    if tool_name in WRITE_TOOLS:
        return "写"
    if tool_name in ROUTINE_TOOLS:
        return "常规/联网"
    if tool_name in HIGH_RISK_TOOLS:
        return "高危"
    return "操作"
