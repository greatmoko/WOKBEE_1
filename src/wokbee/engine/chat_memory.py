"""对话记忆：交互模式结束后自动总结追加到单文件，供 AI 按需注入。

- 单文件 `memory/chat_memory.md`，追加式，永不归档（archive_session 本就跳过 memory/）。
- 记忆**不主动加载**；AI 通过 `load_conversation_memory` 工具按需注入，默认最近 2 轮。
- 单条总结硬性 ≤500 字；中途失败不记录。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from tokbee.core.safe_io import safe_write_text

from wokbee.core.paths import memory_dir

CHAT_MEMORY_FILE = "chat_memory.md"
_ENTRY_MARKER = "<!-- ===CHAT_MEMORY=== -->"
_MAX_SUMMARY_CHARS = 500
_MAX_READ_ROUNDS = 50


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def chat_memory_path(project_root: Path) -> Path:
    return memory_dir(project_root) / CHAT_MEMORY_FILE


def _clip(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max(0, max_chars - 1)].rstrip() + "…"


def render_chat_entry(entry: dict) -> str:
    """把一条记忆条目渲染为追加文本（体控制在 ≤500 字）。"""
    ts = (entry.get("time") or "").strip() or _now()
    intent = _clip(entry.get("intent"), 160)
    process = _clip(entry.get("process"), 300)
    problems = _clip(entry.get("problems_solutions"), 300)
    keywords = _clip(entry.get("keywords"), 120)
    content = (
        f"## 对话 · {ts}\n\n"
        f"- 时间：{ts}\n"
        f"- 用户意图：{intent or '（无）'}\n"
        f"- 实现过程：{process or '（无）'}\n"
        f"- 问题与解决方案：{problems or '（无）'}\n"
        f"- 关键字：{keywords or '（无）'}"
    )
    # 「单次对话总结 ≤500 字」：对整个条目内容（含标题，不含分隔标记）硬性截断
    if len(content) > _MAX_SUMMARY_CHARS:
        content = _clip(content, _MAX_SUMMARY_CHARS)
    return f"{_ENTRY_MARKER}\n{content}\n"


def append_chat_memory(project_root: Path, entry: dict) -> Path:
    """把一条对话记忆追加到单文件（追加式，不覆盖历史）。"""
    root = Path(project_root)
    memory_dir(root).mkdir(parents=True, exist_ok=True)
    path = chat_memory_path(root)
    block = render_chat_entry(entry or {})
    prev = ""
    if path.exists():
        try:
            prev = path.read_text(encoding="utf-8")
        except OSError:
            prev = ""
    if prev and not prev.endswith("\n"):
        prev += "\n"
    safe_write_text(path, prev + "\n" + block)
    return path


def read_recent_chat_memory(
    project_root: Path,
    rounds: int = 2,
    *,
    max_chars: int = 6000,
) -> str:
    """读取最近 N 轮对话记忆（默认 2），渲染成文本供注入。"""
    path = chat_memory_path(project_root)
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    chunks = [c.strip() for c in text.split(_ENTRY_MARKER) if c.strip()]
    if not chunks:
        return ""
    n = max(1, int(rounds or 2))
    if n > _MAX_READ_ROUNDS:
        n = _MAX_READ_ROUNDS
    recent = chunks[-n:]
    joined = "\n\n".join(recent)
    if len(joined) > max_chars:
        joined = "…(前部省略)\n" + joined[-max_chars:]
    return joined


_AI_CHAT_MEMORY_SYSTEM = """你是 WokBee 的「对话记忆」助手。根据一次交互（对话）的过程记录，提炼一条精炼的对话记忆。

硬性要求：
1. 只输出一个 JSON 对象（不要 Markdown 围栏），字段：
   {
     "intent": "用户的意图（一两句）",
     "process": "AI Agent 实现过程的概述（做了什么、怎么做的）",
     "problems_solutions": "遇到的问题与解决方案（无则填“无”）",
     "keywords": "本轮交互核心关键字词，用顿号分隔，便于索引"
   }
2. 全篇不超过 500 字，简洁客观。
3. 只记录「发生了什么 / 怎么做的」，不要记录需要保密的具体密钥。
"""


def summarize_chat_with_ai(
    *,
    model: Any,
    goal: str,
    question: str,
    run_log: str,
) -> dict | None:
    """调用模型提炼一条对话记忆；失败返回 None（调用方据此不记录）。"""
    user = (
        f"项目目标：{goal or '（未设置）'}\n"
        f"本轮用户提问：{question or '（无）'}\n\n"
        f"## 本次交互过程记录\n{run_log or '（无）'}\n\n"
        "请输出符合要求的 JSON。"
    )
    messages = [
        {"role": "system", "content": _AI_CHAT_MEMORY_SYSTEM},
        {"role": "user", "content": user},
    ]
    text = ""
    try:
        parts: list[str] = []
        for chunk in model.stream(messages):
            piece = getattr(chunk, "content", None)
            if piece is None:
                continue
            if isinstance(piece, list):
                piece = "".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in piece
                )
            piece = str(piece)
            if piece:
                parts.append(piece)
        text = "".join(parts).strip()
    except Exception:
        text = ""
    if not text:
        try:
            resp = model.invoke(messages)
            raw = getattr(resp, "content", None) or str(resp)
            if isinstance(raw, list):
                raw = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in raw
                )
            text = str(raw).strip()
        except Exception:
            return None

    from wokbee.engine.lessons import _parse_ai_summary_json

    data = _parse_ai_summary_json(text)
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {
        "time": _now(),
        "intent": str(data.get("intent") or "").strip(),
        "process": str(data.get("process") or "").strip(),
        "problems_solutions": str(data.get("problems_solutions") or "").strip(),
        "keywords": str(data.get("keywords") or "").strip(),
    }
    if not any(out.get(k) for k in ("intent", "process", "keywords")):
        return None
    return out


def build_chat_memory_tools(
    *,
    project_root: Path,
    emit=None,
) -> list[Any]:
    """构造对话记忆注入工具（仅读取，无副作用；AI 按需调用）。"""
    from langchain_core.tools import tool

    root = Path(project_root)

    @tool
    def load_conversation_memory(rounds: int = 2) -> str:
        """读取最近 N 轮对话记忆（默认最近 2 轮；可传更大值注入更多历史以回忆更早对话）。

        对话记忆不会自动加载到上下文；当你需要回忆过往交互（用户之前的意图、做法、
        踩过的坑与解法）时调用本工具。
        """
        return read_recent_chat_memory(root, rounds=rounds)

    return [load_conversation_memory]
