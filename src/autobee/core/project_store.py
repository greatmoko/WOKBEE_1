"""项目存储：工作区根目录下按 project_id 建专属文件夹。"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from wokbee.core.safe_io import safe_write_json, safe_write_text

from autobee.core.models import (
    ApprovalFlags,
    Project,
    ProjectEvent,
    ProjectStatus,
    new_project_id,
)
from autobee.core.paths import (
    ARCHIVABLE_DIRS,
    archives_dir,
    ensure_project_layout,
    events_path,
    meta_path,
    project_dir,
)
from autobee.core.settings import AutoBeeSettings

logger = logging.getLogger("autobee")


class ProjectStore:
    """管理 AutoBee 项目生命周期与磁盘布局。"""

    def __init__(self, settings: AutoBeeSettings | None = None):
        self.settings = settings or AutoBeeSettings()
        self._ensure_workspace()

    def _ensure_workspace(self) -> None:
        root = self.settings.workspace_root
        root.mkdir(parents=True, exist_ok=True)
        index = root / "_index.json"
        if not index.exists():
            safe_write_json(index, {"version": 1, "projects": []})

    @property
    def workspace_root(self) -> Path:
        return self.settings.workspace_root

    def path_for(self, project_id: str) -> Path:
        return project_dir(self.workspace_root, project_id)

    def list_projects(self) -> list[Project]:
        root = self.workspace_root
        if not root.exists():
            return []
        projects: list[Project] = []
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            meta = meta_path(child)
            if not meta.exists():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                projects.append(Project.from_dict(data))
            except (json.JSONDecodeError, OSError, TypeError) as e:
                logger.warning("跳过损坏的项目元数据 %s: %s", meta, e)
        projects.sort(key=lambda p: p.updated_at, reverse=True)
        return projects

    def search(self, keyword: str) -> list[Project]:
        kw = (keyword or "").strip().lower()
        items = self.list_projects()
        if not kw:
            return items
        return [
            p for p in items
            if kw in p.title.lower() or kw in p.id.lower() or kw in (p.goal or "").lower()
        ]

    def get(self, project_id: str) -> Project | None:
        meta = meta_path(self.path_for(project_id))
        if not meta.exists():
            return None
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            return Project.from_dict(data)
        except (json.JSONDecodeError, OSError):
            return None

    def save(self, project: Project) -> None:
        project.touch()
        root = self.path_for(project.id)
        ensure_project_layout(root)
        safe_write_json(meta_path(root), project.to_dict())
        self._rebuild_index()

    def create(
        self,
        title: str = "",
        goal: str = "",
        *,
        approval: ApprovalFlags | None = None,
    ) -> Project:
        pid = new_project_id()
        # 新建时拷贝全局审核勾选，之后项目可独立修改
        flags = (approval or self.settings.approval).copy()
        project = Project(
            id=pid,
            title=(title or "").strip() or "未命名项目",
            goal=(goal or "").strip(),
            approval=flags,
            provider=self.settings.default_provider,
            model_id=self.settings.default_model_id,
            status=ProjectStatus.IDLE,
        )
        self.save(project)
        self.append_event(
            pid,
            ProjectEvent(
                kind="info",
                content=(
                    f"项目已创建。工作目录：{self.path_for(pid)}\n"
                    f"审核策略（继承自全局，可单独修改）：{flags.summary()}"
                ),
            ),
        )
        return project

    def rename(self, project_id: str, new_title: str) -> Project | None:
        project = self.get(project_id)
        if not project:
            return None
        project.title = (new_title or "").strip() or project.title
        self.save(project)
        return project

    def update_goal(self, project_id: str, goal: str) -> Project | None:
        project = self.get(project_id)
        if not project:
            return None
        project.goal = goal or ""
        self.save(project)
        return project

    def set_approval(self, project_id: str, approval: ApprovalFlags) -> Project | None:
        project = self.get(project_id)
        if not project:
            return None
        project.approval = approval.copy()
        self.save(project)
        return project

    def set_status(
        self,
        project_id: str,
        status: ProjectStatus,
        *,
        current_step: str | None = None,
        progress_done: int | None = None,
        progress_total: int | None = None,
    ) -> Project | None:
        project = self.get(project_id)
        if not project:
            return None
        project.status = status
        if current_step is not None:
            project.current_step = current_step
        if progress_done is not None:
            project.progress_done = progress_done
        if progress_total is not None:
            project.progress_total = progress_total
        self.save(project)
        return project

    def delete(self, project_id: str, *, trash: bool = True) -> bool:
        root = self.path_for(project_id)
        if not root.exists():
            return False
        if trash:
            trash_root = self.workspace_root / "_trash"
            trash_root.mkdir(parents=True, exist_ok=True)
            dest = trash_root / project_id
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(root), str(dest))
        else:
            shutil.rmtree(root, ignore_errors=True)
        self._rebuild_index()
        return True

    def append_event(self, project_id: str, event: ProjectEvent) -> None:
        root = self.path_for(project_id)
        ensure_project_layout(root)
        path = events_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        if path.exists():
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        else:
            safe_write_text(path, line)
        # 触碰更新时间
        project = self.get(project_id)
        if project:
            self.save(project)

    def list_events(self, project_id: str, limit: int = 500) -> list[ProjectEvent]:
        path = events_path(self.path_for(project_id))
        if not path.exists():
            return []
        events: list[ProjectEvent] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(ProjectEvent.from_dict(json.loads(line)))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            return []
        if limit > 0 and len(events) > limit:
            return events[-limit:]
        return events

    def clear_events(self, project_id: str) -> None:
        path = events_path(self.path_for(project_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_write_text(path, "")

    def archive_session(self, project_id: str) -> Path | None:
        """将运行记录、工作区、交付物、用户上传一并归档到 archives/，并清空会话态。

        保留：project.json、memory/（经验）、scripts/。
        不会把 archives/、scripts/、memory/ 自身再归档进去。
        """
        project = self.get(project_id)
        if not project:
            return None
        root = self.path_for(project_id)
        ensure_project_layout(root)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = archives_dir(root) / f"arch_{stamp}"
        dest.mkdir(parents=True, exist_ok=False)

        moved: list[str] = []
        for name in ARCHIVABLE_DIRS:
            # 明确跳过 archives / scripts / memory，防止误归档
            if name in ("archives", "scripts", "memory"):
                continue
            src = root / name
            if not src.exists():
                continue
            target = dest / name
            try:
                shutil.copytree(src, target, dirs_exist_ok=True)
                moved.append(name)
            except OSError as e:
                logger.warning("归档复制 %s 失败: %s", src, e)
                continue
            # 清空源目录内容（保留空目录）
            self._empty_dir(src)

        manifest = (
            f"# 归档 {stamp}\n\n"
            f"- 项目：{project.title} (`{project_id}`)\n"
            f"- 目标：{project.goal or '（无）'}\n"
            f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 已归档目录：{', '.join(moved) or '（空）'}\n"
            f"- 保留未归档：`project.json`、`memory/`、`scripts/`、`archives/`\n"
        )
        safe_write_text(dest / "MANIFEST.md", manifest)

        # 重建空布局
        ensure_project_layout(root)
        self.clear_events(project_id)

        project.status = ProjectStatus.IDLE
        project.current_step = "已归档"
        project.progress_done = 0
        project.progress_total = 0
        project.artifacts_summary = ""
        self.save(project)

        self.append_event(
            project_id,
            ProjectEvent(
                kind="info",
                content=(
                    f"已归档到 `archives/arch_{stamp}/`"
                    f"（含 runs / workspace / deliverables / uploads / artifacts）。\n"
                    "对话与工作区、交付物、上传文件已清空；"
                    "目标、memory/EXPERIENCE.md、scripts/ 已保留，可直接再次运行。"
                ),
                meta={"archive": str(dest)},
            ),
        )
        return dest

    @staticmethod
    def _empty_dir(path: Path) -> None:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            return
        for child in path.iterdir():
            try:
                if child.is_file() or child.is_symlink():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
            except OSError as e:
                logger.warning("清空 %s 失败: %s", child, e)

    def effective_approval(self, project: Project) -> ApprovalFlags:
        """项目自带审核策略（创建时已从全局拷贝，之后独立）。"""
        return project.approval.copy()

    def _rebuild_index(self) -> None:
        projects = self.list_projects()
        index = {
            "version": 1,
            "projects": [
                {"id": p.id, "title": p.title, "updated_at": p.updated_at}
                for p in projects
            ],
        }
        safe_write_json(self.workspace_root / "_index.json", index)
