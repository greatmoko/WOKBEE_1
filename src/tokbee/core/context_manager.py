"""对话上下文管理 — token 估算、预算裁剪、摘要压缩（对齐 Chatbox 思路）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Chatbox: reserve output headroom; threshold = available * compaction_threshold
OUTPUT_RESERVE_TOKENS = 4096
DEFAULT_COMPACTION_THRESHOLD = 0.6
TOKENS_PER_IMAGE = 1600
RETAIN_RECENT_RATIO = 0.4  # 压缩时保留最近约 40% 原文


@dataclass
class ContextUsage:
    used: int
    limit: int
    threshold: int
    message_count: int
    max_message_count: int
    ratio: float  # used / limit，limit<=0 时为 0

    @property
    def percent(self) -> float:
        return self.ratio * 100.0


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    # CJK 感知：中文等全角字符约 1 字 = 1 token，其余仍按英文启发式 (n+3)//4。
    # 旧值把中文按 4 字=1 token 低估约 4 倍，导致压缩/裁剪启动过晚。
    cjk = 0
    others = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF  # CJK 统一表意
            or 0x3400 <= cp <= 0x4DBF  # 扩展 A
            or 0x3000 <= cp <= 0x303F  # CJK 标点
            or 0xFF00 <= cp <= 0xFFEF  # 全角符号/字母
            or 0x3040 <= cp <= 0x30FF  # 日文假名
            or 0xAC00 <= cp <= 0xD7AF  # 韩文
        ):
            cjk += 1
        else:
            others += 1
    return max(1, cjk + (others + 3) // 4)


def estimate_content_tokens(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text_tokens(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url" or "image_url" in part:
                total += TOKENS_PER_IMAGE
            else:
                total += estimate_text_tokens(str(part.get("text") or ""))
        return total
    return estimate_text_tokens(str(content))


def estimate_message_tokens(msg: dict) -> int:
    tokens = estimate_content_tokens(msg.get("content"))
    atts = msg.get("attachments") or []
    for att in atts:
        if isinstance(att, dict):
            path = str(att.get("path") or "")
            name = str(att.get("name") or path)
        else:
            path = str(att)
            name = path
        lower = path.lower()
        if any(lower.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
            tokens += TOKENS_PER_IMAGE
        else:
            tokens += estimate_text_tokens(name) + 32
    # role / formatting overhead
    return tokens + 4


def estimate_messages_tokens(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def available_window(context_window: int, max_output: int | None = None) -> int:
    if context_window <= 0:
        return 0
    reserve = max_output if (max_output and max_output > 0) else OUTPUT_RESERVE_TOKENS
    reserve = min(reserve, max(1, context_window // 2))
    return max(context_window - reserve, context_window // 2)


def threshold_tokens(
    context_window: int,
    compaction_threshold: float = DEFAULT_COMPACTION_THRESHOLD,
    max_output: int | None = None,
) -> int:
    avail = available_window(context_window, max_output)
    if avail <= 0:
        return 0
    thr = compaction_threshold if compaction_threshold > 0 else DEFAULT_COMPACTION_THRESHOLD
    thr = min(max(thr, 0.1), 0.95)
    return max(1, int(avail * thr))


def latest_compaction(points: list[dict] | None) -> dict | None:
    if not points:
        return None
    return points[-1]


def slice_after_compaction(
    messages: list[dict],
    points: list[dict] | None,
) -> tuple[str, list[dict], int]:
    """返回 (summary, active_messages, boundary_index)。

    若 compaction point 带 pin_end（Reasonix 式钉住前缀），则保留
    messages[:pin_end] 不被 boundary 吃掉，避免压缩破坏稳定任务头。
    """
    cp = latest_compaction(points)
    if not cp:
        return "", list(messages), 0
    try:
        boundary = int(cp.get("boundary_index", 0))
    except (TypeError, ValueError):
        boundary = 0
    try:
        pin_end = int(cp.get("pin_end", 0) or 0)
    except (TypeError, ValueError):
        pin_end = 0
    boundary = max(0, min(boundary, len(messages)))
    pin_end = max(0, min(pin_end, len(messages)))
    summary = str(cp.get("summary") or "").strip()
    if pin_end > 0 and pin_end < boundary:
        return summary, list(messages[:pin_end]) + list(messages[boundary:]), boundary
    return summary, list(messages[boundary:]), boundary


def apply_message_count_limit(messages: list[dict], max_count: int) -> list[dict]:
    if max_count <= 0 or max_count >= 10_000_000:
        return list(messages)
    if len(messages) <= max_count:
        return list(messages)
    return list(messages[-max_count:])


def trim_to_token_budget(
    messages: list[dict],
    budget: int,
    *,
    system_tokens: int = 0,
) -> list[dict]:
    """从最旧非 system 消息开始丢弃，直到落入 budget。"""
    if budget <= 0:
        return list(messages)
    msgs = list(messages)
    used = system_tokens + estimate_messages_tokens(msgs)
    while used > budget and msgs:
        # 尽量在 user 边界丢弃一整轮
        drop_to = 1
        if msgs[0].get("role") == "user":
            drop_to = 1
            if len(msgs) > 1 and msgs[1].get("role") == "assistant":
                drop_to = 2
        else:
            drop_to = 1
        for _ in range(min(drop_to, len(msgs))):
            dropped = msgs.pop(0)
            used -= estimate_message_tokens(dropped)
    return msgs


def find_compaction_cut(
    active: list[dict],
    retain_ratio: float = RETAIN_RECENT_RATIO,
    *,
    pin_head: int = 0,
) -> int:
    """返回 active 内的切分下标：[pin_head:cut] 将被摘要，钉住头 + [cut:] 保留。

    pin_head：保留开头若干条（通常为首条任务 user），对齐 Reasonix pinned prefix。
    """
    pin_head = max(0, int(pin_head or 0))
    if len(active) < 4 + pin_head:
        return -1
    retain = max(2, int(len(active) * retain_ratio))
    cut = len(active) - retain
    while cut > pin_head and active[cut].get("role") != "user":
        cut -= 1
    if cut <= pin_head:
        return -1
    return cut


def mechanical_summary(messages: list[dict], previous_summary: str = "") -> str:
    """无模型时的保底摘要。"""
    lines: list[str] = []
    if previous_summary.strip():
        lines.append(f"此前摘要：{previous_summary.strip()[:800]}")
    for m in messages:
        role = m.get("role") or "user"
        content = m.get("content") or ""
        if isinstance(content, list):
            parts = [
                str(p.get("text") or "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = " ".join(parts)
        text = str(content).strip().replace("\n", " ")
        if not text:
            if m.get("attachments"):
                text = "[附件]"
            else:
                continue
        if len(text) > 120:
            text = text[:117] + "..."
        label = {"user": "用户", "assistant": "助手", "system": "系统"}.get(role, role)
        lines.append(f"- {label}: {text}")
    body = "\n".join(lines[:40])
    return (
        "以下是对话较早部分的压缩摘要，请在后续回复中沿用其中的关键事实与约定：\n"
        + body
    )


SUMMARY_SYSTEM_PROMPT = (
    "你是对话摘要助手。请将给定的较早对话压缩为简洁中文摘要，"
    "保留：目标、关键事实、已做决定、未完成事项、用户偏好。"
    "不要复述寒暄；不要编造未出现的信息；控制在 600 字以内。"
)


def build_summary_prompt_messages(
    to_compact: list[dict],
    previous_summary: str = "",
) -> list[dict]:
    chunks: list[str] = []
    if previous_summary.strip():
        chunks.append(f"【上一轮摘要】\n{previous_summary.strip()}")
    for m in to_compact:
        role = m.get("role") or "user"
        content = m.get("content") or ""
        if isinstance(content, list):
            parts = [
                str(p.get("text") or "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = "\n".join(parts)
        text = str(content).strip()
        if not text and m.get("attachments"):
            text = "[含附件]"
        if not text:
            continue
        if len(text) > 2000:
            text = text[:2000] + "…"
        chunks.append(f"{role}: {text}")
    user_body = "\n\n".join(chunks)
    if len(user_body) > 24000:
        user_body = user_body[:24000] + "\n…"
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": user_body or "（无内容）"},
    ]


def append_compaction_point(
    points: list[dict] | None,
    *,
    summary: str,
    boundary_index: int,
    pin_end: int | None = None,
) -> list[dict]:
    out = list(points or [])
    entry: dict = {
        "summary": summary,
        "boundary_index": int(boundary_index),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    if pin_end is not None:
        entry["pin_end"] = int(pin_end)
    elif out:
        prev_pin = out[-1].get("pin_end")
        if prev_pin is not None:
            entry["pin_end"] = int(prev_pin)
    out.append(entry)
    return out


def estimate_session_usage(
    *,
    messages: list[dict],
    compaction_points: list[dict] | None,
    system_prompt: str,
    max_context_message_count: int,
    context_window: int,
    compaction_threshold: float = DEFAULT_COMPACTION_THRESHOLD,
    max_output: int | None = None,
    draft_text: str = "",
    pending_image_count: int = 0,
) -> ContextUsage:
    summary, active, _ = slice_after_compaction(messages, compaction_points)
    active = apply_message_count_limit(active, max_context_message_count)
    used = estimate_text_tokens(system_prompt.strip())
    if summary:
        used += estimate_text_tokens(summary) + 8
    used += estimate_messages_tokens(active)
    if draft_text.strip():
        used += estimate_text_tokens(draft_text)
    used += pending_image_count * TOKENS_PER_IMAGE
    limit = context_window if context_window > 0 else 0
    thr = threshold_tokens(context_window, compaction_threshold, max_output) if limit else 0
    ratio = (used / limit) if limit > 0 else 0.0
    return ContextUsage(
        used=used,
        limit=limit,
        threshold=thr,
        message_count=len(active),
        max_message_count=max_context_message_count,
        ratio=min(ratio, 9.99),
    )


def needs_compaction(
    usage: ContextUsage,
    *,
    auto_compaction: bool = True,
    min_messages: int = 4,
) -> bool:
    if not auto_compaction:
        return False
    if usage.limit <= 0 or usage.threshold <= 0:
        return False
    if usage.message_count < min_messages:
        return False
    return usage.used > usage.threshold


def plan_compaction(
    messages: list[dict],
    points: list[dict] | None,
) -> tuple[list[dict], list[dict], int, str, int] | None:
    """
    规划一次压缩。
    返回 (to_compact, retained, new_boundary_index, previous_summary, pin_end)；无法压缩时 None。

    pin_end：全量 messages 中钉住前缀的结束下标（不含），通常为首条 user 之后。
    """
    summary, active, boundary = slice_after_compaction(messages, points)
    prev = latest_compaction(points)
    pin_end = 0
    if prev:
        try:
            pin_end = int(prev.get("pin_end", 0) or 0)
        except (TypeError, ValueError):
            pin_end = 0
    if pin_end <= 0:
        for i, m in enumerate(messages or []):
            if (m.get("role") or "") == "user":
                pin_end = i + 1
                break

    pin_head = 0
    if pin_end > 0 and boundary == 0:
        pin_head = min(pin_end, len(active))
    elif pin_end > 0 and pin_end < boundary:
        pin_head = min(pin_end, len(active))

    cut = find_compaction_cut(active, pin_head=pin_head)
    if cut < 0:
        return None
    to_compact = active[pin_head:cut]
    retained = active[:pin_head] + active[cut:]
    if not to_compact:
        return None
    if pin_end > 0 and pin_end < boundary:
        new_boundary = boundary + (cut - pin_head)
    else:
        new_boundary = boundary + cut
    return to_compact, retained, new_boundary, summary, pin_end


def build_context_message_dicts(
    *,
    messages: list[dict],
    compaction_points: list[dict] | None,
    system_prompt: str,
    max_context_message_count: int,
    context_window: int,
    max_output: int | None = None,
    exclude_last: bool = False,
) -> tuple[str, list[dict]]:
    """
    构造将发给模型的逻辑消息列表（仍为 session 消息 dict）。
    exclude_last=True 时不包含最后一条（通常由调用方单独附加多模态 user）。
    返回 (summary, history_msgs)。
    """
    working = list(messages[:-1] if exclude_last and messages else messages)
    summary, active, _ = slice_after_compaction(working, compaction_points)
    limit_n = max_context_message_count
    if exclude_last and 0 < limit_n < 10_000_000:
        # 为当前 user 留 1 个名额
        active = [] if limit_n <= 1 else apply_message_count_limit(active, limit_n - 1)
    else:
        active = apply_message_count_limit(active, limit_n)

    sys_tokens = estimate_text_tokens(system_prompt.strip())
    if summary:
        sys_tokens += estimate_text_tokens(summary) + 8
    budget = available_window(context_window, max_output) if context_window > 0 else 0
    if budget > 0:
        headroom = 512 if exclude_last else 0
        active = trim_to_token_budget(active, max(1, budget - headroom), system_tokens=sys_tokens)
    return summary, active


def summary_as_message(summary: str) -> dict:
    return {
        "role": "system",
        "content": "【对话摘要】\n" + summary.strip(),
    }
