"""从运行轨迹固化可本地执行的脚本（不耗 Token）。"""

from __future__ import annotations

import ast
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from tokbee.core.safe_io import safe_write_text

from wokbee.core.paths import archives_dir, ensure_project_layout, scripts_dir

SCRIPTABLE_TOOLS = frozenset({"web_search", "http_get", "http_request", "execute"})

# execute 命令中可固化为本地脚本的扩展名
_SCRIPT_FILE_RE = re.compile(
    r"""(?ix)
    (?:^|[\\/"'\s])
    (?P<path>(?:[A-Za-z]:[\\/])?[^\s"'<>|]+\.(?:py|bat|cmd|ps1))
    """
)
_REDIRECT_RE = re.compile(
    r"""(?ix)
    \s*(?:>>?)\s*(?:"[^"]*"|'[^']*'|[^\s&|;]+)
    (?:\s*2>&1|\s*2>\s*(?:"[^"]*"|'[^']*'|[^\s&|;]+))?
    |
    \s*2>&1
    """
)

_COMMON_HEADER = '''# -*- coding: utf-8 -*-
"""WokBee 固化脚本 — 本地执行，不耗 Token。由经验总结自动生成。

约定：脚本 callback（返回内容）必须写入 workspace/script_callback_*.md，
供后续 AI 步骤读取；同时打印到 stdout。
"""
from __future__ import annotations

import json
import re
import sys
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus, unquote

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import httpx
except ImportError:
    print("脚本执行失败：缺少 httpx，请在 WokBee 同一 Python 环境中运行")
    sys.exit(1)


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\\s+", " ", text)
    return text.strip()


def _decode_http_bytes(raw: bytes, content_type: str = "") -> str:
    """按声明编码优先，失败再试 utf-8/gbk，避免中文页乱码。"""
    if not raw:
        return ""
    declared = ""
    m = re.search(r"charset\\s*=\\s*['\\"]?([\\w\\-]+)", content_type or "", re.I)
    if m:
        declared = m.group(1).strip().lower().replace("gb2312", "gbk")
    head = raw[:4096]
    if not declared:
        m2 = re.search(br"charset\\s*=\\s*['\\"]?([\\w\\-]+)", head, re.I)
        if m2:
            declared = m2.group(1).decode("ascii", "ignore").lower().replace("gb2312", "gbk")

    def _try(enc: str):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            return None

    if declared:
        hit = _try(declared)
        if hit is not None and hit.count(chr(0xFFFD)) == 0:
            return hit
    for enc in ("utf-8", "utf-8-sig", "gbk", "cp936", "big5"):
        if enc == declared:
            continue
        hit = _try(enc)
        if hit is not None:
            return hit
    return raw.decode("utf-8", errors="replace")


def _resp_text(resp) -> str:
    return _decode_http_bytes(resp.content or b"", resp.headers.get("content-type") or "")


def _save(result: str) -> None:
    """把脚本 callback 写入 workspace/，便于后续 AI 步骤读取复用。"""
    from datetime import datetime

    root = Path(__file__).resolve().parents[1]
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()
    stem = script_path.stem
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"# 脚本 callback：{stem}\\n\\n"
        f"- 生成时间：{stamp}\\n"
        f"- 脚本：scripts/{script_path.name}\\n\\n"
        f"---\\n\\n"
        f"{result}\\n"
    )
    out = ws / f"script_callback_{stem}.md"
    out.write_text(body, encoding="utf-8")
    print(f"[callback 已写入] {out.relative_to(root).as_posix()}")
    print(result)

'''

_EXECUTE_HEADER = '''# -*- coding: utf-8 -*-
"""WokBee 固化脚本 — 复现 execute / Skill 本地命令，不耗 Token。

约定：stdout/stderr 作为 callback 写入 workspace/script_callback_*.md。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _skills_home() -> Path:
    env = (os.environ.get("WOKBEE_SKILLS_ROOT") or "").strip()
    if env:
        return Path(env)
    return Path.home() / ".wokbee" / "skills"


def _save(result: str, *, label: str = "") -> None:
    from datetime import datetime

    root = Path(__file__).resolve().parents[1]
    ws = root / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    script_path = Path(__file__).resolve()
    stem = (label or script_path.stem).strip() or script_path.stem
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        f"# 脚本 callback：{stem}\\n\\n"
        f"- 生成时间：{stamp}\\n"
        f"- 脚本：scripts/{script_path.name}\\n\\n"
        f"---\\n\\n"
        f"{result}\\n"
    )
    out = ws / f"script_callback_{stem}.md"
    out.write_text(body, encoding="utf-8")
    print(f"[callback 已写入] {out.relative_to(root).as_posix()}")
    print(result)

'''


@dataclass
class ScriptStep:
    tool: str
    args: dict[str, Any]
    description: str
    rel_path: str = ""


@dataclass
class AiStep:
    description: str
    prompt_hint: str = ""


@dataclass
class SolidifyResult:
    script_steps: list[ScriptStep] = field(default_factory=list)
    ai_steps: list[AiStep] = field(default_factory=list)
    pipeline_rel: str = "scripts/pipeline.json"
    script_section_md: str = ""
    ai_section_md: str = ""
    order_section_md: str = ""


def _parse_tool_call_line(content: str) -> tuple[str, dict] | None:
    text = (content or "").strip()
    for prefix in (
        "call: ",
        "⟶ 调用工具：",
        "调用工具：",
        "1. 调用工具：",
        "1. call: ",
    ):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    text = re.sub(r"^\d+\.\s*", "", text)
    if text.lower().startswith("call:"):
        text = text.split(":", 1)[-1].strip()
    if "调用工具：" in text:
        text = text.split("调用工具：", 1)[-1].strip()
    m = re.match(r"^([A-Za-z_][\w]*)\((.*)\)\s*$", text, re.DOTALL)
    if not m:
        m2 = re.match(r"^([A-Za-z_][\w]*)\b", text)
        if m2 and m2.group(1) in SCRIPTABLE_TOOLS:
            return m2.group(1), {}
        return None
    name, args_s = m.group(1), m.group(2).strip()
    args: dict[str, Any] = {}
    if args_s and not args_s.endswith("…"):
        try:
            val = json.loads(args_s)
            if isinstance(val, dict):
                args = val
            else:
                args = {"raw": val}
        except json.JSONDecodeError:
            try:
                val = ast.literal_eval(args_s)
                if isinstance(val, dict):
                    args = val
                else:
                    args = {"raw": val}
            except (ValueError, SyntaxError):
                args = {"raw": args_s[:500]}
    elif args_s.endswith("…"):
        # 截断参数：尽量从 JSON 残片里抠 query/url
        m_q = re.search(r'"query"\s*:\s*"([^"]+)"', args_s) or re.search(
            r"'query'\s*:\s*'([^']+)'", args_s
        )
        m_u = re.search(r'"url"\s*:\s*"([^"]+)"', args_s) or re.search(
            r"'url'\s*:\s*'([^']+)'", args_s
        )
        if m_q:
            args = {"query": m_q.group(1)}
        elif m_u:
            args = {"url": m_u.group(1)}
        else:
            args = {"raw": args_s[:500]}
    return name, args


def _strip_shell_redirects(cmd: str) -> str:
    text = (cmd or "").strip()
    # 去掉末尾 redirect，保留真正执行的命令
    prev = None
    while prev != text:
        prev = text
        text = _REDIRECT_RE.sub("", text).strip()
        text = re.sub(r"[\s;]+$", "", text).strip()
    # 去掉 "; echo EXIT:$?" 一类探测尾巴
    text = re.sub(r";\s*echo\s+[\"']?EXIT:.*?[\"']?\s*$", "", text, flags=re.I).strip()
    return text


def _execute_script_label(cmd: str) -> str:
    m = _SCRIPT_FILE_RE.search(cmd or "")
    if m:
        return Path(m.group("path").strip("\"'")).stem[:40] or "execute"
    return "execute"


def _is_scriptable_execute(cmd: str) -> bool:
    """仅固化「跑本地脚本文件」类命令，避免把任意 shell 都写成脚本。"""
    text = _strip_shell_redirects(cmd)
    if not text or len(text) > 4000:
        return False
    if not _SCRIPT_FILE_RE.search(text):
        return False
    # 排除明显危险的整盘操作（仍可由 AI 手动 execute）
    lowered = text.lower()
    for bad in ("rm -rf /", "format ", "del /s /q c:\\", "shutdown"):
        if bad in lowered:
            return False
    return True


def _rewrite_skills_path(cmd: str) -> str:
    """把本机 skills 绝对路径改写为 {SKILLS}/…，便于换机复用。"""
    text = cmd or ""
    lower = text.lower()
    for marker in (".wokbee\\skills", ".wokbee/skills", ".tokbee\\skills", ".tokbee/skills"):
        idx = lower.find(marker)
        if idx < 0:
            continue
        # 向前找到路径起点（盘符或引号后）
        start = idx
        while start > 0 and text[start - 1] not in "\"' \t\n\r":
            start -= 1
        end = idx + len(marker)
        return text[:start] + "{SKILLS}" + text[end:]
    return text


def _normalize_execute_command(cmd: str) -> tuple[str, str] | None:
    """返回 (可用于模板的命令, label)。技能绝对路径改写为 {SKILLS}/..."""
    text = _strip_shell_redirects(cmd)
    if not _is_scriptable_execute(text):
        return None
    label = _execute_script_label(text)
    text = _rewrite_skills_path(text)
    # 统一斜杠，便于跨机；Windows shell 仍能跑
    text = text.replace("\\\\", "/").replace("\\", "/")
    return text, label


def _script_execute(command: str, label: str = "execute") -> str:
    cmd = json.dumps(command, ensure_ascii=False)
    lab = json.dumps(label or "execute", ensure_ascii=False)
    return (
        _EXECUTE_HEADER
        + f"""
CMD_TEMPLATE = {cmd}
LABEL = {lab}

def main() -> None:
    skills = _skills_home()
    cmd = CMD_TEMPLATE.replace("{{SKILLS}}", str(skills).replace("\\\\", "/"))
    # 本脚本位于 scripts/，工作目录应为项目根
    root = Path(__file__).resolve().parents[1]
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        _save("脚本执行失败：超时（>180s）", label=LABEL)
        sys.exit(1)
    except OSError as e:
        _save(f"脚本执行失败：{{e}}", label=LABEL)
        sys.exit(1)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    body = out if out else err
    if proc.returncode != 0:
        msg = body or f"exit={{proc.returncode}}"
        if err and out:
            msg = f"{{out}}\\n\\n[stderr]\\n{{err}}"
        _save(f"脚本执行失败：{{msg}}", label=LABEL)
        sys.exit(proc.returncode or 1)
    if not body:
        body = "(无输出)"
    _save(body, label=LABEL)

if __name__ == "__main__":
    main()
"""
    )


def extract_scriptable_from_events(events: list) -> list[ScriptStep]:
    steps: list[ScriptStep] = []
    seen: set[str] = set()

    def _try_add(name: str, args: dict[str, Any]) -> None:
        name = (name or "").strip()
        if name not in SCRIPTABLE_TOOLS:
            return
        args = dict(args or {})
        if name == "execute":
            raw = str(args.get("command") or args.get("raw") or "").strip()
            norm = _normalize_execute_command(raw)
            if not norm:
                return
            cmd, label = norm
            args = {"command": cmd, "label": label}
        key = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
        if key in seen:
            return
        seen.add(key)
        if name == "execute":
            desc = f"execute({args.get('label')}: {args.get('command')})"
        else:
            desc = f"{name}({args})" if args else name
        steps.append(ScriptStep(tool=name, args=args, description=desc[:200]))

    for ev in events or []:
        kind = getattr(ev, "kind", "") or ""
        content = getattr(ev, "content", None) or ""
        meta = getattr(ev, "meta", None) or {}
        if not isinstance(meta, dict):
            meta = {}

        # 结构化 meta（推荐路径）：工具调用阶段直接带 tool/args
        if kind == "tool" and meta.get("phase") == "call":
            name = str(meta.get("tool") or "").strip()
            args = meta.get("args") if isinstance(meta.get("args"), dict) else {}
            _try_add(name, args)
            continue

        candidates = [content]
        if kind != "tool":
            candidates = content.splitlines()
        else:
            # 工具事件也按行扫，兼容「调用工具」写在首行
            candidates = [content] + content.splitlines()

        for line in candidates:
            parsed = _parse_tool_call_line(line)
            if not parsed:
                continue
            name, args = parsed
            _try_add(name, args)
    return steps


def extract_scriptable_from_path_text(success_path: str) -> list[ScriptStep]:
    class _E:
        def __init__(self, content: str, *, kind: str = "tool", meta: dict | None = None):
            self.kind = kind
            self.content = content
            self.meta = meta or {}

    events: list = []
    for line in (success_path or "").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(_E(line))
        # 散文中夹带的脚本命令：python …/foo.py …
        if _is_scriptable_execute(line) or (
            ".py" in line.lower() and ("python" in line.lower() or "execute" in line.lower())
        ):
            # 尝试抠出可执行片段
            m = re.search(
                r"""(?ix)((?:python(?:3)?|py)\s+[\"']?[^\s\"']+\.(?:py|bat|cmd|ps1)[\"']?(?:\s+[^\n]*)?)""",
                line,
            )
            if m and _is_scriptable_execute(m.group(1)):
                events.append(
                    _E(
                        "",
                        kind="tool",
                        meta={
                            "phase": "call",
                            "tool": "execute",
                            "args": {"command": m.group(1).strip()},
                        },
                    )
                )
    return extract_scriptable_from_events(events)


def _script_web_search(query: str, max_results: int = 5) -> str:
    q = json.dumps(query, ensure_ascii=False)
    mr = int(max_results)
    return (
        _COMMON_HEADER
        + f"""
QUERY = {q}
MAX_RESULTS = {mr}

def main() -> None:
    url = f"https://html.duckduckgo.com/html/?q={{quote_plus(QUERY)}}"
    try:
        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={{"User-Agent": "Mozilla/5.0 (compatible; WokBeeScript/0.1)"}},
        ) as client:
            html = _resp_text(client.get(url))
    except Exception as e:
        _save(f"脚本执行失败：搜索失败：{{e}}")
        sys.exit(1)
    results = []
    blocks = re.findall(
        r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
    )
    for href, title in blocks[:MAX_RESULTS]:
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                href = unquote(m.group(1))
        results.append(f"- {{_strip_html(title)[:120]}}\\n  URL: {{href}}")
    if not results:
        _save("未找到搜索结果")
        return
    _save(f"搜索「{{QUERY}}」结果：\\n" + "\\n".join(results))

if __name__ == "__main__":
    main()
"""
    )


def _script_http_get(url: str, max_chars: int = 12000) -> str:
    u = json.dumps(url, ensure_ascii=False)
    mc = int(max_chars)
    return (
        _COMMON_HEADER
        + f"""
URL = {u}
MAX_CHARS = {mc}

def main() -> None:
    try:
        with httpx.Client(
            timeout=45.0,
            follow_redirects=True,
            headers={{
                "User-Agent": "Mozilla/5.0 (compatible; WokBeeScript/0.1)",
                "Accept": "text/html,application/json,text/plain,*/*",
            }},
        ) as client:
            resp = client.get(URL)
            ctype = (resp.headers.get("content-type") or "").lower()
            text = _resp_text(resp)
            if "application/json" in ctype:
                try:
                    text = json.dumps(resp.json(), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            elif "html" in ctype:
                text = _strip_html(text)
            body = text[:MAX_CHARS]
            header = f"HTTP {{resp.status_code}} | {{ctype or 'unknown'}} | len={{len(text)}}"
            _save(f"{{header}}\\nURL: {{resp.url}}\\n\\n{{body}}")
    except Exception as e:
        _save(f"脚本执行失败：请求失败：{{e}}")
        sys.exit(1)

if __name__ == "__main__":
    main()
"""
    )


def _script_http_request(
    url: str,
    method: str = "GET",
    headers_json: str = "",
    body: str = "",
    max_chars: int = 12000,
) -> str:
    return (
        _COMMON_HEADER
        + f"""
URL = {json.dumps(url, ensure_ascii=False)}
METHOD = {json.dumps(method, ensure_ascii=False)}
HEADERS_JSON = {json.dumps(headers_json, ensure_ascii=False)}
BODY = {json.dumps(body, ensure_ascii=False)}
MAX_CHARS = {int(max_chars)}

def main() -> None:
    headers = {{"User-Agent": "Mozilla/5.0 (compatible; WokBeeScript/0.1)"}}
    if HEADERS_JSON.strip():
        try:
            extra = json.loads(HEADERS_JSON)
            if isinstance(extra, dict):
                headers.update({{str(k): str(v) for k, v in extra.items()}})
        except json.JSONDecodeError as e:
            _save(f"脚本执行失败：headers_json 非法：{{e}}")
            sys.exit(1)
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.request(
                METHOD,
                URL,
                headers=headers,
                content=BODY.encode("utf-8") if BODY else None,
            )
            ctype = (resp.headers.get("content-type") or "").lower()
            text = _resp_text(resp)
            if "json" in ctype:
                try:
                    text = json.dumps(resp.json(), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            elif "html" in ctype:
                text = _strip_html(text)
            _save(
                f"HTTP {{resp.status_code}} {{METHOD}} | {{ctype or 'unknown'}}\\n"
                f"URL: {{resp.url}}\\n\\n{{text[:MAX_CHARS]}}"
            )
    except Exception as e:
        _save(f"脚本执行失败：请求失败：{{e}}")
        sys.exit(1)

if __name__ == "__main__":
    main()
"""
    )


def _render_script(step: ScriptStep) -> str | None:
    args = step.args or {}
    if step.tool == "web_search":
        q = str(args.get("query") or args.get("q") or args.get("raw") or "").strip()
        if not q:
            return None
        return _script_web_search(q, int(args.get("max_results") or 5))
    if step.tool == "http_get":
        url = str(args.get("url") or args.get("raw") or "").strip()
        if not url.startswith("http"):
            return None
        return _script_http_get(url, int(args.get("max_chars") or 12000))
    if step.tool == "http_request":
        url = str(args.get("url") or "").strip()
        if not url.startswith("http"):
            return None
        return _script_http_request(
            url,
            method=str(args.get("method") or "GET"),
            headers_json=str(args.get("headers_json") or ""),
            body=str(args.get("body") or ""),
            max_chars=int(args.get("max_chars") or 12000),
        )
    if step.tool == "execute":
        cmd = str(args.get("command") or "").strip()
        label = str(args.get("label") or _execute_script_label(cmd) or "execute")
        if not cmd:
            return None
        # 若尚未规范化，再规范化一次
        norm = _normalize_execute_command(cmd)
        if not norm:
            return None
        cmd, label = norm[0], (args.get("label") or norm[1] or label)
        return _script_execute(cmd, str(label))
    return None


def infer_ai_steps(goal: str, summary: str, script_count: int) -> list[AiStep]:
    text = f"{goal}\n{summary}"
    keywords_extract = ("提取", "总结", "汇总", "分析", "结构化", "要点")
    keywords_create = (
        "文案",
        "改写",
        "润色",
        "风格",
        "小红书",
        "写一篇",
        "撰写",
        "翻译",
        "报告",
        "生成文档",
        "写成",
        "输出文档",
    )
    steps: list[AiStep] = []
    if script_count > 0 and (
        any(k in text for k in keywords_extract)
        or any(k in text for k in keywords_create)
        or goal.strip()
    ):
        # 数据已由脚本拉取后：先提取/总结
        steps.append(
            AiStep(
                description="提取脚本产出的关键数据，总结为结构化要点",
                prompt_hint=(
                    "必须先读取 workspace/script_callback_*.md"
                    "中的脚本 callback；仅基于这些事实提取；禁止编造；"
                    "将中间结果写入 workspace/ai_extract.md"
                ),
            )
        )
    if any(k in text for k in keywords_create) or (script_count > 0 and goal.strip()):
        # 有目标/创作类需求时：在提取之后成文（即使已有提取步骤也要追加）
        steps.append(
            AiStep(
                description=goal.strip()[:200] or "基于要点完成最终交付并写入产物",
                prompt_hint=(
                    "基于 workspace/script_callback_*.md 与 ai_extract.md 完成创作/成文；"
                    "最终写入 deliverables/；用户上传在 uploads/ 请直接读取；"
                    "同名或相近文件以最新修改时间为准；"
                    "禁止编造脚本未提供的事实；若数据不足再说明，勿重复跑已成功的拉取脚本"
                ),
            )
        )
    if script_count == 0 and not steps:
        steps.append(
            AiStep(
                description=goal.strip()[:200] or "完成项目目标",
                prompt_hint="无可用固化脚本，请按目标正常使用工具完成。",
            )
        )
    return steps


def solidify_scripts(
    project_root: Path,
    *,
    lesson_id: str,
    goal: str = "",
    summary: str = "",
    success_path: str = "",
    events: list | None = None,
) -> SolidifyResult:
    """根据轨迹生成 scripts/*.py 与有序 pipeline.json（按 steps 顺序，非强制交错）。"""
    from wokbee.engine.script_runner import format_order_markdown

    ensure_project_layout(project_root)
    sdir = scripts_dir(project_root)
    sdir.mkdir(parents=True, exist_ok=True)

    scriptable = extract_scriptable_from_events(events or [])
    from_path = extract_scriptable_from_path_text(success_path)
    # 合并：事件优先，路径轨迹补漏（避免 AI 散文覆盖后丢工具）
    seen_keys = {
        f"{s.tool}:{json.dumps(s.args, ensure_ascii=False, sort_keys=True)}"
        for s in scriptable
    }
    for s in from_path:
        key = f"{s.tool}:{json.dumps(s.args, ensure_ascii=False, sort_keys=True)}"
        if key not in seen_keys:
            seen_keys.add(key)
            scriptable.append(s)

    written: list[ScriptStep] = []
    ordered_steps: list[dict] = []

    for i, step in enumerate(scriptable, 1):
        src = _render_script(step)
        if not src:
            continue
        if step.tool == "execute":
            label = re.sub(
                r"[^\w\-]+",
                "_",
                str((step.args or {}).get("label") or "execute"),
            )[:40]
            fname = f"{lesson_id}_{i:02d}_{label}.py"
        else:
            fname = f"{lesson_id}_{i:02d}_{step.tool}.py"
        path = sdir / fname
        safe_write_text(path, src)
        step.rel_path = f"scripts/{fname}"
        written.append(step)
        ordered_steps.append(
            {
                "id": f"script_{i}",
                "type": "script",
                "path": step.rel_path,
                "tool": step.tool,
                "description": f"获取数据：{step.description}"[:200],
                "args": step.args,
            }
        )

    # 默认顺序：全部数据脚本 → AI 步骤 → 可选收尾脚本（连续同类，非强制交错）
    ai_steps = infer_ai_steps(goal, summary, len(written))
    for j, a in enumerate(ai_steps, 1):
        ordered_steps.append(
            {
                "id": f"ai_{j}",
                "type": "ai",
                "description": a.description,
                "prompt_hint": a.prompt_hint,
            }
        )

    # 收尾脚本：把 workspace 中 AI 产出归档到 deliverables（script→AI→script）
    if written and ai_steps:
        pub_name = f"{lesson_id}_publish_deliverables.py"
        pub_path = sdir / pub_name
        pub_src = '''# -*- coding: utf-8 -*-
"""将 workspace 中 AI 产出整理到 deliverables/（本地脚本，不耗 Token）"""
from __future__ import annotations
from pathlib import Path
from datetime import datetime

root = Path(__file__).resolve().parents[1]
ws = root / "workspace"
art = root / "deliverables"
art.mkdir(parents=True, exist_ok=True)
parts = []
if ws.exists():
    for p in sorted(ws.glob("*.md")):
        try:
            parts.append(f"## {p.name}\\n\\n{p.read_text(encoding='utf-8')}\\n")
        except OSError:
            pass
if not parts:
    parts.append("（workspace 中暂无 md；若 AI 已写入 deliverables 可忽略本步）\\n")
out = art / "final.md"
header = f"# 自动归档\\n\\n生成时间：{datetime.now().isoformat(timespec='seconds')}\\n\\n"
out.write_text(header + "\\n".join(parts), encoding="utf-8")
print(f"已生成 {out.relative_to(root)}，共合并 {len(parts)} 段")
'''
        safe_write_text(pub_path, pub_src)
        ordered_steps.append(
            {
                "id": f"script_publish",
                "type": "script",
                "path": f"scripts/{pub_name}",
                "tool": "publish",
                "description": "生成文档：合并 workspace 产出到 deliverables/final.md",
                "args": {},
            }
        )
        written.append(
            ScriptStep(
                tool="publish",
                args={},
                description="publish deliverables",
                rel_path=f"scripts/{pub_name}",
            )
        )

    pipeline = {
        "version": 2,
        "lesson_id": lesson_id,
        "goal": goal,
        "steps": ordered_steps,
        # 兼容旧字段
        "scripts": [
            {
                "path": s.rel_path,
                "tool": s.tool,
                "description": s.description,
                "args": s.args,
            }
            for s in written
        ],
        "ai_steps": [
            {"description": a.description, "prompt_hint": a.prompt_hint}
            for a in ai_steps
        ],
        "policy": {
            "ordered_execution": True,
            "invoke_ai_on_script_error": True,
            "invoke_ai_on_bad_data": True,
            "scripts_not_archived": True,
        },
    }
    safe_write_text(
        sdir / "pipeline.json",
        json.dumps(pipeline, ensure_ascii=False, indent=2),
    )

    order_md = format_order_markdown(ordered_steps)
    if written:
        script_md = "\n".join(
            f"- `{s.rel_path}` — {s.description}" for s in written
        )
        script_md += (
            "\n\n约定：每个脚本执行后把 callback 写入 "
            "`workspace/script_callback_<脚本名>.md`；"
            "后续 AI 必须先读再写。详见下方「执行顺序」。"
        )
    else:
        script_md = "（本轮无可固化脚本；顺序中以 AI 为主。）"

    if ai_steps:
        ai_md = "\n".join(
            f"- {a.description}"
            + (f"\n  - 提示：{a.prompt_hint}" if a.prompt_hint else "")
            for a in ai_steps
        )
    else:
        ai_md = "（顺序中无 AI 步骤；脚本成功即可结束。）"

    return SolidifyResult(
        script_steps=written,
        ai_steps=ai_steps,
        pipeline_rel="scripts/pipeline.json",
        script_section_md=script_md,
        ai_section_md=ai_md,
        order_section_md=order_md,
    )


# AI 手写脚本允许的扩展名（写入 scripts/；运行器按扩展名调度）
AI_SCRIPT_EXTENSIONS = frozenset(
    {".py", ".bat", ".cmd", ".ps1", ".json", ".sh", ".js", ".vbs"}
)
_RESERVED_SCRIPT_NAMES = frozenset({"pipeline.json"})


def sanitize_ai_script_filename(name: str) -> str | None:
    """只保留 scripts/ 下安全文件名；拒绝路径穿越与保留名。"""
    raw = (name or "").strip().replace("\\", "/")
    if not raw or ".." in raw.split("/"):
        return None
    base = Path(raw).name
    if not base or base.lower() in _RESERVED_SCRIPT_NAMES:
        return None
    # 去掉奇怪前缀
    base = re.sub(r"[^\w.\-一-龥]+", "_", base).strip("._")
    if not base:
        return None
    ext = Path(base).suffix.lower()
    if ext not in AI_SCRIPT_EXTENSIONS:
        # 无扩展名时默认 .py；未知扩展名拒绝（避免写入任意二进制伪装）
        if not ext:
            base = f"{base}.py"
            ext = ".py"
        else:
            return None
    stem = Path(base).stem[:80] or "script"
    stem = re.sub(r"[^\w.\-一-龥]+", "_", stem).strip("._") or "script"
    return f"{stem}{ext}"


def apply_ai_authored_scripts(
    project_root: Path,
    *,
    lesson_id: str,
    script_files: list[dict[str, Any]] | None,
) -> list[ScriptStep]:
    """把总结 AI 手写的脚本写入 scripts/，并合并进 pipeline.json。

    返回成功写入且纳入管线的 ScriptStep 列表。
    """
    from wokbee.engine.script_runner import load_pipeline

    files = script_files or []
    if not files:
        return []

    ensure_project_layout(project_root)
    sdir = scripts_dir(project_root)
    sdir.mkdir(parents=True, exist_ok=True)

    written: list[ScriptStep] = []
    pipeline_entries: list[dict[str, Any]] = []

    for i, item in enumerate(files, 1):
        if not isinstance(item, dict):
            continue
        fname = sanitize_ai_script_filename(str(item.get("filename") or ""))
        content = str(item.get("content") or "")
        if not fname or not content.strip():
            continue
        # 与自动固化区分：AI 手写保留可读名；冲突时加 lesson 前缀
        target = sdir / fname
        if target.exists() and lesson_id:
            # 若已有同名且内容不同，写带前缀副本，避免误覆盖自动固化脚本
            try:
                old = target.read_text(encoding="utf-8")
            except OSError:
                old = ""
            if old.strip() != content.strip():
                fname = f"{lesson_id}_ai_{fname}"
                target = sdir / fname

        ext = Path(fname).suffix.lower()
        body = content
        if ext in {".bat", ".cmd"}:
            body = body.replace("\r\n", "\n").replace("\n", "\r\n")
        safe_write_text(target, body)

        rel = f"scripts/{fname}"
        desc = str(item.get("description") or "").strip() or f"AI 手写脚本 {fname}"
        step = ScriptStep(
            tool="ai_authored",
            args={"filename": fname},
            description=desc[:200],
            rel_path=rel,
        )
        written.append(step)
        if bool(item.get("in_pipeline", True)):
            pipeline_entries.append(
                {
                    "id": f"script_ai_{i}",
                    "type": "script",
                    "path": rel,
                    "tool": "ai_authored",
                    "description": f"获取数据：{desc}"[:200],
                    "args": {"filename": fname, "source": "ai_summary"},
                }
            )

    if not written:
        return []

    # 合并 pipeline：AI 脚本插到首个 AI 步骤之前；已有同 path 则跳过
    data = load_pipeline(project_root) or {
        "version": 2,
        "lesson_id": lesson_id,
        "goal": "",
        "steps": [],
        "scripts": [],
        "ai_steps": [],
        "policy": {
            "ordered_execution": True,
            "invoke_ai_on_script_error": True,
            "invoke_ai_on_bad_data": True,
            "scripts_not_archived": True,
        },
    }
    steps = list(data.get("steps") or []) if isinstance(data.get("steps"), list) else []
    existing_paths = {
        str(s.get("path") or "")
        for s in steps
        if isinstance(s, dict) and s.get("type") == "script"
    }
    insert_at = next(
        (idx for idx, s in enumerate(steps) if isinstance(s, dict) and s.get("type") == "ai"),
        len(steps),
    )
    added = 0
    for entry in pipeline_entries:
        path = str(entry.get("path") or "")
        if not path or path in existing_paths:
            continue
        steps.insert(insert_at + added, entry)
        existing_paths.add(path)
        added += 1

    # 同步 scripts 兼容字段
    scripts_compat = [
        {
            "path": s.rel_path,
            "tool": s.tool,
            "description": s.description,
            "args": s.args,
        }
        for s in written
    ]
    old_scripts = data.get("scripts") if isinstance(data.get("scripts"), list) else []
    seen_script_paths = {
        str(x.get("path") or "") for x in old_scripts if isinstance(x, dict)
    }
    merged_scripts = list(old_scripts)
    for sc in scripts_compat:
        if sc["path"] not in seen_script_paths:
            merged_scripts.append(sc)
            seen_script_paths.add(sc["path"])

    data["steps"] = steps
    data["scripts"] = merged_scripts
    data["lesson_id"] = data.get("lesson_id") or lesson_id
    data["version"] = int(data.get("version") or 2)
    # 附注：记录 AI 手写
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    policy["ai_authored_scripts"] = True
    data["policy"] = policy

    safe_write_text(
        sdir / "pipeline.json",
        json.dumps(data, ensure_ascii=False, indent=2),
    )
    return written


def apply_ai_pipeline_steps(
    project_root: Path,
    *,
    lesson_id: str,
    goal: str = "",
    pipeline_steps: list[dict[str, Any]] | None,
) -> bool:
    """若总结 AI 给出了 pipeline_steps，则以该顺序覆盖 pipeline.json 的 steps。

    允许 script×N → ai×N → script… 任意有序组合；不强制交错。
    返回是否成功应用。
    """
    from wokbee.engine.script_runner import format_order_markdown, load_pipeline

    steps_in = pipeline_steps or []
    if not steps_in:
        return False

    ensure_project_layout(project_root)
    sdir = scripts_dir(project_root)
    sdir.mkdir(parents=True, exist_ok=True)
    root = Path(project_root)

    normalized: list[dict[str, Any]] = []
    for i, raw in enumerate(steps_in):
        if not isinstance(raw, dict):
            continue
        t = str(raw.get("type") or "").lower().strip()
        if t not in ("script", "ai"):
            continue
        if t == "script":
            path = str(raw.get("path") or "").replace("\\", "/").strip()
            if path and not path.startswith("scripts/"):
                path = f"scripts/{Path(path).name}"
            if not path:
                continue
            # 允许总结时尚未落盘的短暂窗口：仍写入管线，运行时再报错
            abs_path = root / path
            if not abs_path.exists():
                # 尝试仅文件名匹配 scripts/
                cand = sdir / Path(path).name
                if cand.exists():
                    path = f"scripts/{cand.name}"
            step = {
                "id": str(raw.get("id") or f"script_{i+1}"),
                "type": "script",
                "path": path,
                "tool": str(raw.get("tool") or "script"),
                "description": str(raw.get("description") or path)[:200],
                "args": raw.get("args") if isinstance(raw.get("args"), dict) else {},
            }
        else:
            step = {
                "id": str(raw.get("id") or f"ai_{i+1}"),
                "type": "ai",
                "description": str(raw.get("description") or "AI 步骤")[:300],
                "prompt_hint": str(raw.get("prompt_hint") or "").strip(),
            }
        normalized.append(step)

    if not normalized:
        return False

    data = load_pipeline(project_root) or {
        "version": 2,
        "scripts": [],
        "ai_steps": [],
        "policy": {},
    }
    data["version"] = 2
    data["lesson_id"] = lesson_id or data.get("lesson_id") or ""
    if goal:
        data["goal"] = goal
    data["steps"] = normalized
    data["scripts"] = [
        {
            "path": s.get("path"),
            "tool": s.get("tool"),
            "description": s.get("description"),
            "args": s.get("args") or {},
        }
        for s in normalized
        if s.get("type") == "script"
    ]
    data["ai_steps"] = [
        {
            "description": s.get("description"),
            "prompt_hint": s.get("prompt_hint") or "",
        }
        for s in normalized
        if s.get("type") == "ai"
    ]
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    policy.update(
        {
            "ordered_execution": True,
            "invoke_ai_on_script_error": True,
            "invoke_ai_on_bad_data": True,
            "scripts_not_archived": True,
            "order_source": "ai_summary",
        }
    )
    data["policy"] = policy
    data["order_markdown"] = format_order_markdown(normalized)

    safe_write_text(
        sdir / "pipeline.json",
        json.dumps(data, ensure_ascii=False, indent=2),
    )
    return True


# 清理时会处理/忽略的脚本扩展名（pipeline.json 另作保留）
_QUARANTINE_EXTENSIONS = frozenset(
    {".py", ".bat", ".cmd", ".ps1", ".json", ".sh", ".js", ".vbs"}
)


def quarantine_obsolete_scripts(
    project_root: Path,
    *,
    kept_paths: list[str],
    lesson_id: str = "",
) -> tuple[list[str], Path]:
    """把 scripts/ 顶层中不在 kept_paths 里的脚本移入 archives/discard_<ts>/scripts/。

    kept_paths 形如 "scripts/foo.py"，按文件名匹配；pipeline.json 与子目录不处理。
    「下次运行只执行 pipeline.json steps[].path」，因此保留 kept 之外的脚本即可安全孤立。
    返回 (已移走文件名列表, 目标目录)。可逆：不删除，只是搬到归档下。
    """
    ensure_project_layout(Path(project_root))
    sdir = scripts_dir(project_root)
    if not sdir.exists():
        return [], Path()

    kept_names = {Path(p).name for p in kept_paths if p}
    moved: list[str] = []
    dest: Path | None = None

    for p in sorted(sdir.iterdir(), key=lambda x: x.name):
        if not p.is_file():
            continue
        if p.name == "pipeline.json" or p.name in kept_names:
            continue
        ext = p.suffix.lower()
        if ext not in _QUARANTINE_EXTENSIONS:
            continue
        if dest is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = archives_dir(project_root) / f"discard_{stamp}" / "scripts"
            dest.mkdir(parents=True, exist_ok=True)
        try:
            target = dest / p.name
            if target.exists():
                target = dest / f"{p.stem}_{lesson_id[-4:] or 'x'}{p.suffix}"
            shutil.move(str(p), str(target))
            moved.append(p.name)
        except OSError:
            continue
    return moved, dest or Path()
