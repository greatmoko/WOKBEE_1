"""经验总结：每项目仅维护一份 memory/EXPERIENCE.md（Skills 风格），总结时覆盖更新。"""

from __future__ import annotations

import html
import platform
import re
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from wokbee.core.safe_io import safe_write_text

from autobee.core.paths import ensure_project_layout, memory_dir

EXPERIENCE_FILENAME = "EXPERIENCE.md"
EXPERIENCE_VIEW_HTML = "EXPERIENCE.html"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _slug(text: str, max_len: int = 40) -> str:
    s = re.sub(r"\s+", "-", (text or "").strip())
    s = re.sub(r"[^\w\u4e00-\u9fff\-]+", "", s)
    s = s.strip("-")[:max_len] or "exp"
    return s.lower() if s.isascii() else s


@dataclass
class Lesson:
    """一条经验（Skills 风格 Markdown）；每项目只保留最新一份。"""

    id: str = field(default_factory=lambda: f"exp_{uuid.uuid4().hex[:10]}")
    project_id: str = ""
    goal: str = ""
    outcome: str = "unknown"  # success | failed | cancelled | partial
    summary: str = ""
    success_path: str = ""
    environment: str = ""
    notes: str = ""
    artifacts: str = ""
    errors: str = ""
    model: str = ""
    policy: str = ""
    script_section: str = ""
    ai_section: str = ""
    order_section: str = ""  # 脚本/AI 交错执行顺序
    scripts: list[str] = field(default_factory=list)
    pipeline: str = "scripts/pipeline.json"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def name(self) -> str:
        # 固定逻辑名，便于 Skills 风格 front matter；磁盘文件恒为 EXPERIENCE.md
        return "project-experience"

    @property
    def description(self) -> str:
        base = (self.summary or self.goal or self.name).replace("\n", " ").strip()
        return f"[{self.outcome}] {base}"[:180]


def render_lesson_md(lesson: Lesson) -> str:
    """按 Skills / SKILL.md 格式渲染（单文件，总结时整份覆盖）。"""
    desc = lesson.description.replace('"', "'")
    scripts_yaml = ", ".join(f'"{s}"' for s in lesson.scripts) if lesson.scripts else ""
    lines = [
        "---",
        f"name: {lesson.name}",
        f'id: {lesson.id}',
        f'description: "{desc}"',
        f"outcome: {lesson.outcome}",
        f'goal: "{(lesson.goal or "").replace(chr(34), chr(39))[:120]}"',
        f"created_at: {lesson.created_at}",
        f"updated_at: {lesson.updated_at}",
        f"project_id: {lesson.project_id}",
        "automation: hybrid",
        f"pipeline: {lesson.pipeline or 'scripts/pipeline.json'}",
    ]
    if scripts_yaml:
        lines.append(f"scripts: [{scripts_yaml}]")
    lines.extend(
        [
            "---",
            "",
            f"# 项目经验：{lesson.goal or lesson.summary or lesson.id}",
            "",
            f"> 结果：**{lesson.outcome}** · 创建：{lesson.created_at} · 更新：{lesson.updated_at}",
            "",
            "## 摘要",
            "",
            lesson.summary.strip() or "（无摘要）",
            "",
            "## 成功实现路径",
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
                or "（暂无；总结经验后会写入有序步骤。下次运行严格按此顺序：脚本取数 → AI 处理 → 脚本/AI 后续步骤…）"
            ),
            "",
            "## 可本地脚本步骤（清单）",
            "",
            (
                lesson.script_section.strip()
                or "（无；详见执行顺序。）"
            ),
            "",
            "## 需 AI 完成的步骤（清单）",
            "",
            (
                lesson.ai_section.strip()
                or "（无；详见执行顺序。）"
            ),
            "",
            "## 运行环境",
            "",
            (lesson.environment.strip() or "（未记录）"),
            "",
            "## 注意事项",
            "",
            (lesson.notes.strip() or lesson.errors.strip() or "（无特殊注意点）"),
            "",
        ]
    )
    if lesson.artifacts.strip():
        lines.extend(["## 产物", "", lesson.artifacts.strip(), ""])
    if lesson.errors.strip() and lesson.outcome != "success":
        lines.extend(["## 错误与失败原因", "", lesson.errors.strip(), ""])
    lines.extend(
        [
            "## 复用建议",
            "",
            "- 再次运行：**先读本文件「执行顺序」**，按 `scripts/pipeline.json` 的 `steps` 交错执行脚本与 AI。",
            "- 不要并行跑完所有脚本；AI 只在顺序指定的步骤介入。",
            "- `scripts/` **不参与归档**；归档后脚本与经验仍保留。",
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
    parts = [
        f"- OS: {platform.system()} {platform.release()} ({platform.machine()})",
        f"- Python: {platform.python_version()}",
        f"- 模型: {model or '（未知）'}",
        f"- 审核策略: {policy or '（未知）'}",
        f"- 项目目录: {project_root or '（未知）'}",
        "- 能力: 联网工具(web_search/http_get/http_request) + 本机 execute + Skills/MCP（若已配置）",
    ]
    if extra.strip():
        parts.append(extra.strip())
    return "\n".join(parts)


class LessonStore:
    """每项目仅一份 `memory/EXPERIENCE.md`；总结时覆盖更新。"""

    def __init__(self, project_root: Path):
        self.root = Path(project_root)
        ensure_project_layout(self.root)
        self.memory = memory_dir(self.root)
        self.memory.mkdir(parents=True, exist_ok=True)
        # 兼容旧目录（只读迁移，不再写入多版本）
        self.experiences_dir = self.memory / "experiences"
        self._maybe_migrate_legacy()

    @property
    def experience_path(self) -> Path:
        return self.memory / EXPERIENCE_FILENAME

    @property
    def index_path(self) -> Path:
        # 兼容旧引用：现与单文件相同
        return self.experience_path

    def _maybe_migrate_legacy(self) -> None:
        """若仅有旧版 exp_*.md，将最新一份迁移为 EXPERIENCE.md。"""
        if self.experience_path.exists():
            return
        if not self.experiences_dir.exists():
            return
        legacy = sorted(
            self.experiences_dir.glob("exp_*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not legacy:
            # 旧索引
            old_idx = self.memory / "EXPERIENCES.md"
            if old_idx.exists() and old_idx.stat().st_size > 40:
                try:
                    safe_write_text(
                        self.experience_path,
                        old_idx.read_text(encoding="utf-8"),
                    )
                except OSError:
                    pass
            return
        try:
            safe_write_text(
                self.experience_path,
                legacy[0].read_text(encoding="utf-8"),
            )
        except OSError:
            pass

    def read_existing_id(self) -> str | None:
        path = self.experience_path
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.startswith("---"):
            return None
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        for line in parts[1].splitlines():
            if line.startswith("id:"):
                return line.split(":", 1)[1].strip() or None
        return None

    def read_created_at(self) -> str | None:
        path = self.experience_path
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not text.startswith("---"):
            return None
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        for line in parts[1].splitlines():
            if line.startswith("created_at:"):
                return line.split(":", 1)[1].strip() or None
        return None

    def save(self, lesson: Lesson) -> Path:
        """覆盖写入唯一经验文档；复用已有 id / created_at。"""
        existing_id = self.read_existing_id()
        if existing_id:
            lesson.id = existing_id
        existing_created = self.read_created_at()
        if existing_created:
            lesson.created_at = existing_created
        lesson.updated_at = _now()
        path = self.experience_path
        safe_write_text(path, render_lesson_md(lesson))
        return path

    def list_paths(self) -> list[Path]:
        """兼容旧接口：有经验则返回 [EXPERIENCE.md]。"""
        if self.is_empty():
            return []
        return [self.experience_path]

    def list_recent(self, limit: int = 20) -> list[Path]:
        return self.list_paths()[:limit]

    def is_empty(self) -> bool:
        path = self.experience_path
        if not path.exists():
            return True
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            return True
        if len(text) < 30:
            return True
        if "（暂无经验" in text and "## 摘要" not in text:
            return True
        return False

    def virtual_memory_paths(self, *, recent: int = 8) -> list[str]:
        paths = ["/memory/AGENTS.md"]
        if not self.is_empty():
            paths.append(f"/memory/{EXPERIENCE_FILENAME}")
        return paths

    def prompt_digest(self, *, limit: int = 5, max_chars: int = 3500) -> str:
        if self.is_empty():
            return ""
        try:
            text = self.experience_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
        if len(text) > max_chars:
            text = text[:max_chars] + "\n…(截断，详见 memory/EXPERIENCE.md)"
        return (
            "【项目经验记忆】以下来自 memory/EXPERIENCE.md（唯一经验文档），"
            "请优先复用成功路径与本地脚本，并规避注意事项：\n\n"
            + text
        )

    def rebuild_index(self) -> None:
        """兼容旧调用：若尚无经验则写入占位说明。"""
        if not self.is_empty():
            return
        placeholder = (
            "---\n"
            "name: project-experience\n"
            'description: "项目经验（尚未总结）"\n'
            "---\n\n"
            "# 项目经验\n\n"
            "（暂无经验。完成运行后，首次会自动总结；之后请点击「总结经验」覆盖更新本文件。）\n"
        )
        safe_write_text(self.experience_path, placeholder)

    def open_in_browser(self) -> bool:
        """用系统默认浏览器打开经验文档（HTML 预览，避免 .md 被当成下载）。"""
        path = self.experience_path
        if self.is_empty():
            return False
        try:
            md = path.read_text(encoding="utf-8")
        except OSError:
            return False
        view = self.memory / EXPERIENCE_VIEW_HTML
        body = html.escape(md)
        page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>项目经验</title>
  <style>
    body {{ font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
           max-width: 880px; margin: 2rem auto; padding: 0 1.25rem; line-height: 1.65;
           color: #1a1a1a; background: #fafafa; }}
    h1 {{ font-size: 1.35rem; }}
    .meta {{ color: #666; font-size: 0.9rem; margin-bottom: 1rem; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #fff;
           border: 1px solid #e5e5e5; border-radius: 8px; padding: 1rem 1.1rem;
           font-size: 13px; }}
  </style>
</head>
<body>
  <h1>项目经验</h1>
  <p class="meta">源文件：{html.escape(str(path))}（浏览器预览；总结经验时会覆盖更新此文档）</p>
  <pre>{body}</pre>
</body>
</html>
"""
        try:
            safe_write_text(view, page)
            webbrowser.open(view.resolve().as_uri())
            return True
        except OSError:
            return False

    @staticmethod
    def _peek_meta(path: Path) -> tuple[str, str, str]:
        outcome, created, summary = "?", "", path.stem
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return outcome, created, summary
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                fm = parts[1]
                for line in fm.splitlines():
                    if line.startswith("outcome:"):
                        outcome = line.split(":", 1)[1].strip()
                    elif line.startswith("updated_at:"):
                        created = line.split(":", 1)[1].strip()
                    elif line.startswith("created_at:") and not created:
                        created = line.split(":", 1)[1].strip()
                    elif line.startswith("description:"):
                        summary = line.split(":", 1)[1].strip().strip('"')
        return outcome, created, summary


def build_success_path_from_timeline_events(
    events: list,
    *,
    limit: int = 40,
) -> tuple[str, str, str]:
    """从时间线事件提炼 (success_path, summary, errors)。"""
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
            for prefix in ("⟶ 调用工具：", "⟵ "):
                if line.startswith(prefix):
                    line = line[len(prefix) :].strip()
                    break
            if len(line) > 280:
                line = line[:280] + "…"
            steps.append(f"{len(steps) + 1}. {line}")
        elif kind == "agent":
            agent_bits.append(content[:500])
        elif kind == "error":
            errors.append(content[:400])
        elif kind == "info" and ("失败" in content or "取消" in content):
            errors.append(content[:400])

    success_path = "\n".join(steps) if steps else ""
    summary = ""
    if agent_bits:
        summary = agent_bits[-1][:800]
    elif steps:
        summary = f"共记录 {len(steps)} 个工具相关步骤。"
    err_text = "\n".join(errors[-5:]) if errors else ""
    return success_path, summary, err_text
