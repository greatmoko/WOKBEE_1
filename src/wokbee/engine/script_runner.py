"""本地执行经验固化脚本，并支持按 pipeline steps 有序推进（script/ai 任意组合）。"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from wokbee.core.paths import scripts_dir

logger = logging.getLogger("wokbee")

EventFn = Callable[[str, str, dict], None]


@dataclass
class ScriptRunItem:
    path: str
    ok: bool
    output: str = ""
    error: str = ""
    description: str = ""
    step_id: str = ""


@dataclass
class PhaseResult:
    """一个连续阶段（若干同类型步骤）的执行结果。"""

    type: str  # script | ai
    ok: bool = True
    items: list[ScriptRunItem] = field(default_factory=list)
    ai_steps: list[dict] = field(default_factory=list)
    output: str = ""
    error: str = ""
    index: int = 0


@dataclass
class PipelineRunResult:
    """整条有序管线的状态（可能只跑到第一个 AI 阶段前）。"""

    ran: bool = False
    ok: bool = False
    items: list[ScriptRunItem] = field(default_factory=list)
    ai_steps: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    phases: list[dict] = field(default_factory=list)
    combined_output: str = ""
    error_summary: str = ""
    pipeline_path: Path | None = None
    need_ai: bool = True
    reason: str = ""
    # 交错执行：当前停在第几个 phase（0-based），后续由 runner 继续
    # （phase = 连续同 type 步骤合并后的阶段；整体仍是有序一路执行）
    next_phase_index: int = 0
    context_parts: list[str] = field(default_factory=list)


def load_pipeline(project_root: Path) -> dict | None:
    path = scripts_dir(project_root) / "pipeline.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("读取 pipeline.json 失败: %s", e)
        return None


def normalize_steps(data: dict) -> list[dict]:
    """统一为有序 steps；兼容旧版 scripts + ai_steps。"""
    raw = data.get("steps")
    if isinstance(raw, list) and raw:
        out: list[dict] = []
        for i, s in enumerate(raw):
            if not isinstance(s, dict):
                continue
            t = str(s.get("type") or "").lower().strip()
            if t not in ("script", "ai"):
                continue
            step = dict(s)
            step["type"] = t
            step.setdefault("id", f"{t}_{i+1}")
            out.append(step)
        if out:
            return out

    # 兼容：先全部脚本，再全部 AI
    steps: list[dict] = []
    for i, entry in enumerate(data.get("scripts") or []):
        if not isinstance(entry, dict):
            continue
        steps.append(
            {
                "id": f"script_{i+1}",
                "type": "script",
                "path": entry.get("path") or "",
                "tool": entry.get("tool") or "",
                "description": entry.get("description") or entry.get("path") or "",
                "args": entry.get("args") or {},
            }
        )
    for i, entry in enumerate(data.get("ai_steps") or []):
        if isinstance(entry, dict):
            steps.append(
                {
                    "id": f"ai_{i+1}",
                    "type": "ai",
                    "description": entry.get("description") or "AI 步骤",
                    "prompt_hint": entry.get("prompt_hint") or "",
                }
            )
        else:
            steps.append(
                {
                    "id": f"ai_{i+1}",
                    "type": "ai",
                    "description": str(entry),
                    "prompt_hint": "",
                }
            )
    return steps


def group_phases(steps: list[dict]) -> list[dict]:
    """将同类型连续步骤合并为阶段，例如 script→ai→script。"""
    phases: list[dict] = []
    for step in steps:
        t = step.get("type")
        if not phases or phases[-1]["type"] != t:
            phases.append({"type": t, "steps": [step]})
        else:
            phases[-1]["steps"].append(step)
    return phases


def _decode_bytes(data: bytes | None) -> str:
    """尽量用 utf-8 / gbk 解码脚本 stdout/stderr（Windows 常见混码）。"""
    if not data:
        return ""

    def _try(enc: str) -> str | None:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            return None

    for enc in ("utf-8", "utf-8-sig", "gbk", "cp936", "big5"):
        hit = _try(enc)
        if hit is not None:
            return hit.strip()
    return data.decode("utf-8", errors="replace").strip()


def _persist_script_callback(project_root: Path, script_rel: str, body: str) -> str | None:
    """把脚本 stdout/stderr 落到 workspace/script_callback_*.md（主机兜底）。"""
    from datetime import datetime

    text = (body or "").strip()
    if not text:
        return None
    root = Path(project_root)
    ws = root / "workspace"
    try:
        ws.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    stem = Path(script_rel or "script").stem or "script"
    # 自动固化名较长时，尽量用可读尾段
    if "_ai_" in stem:
        stem = stem.split("_ai_", 1)[-1] or stem
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    content = (
        f"# 脚本 callback：{stem}\n\n"
        f"- 生成时间：{stamp}\n"
        f"- 脚本：{script_rel}\n\n"
        f"---\n\n"
        f"{text}\n"
    )
    out = ws / f"script_callback_{stem}.md"
    try:
        out.write_text(content, encoding="utf-8")
    except OSError:
        return None
    try:
        return out.relative_to(root).as_posix()
    except ValueError:
        return str(out)


def run_one_script(
    project_root: Path,
    entry: dict,
    *,
    timeout_sec: int = 120,
) -> ScriptRunItem:
    import os

    root = Path(project_root)
    rel = str(entry.get("path") or "")
    desc = str(entry.get("description") or rel)
    step_id = str(entry.get("id") or "")
    script_file = root / rel
    if not rel or not script_file.exists():
        return ScriptRunItem(
            path=rel,
            ok=False,
            error=f"文件不存在：{rel}",
            description=desc,
            step_id=step_id,
        )
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    suffix = script_file.suffix.lower()
    if suffix == ".py":
        cmd = [sys.executable, "-X", "utf8", str(script_file)]
        use_shell = False
    elif suffix in {".bat", ".cmd"}:
        cmd = ["cmd", "/c", str(script_file)]
        use_shell = False
    elif suffix == ".ps1":
        cmd = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_file),
        ]
        use_shell = False
    elif suffix == ".js":
        cmd = ["node", str(script_file)]
        use_shell = False
    elif suffix == ".sh":
        cmd = ["bash", str(script_file)]
        use_shell = False
    elif suffix == ".vbs":
        cmd = ["cscript", "//Nologo", str(script_file)]
        use_shell = False
    elif suffix == ".json":
        # 约定：{"command":"..."} 或 {"argv":[...]}
        try:
            data = json.loads(script_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return ScriptRunItem(
                path=rel,
                ok=False,
                error=f"JSON 脚本无效：{e}",
                description=desc,
                step_id=step_id,
            )
        if isinstance(data, dict) and isinstance(data.get("argv"), list) and data["argv"]:
            cmd = [str(x) for x in data["argv"]]
            use_shell = False
        elif isinstance(data, dict) and str(data.get("command") or "").strip():
            cmd = str(data["command"]).strip()
            use_shell = True
        else:
            return ScriptRunItem(
                path=rel,
                ok=False,
                error="JSON 脚本需含 command 或 argv",
                description=desc,
                step_id=step_id,
            )
    else:
        return ScriptRunItem(
            path=rel,
            ok=False,
            error=f"不支持的脚本格式：{suffix or '(无扩展名)'}",
            description=desc,
            step_id=step_id,
        )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            capture_output=True,
            timeout=timeout_sec,
            env=env,
            shell=use_shell,
        )
        out = _decode_bytes(proc.stdout)
        err = _decode_bytes(proc.stderr)
        ok = proc.returncode == 0 and not out.startswith("脚本执行失败")
        if out.startswith("错误：") or "失败：" in out[:80]:
            ok = False
        # 主机兜底：无论脚本内部是否 _save，都把输出落到 workspace
        persist_body = out if out else err
        if persist_body:
            saved = _persist_script_callback(root, rel, persist_body)
            if saved and saved not in (out or ""):
                out = (out or "") + (f"\n[callback 已写入] {saved}" if out else f"[callback 已写入] {saved}")
        return ScriptRunItem(
            path=rel,
            ok=ok,
            output=out[:8000],
            error=(err[:2000] if err else "") or ("" if ok else out[:2000]),
            description=desc,
            step_id=step_id,
        )
    except subprocess.TimeoutExpired:
        return ScriptRunItem(
            path=rel,
            ok=False,
            error=f"超时（>{timeout_sec}s）",
            description=desc,
            step_id=step_id,
        )
    except OSError as e:
        return ScriptRunItem(
            path=rel,
            ok=False,
            error=str(e),
            description=desc,
            step_id=step_id,
        )


def run_script_phase(
    project_root: Path,
    steps: list[dict],
    *,
    timeout_sec: int = 120,
) -> PhaseResult:
    phase = PhaseResult(type="script")
    outputs: list[str] = []
    for entry in steps:
        item = run_one_script(project_root, entry, timeout_sec=timeout_sec)
        phase.items.append(item)
        if item.output:
            outputs.append(f"### [{item.step_id or item.path}] {item.description}\n{item.output[:4000]}")
        if not item.ok:
            phase.ok = False
            phase.error = item.error or "脚本失败"
            break  # 有序执行：失败则停在本步，交给 AI 或中止
    phase.output = "\n\n".join(outputs)
    return phase


def peek_pipeline(project_root: Path) -> PipelineRunResult:
    """读取并规范化管线，不执行。"""
    root = Path(project_root)
    pipe_path = scripts_dir(root) / "pipeline.json"
    data = load_pipeline(root)
    result = PipelineRunResult(pipeline_path=pipe_path if pipe_path.exists() else None)
    if not data:
        result.reason = "无 pipeline.json，走完整 AI 流程"
        result.need_ai = True
        return result
    steps = normalize_steps(data)
    result.steps = steps
    result.phases = group_phases(steps)
    result.ran = True
    if not steps:
        result.reason = "pipeline 无步骤"
        result.need_ai = True
        result.ok = True
        return result
    result.reason = f"有序管线共 {len(steps)} 步、{len(result.phases)} 个阶段"
    result.need_ai = any(s.get("type") == "ai" for s in steps)
    result.ok = True
    return result


def run_pipeline_until_ai_or_end(
    project_root: Path,
    *,
    start_phase: int = 0,
    timeout_sec: int = 120,
    prior_context: list[str] | None = None,
) -> PipelineRunResult:
    """从 start_phase 起执行：连续脚本阶段会跑完；遇到 AI 阶段则暂停并 need_ai。

    若整段均为脚本且成功，则 need_ai=False。
    """
    result = peek_pipeline(project_root)
    if not result.ran or not result.phases:
        return result

    context = list(prior_context or [])
    result.context_parts = context
    all_items: list[ScriptRunItem] = []

    i = start_phase
    while i < len(result.phases):
        phase = result.phases[i]
        if phase["type"] == "script":
            pr = run_script_phase(
                project_root, phase["steps"], timeout_sec=timeout_sec
            )
            all_items.extend(pr.items)
            if pr.output:
                context.append(
                    f"## 阶段 {i+1}（脚本）\n{pr.output}"
                )
            if not pr.ok:
                result.items = all_items
                result.ok = False
                result.combined_output = "\n\n".join(context)
                result.context_parts = context
                result.error_summary = "\n".join(
                    f"- `{it.path}`: {it.error or '失败'}" for it in pr.items if not it.ok
                )
                result.need_ai = True
                result.next_phase_index = i
                result.ai_steps = []  # 本阶段失败，交给 AI 补救本步
                result.reason = (
                    f"执行顺序第 {i+1} 阶段（脚本）失败，暂停；"
                    "请 AI 补救后再继续后续步骤"
                )
                return result
            i += 1
            continue

        # AI 阶段：暂停，把本阶段 AI 步骤交给模型
        result.items = all_items
        result.ok = True
        result.combined_output = "\n\n".join(context)
        result.context_parts = context
        result.next_phase_index = i
        result.ai_steps = list(phase["steps"])
        result.need_ai = True
        # 后续还有什么（告知 AI 不要越权做后面的脚本）
        remaining = result.phases[i + 1 :]
        tail = ""
        if remaining:
            tail = "；本阶段完成后主机将继续执行后续脚本/AI 阶段，请勿越权执行后续脚本步骤"
        result.reason = f"按执行顺序进入第 {i+1} 阶段（AI）{tail}"
        return result

    # 全部阶段完成且无 AI
    result.items = all_items
    result.ok = True
    result.combined_output = "\n\n".join(context)
    result.context_parts = context
    result.next_phase_index = len(result.phases)
    result.need_ai = False
    result.ai_steps = []
    result.reason = "有序管线全部为脚本且已成功，跳过模型"
    return result


def build_user_message_for_ai_phase(
    *,
    original_message: str,
    pipeline: PipelineRunResult,
    phase_index: int | None = None,
) -> str:
    """构造当前 AI 阶段的用户消息（含先前脚本上下文与本阶段任务）。"""
    idx = phase_index if phase_index is not None else pipeline.next_phase_index
    parts = [
        original_message.strip() or "请根据项目目标推进工作。",
        "",
        "【有序执行管线 — 当前为 AI 阶段】",
        f"说明：{pipeline.reason}",
        "请严格只完成本阶段列出的 AI 任务；不要重复已成功的脚本拉取；"
        "不要擅自执行后续应由本地脚本完成的步骤。",
    ]
    if pipeline.context_parts or pipeline.combined_output:
        parts.extend(
            [
                "",
                "## 此前阶段已产出的上下文（脚本结果等）",
                (pipeline.combined_output or "\n\n".join(pipeline.context_parts))[:8000],
            ]
        )
    if not pipeline.ok and pipeline.error_summary:
        parts.extend(
            [
                "",
                "## 脚本失败（请先补救）",
                pipeline.error_summary,
                "补救后把关键结果写入 workspace/script_callback_*.md，主机将按经验顺序继续后续步骤。",
            ]
        )
    elif pipeline.ai_steps:
        parts.extend(["", f"## 本阶段 AI 任务（阶段 {idx+1}）"])
        for n, step in enumerate(pipeline.ai_steps, 1):
            if isinstance(step, dict):
                parts.append(f"{n}. {step.get('description') or step}")
                hint = step.get("prompt_hint") or ""
                if hint:
                    parts.append(f"   （{hint}）")
            else:
                parts.append(f"{n}. {step}")
        parts.append(
            "请先读取 workspace/script_callback_*.md 中的脚本 callback；"
            "完成后把中间结果写入 workspace/（或最终交付写入 deliverables/）；"
            "用户上传文件在 uploads/。"
        )

    # 预告后续阶段，避免 AI 包办
    remaining = []
    for ph in pipeline.phases[idx + 1 :]:
        labels = []
        for s in ph.get("steps") or []:
            labels.append(str(s.get("description") or s.get("path") or s.get("type")))
        remaining.append(f"- [{ph.get('type')}] " + "；".join(labels))
    if remaining:
        parts.extend(
            [
                "",
                "## 后续阶段（由主机按顺序执行，你现在不要做）",
                *remaining,
            ]
        )
    return "\n".join(parts)


# 兼容旧名
def run_pipeline(project_root: Path, *, timeout_sec: int = 120) -> PipelineRunResult:
    return run_pipeline_until_ai_or_end(project_root, timeout_sec=timeout_sec)


def build_user_message_after_scripts(
    *,
    original_message: str,
    pipeline: PipelineRunResult,
) -> str:
    return build_user_message_for_ai_phase(
        original_message=original_message,
        pipeline=pipeline,
    )


def format_order_markdown(steps: list[dict]) -> str:
    """写入经验文档的「执行顺序」章节。"""
    if not steps:
        return "（暂无有序步骤；下次运行将走完整 AI。）"
    lines = [
        "下次运行将**严格按下列顺序一路执行**"
        "（可连续多个脚本或连续多个 AI；不强制 script/AI 交替）：",
        "",
    ]
    for i, s in enumerate(steps, 1):
        t = s.get("type")
        if t == "script":
            lines.append(
                f"{i}. **[脚本]** `{s.get('path')}` — {s.get('description') or ''}"
            )
        else:
            lines.append(f"{i}. **[AI]** {s.get('description') or 'AI 步骤'}")
            if s.get("prompt_hint"):
                lines.append(f"   - 提示：{s.get('prompt_hint')}")
    lines.append("")
    lines.append("`scripts/` **不参与归档**；本顺序保存在 `scripts/pipeline.json` 的 `steps` 中。")
    return "\n".join(lines)
