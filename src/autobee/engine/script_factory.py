"""从运行轨迹固化可本地执行的脚本（不耗 Token）。"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wokbee.core.safe_io import safe_write_text

from autobee.core.paths import ensure_project_layout, scripts_dir

SCRIPTABLE_TOOLS = frozenset({"web_search", "http_get", "http_request"})

_COMMON_HEADER = '''# -*- coding: utf-8 -*-
"""AutoBee 固化脚本 — 本地执行，不耗 Token。由经验总结自动生成。"""
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
    print("脚本执行失败：缺少 httpx，请在 AutoBee 同一 Python 环境中运行")
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
    out = Path(__file__).resolve().parent / "_last_result.txt"
    out.write_text(result, encoding="utf-8")
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
    for prefix in ("⟶ 调用工具：", "调用工具：", "1. 调用工具："):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
            break
    text = re.sub(r"^\d+\.\s*", "", text)
    # "调用工具：xxx" already stripped; also "N. 调用工具：..."
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
    if args_s:
        try:
            val = ast.literal_eval(args_s)
            if isinstance(val, dict):
                args = val
            else:
                args = {"raw": val}
        except (ValueError, SyntaxError):
            try:
                val = json.loads(args_s.replace("'", '"'))
                if isinstance(val, dict):
                    args = val
            except json.JSONDecodeError:
                args = {"raw": args_s[:500]}
    return name, args


def extract_scriptable_from_events(events: list) -> list[ScriptStep]:
    steps: list[ScriptStep] = []
    seen: set[str] = set()
    for ev in events or []:
        kind = getattr(ev, "kind", "") or ""
        content = getattr(ev, "content", None) or ""
        candidates = [content]
        if kind != "tool":
            candidates = content.splitlines()
        for line in candidates:
            parsed = _parse_tool_call_line(line)
            if not parsed:
                continue
            name, args = parsed
            if name not in SCRIPTABLE_TOOLS:
                continue
            key = f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
            if key in seen:
                continue
            seen.add(key)
            desc = f"{name}({args})" if args else name
            steps.append(ScriptStep(tool=name, args=args, description=desc[:200]))
    return steps


def extract_scriptable_from_path_text(success_path: str) -> list[ScriptStep]:
    class _E:
        def __init__(self, content: str):
            self.kind = "tool"
            self.content = content

    return extract_scriptable_from_events(
        [_E(line) for line in (success_path or "").splitlines() if line.strip()]
    )


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
            headers={{"User-Agent": "Mozilla/5.0 (compatible; AutoBeeScript/0.1)"}},
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
                "User-Agent": "Mozilla/5.0 (compatible; AutoBeeScript/0.1)",
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
    headers = {{"User-Agent": "Mozilla/5.0 (compatible; AutoBeeScript/0.1)"}}
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
                    "仅基于此前脚本输出提取事实；禁止编造；"
                    "将中间结果写入 workspace/ai_extract.md"
                ),
            )
        )
    if any(k in text for k in keywords_create) or (
        script_count > 0 and goal.strip() and not steps
    ):
        steps.append(
            AiStep(
                description=goal.strip()[:200] or "基于要点完成最终交付并写入产物",
                prompt_hint=(
                    "基于 workspace/ 中已有提取结果完成创作/成文；"
                    "最终写入 deliverables/；用户上传在 uploads/ 请直接读取；"
                    "若数据不足再说明，勿重复跑已成功的拉取脚本"
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
    """根据轨迹生成 scripts/*.py 与有序 pipeline.json（script/ai 交错）。"""
    from autobee.engine.script_runner import format_order_markdown

    ensure_project_layout(project_root)
    sdir = scripts_dir(project_root)
    sdir.mkdir(parents=True, exist_ok=True)

    scriptable = extract_scriptable_from_events(events or [])
    if not scriptable:
        scriptable = extract_scriptable_from_path_text(success_path)

    written: list[ScriptStep] = []
    ordered_steps: list[dict] = []

    for i, step in enumerate(scriptable, 1):
        src = _render_script(step)
        if not src:
            continue
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

    # 在数据脚本之后插入 AI 步骤（提取→成文），形成交错顺序
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
        script_md += "\n\n详见下方「执行顺序」；勿理解为可并行跑完所有脚本。"
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
