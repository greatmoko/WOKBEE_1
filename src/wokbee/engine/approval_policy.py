"""根据 ApprovalFlags 生成 Deep Agents 的 interrupt_on。

说明：带命令执行的 LocalShellBackend 不能同时使用 FilesystemPermission
（Deep Agents 尚未实现 execute 的 tool-level permissions），因此审批只走 interrupt_on。
"""

from __future__ import annotations

from wokbee.core.models import ApprovalFlags


# 内置工具名（Deep Agents 默认）
READ_TOOLS = ("ls", "read_file", "glob", "grep")
# delete 对目录是递归删除，属破坏性写入：并入写分类，随 skip_write 一起受控
WRITE_TOOLS = ("write_file", "edit_file", "delete")
HIGH_RISK_TOOLS = ("execute", "request_access", "get_credential")
# 常规/联网：与 web_search/http_* 一致受控；deepseek_web_search 同属联网检索，不应游离在外
ROUTINE_TOOLS = ("task", "web_search", "http_get", "http_request", "deepseek_web_search")


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
    if tool_name == "delete":
        return "删除"
    if tool_name in WRITE_TOOLS:
        return "写"
    if tool_name in ROUTINE_TOOLS:
        return "常规/联网"
    if tool_name in HIGH_RISK_TOOLS:
        return "高危"
    return "操作"
