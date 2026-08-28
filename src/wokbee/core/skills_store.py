"""全局 Skills 存储（~/.wokbee/skills/）。"""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from tokbee.core.config import Config, default_data_dir
from tokbee.core.safe_io import safe_write_text


def default_skills_root() -> Path:
    return default_data_dir() / "skills"


_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class SkillInfo:
    name: str
    path: Path
    description: str = ""
    enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "description": self.description,
            "enabled": self.enabled,
        }


def _parse_skill_md(path: Path) -> tuple[str, str]:
    """返回 (name, description)。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return path.parent.name, ""
    name = path.parent.name
    desc = ""
    m = _FRONT_MATTER.match(text)
    if m:
        for line in m.group(1).splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip().strip("\"'")
            if k == "name" and v:
                name = v
            elif k in ("description", "desc") and v:
                desc = v
    if not desc:
        # 取正文第一行非空
        body = _FRONT_MATTER.sub("", text).strip()
        for line in body.splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                desc = line[:120]
                break
    return name, desc


class SkillsStore:
    def __init__(self, config: Config | None = None):
        self._config = config or Config()
        self._migrate_legacy_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_example()

    def _migrate_legacy_root(self) -> None:
        """旧路径 …/wokbee/skills 或 ~/.tokbee/skills → ~/.wokbee/skills。"""
        if str(self._config.get("wokbee.skills_root") or "").strip():
            return
        target = default_skills_root()
        if any(target.iterdir()) if target.exists() else False:
            return
        candidates = [
            default_data_dir() / "wokbee" / "skills",
            Path.home() / ".tokbee" / "skills",
            Path.home() / ".tokbee" / "wokbee" / "skills",
            Path.home() / ".wokbee" / "wokbee" / "skills",
        ]
        for legacy in candidates:
            if not legacy.exists() or legacy.resolve() == target.resolve():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    for child in legacy.iterdir():
                        dest = target / child.name
                        if not dest.exists():
                            shutil.move(str(child), str(dest))
                else:
                    shutil.move(str(legacy), str(target))
                return
            except OSError:
                try:
                    shutil.copytree(legacy, target, dirs_exist_ok=True)
                    return
                except OSError:
                    continue

    @property
    def root(self) -> Path:
        raw = self._config.get("wokbee.skills_root") or ""
        if str(raw).strip():
            return Path(str(raw)).expanduser()
        return default_skills_root()

    def set_root(self, path: str | Path) -> None:
        self._config.set("wokbee.skills_root", str(path))
        self._config.save()
        Path(path).mkdir(parents=True, exist_ok=True)

    def _enabled_map(self) -> dict[str, bool]:
        raw = self._config.get("wokbee.skills_enabled")
        if isinstance(raw, dict):
            return {str(k): bool(v) for k, v in raw.items()}
        return {}

    def _save_enabled_map(self, data: dict[str, bool]) -> None:
        self._config.set("wokbee.skills_enabled", data)
        self._config.save()

    def _ensure_example(self) -> None:
        sample = self.root / "research-brief"
        skill_md = sample / "SKILL.md"
        if skill_md.exists():
            return
        sample.mkdir(parents=True, exist_ok=True)
        safe_write_text(
            skill_md,
            """---
name: research-brief
description: 联网调研并输出结构化简报的工作流技能
---

# Research Brief

当用户需要调研、汇总外部信息时：

1. 用 `web_search` 检索关键词
2. 用 `http_get` 打开权威来源核对
3. 把结论与链接写入 `artifacts/` 下的 Markdown
4. 明确区分「已核实事实」与「推断」
""",
        )

    def list_skills(self) -> list[SkillInfo]:
        enabled_map = self._enabled_map()
        items: list[SkillInfo] = []
        if not self.root.exists():
            return items
        for child in sorted(self.root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            md = child / "SKILL.md"
            if not md.exists():
                continue
            name, desc = _parse_skill_md(md)
            folder = child.name
            enabled = enabled_map.get(folder, True)
            items.append(
                SkillInfo(name=name, path=child, description=desc, enabled=enabled)
            )
        return items

    def list_enabled(self) -> list[SkillInfo]:
        return [s for s in self.list_skills() if s.enabled]

    def set_enabled(self, folder_name: str, enabled: bool) -> None:
        data = self._enabled_map()
        data[folder_name] = bool(enabled)
        self._save_enabled_map(data)

    def create(self, folder_name: str, description: str = "") -> SkillInfo:
        safe = re.sub(r"[^\w\-]+", "-", folder_name.strip()) or f"skill-{uuid.uuid4().hex[:6]}"
        safe = safe.strip("-").lower()
        dest = self.root / safe
        dest.mkdir(parents=True, exist_ok=True)
        md = dest / "SKILL.md"
        if not md.exists():
            safe_write_text(
                md,
                f"""---
name: {safe}
description: {description or safe}
---

# {safe}

在此编写技能说明与操作步骤，Agent 会按需读取并遵循。
""",
            )
        data = self._enabled_map()
        data[safe] = True
        self._save_enabled_map(data)
        name, desc = _parse_skill_md(md)
        return SkillInfo(name=name, path=dest, description=desc, enabled=True)

    def delete(self, folder_name: str) -> bool:
        dest = self.root / folder_name
        if not dest.exists():
            return False
        shutil.rmtree(dest, ignore_errors=True)
        data = self._enabled_map()
        data.pop(folder_name, None)
        self._save_enabled_map(data)
        return True

    def global_skills_paths(self) -> list[str]:
        """供 create_deep_agent(skills=...)：通过 CompositeBackend 挂载全局目录。"""
        if self.list_enabled():
            return ["/skills/"]
        return []

    def cleanup_project_copies(self, project_root: Path) -> None:
        """清理历史上同步进项目的 /skills 副本（带同步标记的）。"""
        dest_root = Path(project_root) / "skills"
        markers = (
            dest_root / ".wokbee_synced",
            dest_root / ".autobee_synced",
        )
        if not any(m.exists() for m in markers):
            return
        try:
            for child in list(dest_root.iterdir()):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
            for m in markers:
                m.unlink(missing_ok=True)
        except OSError:
            pass
