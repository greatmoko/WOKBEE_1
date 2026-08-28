"""经验总结：每次总结生成带时间戳的新文档；运行时只加载最新一份。

经验只记录：实现步骤 / 脚本执行顺序 / 运行环境 / 注意事项。
不记录结果、产物或交付内容。
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tokbee.core.safe_io import safe_write_text

from wokbee.core.paths import ensure_project_layout, memory_dir, scripts_dir

EXPERIENCES_SUBDIR = "experiences"
LEGACY_SINGLE = "EXPERIENCE.md"
_EXP_NAME_RE = re.compile(r"^exp_(\d{8}_\d{6}(?:_\d{3})?)(?:_[a-z0-9]+)?\.md$", re.I)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    # 含毫秒，避免同一秒内多次总结互相覆盖/排序错乱
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"\s+", "-", (text or "").strip())
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "", s)
    s = s.strip("-")[:max_len] or "exp"
    return s.lower() if s.isascii() else s


@dataclass
class Lesson:
    """一条经验（Skills 风格 Markdown）；每次总结新建一份带时间戳文件。"""

    id: str = field(default_factory=lambda: f"exp_{uuid.uuid4().hex[:10]}")
    project_id: str = ""
    goal: str = ""
    outcome: str = "unknown"  # success | failed | cancelled | partial
    summary: str = ""  # 流程/方法摘要，非结果
    success_path: str = ""  # 实现步骤
    environment: str = ""
    notes: str = ""
    errors: str = ""
    model: str = ""
    policy: str = ""
    script_section: str = ""
    ai_section: str = ""
    order_section: str = ""
    scripts: list[str] = field(default_factory=list)
    pipeline: str = "scripts/pipeline.json"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    filename: str = ""  # 相对 memory/，如 experiences/exp_....md

    @property
    def description(self) -> str:
        base = (self.summary or self.goal or self.id).replace("\n", " ").strip()
        return f"[{self.outcome}] {base}"[:180]


def render_lesson_md(lesson: Lesson) -> str:
    """渲染经验文档：不含结果/产物章节。"""
    desc = lesson.description.replace('"', "'")
    scripts_yaml = ", ".join(f'"{s}"' for s in lesson.scripts) if lesson.scripts else ""
    display_name = lesson.filename or lesson.id
    lines = [
        "---",
        f"name: {_slug(lesson.goal or 'experience', 32)}",
        f"id: {lesson.id}",
        f'description: "{desc}"',
        f"outcome: {lesson.outcome}",
        f'goal: "{(lesson.goal or "").replace(chr(34), chr(39))[:120]}"',
        f"created_at: {lesson.created_at}",
        f"updated_at: {lesson.updated_at}",
        f"project_id: {lesson.project_id}",
        "automation: hybrid",
        f"pipeline: {lesson.pipeline or 'scripts/pipeline.json'}",
        f"file: {display_name}",
    ]
    if scripts_yaml:
        lines.append(f"scripts: [{scripts_yaml}]")
    lines.extend(
        [
            "---",
            "",
            f"# 项目经验：{lesson.goal or lesson.summary or lesson.id}",
            "",
            f"> 流程记录 · {lesson.outcome} · {lesson.created_at}",
            "",
            "> 本文件只记录**怎么做**（步骤/顺序/环境/注意），不记录运行结果或交付产物。",
            "",
            "## 摘要（方法，非结果）",
            "",
            lesson.summary.strip() or "（无摘要）",
            "",
            "## 成功实现路径",
            "",
            "> 本节只记录**成功**的有序步骤；每步注明「做什么操作 × 达成什么目的」。"
            "失败/试错/被弃用的尝试不写入。",
            "",
            (
                lesson.success_path.strip()
                or "（未记录具体步骤；请在下次运行中补充工具调用与关键决策。）"
            ),
            "",
            "## 执行顺序（脚本 ↔ AI，必须按序）",
            "",
            (
                lesson.order_section.strip()
                or "（暂无；总结经验后会写入有序步骤。）"
            ),
            "",
            "## 可本地脚本步骤（清单）",
            "",
            lesson.script_section.strip() or "（无；详见执行顺序。）",
            "",
            "## 需 AI 完成的步骤（清单）",
            "",
            lesson.ai_section.strip() or "（无；详见执行顺序。）",
            "",
            "## 运行环境",
            "",
            lesson.environment.strip() or "（未记录）",
            "",
            "## 注意事项",
            "",
            lesson.notes.strip() or "（无特殊注意点）",
            "",
            "## 复用建议",
            "",
            "- 再次运行：只加载**最新一份**经验；按「执行顺序」与 `scripts/pipeline.json` 的 steps **有序**执行。",
            "- 顺序在总结时确定，例如：脚本1→脚本2→脚本3→AI1→AI2→脚本4…（不强制 script/AI 一一交错）。",
            "- 可复用脚本落在 `scripts/`；运行输出（callback）落在 `workspace/script_callback_*.md`。",
            "- 后续 AI 必须先读 workspace callback 再写 deliverables；禁止编造。",
            "- 禁止把 archives/ 归档数据当作本轮数据来源。",
            "- `scripts/` 与 `references/` 均不参与归档；经验可多份并存，以最新为准。",
            "- 复跑前先读 `references/MANIFEST.md` 确认第三方代码/登录/环境参数齐全；敏感信息仅供本机使用。",
            "",
        ]
    )
    return "\n".join(lines)


def build_environment_block(
    *,
    model: str = "",
    policy: str = "",
    project_root: str = "",
    extra: str = "",
) -> str:
    """经验总结用运行环境块（与 Agent 会话上下文同源）。"""
    from wokbee.engine.runtime_env import build_runtime_env_block

    return build_runtime_env_block(
        project_root=project_root,
        model=model,
        policy=policy,
        extra=extra,
    )


def collect_scripts_context(project_root: Path, *, max_chars: int = 12000) -> str:
    """收集 scripts/pipeline.json 与脚本源码摘要，供 AI 总结。"""
    root = Path(project_root)
    sdir = scripts_dir(root)
    parts: list[str] = []
    pipe = sdir / "pipeline.json"
    if pipe.exists():
        try:
            parts.append(
                "### scripts/pipeline.json\n```json\n"
                + pipe.read_text(encoding="utf-8")[:4000]
                + "\n```"
            )
        except OSError:
            pass
    if sdir.exists():
        for p in sorted(sdir.glob("*.py"))[:20]:
            try:
                body = p.read_text(encoding="utf-8")
            except OSError:
                continue
            if len(body) > 2500:
                body = body[:2500] + "\n# …(截断)"
            parts.append(f"### scripts/{p.name}\n```python\n{body}\n```")
    text = "\n\n".join(parts) if parts else "（尚无 scripts/ 内容）"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(脚本上下文截断)"
    return text


def collect_events_log(events: list | None, *, max_chars: int = 20000) -> str:
    """把时间线事件压成日志文本（原文拼接，供 refine 等轻量场景）。"""
    lines: list[str] = []
    for ev in events or []:
        kind = getattr(ev, "kind", "") or ""
        content = (getattr(ev, "content", None) or "").strip()
        ts = getattr(ev, "ts", None) or getattr(ev, "created_at", "") or ""
        if not content:
            continue
        if kind in ("tool", "agent", "error", "user", "info", "approval", "lesson"):
            chunk = content if len(content) <= 1200 else content[:1200] + "…"
            prefix = f"[{ts}] " if ts else ""
            lines.append(f"{prefix}{kind}: {chunk}")
    text = "\n".join(lines) if lines else "（无运行日志）"
    if len(text) > max_chars:
        text = "…(日志前部截断)\n" + text[-max_chars:]
    return text


# --------------------------------------------------------------------------- #
# 经验总结用结构化轨迹 Digest（压缩噪声、去重、成功路径前置）
# --------------------------------------------------------------------------- #

_RESULT_DIGEST_CHARS = 200
_AGENT_DIGEST_CHARS = 280
_ERROR_DIGEST_CHARS = 800
_SUCCESS_SECTION_MAX = 8000

_NAV_NOISE_RE = re.compile(
    r"(首页|导航|登录|注册|隐私|关于我们|copyright|cookie|订阅|菜单|"
    r"sitemap|footer|header|navbar|breadcrumb)",
    re.I,
)
_URL_RE = re.compile(r"https?://[^\s\]\"'<>]+", re.I)
_PATH_RE = re.compile(
    r"(?:scripts|workspace|deliverables|uploads|memory|references)/"
    r"[^\s\]\"'<>]+",
    re.I,
)
_MD_CALL_HEAD = re.compile(r"^\*\*call:\*\*\s*`([^`]+)`\s*", re.I)
_MD_CB_HEAD = re.compile(r"^\*\*callback:\*\*\s*`([^`]+)`\s*", re.I)
_ARG_LINE_RE = re.compile(r"^-\s+\*\*([^*]+):\*\*\s*(.*)$")


def _collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _clip(text: str, max_chars: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    return s[: max(0, max_chars - 1)].rstrip() + "…"


def _strip_md_fence(body: str) -> str:
    t = (body or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[^\n]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def _parse_tool_call_content(content: str) -> tuple[str, dict[str, str]]:
    """从时间线 Markdown call 正文解析 tool 名与关键参数。"""
    text = (content or "").strip()
    name = ""
    m = _MD_CALL_HEAD.match(text)
    if m:
        name = m.group(1).strip()
        text = text[m.end() :]
    args: dict[str, str] = {}
    for line in text.splitlines():
        am = _ARG_LINE_RE.match(line.strip())
        if not am:
            continue
        key = am.group(1).strip()
        val = am.group(2).strip()
        if key and val and key not in args:
            args[key] = val
    return name, args


def _parse_tool_callback_content(content: str) -> tuple[str, str]:
    text = (content or "").strip()
    name = ""
    m = _MD_CB_HEAD.match(text)
    if m:
        name = m.group(1).strip()
        text = text[m.end() :]
    return name, _strip_md_fence(text)


def _args_digest(args: dict | None, *, content_preview: int = 80) -> str:
    if not isinstance(args, dict) or not args:
        return ""
    preferred = (
        "url",
        "path",
        "file_path",
        "command",
        "query",
        "method",
        "label",
        "max_chars",
    )
    parts: list[str] = []
    keys = [k for k in preferred if k in args] + [
        k for k in args.keys() if k not in preferred and k not in ("content", "body", "text", "code")
    ]
    for k in keys[:8]:
        v = args.get(k)
        if v is None:
            continue
        if isinstance(v, (dict, list)):
            raw = json.dumps(v, ensure_ascii=False)
        else:
            raw = str(v)
        raw = _collapse_ws(raw)
        if len(raw) > 160:
            raw = raw[:160] + "…"
        parts.append(f"{k}={raw}")
    for heavy in ("content", "body", "text", "code"):
        if heavy in args and args.get(heavy) is not None:
            raw = _collapse_ws(str(args.get(heavy)))
            parts.append(f"{heavy}=({len(raw)}字){_clip(raw, content_preview)}")
            break
    return " | ".join(parts)


def _result_digest(body: str, *, status: str = "") -> str:
    raw = _collapse_ws(_strip_md_fence(body))
    # 去掉导航噪声占主导时，尽量保留后面更有信息量的片段
    if len(raw) > 80 and _NAV_NOISE_RE.search(raw[:120]):
        mid = raw[len(raw) // 4 : len(raw) // 4 + _RESULT_DIGEST_CHARS * 2]
        mid_c = _collapse_ws(mid)
        # 中段若几乎是同一字符（截断填充），仍用去噪后的前部
        if len(mid_c) > 40 and len(set(mid_c[:80])) > 4:
            raw = mid_c
        else:
            raw = _NAV_NOISE_RE.sub(" ", raw)
            raw = _collapse_ws(raw)
    st = (status or "").strip().lower()
    if not st:
        low = raw.lower()
        if any(x in low for x in ("error", "traceback", "失败", "【循环检测】", "blocked")):
            st = "failed"
        elif not raw or raw in ("（无输出）", "(无输出)"):
            st = "empty"
        else:
            st = "ok"
    return f"{st} | len={len(body or '')} | {_clip(raw, _RESULT_DIGEST_CHARS)}"


def _is_successish_status(status: str) -> bool:
    s = (status or "").strip().lower()
    return s in ("", "ok", "success", "succeeded", "done")


def _collect_success_hints(
    tool: str,
    phase: str,
    args: dict | None,
    body: str,
    status: str,
) -> list[str]:
    """从单次工具事件提炼无序检索线索（URL/路径等），非执行顺序。"""
    hints: list[str] = []
    name = (tool or "").strip()
    if not name:
        return hints
    args = args if isinstance(args, dict) else {}

    if phase == "call":
        path = str(args.get("file_path") or args.get("path") or "").strip()
        cmd = str(args.get("command") or "").strip()
        url = str(args.get("url") or "").strip()
        query = str(args.get("query") or "").strip()
        if name in ("write_file", "edit_file") and path:
            if any(
                path.replace("\\", "/").startswith(p)
                for p in ("scripts/", "deliverables/", "workspace/", "references/")
            ):
                hints.append(f"写入 {path}")
        if name == "execute" and cmd:
            if re.search(r"\.(py|bat|cmd|ps1|sh)\b", cmd, re.I) or "scripts/" in cmd.replace(
                "\\", "/"
            ):
                hints.append(f"execute → {_clip(cmd, 120)}")
        if name in ("http_get", "http_request") and url:
            hints.append(f"{name} {url}")
        if name in ("web_search", "deepseek_web_search") and query:
            hints.append(f"{name} q={_clip(query, 80)}")
        return hints

    if phase == "callback" and not _is_successish_status(status):
        return hints

    # callback：从正文抠路径/URL（补充 meta 缺失时）
    for p in _PATH_RE.findall(body or ""):
        if p.lower().startswith(("scripts/", "deliverables/", "references/")):
            hints.append(f"产物/脚本 {p}")
    if name in ("http_get", "http_request", "web_search", "deepseek_web_search"):
        for u in _URL_RE.findall(str(args.get("url") or ""))[:1]:
            hints.append(f"已请求 {u}")
    if "script_callback_" in (body or ""):
        for p in _PATH_RE.findall(body or ""):
            if "script_callback_" in p:
                hints.append(f"callback 落盘 {p}")
    return hints


def _fingerprint_line(kind: str, tool: str, phase: str, args_s: str, result_s: str) -> str:
    return f"{kind}|{tool}|{phase}|{args_s}|{result_s[:120]}"


def build_lesson_digest(events: list | None, *, max_chars: int = 50000) -> str:
    """结构化压缩运行轨迹，供经验总结 AI 使用。

    - 工具 call/result 压成短行；agent 截短；error 尽量保留
    - 连续相同 tool+参数+结果去重为 ×N
    - 时间序「压缩轨迹」在前；无序「关键线索」在后，避免干扰 success_path 顺序
    """
    hint_ordered: list[str] = []
    hint_seen: set[str] = set()
    caution_ordered: list[str] = []
    caution_seen: set[str] = set()
    traj: list[str] = []

    last_fp = ""
    last_idx = -1
    repeat = 0

    def _flush_repeat() -> None:
        nonlocal repeat, last_idx
        if repeat > 0 and 0 <= last_idx < len(traj):
            traj[last_idx] = traj[last_idx] + f"  ×{repeat + 1}"
        repeat = 0

    def _append_traj(line: str, fp: str) -> None:
        nonlocal last_fp, last_idx, repeat
        if fp and fp == last_fp and last_idx >= 0:
            repeat += 1
            return
        _flush_repeat()
        traj.append(line)
        last_fp = fp
        last_idx = len(traj) - 1

    def _add_hint(hints: list[str]) -> None:
        for h in hints:
            key = h.strip()
            if not key or key in hint_seen:
                continue
            hint_seen.add(key)
            hint_ordered.append(key)

    def _add_caution(text: str) -> None:
        key = (text or "").strip()
        if not key or key in caution_seen:
            return
        caution_seen.add(key)
        caution_ordered.append(key)

    for ev in events or []:
        kind = (getattr(ev, "kind", "") or "").strip()
        content = (getattr(ev, "content", None) or "").strip()
        ts = getattr(ev, "ts", None) or getattr(ev, "created_at", "") or ""
        meta = getattr(ev, "meta", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        if not content and kind != "tool":
            continue
        if kind not in ("tool", "agent", "error", "user", "info", "approval", "lesson"):
            continue

        prefix = f"[{ts}] " if ts else ""

        if kind == "tool":
            phase = str(meta.get("phase") or "").lower()
            tool = str(meta.get("tool") or "").strip()
            status = str(meta.get("status") or "").strip()
            args = meta.get("args") if isinstance(meta.get("args"), dict) else {}

            if not phase:
                if content.startswith("**call:**") or content.startswith("call:"):
                    phase = "call"
                elif content.startswith("**callback:**") or content.startswith("callback:"):
                    phase = "callback"

            if phase == "call":
                if not tool:
                    parsed_name, parsed_args = _parse_tool_call_content(content)
                    tool = parsed_name or "tool"
                    if not args:
                        args = parsed_args
                args_s = _args_digest(args if isinstance(args, dict) else {})
                if not args_s:
                    _, parsed_args = _parse_tool_call_content(content)
                    args_s = _args_digest(parsed_args)
                line = f"{prefix}call {tool}" + (f" | {args_s}" if args_s else "")
                fp = _fingerprint_line("tool", tool, "call", args_s, "")
                _append_traj(line, fp)
                _add_hint(
                    _collect_success_hints(
                        tool, "call", args if isinstance(args, dict) else {}, "", status
                    )
                )
            else:
                body = content
                if not tool:
                    tool, body = _parse_tool_callback_content(content)
                    tool = tool or "tool"
                else:
                    _, body = _parse_tool_callback_content(content)
                dig = _result_digest(body, status=status)
                line = f"{prefix}result {tool} | {dig}"
                fp = _fingerprint_line("tool", tool, "callback", "", dig)
                _append_traj(line, fp)
                if "【循环检测】" in body or "循环检测" in body:
                    _add_caution(f"{tool} 触发循环检测（勿再同参重试）")
                else:
                    _add_hint(
                        _collect_success_hints(
                            tool, "callback", args, body, status or "ok"
                        )
                    )
            continue

        if kind == "agent":
            phase = str(meta.get("phase") or "")
            chunk = _clip(_collapse_ws(content), _AGENT_DIGEST_CHARS)
            if not chunk:
                continue
            tag = f"agent/{phase}" if phase else "agent"
            line = f"{prefix}{tag}: {chunk}"
            _append_traj(line, _fingerprint_line("agent", phase, "", chunk, ""))
            continue

        if kind == "error":
            chunk = _clip(content, _ERROR_DIGEST_CHARS)
            line = f"{prefix}error: {chunk}"
            _append_traj(line, _fingerprint_line("error", "", "", chunk, ""))
            if "循环" in content:
                _add_caution(_clip(_collapse_ws(content), 120))
            continue

        # user / info / approval / lesson — 短摘
        cap = 200 if kind in ("info", "approval", "lesson") else 160
        chunk = _clip(_collapse_ws(content), cap)
        if not chunk:
            continue
        if kind == "info" and any(
            x in chunk for x in ("准备经验总结", "正在调用 AI 总结", "cache ")
        ):
            continue
        line = f"{prefix}{kind}: {chunk}"
        _append_traj(line, _fingerprint_line(kind, "", "", chunk, ""))

    _flush_repeat()

    if not traj and not hint_ordered and not caution_ordered:
        return "（无运行日志）"

    traj_body = "\n".join(traj) if traj else "（无工具/过程事件）"
    hint_sec = ""
    if hint_ordered:
        bullets = "\n".join(f"- {h}" for h in hint_ordered[:80])
        hint_sec = (
            "## 关键线索（无序，勿当执行顺序）\n"
            "以下仅为 URL/脚本/产物等检索提示；success_path 必须以时间序「压缩轨迹」为准。\n"
            + bullets
        )
        if len(hint_sec) > _SUCCESS_SECTION_MAX:
            hint_sec = hint_sec[:_SUCCESS_SECTION_MAX] + "\n…(线索截断)"

    caution_sec = ""
    if caution_ordered:
        bullets = "\n".join(f"- {h}" for h in caution_ordered[:40])
        caution_sec = "## 失败教训线索（无序）\n" + bullets

    header = (
        "（结构化压缩轨迹：按时间序排列；文末无序线索仅供检索，"
        "勿当作 success_path 的执行顺序；非原始全文。）"
    )
    parts = [header, "## 压缩轨迹\n" + traj_body]
    if hint_sec:
        parts.append(hint_sec)
    if caution_sec:
        parts.append(caution_sec)
    text = "\n\n".join(parts)

    if len(text) <= max_chars:
        return text

    # 截断：优先保住时间序轨迹；线索仅用剩余预算追加到文末
    fixed = (
        "（结构化压缩轨迹：已按上限截断，优先保留时间序轨迹前部；"
        "文末线索若空间不足可能省略。）\n\n## 压缩轨迹\n"
    )
    tail_extra = ""
    for sec in (hint_sec, caution_sec):
        if sec:
            tail_extra += "\n\n" + sec

    # 先为文末线索预留至多 25% 预算，其余给轨迹
    reserve = min(len(tail_extra), max(0, (max_chars - len(fixed)) // 4)) if tail_extra else 0
    remain_for_traj = max(0, max_chars - len(fixed) - reserve)

    if len(traj_body) <= remain_for_traj:
        traj_out = traj_body
        used = len(fixed) + len(traj_out)
    else:
        head_n = int(remain_for_traj * 0.70)
        mid = "\n…(轨迹中部省略)\n"
        use = remain_for_traj - len(mid)
        head_n = max(0, min(head_n, use))
        tail_n = max(0, use - head_n)
        traj_out = traj_body[:head_n] + mid + traj_body[-tail_n:]
        used = len(fixed) + len(traj_out)

    out = fixed + traj_out
    leftover = max_chars - used
    if leftover > 80 and tail_extra:
        # 线索整体放不下则尽量塞，超限再截
        extra = tail_extra
        if len(extra) > leftover:
            extra = extra[: leftover - 1] + "…"
        out += extra
    return out


_AI_SUMMARY_SYSTEM = """你是 WokBee 的「经验总结」助手。根据「上一份经验 + 本次运行日志 + 现有脚本」总结可复用的流程经验。
「本次运行日志」可能是结构化压缩轨迹：以时间序「压缩轨迹」为主；文末「关键线索」无序且勿当执行顺序。勿假设含网页全文。

硬性要求：
1. 只写「怎么做」：实现步骤、脚本↔AI 执行顺序、运行环境要点、注意事项。
2. 禁止写入：最终结果数值、交付产物内容、报告正文、截图描述、成功产出的具体文案。
3. 不要引用或依赖 archives/ 归档数据。
4. **success_path（成功实现路径）只保留真正成功且有序的步骤**：
   - 以时间序「压缩轨迹」为基准；文末无序线索仅作检索辅助。
   - 但若后续步骤依赖前置结果（需要前置数据/确认才能选对输入），应按**逻辑依赖**顺序排，勿把历史里「先取数、后补前置确认」的脏顺序原样固化。例如「先用 Get-Date 确认当前日期，再选取对应日期的数据」「先读配置，再跑脚本」。
   - 剔除所有失败调用、试错、被弃用/重复的尝试、未采用的方案。
   - 每步编号并写明「**做什么操作 × 达成什么目的**」，例如：
     「用 web_search 查深圳今日天气，以获取实时数据源」「用 execute 跑 .py 清洗数据，以得到干净表格」。
   - order_section / script_section / ai_section 同样按「操作 + 目的」描述，强调有序而非强制交错。
5. 自动化脚本与管线约定（重要）：
   - 可复用本地命令落到项目 `scripts/`；运行输出落到 `workspace/script_callback_*.md`。
   - 你可在 script_files 中手写完整脚本（.py/.bat/.cmd/.ps1/.json/.sh/.js/.vbs）。
   - **pipeline_steps** 决定下次「运行」的真实顺序：按数组从头到尾一路执行。
     允许连续多个 script，也允许连续多个 ai，例如：
     script → script → script → ai → ai → script
     不要理解为必须「脚本、AI」交替。
   - pipeline_steps 里 script 的 path 须指向 scripts/ 下真实文件（与 script_files.filename 或已有脚本一致）。
   - 禁止用 script_files 覆盖 pipeline.json（由系统根据 pipeline_steps 维护）。
6. 用中文。输出必须是一个 JSON 对象（不要 Markdown 围栏），字段如下：
{
  "summary": "方法/流程摘要（一两段，非结果）",
  "success_path": "仅成功步骤的有序清单（编号列表，每步=操作+目的）",
  "order_section": "执行顺序说明（与 pipeline_steps 一致，强调有序而非强制交错）",
  "script_section": "脚本步骤清单（每步=操作+目的）",
  "ai_section": "AI 步骤清单（须注明先读 workspace/script_callback_*.md）",
  "environment": "运行环境要点",
  "notes": "注意事项",
  "used_skills": ["skill-folder-name"],
  "reference_materials": [
    {"path": "references/config.json", "note": "服务端环境参数，复跑需用"}
  ],
  "script_files": [
    {"filename": "query_weather.bat", "content": "@echo off\\n...", "description": "...", "in_pipeline": true}
  ],
  "pipeline_steps": [
    {"type": "script", "path": "scripts/query_weather.bat", "description": "拉取天气"},
    {"type": "script", "path": "scripts/other.py", "description": "清洗数据"},
    {"type": "ai", "description": "提取要点", "prompt_hint": "先读 workspace/script_callback_*.md"},
    {"type": "ai", "description": "写出行建议到 deliverables/"},
    {"type": "script", "path": "scripts/publish.py", "description": "合并交付"}
  ]
}
说明：
- **used_skills**：本次真实调用过的全局 Skill 目录名（如 "web-search"、"pdf-tools"），供快照到 references/skills/。
- **reference_materials**：本次用到的可复用外部材料（第三方代码/登录与密钥配置/环境参数等），需保存进 references/ 并登记；敏感信息仅供本机使用。没有则为空数组。
- 有可复用命令时尽量同时给出 script_files 与 pipeline_steps；没有则可为空数组。
"""


def _extract_json_object(text: str) -> str | None:
    """从可能带围栏/前后叙述的文本里截出第一个平衡的 {…} JSON 对象。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_ai_summary_json(text: str) -> dict | None:
    """宽松解析 AI 总结 JSON：去围栏 → 直接 → 截首个平衡 {…} → 失败返回 None。"""
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    obj = _extract_json_object(t)
    if obj is not None:
        try:
            data = json.loads(obj)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def summarize_lesson_with_ai(
    *,
    model: Any,
    goal: str,
    outcome: str,
    previous_experience: str,
    run_log: str,
    scripts_context: str,
    environment_hint: str = "",
) -> dict[str, Any]:
    """调用模型总结经验；失败时抛出异常由调用方回退。

    返回字段均为 str，另含：
    - script_files: list[dict]（AI 手写脚本，可为空）
    - pipeline_steps: list[dict]（有序管线步骤）
    - used_skills: list[str]（本次用到的 Skill 目录名）
    - reference_materials: list[dict]（需保存进 references/ 的材料）
    生成过程在内部收齐，结束后一次性返回，避免向时间线刷进度气泡。
    """
    user = (
        f"项目目标：{goal or '（未设置）'}\n"
        f"本轮 outcome：{outcome}\n\n"
        f"## 上一份经验（可能为空）\n{previous_experience or '（无）'}\n\n"
        f"## 本次运行日志\n{run_log}\n\n"
        f"## 现有脚本与 pipeline\n{scripts_context}\n\n"
        f"## 环境提示\n{environment_hint or '（无）'}\n\n"
        "请输出符合要求的 JSON。success_path 只保留**成功**的有序步骤（每步=操作+目的），"
        "剔除失败/试错/被弃用尝试。若日志里出现可复用的本地脚本/命令（如 execute 跑 .py/.bat、"
        "Skill 脚本），请在 script_files 写出完整源码，并在 pipeline_steps 给出下次运行的"
        "有序步骤（可连续多个脚本再 AI，勿强制交错）。"
        "若用到了第三方代码/登录/环境参数，请填到 used_skills 与 reference_materials，"
        "供保存到 references/ 供下次稳定复跑。"
    )
    messages = [
        {"role": "system", "content": _AI_SUMMARY_SYSTEM},
        {"role": "user", "content": user},
    ]
    text = ""
    # 优先 stream 仅用于内部拼装；不向外刷进度。失败则 invoke。
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
        resp = model.invoke(messages)
        raw = getattr(resp, "content", None) or str(resp)
        if isinstance(raw, list):
            raw = "\n".join(
                b.get("text", "") if isinstance(b, dict) else str(b) for b in raw
            )
        text = str(raw).strip()

    data = _parse_ai_summary_json(text)
    if data is None:
        raise ValueError("AI 总结未返回有效 JSON（已按宽松解析尝试，仍失败）")
    out: dict[str, Any] = {}
    for key in (
        "summary",
        "success_path",
        "order_section",
        "script_section",
        "ai_section",
        "environment",
        "notes",
    ):
        val = data.get(key)
        out[key] = str(val).strip() if val is not None else ""
    out["script_files"] = _normalize_ai_script_files(data.get("script_files"))
    out["pipeline_steps"] = _normalize_ai_pipeline_steps(data.get("pipeline_steps"))
    out["used_skills"] = _normalize_ai_used_skills(data.get("used_skills"))
    out["reference_materials"] = _normalize_ai_reference_materials(
        data.get("reference_materials")
    )
    return out


def _normalize_ai_script_files(raw: Any) -> list[dict[str, Any]]:
    """校验并规整 AI 返回的 script_files。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        filename = str(
            item.get("filename") or item.get("name") or item.get("path") or ""
        ).strip()
        content = item.get("content")
        if content is None:
            content = item.get("source") or item.get("code") or ""
        content = str(content)
        if not filename or not content.strip():
            continue
        if len(content) > 256_000:
            content = content[:256_000]
        desc = str(item.get("description") or item.get("desc") or "").strip()
        in_pipeline = item.get("in_pipeline")
        if in_pipeline is None:
            in_pipeline = item.get("pipeline", True)
        out.append(
            {
                "filename": filename,
                "content": content,
                "description": desc,
                "in_pipeline": bool(in_pipeline),
            }
        )
    return out


def _normalize_ai_pipeline_steps(raw: Any) -> list[dict[str, Any]]:
    """规整 AI 给出的有序管线步骤。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for i, item in enumerate(raw[:80]):
        if not isinstance(item, dict):
            continue
        t = str(item.get("type") or "").lower().strip()
        if t not in ("script", "ai"):
            continue
        step: dict[str, Any] = {
            "id": str(item.get("id") or f"{t}_{i+1}"),
            "type": t,
            "description": str(item.get("description") or "").strip()[:300],
        }
        if t == "script":
            path = str(item.get("path") or item.get("filename") or "").strip().replace("\\", "/")
            if path and not path.startswith("scripts/"):
                path = f"scripts/{Path(path).name}"
            if not path:
                continue
            step["path"] = path
            step["tool"] = str(item.get("tool") or "ai_authored")
            step["args"] = item.get("args") if isinstance(item.get("args"), dict) else {}
        else:
            step["prompt_hint"] = str(item.get("prompt_hint") or item.get("hint") or "").strip()
            if not step["description"]:
                step["description"] = "AI 步骤"
        out.append(step)
    return out


def _normalize_ai_used_skills(raw: Any) -> list[str]:
    """规整 AI 返回的 used_skills（Skill 目录名列表，去重保序）。"""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw[:50]:
        if isinstance(item, str):
            s = item.strip()
        elif isinstance(item, dict):
            s = str(item.get("name") or "").strip()
        else:
            s = ""
        if s and s not in out:
            out.append(s)
    return out


def _normalize_ai_reference_materials(raw: Any) -> list[dict[str, Any]]:
    """规整 AI 返回的 reference_materials（path + note）。"""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:50]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        note = str(item.get("note") or item.get("desc") or "").strip()[:300]
        if not path and not note:
            continue
        out.append({"path": path, "note": note})
    return out


class LessonStore:
    """`memory/experiences/exp_YYYYMMDD_HHMMSS.md` 多份经验；运行只读最新。"""

    def __init__(self, project_root: Path):
        self.root = Path(project_root)
        ensure_project_layout(self.root)
        self.memory = memory_dir(self.root)
        self.memory.mkdir(parents=True, exist_ok=True)
        self.experiences_dir = self.memory / EXPERIENCES_SUBDIR
        self.experiences_dir.mkdir(parents=True, exist_ok=True)
        self._maybe_migrate_legacy()

    @property
    def experience_path(self) -> Path | None:
        return self.latest_path()

    @property
    def index_path(self) -> Path:
        latest = self.latest_path()
        return latest if latest else self.experiences_dir

    def _maybe_migrate_legacy(self) -> None:
        legacy_single = self.memory / LEGACY_SINGLE
        if legacy_single.exists() and legacy_single.is_file():
            if not any(self.experiences_dir.glob("exp_*.md")):
                dest = self.experiences_dir / f"exp_{_stamp()}_migrated.md"
                try:
                    safe_write_text(dest, legacy_single.read_text(encoding="utf-8"))
                except OSError:
                    pass
            try:
                bak = self.memory / "EXPERIENCE.md.bak"
                if not bak.exists():
                    legacy_single.replace(bak)
            except OSError:
                pass

        old_idx = self.memory / "EXPERIENCES.md"
        if old_idx.exists() and not any(self.experiences_dir.glob("exp_*.md")):
            try:
                safe_write_text(
                    self.experiences_dir / f"exp_{_stamp()}_index.md",
                    old_idx.read_text(encoding="utf-8"),
                )
            except OSError:
                pass

    def list_paths(self) -> list[Path]:
        files = [p for p in self.experiences_dir.glob("exp_*.md") if p.is_file()]

        def sort_key(p: Path):
            m = _EXP_NAME_RE.match(p.name)
            stamp = m.group(1) if m else ""
            try:
                mtime = p.stat().st_mtime
            except OSError:
                mtime = 0.0
            return (stamp, mtime, p.name)

        return sorted(files, key=sort_key, reverse=True)

    def list_recent(self, limit: int = 20) -> list[Path]:
        return self.list_paths()[:limit]

    def latest_path(self) -> Path | None:
        paths = self.list_paths()
        return paths[0] if paths else None

    def is_empty(self) -> bool:
        latest = self.latest_path()
        if not latest:
            return True
        try:
            text = latest.read_text(encoding="utf-8").strip()
        except OSError:
            return True
        if not text:
            return True
        if "（暂无经验" in text and "## 实现步骤" not in text and "## 成功实现路径" not in text:
            return True
        # 至少要有 front matter 或某个核心章节
        if text.startswith("---") or "## 实现步骤" in text or "## 成功实现路径" in text or "## 执行顺序" in text:
            return False
        return len(text) < 80

    def read_latest_text(self, *, max_chars: int = 0) -> str:
        path = self.latest_path()
        if not path:
            return ""
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if max_chars > 0 and len(text) > max_chars:
            return text[:max_chars] + "\n…(截断)"
        return text

    def save(self, lesson: Lesson) -> Path:
        """始终新建带时间戳的经验文件（不覆盖旧文件）。"""
        lesson.created_at = lesson.created_at or _now()
        lesson.updated_at = _now()
        stamp = _stamp()
        fname = f"exp_{stamp}.md"
        path = self.experiences_dir / fname
        if path.exists():
            fname = f"exp_{stamp}_{lesson.id[-4:]}.md"
            path = self.experiences_dir / fname
        lesson.filename = f"{EXPERIENCES_SUBDIR}/{fname}"
        safe_write_text(path, render_lesson_md(lesson))
        return path

    def virtual_memory_paths(self, *, recent: int = 8) -> list[str]:
        paths = ["/memory/AGENTS.md"]
        latest = self.latest_path()
        if latest:
            rel = latest.relative_to(self.memory).as_posix()
            paths.append(f"/memory/{rel}")
        return paths

    def prompt_digest(self, *, limit: int = 5, max_chars: int = 3500) -> str:
        text = self.read_latest_text(max_chars=max_chars)
        if not text:
            return ""
        latest = self.latest_path()
        name = latest.name if latest else "latest"
        return (
            f"【项目经验记忆】以下来自最新经验 `{name}`（历史经验不自动注入；"
            "只关注实现步骤/执行顺序/环境/注意事项，忽略任何结果或产物描述）：\n\n"
            + text
            + "\n\n经验只含**成功路径**：按每步「操作+目的」理解，忽略任何失败/试错细节。\n"
        )

    def rebuild_index(self) -> None:
        self.experiences_dir.mkdir(parents=True, exist_ok=True)

    def open_in_browser(self) -> bool:
        path = self.latest_path()
        if not path:
            return False
        try:
            import webbrowser

            webbrowser.open(path.resolve().as_uri())
            return True
        except OSError:
            return False


def build_success_path_from_timeline_events(
    events: list,
    *,
    limit: int = 40,
) -> tuple[str, str, str]:
    """从时间线事件提炼 (success_path, summary, errors) — 作 AI 回退用。"""
    steps: list[str] = []
    agent_bits: list[str] = []
    errors: list[str] = []
    for ev in events or []:
        kind = getattr(ev, "kind", "") or ""
        content = (getattr(ev, "content", None) or "").strip()
        if not content:
            continue
        if kind == "tool":
            if len(steps) >= limit:
                continue
            line = content
            for prefix in (
                "call: ",
                "callback: ",
                "⟶ 调用工具：",
                "⟵ ",
            ):
                if line.startswith(prefix):
                    line = line[len(prefix) :].strip()
                    break
            if len(line) > 280:
                line = line[:280] + "…"
            if "本地脚本" in line and "成功" in line:
                line = re.split(r"成功[:：]", line, maxsplit=1)[0] + "成功"
            steps.append(f"{len(steps) + 1}. {line}")
        elif kind == "agent":
            agent_bits.append(content[:400])
        elif kind == "error":
            errors.append(content[:400])
        elif kind == "info" and ("失败" in content or "取消" in content):
            errors.append(content[:400])

    success_path = "\n".join(steps) if steps else ""
    summary = ""
    if steps:
        summary = f"本轮记录了 {len(steps)} 个工具/脚本相关流程步骤（不含结果正文）。"
    elif agent_bits:
        summary = "本轮以 Agent 过程说明为主，详见注意事项与实现步骤。"
    err_text = "\n".join(errors[-5:]) if errors else ""
    return success_path, summary, err_text
