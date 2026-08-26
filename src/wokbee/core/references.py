"""参考材料 references/：保存可复用的外部材料，归档不清理。

用途：
- 第三方代码/脚本、登录与密钥配置、环境参数。
- 「本次用到的全局 Skills」快照（照原样复制到 references/skills/<name>/）。
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from tokbee.core.safe_io import safe_write_text

from wokbee.core.paths import archives_dir, references_dir

_SKILL_NAME_SAFE = re.compile(r"[^\w\-]+")


def _safe_folder_name(name: str) -> str:
    s = _SKILL_NAME_SAFE.sub("_", (name or "").strip()).strip("_")
    return s or "reference"


def snapshot_used_skills(
    project_root: Path,
    skill_names: list[str] | None,
    *,
    skills_store: Any = None,
) -> list[Path]:
    """把用到的全局 Skills 整目录复制进 references/skills/<folder>/。

    skill_names 既接受 frontmatter 的 name，也接受 skills 根下的文件夹名。
    返回成功写入的目标路径列表；幂等，重复调用直接覆盖合并。
    """
    names = [str(n).strip() for n in (skill_names or []) if str(n).strip()]
    if not names:
        return []

    if skills_store is None:
        from wokbee.core.skills_store import SkillsStore

        skills_store = SkillsStore()
    skills_root = Path(skills_store.root)
    if not skills_root.exists():
        return []

    # 建立「请求名 → 真实文件夹」映射：优先文件夹名，其次 frontmatter 的 name
    folder_by_name: dict[str, str] = {}
    try:
        for child in sorted(skills_root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if not (child / "SKILL.md").exists():
                continue
            folder_by_name[child.name] = child.name
            # 读取 frontmatter name，作为别名
            try:
                from wokbee.core.skills_store import _parse_skill_md

                nm, _ = _parse_skill_md(child / "SKILL.md")
                if nm and nm not in folder_by_name:
                    folder_by_name[nm] = child.name
            except Exception:
                continue  # noqa: BLE001 模糊匹配失败不致命
    except OSError:
        pass

    dest_root = references_dir(project_root) / "skills"
    written: list[Path] = []
    for name in names:
        folder = folder_by_name.get(name, name)
        src = skills_root / folder
        # 路径穿越防御：只允许 skills_root 下一层的真实目录
        try:
            rel = src.resolve().relative_to(skills_root.resolve())
            if not rel.parts or any(p in ("..", ".") for p in rel.parts):
                continue
        except ValueError:
            continue
        if not src.is_dir() or not (src / "SKILL.md").exists():
            continue
        safe = _safe_folder_name(folder)
        target = dest_root / safe
        try:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(src, target)
            written.append(target)
        except OSError:
            continue
    return written


def write_reference_manifest(
    project_root: Path,
    *,
    used_skills: list[str] | None = None,
    materials: list[dict[str, Any]] | None = None,
    goal: str = "",
) -> Path | None:
    """重建 references/MANIFEST.md，登记用到的 Skills 与参考材料。

    仅当有内容（skills 或 materials 任一）时写入，避免每次总结都清空用户手动登记。
    返回写入的路径；无内容返回 None。
    """
    skills = [str(s).strip() for s in (used_skills or []) if str(s).strip()]
    mats = [
        m
        for m in (materials or [])
        if isinstance(m, dict) and (str(m.get("path") or "").strip() or str(m.get("note") or "").strip())
    ]
    if not skills and not mats:
        return None

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# references/ 参考材料清单",
        "",
        f"> 由经验总结自动生成 · {now} · 供下次稳定复跑使用。归档时本目录不会被清理。",
        "",
    ]
    if goal:
        lines.append(f"- 项目目标：{(goal or '').strip()[:200]}")
        lines.append("")
    if skills:
        lines.append("## 本次使用的全局 Skills（快照见 `references/skills/`）")
        for s in skills:
            lines.append(f"- `{s}`")
        lines.append("")
    if mats:
        lines.append("## 参考材料（第三方代码/登录/环境参数等）")
        for m in mats:
            path = str(m.get("path") or "").strip()
            note = str(m.get("note") or "").strip()
            if path and note:
                lines.append(f"- `{path}` — {note}")
            elif path:
                lines.append(f"- `{path}`")
            elif note:
                lines.append(f"- {note}")
        lines.append("")
    lines.extend(
        [
            "## 复跑提示",
            "1. 先读本清单，确认 references/ 下材料齐全。",
            "2. 敏感信息（登录/密钥）仅供本机使用，切勿外发。",
            "3. 需要时把第三方代码/配置直接复用，避免重新摸索。",
        ]
    )
    ref_dir = references_dir(project_root)
    ref_dir.mkdir(parents=True, exist_ok=True)
    out = ref_dir / "MANIFEST.md"
    safe_write_text(out, "\n".join(lines) + "\n")
    return out


def quarantine_obsolete_skill_snapshots(
    project_root: Path,
    *,
    used_skills: list[str],
    lesson_id: str = "",
) -> tuple[list[str], Path]:
    """把 references/skills/ 下不在 used_skills 里的快照目录移入 archives/discard_<ts>/skills/。

    只动 references/skills/ 下的目录；references/ 根下的材料文件、MANIFEST.md、README.txt 一律不动。
    返回 (已移走目录名列表, 目标目录)。可逆：不删除，只是搬到归档下。
    """
    keep = {str(s).strip() for s in (used_skills or []) if str(s).strip()}
    skill_root = references_dir(project_root) / "skills"
    if not skill_root.exists():
        return [], Path()

    moved: list[str] = []
    dest: Path | None = None
    for child in sorted(skill_root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name in keep or child.name.startswith("."):
            continue
        if dest is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = archives_dir(project_root) / f"discard_{stamp}" / "skills"
            dest.mkdir(parents=True, exist_ok=True)
        try:
            target = dest / child.name
            shutil.move(str(child), str(target))
            moved.append(child.name)
        except OSError:
            continue
    return moved, dest or Path()


def count_reference_files(project_root: Path, limit: int = 8) -> list[str]:
    """references/ 下的材料文件名（忽略 README / MANIFEST。）。"""
    folder = references_dir(project_root)
    if not folder.exists():
        return []
    names: list[str] = []
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        if p.name.lower() in ("readme.txt", "readme.md", "manifest.md"):
            continue
        rel = p.relative_to(folder)
        names.append(rel.as_posix())
        if len(names) >= limit:
            return names
    return names
