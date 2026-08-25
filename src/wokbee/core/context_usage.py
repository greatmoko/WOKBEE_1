"""WokBee 上下文用量：复用 TokBee context_manager，基于项目时间线估算。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tokbee.core import context_manager as ctxman
from tokbee.core.safe_io import safe_write_json

CONTEXT_STATE_REL = "memory/context_state.json"


def events_as_messages(events: list[Any]) -> list[dict]:
    """把 ProjectEvent 转成 context_manager 可用的 messages。"""
    out: list[dict] = []
    for ev in events or []:
        kind = getattr(ev, "kind", "") or ""
        content = (getattr(ev, "content", None) or "").strip()
        if not content:
            continue
        if kind == "user":
            role = "user"
        elif kind in ("agent", "tool", "info", "lesson", "approval"):
            role = "assistant"
        elif kind == "error":
            role = "assistant"
        else:
            continue
        out.append({"role": role, "content": content, "kind": kind})
    return out


def context_state_path(project_root: Path) -> Path:
    return Path(project_root) / CONTEXT_STATE_REL


def load_context_state(project_root: Path) -> dict:
    path = context_state_path(project_root)
    if not path.exists():
        return {"compaction_points": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"compaction_points": []}
        pts = data.get("compaction_points")
        if not isinstance(pts, list):
            data["compaction_points"] = []
        return data
    except (OSError, json.JSONDecodeError):
        return {"compaction_points": []}


def save_context_state(project_root: Path, state: dict) -> None:
    path = context_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_write_json(path, state)


def estimate_project_usage(
    *,
    events: list[Any],
    project_root: Path,
    system_prompt: str = "",
    context_window: int = 0,
    draft_text: str = "",
    max_context_message_count: int = 80,
) -> ctxman.ContextUsage:
    messages = events_as_messages(events)
    state = load_context_state(project_root)
    return ctxman.estimate_session_usage(
        messages=messages,
        compaction_points=state.get("compaction_points") or [],
        system_prompt=system_prompt
        or "你是 WokBee，具备完整本机文件与联网能力的工作助手。",
        max_context_message_count=max_context_message_count,
        context_window=int(context_window or 0),
        draft_text=draft_text or "",
    )


def plan_project_compaction(events: list[Any], project_root: Path):
    messages = events_as_messages(events)
    state = load_context_state(project_root)
    return ctxman.plan_compaction(messages, state.get("compaction_points") or [])
