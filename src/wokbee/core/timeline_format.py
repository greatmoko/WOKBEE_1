"""时间线工具调用的纯文本格式化。

这两个函数原本与格式器一同定义在 engine/runner.py（重度模块），但实际只用标准库
（json / str），并不需要 deepagents 栈。把它们抽成轻量叶子模块，是为了让时间线 UI
渲染工具行时**不**把整个引擎拖进 UI 线程（否则首次打开 WokBee tab 会卡住，违背
「不能分模块/按需加载把页面搞卡」的约束）。引擎侧（runner.py）复用同一实现。
"""

from __future__ import annotations

import json
from typing import Any


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
        lines.append(f"- …还有 {len(args) - 12} 个参数")
    return "\n".join(lines)


def format_tool_callback_for_timeline(name: str, body: str) -> str:
    """时间线展示：工具回调的多行 Markdown（固定宽高，超长截断）。"""
    name = (name or "tool").strip() or "tool"
    text = (body or "").strip()
    if len(text) > 2000:
        text = text[:2000].rstrip() + f"\n…（已截断，共约 {len(body or '')} 字）"
    if not text:
        return f"**callback:** `{name}`\n\n（无输出）"
    return f"**callback:** `{name}`\n\n```\n{text}\n```"


__all__ = ["format_tool_call_for_timeline", "format_tool_callback_for_timeline", "_format_arg_preview"]
