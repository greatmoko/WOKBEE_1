"""DeepSeek 缓存命中观测（CacheHitTracker）与前缀护栏（PrefixGuard）。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable


logger = logging.getLogger("wokbee")


def extract_cache_tokens(msg: Any) -> tuple[int, int]:
    """从 AIMessage 解析 (hit, miss)；无数据则 (0, 0)。"""
    hit = 0
    miss = 0

    um = getattr(msg, "usage_metadata", None)
    if isinstance(um, dict):
        details = um.get("input_token_details") or {}
        if isinstance(details, dict):
            hit = int(details.get("cache_read") or details.get("cache_read_tokens") or 0)
        inp = int(um.get("input_tokens") or 0)
        if inp and hit:
            miss = max(0, inp - hit)
        elif inp and not hit:
            miss = inp

    rm = getattr(msg, "response_metadata", None) or {}
    if not isinstance(rm, dict):
        rm = {}
    usage = rm.get("token_usage") or rm.get("usage") or {}
    if isinstance(usage, dict):
        if not hit:
            hit = int(
                usage.get("prompt_cache_hit_tokens")
                or usage.get("cache_hit_tokens")
                or 0
            )
        ds_miss = usage.get("prompt_cache_miss_tokens")
        if ds_miss is not None:
            miss = int(ds_miss)
        elif not miss:
            prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            if prompt:
                miss = max(0, prompt - hit)

    if isinstance(msg, dict):
        usage = msg.get("usage") or {}
        if isinstance(usage, dict):
            if not hit:
                hit = int(usage.get("prompt_cache_hit_tokens") or 0)
            if usage.get("prompt_cache_miss_tokens") is not None:
                miss = int(usage["prompt_cache_miss_tokens"])

    return max(0, hit), max(0, miss)


@dataclass
class CacheHitTracker:
    """会话级累计 hit/miss（Reasonix：本轮% + 会话 avg%）。"""

    hit_total: int = 0
    miss_total: int = 0
    last_hit: int = 0
    last_miss: int = 0
    turns: int = 0
    prefix_fp: str = ""
    on_update: Callable[[dict], None] | None = field(default=None, repr=False)

    def note_prefix(self, fp: str, tool_count: int) -> None:
        self.prefix_fp = fp
        self._emit_update(extra={"prefix_fp": fp, "tool_count": tool_count, "phase": "pin"})

    def observe_message(self, msg: Any) -> dict | None:
        hit, miss = extract_cache_tokens(msg)
        if hit <= 0 and miss <= 0:
            return None
        self.last_hit = hit
        self.last_miss = miss
        self.hit_total += hit
        self.miss_total += miss
        self.turns += 1
        payload = self.as_dict()
        self._emit_update(extra={"phase": "turn"})
        return payload

    def as_dict(self) -> dict:
        last_den = self.last_hit + self.last_miss
        sess_den = self.hit_total + self.miss_total
        return {
            "last_hit": self.last_hit,
            "last_miss": self.last_miss,
            "hit_total": self.hit_total,
            "miss_total": self.miss_total,
            "turns": self.turns,
            "now_pct": round(100.0 * self.last_hit / last_den) if last_den else None,
            "avg_pct": round(100.0 * self.hit_total / sess_den) if sess_den else None,
            "prefix_fp": self.prefix_fp,
        }

    def format_tag(self) -> str:
        d = self.as_dict()
        now_s = f"{d['now_pct']}%" if d["now_pct"] is not None else "—"
        avg_s = f"{d['avg_pct']}%" if d["avg_pct"] is not None else "—"
        return f"cache {now_s} · avg {avg_s}"

    def _emit_update(self, *, extra: dict | None = None) -> None:
        if not self.on_update:
            return
        payload = self.as_dict()
        if extra:
            payload.update(extra)
        try:
            self.on_update(payload)
        except Exception:
            logger.exception("cache tracker on_update 失败")


# --------------------------------------------------------------------------- #
# 前缀护栏（Prefix Guard）：Reasonix 式 append-only 检测
# --------------------------------------------------------------------------- #
# DeepSeek 自动前缀缓存不认 cache_control，只认「本请求前缀与上一请求逐字节一致」。
# 命中率低的根源几乎都是「静态前缀或消息历史被改写」，而非没开缓存。
# 本护栏在每次模型轮次结束后，对整条消息历史做 stable-JSON + SHA-256 逐下标哈希，
# 只在「旧前缀被改写」时告警（正常 append 不告警），并定位到具体消息。
# 对齐社区 pi-deepseek-cache 的 P2 前缀护栏：append-only、只在改写时告警。


def _message_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or msg.get("type") or "")
    return str(getattr(msg, "type", None) or "")


def _message_content_text(content: Any) -> str:
    """把 str / list[block] / dict 统一成纯文本，稳定参与哈希。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type")
                if t == "text":
                    parts.append(str(block.get("text") or ""))
                elif t == "image":
                    parts.append("[image]")
                elif isinstance(block.get("content"), (str, list)):
                    parts.append(_message_content_text(block.get("content")))
                else:
                    parts.append(str(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if isinstance(content, dict):
        return _message_content_text(content.get("content") or content)
    return str(content)


def _message_fingerprint(msg: Any) -> str:
    """单条消息 → stable-JSON 哈希。只取会上行模型的关键字段，不含 metadata。"""
    if isinstance(msg, dict):
        role = _message_role(msg)
        payload = {
            "role": role,
            "content": _message_content_text(msg.get("content")),
        }
        if msg.get("name"):
            payload["name"] = str(msg["name"])
        if msg.get("tool_call_id"):
            payload["tool_call_id"] = str(msg["tool_call_id"])
        tcs = msg.get("tool_calls")
        if tcs:
            normalized = []
            for tc in tcs:
                if hasattr(tc, "get"):
                    function = tc.get("function") or {}
                    normalized.append(
                        {
                            "id": str(tc.get("id") or ""),
                            "name": str(tc.get("name") or function.get("name") or ""),
                            "args": _stable_json(tc.get("args") or function.get("arguments") or function),
                        }
                    )
                else:
                    normalized.append(
                        {
                            "id": str(getattr(tc, "id", "") or ""),
                            "name": str(getattr(tc, "name", "") or ""),
                            "args": _stable_json(getattr(tc, "args", None) or {}),
                        }
                    )
            payload["tool_calls"] = normalized
    else:
        payload = {
            "role": _message_role(msg),
            "content": _message_content_text(getattr(msg, "content", None)),
        }
        name = getattr(msg, "name", None)
        if name:
            payload["name"] = str(name)
        tcid = getattr(msg, "tool_call_id", None)
        if tcid:
            payload["tool_call_id"] = str(tcid)
        tcs = getattr(msg, "tool_calls", None) or getattr(msg, "additional_kwargs", {}).get("tool_calls")
        if tcs:
            normalized = []
            for tc in tcs:
                if hasattr(tc, "get"):
                    function = tc.get("function") or {}
                    normalized.append(
                        {
                            "id": str(tc.get("id") or ""),
                            "name": str(tc.get("name") or function.get("name") or ""),
                            "args": _stable_json(tc.get("args") or function.get("arguments") or {}),
                        }
                    )
                else:
                    normalized.append({"id": str(getattr(tc, "id", "") or ""), "name": str(getattr(tc, "name", "") or "")})
            payload["tool_calls"] = normalized
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _stable_json(obj: Any) -> Any:
    """尽力把 args/function 归一化成可稳定序列化的 JSON 值。"""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_stable_json(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _stable_json(v) for k, v in obj.items()}
    return str(obj)


@dataclass
class PrefixGuard:
    """会话级前缀护栏：检测消息历史 append-only 是否被破坏。

    复用 Reasonix 的「turn-end cap + 只追加」不变量：正常往尾部追加新消息
    不会触发告警；一旦某个已出现下标的前缀哈希改变（改写/重排/压缩/就地修改），
    即上报改写点，用于归因命中率骤降。
    """

    _seen: dict[int, str] = field(default_factory=dict)  # index -> 前缀哈希
    _count: int = 0
    _fprs: list[str] = field(default_factory=list, repr=False)  # index -> 指纹（缓存）
    static_fp: str = ""
    on_drift: Callable[[dict], None] | None = field(default=None, repr=False)

    def note_static(self, fp: str, tool_count: int) -> None:
        # 只存静态指纹；钉死/前缀信息由 CacheHitTracker 的 pin 事件展示，
        # 不把「无 drift」的载荷送进 on_drift，避免误报改写。
        self.static_fp = fp

    def check(self, messages: list) -> dict:
        """对整条消息历史做 append-only 校验（增量）。

        返回 {"ok": bool, "drift": dict|None, "count": int}；发现改写时先上报再返回。
        纯追加（数量增加）时复用缓存指纹、只重算新增下标，避免每轮对整条历史全量
        重哈希（会话越长开销越大的问题）；数量未变（可能就地改写）或缩短时全量重算以
        检测 drift。
        """
        if not messages:
            return {"ok": True, "drift": None, "count": self._count}

        n = len(messages)
        if n > self._count and len(self._fprs) == self._count:
            # 纯追加：复用旧段指纹，只算新增尾部（sha256 逐 index 重算仍便宜，
            # 唯一变贵的是 _message_fingerprint，已通过缓存规避）
            fprs = self._fprs + [_message_fingerprint(m) for m in messages[self._count:]]
        else:
            # 数量未变（可能改写）或缩短：全量重算以检测 rewrite/truncated
            fprs = [_message_fingerprint(m) for m in messages]

        hasher = hashlib.sha256()
        current: dict[int, str] = {}
        drift: dict | None = None

        for i, fp in enumerate(fprs):
            hasher.update(fp.encode("utf-8"))
            current[i] = hasher.hexdigest()[:20]
            if drift is None and i in self._seen and self._seen[i] != current[i]:
                drift = {
                    "index": i,
                    "role": _message_role(messages[i]),
                    "content": _message_content_text(
                        messages[i].get("content") if isinstance(messages[i], dict)
                        else getattr(messages[i], "content", "")
                    )[:160],
                    "kind": "rewrite",
                }

        # 历史缩短：原地删/切中间消息也算破坏前缀
        if drift is None and messages and len(fprs) < self._count:
            drift = {
                "index": len(fprs),
                "role": "eof",
                "content": f"消息数由 {self._count} 缩为 {len(fprs)}",
                "kind": "truncated",
            }

        self._seen = current
        self._count = len(fprs)
        self._fprs = fprs

        result = {"ok": drift is None, "drift": drift, "count": len(fprs)}
        if drift is not None:
            self._report_extra({"phase": "drift", **result})
        return result

    def _report_extra(self, extra: dict) -> None:
        if not self.on_drift:
            return
        try:
            self.on_drift(extra)
        except Exception:
            logger.exception("prefix guard on_drift 失败")
