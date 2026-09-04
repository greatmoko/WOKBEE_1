"""项目存储：工作区根目录下按 project_id 建专属文件夹。"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from datetime import datetime
from pathlib import Path

from tokbee.core.safe_io import safe_write_json, safe_write_text

from wokbee.core.models import (
    ApprovalFlags,
    Project,
    ProjectEvent,
    ProjectStatus,
    new_project_id,
    MAX_PROJECT_TITLE_LEN,
    _now,
)
from wokbee.core.paths import (
    ARCHIVABLE_DIRS,
    archives_dir,
    ensure_project_layout,
    events_path,
    meta_path,
    project_dir,
)
from wokbee.core.settings import WokBeeSettings

logger = logging.getLogger("wokbee")

# 跨线程（并行工具调用）保护 project.json 的读改写，避免字段互相覆盖
_META_LOCK = threading.RLock()

# events.jsonl 行缓存（不做 json 解析，供时间线增量读取），append 时追加、清空时失效
_EVENTS_LOCK = threading.RLock()
_event_lines_cache: dict[str, list[str]] = {}

# 回收站保留天数（超时永久删除）
TRASH_RETENTION_DAYS = 7
_TRASHED_AT_NAME = "_trashed_at"

# 每个项目 archives/ 下最多保留的存档份数（超出删除最旧）
MAX_ARCHIVES = 50


def _clean_text(value: str | None) -> str:
    """去掉 NUL 等脏字符，避免标题/目标异常。"""
    return (value or "").replace("\x00", "").strip()


def _normalize_title(value: str | None) -> str:
    """项目名称：去脏字符并截断到上限。"""
    title = _clean_text(value)
    if len(title) > MAX_PROJECT_TITLE_LEN:
        title = title[:MAX_PROJECT_TITLE_LEN]
    return title


class ProjectStore:
    """管理 WokBee 项目生命周期与磁盘布局。"""

    def __init__(self, settings: WokBeeSettings | None = None):
        self.settings = settings or WokBeeSettings()
        self._ensure_workspace()

    def _ensure_workspace(self) -> None:
        root = self.settings.workspace_root
        root.mkdir(parents=True, exist_ok=True)
        index = root / "_index.json"
        if not index.exists():
            safe_write_json(index, {"version": 1, "projects": []})
        # 启动时顺带清理过期回收站
        self.purge_expired_trash()

    def trash_root(self) -> Path:
        path = self.workspace_root / "_trash"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def purge_expired_trash(self, *, max_days: int = TRASH_RETENTION_DAYS) -> int:
        """删除回收站中超过保留期的项目目录，返回删除数量。"""
        days = max(1, int(max_days or TRASH_RETENTION_DAYS))
        trash = self.workspace_root / "_trash"
        if not trash.exists():
            return 0
        cutoff = datetime.now().timestamp() - days * 86400
        removed = 0
        try:
            children = list(trash.iterdir())
        except OSError as e:
            logger.warning("读取回收站失败: %s", e)
            return 0
        for child in children:
            if not child.is_dir():
                continue
            try:
                trashed_at = self._trash_entry_time(child)
                if trashed_at is None or trashed_at > cutoff:
                    continue
                shutil.rmtree(child, ignore_errors=True)
                if not child.exists():
                    removed += 1
                    logger.info("已清理过期回收站项目：%s", child.name)
            except OSError as e:
                logger.warning("清理回收站项目失败 %s: %s", child, e)
        return removed

    @staticmethod
    def _trash_entry_time(entry: Path) -> float | None:
        """回收时间戳：优先读 _trashed_at，否则用目录 mtime。"""
        marker = entry / _TRASHED_AT_NAME
        if marker.exists():
            try:
                raw = marker.read_text(encoding="utf-8").strip()
                if raw.isdigit():
                    return float(raw)
                return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp()
            except (OSError, ValueError):
                pass
        try:
            return entry.stat().st_mtime
        except OSError:
            return None

    def _mark_trashed(self, dest: Path) -> None:
        try:
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            (dest / _TRASHED_AT_NAME).write_text(stamp, encoding="utf-8")
        except OSError as e:
            logger.warning("写入回收时间失败 %s: %s", dest, e)

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
        pinned = sorted(
            [p for p in projects if p.pinned],
            key=lambda p: p.pinned_at or p.created_at,
            reverse=True,
        )
        normal = sorted(
            [p for p in projects if not p.pinned],
            key=lambda p: p.created_at,
            reverse=True,
        )
        return pinned + normal

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
        with _META_LOCK:
            project.title = _normalize_title(project.title) or "未命名项目"
            project.goal = _clean_text(project.goal)
            project.touch()
            root = self.path_for(project.id)
            ensure_project_layout(root)
            safe_write_json(meta_path(root), project.to_dict())
            # 只更新本项目索引条目，避免每次保存/事件都全量扫描全部项目（O(n²) 卡顿）
            self._touch_index_entry(project)

    def patch(self, project_id: str, **fields) -> Project | None:
        """原子更新若干字段：持锁下重新读盘再写，避免并行工具互相覆盖。"""
        with _META_LOCK:
            project = self.get(project_id)
            if not project:
                return None
            if "title" in fields:
                title = _normalize_title(fields.get("title"))
                if title:
                    project.title = title
            if "goal" in fields:
                project.goal = _clean_text(fields.get("goal"))
            if "approval" in fields and fields["approval"] is not None:
                project.approval = fields["approval"].copy()
            if "status" in fields and fields["status"] is not None:
                project.status = fields["status"]
            if "current_step" in fields and fields["current_step"] is not None:
                project.current_step = str(fields["current_step"])
            if "progress_done" in fields and fields["progress_done"] is not None:
                project.progress_done = int(fields["progress_done"])
            if "progress_total" in fields and fields["progress_total"] is not None:
                project.progress_total = int(fields["progress_total"])
            if "artifacts_summary" in fields and fields["artifacts_summary"] is not None:
                project.artifacts_summary = str(fields["artifacts_summary"])
            if "provider" in fields and fields["provider"] is not None:
                project.provider = str(fields["provider"])
            if "model_id" in fields and fields["model_id"] is not None:
                project.model_id = str(fields["model_id"])
            if "pinned" in fields and fields["pinned"] is not None:
                new_pinned = bool(fields["pinned"])
                if new_pinned and not project.pinned:
                    project.pinned_at = _now()
                elif not new_pinned:
                    project.pinned_at = ""
                project.pinned = new_pinned
            if "pinned_at" in fields and fields["pinned_at"] is not None:
                project.pinned_at = str(fields["pinned_at"])
            # 直接写盘（已在锁内，勿再进 save 的同锁递归也可，RLock 安全）
            self.save(project)
            return project

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
        # 新建项目：优先厂商设置「默认模型」（用户在模型旁点的「默认」），再回退 WokBee 设置
        provider = ""
        model_id = ""
        display = ""
        try:
            from tokbee.core.provider_store import ProviderStore

            default = ProviderStore().resolve_default()
            if default:
                provider = default.provider_id
                model_id = default.model_id
                display = f"{default.provider_name} / {default.model_id}"
        except Exception:
            logger.exception("读取厂商默认模型失败")
        if not (provider and model_id):
            provider = (self.settings.default_provider or "").strip()
            model_id = (self.settings.default_model_id or "").strip()
            if provider and model_id:
                display = f"{provider}/{model_id}"
        project = Project(
            id=pid,
            title=_normalize_title(title) or "未命名项目",
            goal=_clean_text(goal),
            approval=flags,
            provider=provider,
            model_id=model_id,
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
                    + (
                        f"\n默认模型：{display or f'{provider}/{model_id}'}"
                        if provider and model_id
                        else "\n未绑定模型：将使用厂商默认或列表第一个可用模型。"
                    )
                ),
            ),
        )
        return project

    def rename(self, project_id: str, new_title: str) -> Project | None:
        return self.patch(project_id, title=new_title)

    def update_goal(self, project_id: str, goal: str) -> Project | None:
        return self.patch(project_id, goal=goal)

    def set_approval(self, project_id: str, approval: ApprovalFlags) -> Project | None:
        return self.patch(project_id, approval=approval)

    def set_status(
        self,
        project_id: str,
        status: ProjectStatus,
        *,
        current_step: str | None = None,
        progress_done: int | None = None,
        progress_total: int | None = None,
    ) -> Project | None:
        fields: dict = {"status": status}
        if current_step is not None:
            fields["current_step"] = current_step
        if progress_done is not None:
            fields["progress_done"] = progress_done
        if progress_total is not None:
            fields["progress_total"] = progress_total
        return self.patch(project_id, **fields)

    def toggle_pin(self, project_id: str) -> Project | None:
        """切换置顶；置顶项目不可删除，防止误操作。"""
        project = self.get(project_id)
        if not project:
            return None
        return self.patch(project_id, pinned=not project.pinned)

    def delete(self, project_id: str, *, trash: bool = True) -> bool:
        project = self.get(project_id)
        if project and project.pinned:
            logger.info("拒绝删除置顶项目：%s", project_id)
            return False
        if project and project.status in (
            ProjectStatus.RUNNING, ProjectStatus.AWAITING_APPROVAL,
        ):
            logger.info("拒绝删除运行中项目：%s", project_id)
            return False
        root = self.path_for(project_id)
        if not root.exists():
            return False
        if trash:
            trash_root = self.trash_root()
            dest = trash_root / project_id
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(root), str(dest))
            self._mark_trashed(dest)
        else:
            shutil.rmtree(root, ignore_errors=True)
        self._rebuild_index()
        # 删除后顺带清一次过期项
        self.purge_expired_trash()
        return True

    def delete_unpinned(self, *, trash: bool = True) -> int:
        """删除全部未置顶项目；置顶与运行中项目跳过。单条逻辑与 ``delete`` / 侧栏删除一致。"""
        ids = [p.id for p in self.list_projects() if not p.pinned]
        deleted = 0
        for pid in ids:
            if self.delete(pid, trash=trash):
                deleted += 1
        return deleted

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
        # 追加写入后同步行缓存（避免下次读再全量重读）
        with _EVENTS_LOCK:
            cached = _event_lines_cache.get(project_id)
            if cached is not None:
                cached.append(line.rstrip("\n"))
        # 触碰更新时间（持锁重读，避免覆盖并行中的 title/goal 写入）
        with _META_LOCK:
            project = self.get(project_id)
            if project:
                self.save(project)

    def _event_lines(self, project_id: str) -> list[str]:
        """读取项目的 events.jsonl 行列表（不做 json 解析），按 project_id 缓存。"""
        with _EVENTS_LOCK:
            cached = _event_lines_cache.get(project_id)
            if cached is not None:
                return cached
        path = events_path(self.path_for(project_id))
        lines: list[str] = []
        if path.exists():
            try:
                with path.open("r", encoding="utf-8") as f:
                    lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            except OSError:
                lines = []
        with _EVENTS_LOCK:
            _event_lines_cache[project_id] = lines
        return lines

    def events_window(
        self,
        project_id: str,
        *,
        skip_from_end: int = 0,
        count: int = 50,
    ) -> tuple[list[ProjectEvent], int]:
        """从文件尾部往前取一段事件（oldest→newest），返回 (events, older_remaining)。

        skip_from_end = 已从尾部加载的事件数；count = 本次再往前取多少条。
        只对窗口内的行做 json 解析，避免每次切换项目全量解析历史。
        """
        lines = self._event_lines(project_id)
        total = len(lines)
        end = max(0, total - max(0, int(skip_from_end)))
        start = max(0, end - max(0, int(count)))
        events: list[ProjectEvent] = []
        for line in lines[start:end]:
            try:
                events.append(ProjectEvent.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return events, start

    def list_events(self, project_id: str, limit: int = 500) -> list[ProjectEvent]:
        lines = self._event_lines(project_id)
        if limit > 0 and len(lines) > limit:
            lines = lines[-limit:]
        events: list[ProjectEvent] = []
        for line in lines:
            try:
                events.append(ProjectEvent.from_dict(json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return events

    def clear_events(self, project_id: str) -> None:
        path = events_path(self.path_for(project_id))
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_write_text(path, "")
        with _EVENTS_LOCK:
            _event_lines_cache.pop(project_id, None)

    def needs_auto_archive(self, project_id: str) -> bool:
        """上一轮是否有可归档内容（避免首次空跑也产生空存档）。"""
        events = self.list_events(project_id)
        if any(
            e.kind in ("user", "agent", "tool", "error", "lesson", "approval")
            for e in events
        ):
            return True
        root = self.path_for(project_id)
        skip = {"archives", "scripts", "memory", "references", "uploads"}
        for name in ARCHIVABLE_DIRS:
            if name in skip:
                continue
            folder = root / name
            if not folder.exists():
                continue
            try:
                for p in folder.rglob("*"):
                    if not p.is_file():
                        continue
                    if p.name.lower() in ("readme.txt", "readme.md"):
                        continue
                    return True
            except OSError:
                continue
        return False

    def prune_archives(self, project_id: str, *, keep: int = MAX_ARCHIVES) -> int:
        """删除最旧存档，使 arch_* 目录不超过 keep 份。返回删除数量。"""
        root = archives_dir(self.path_for(project_id))
        if not root.exists():
            return 0
        archives = [
            p for p in root.iterdir()
            if p.is_dir() and p.name.startswith("arch_")
        ]
        archives.sort(key=lambda p: p.name)
        removed = 0
        while len(archives) > max(1, int(keep)):
            oldest = archives.pop(0)
            try:
                shutil.rmtree(oldest, ignore_errors=False)
                removed += 1
            except OSError as e:
                logger.warning("删除旧存档失败 %s: %s", oldest, e)
                break
        return removed

    def archive_session(
        self,
        project_id: str,
        *,
        include_memory: bool = False,
        reason: str = "manual",
    ) -> Path | None:
        """将运行记录、工作区、交付物一并归档到 archives/，并清空会话态。

        默认保留：project.json、memory/（经验）、scripts/、uploads/、references/。
        include_memory=True（清空经验）时：连同 memory/ 与 scripts/ 一并归档并清空。
        不会把 archives/ 自身再归档进去；用户上传 uploads/ 始终保留。
        存档超过 MAX_ARCHIVES 份时自动删除最旧。
        """
        project = self.get(project_id)
        if not project:
            return None
        root = self.path_for(project_id)
        ensure_project_layout(root)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = archives_dir(root) / f"arch_{stamp}"
        # 同秒冲突时追加序号
        if dest.exists():
            n = 1
            while True:
                alt = archives_dir(root) / f"arch_{stamp}_{n}"
                if not alt.exists():
                    dest = alt
                    break
                n += 1
        dest.mkdir(parents=True, exist_ok=False)

        moved: list[str] = []
        for name in ARCHIVABLE_DIRS:
            # 明确跳过长期保留目录，防止误归档
            if name in ("archives", "scripts", "memory", "references", "uploads"):
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

        kept = ["project.json", "archives/", "uploads/", "references/"]
        if include_memory:
            for extra in ("memory", "scripts"):
                src = root / extra
                if not src.exists():
                    continue
                try:
                    shutil.copytree(src, dest / extra, dirs_exist_ok=True)
                    moved.append(extra)
                    self._empty_dir(src)
                except OSError as e:
                    logger.warning("归档复制 %s 失败: %s", extra, e)
        else:
            kept.insert(1, "memory/")
            kept.insert(2, "scripts/")

        mode_line = (
            "- 模式：清空经验（含 memory/、scripts/；保留 uploads/、references/）\n"
            if include_memory
            else (
                "- 模式：运行前自动归档（保留 memory/、scripts/、uploads/、references/）\n"
                if reason == "auto_before_run"
                else "- 模式：普通归档（保留 memory/、scripts/、uploads/、references/）\n"
            )
        )
        manifest = (
            f"# 归档 {stamp}\n\n"
            f"- 项目：{project.title} (`{project_id}`)\n"
            f"- 目标：{project.goal or '（无）'}\n"
            f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- 已归档目录：{', '.join(moved) or '（空）'}\n"
            f"- 保留未归档：{', '.join(f'`{x}`' for x in kept)}\n"
            f"{mode_line}"
        )
        safe_write_text(dest / "MANIFEST.md", manifest)

        # 重建空布局
        ensure_project_layout(root)
        self.clear_events(project_id)

        project.status = ProjectStatus.IDLE
        project.current_step = "已归档" if not include_memory else "已清空经验"
        project.progress_done = 0
        project.progress_total = 0
        project.artifacts_summary = ""
        self.save(project)

        pruned = self.prune_archives(project_id, keep=MAX_ARCHIVES)
        prune_note = (
            f"\n已清理旧存档 {pruned} 份（最多保留 {MAX_ARCHIVES} 份）。"
            if pruned
            else ""
        )

        if include_memory:
            notice = (
                f"已归档到 `archives/{dest.name}`"
                f"（含会话目录、memory 经验与 scripts）。\n"
                "对话、工作区、交付物、经验文档、本地脚本已清空；"
                "项目名称、目标、uploads/ 上传资料与 references/ 参考材料已保留。\n"
                "注意：后续 Agent 运行禁止访问 archives/。"
                f"{prune_note}"
            )
        elif reason == "auto_before_run":
            notice = (
                f"运行前已自动归档到 `archives/{dest.name}`"
                f"（保留经验、scripts/、uploads/ 与 references/）。{prune_note}"
            )
        else:
            notice = (
                f"已归档到 `archives/{dest.name}`"
                f"（含 runs / workspace / deliverables / artifacts）。\n"
                "对话与工作区、交付物已清空；"
                "目标、memory/experiences/、scripts/、uploads/、references/ 已保留，可直接再次运行。\n"
                "注意：后续 Agent 运行禁止访问 archives/。"
                f"{prune_note}"
            )
        self.append_event(
            project_id,
            ProjectEvent(
                kind="info",
                content=notice,
                meta={
                    "archive": str(dest),
                    "include_memory": include_memory,
                    "include_scripts": include_memory,
                    "reason": reason,
                    "pruned": pruned,
                },
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

    def _touch_index_entry(self, project: Project) -> None:
        """只更新本项目的索引条目（标题/时间），不扫描全部项目。

        常驻路径（每事件/每次保存）不再走 _rebuild_index 的 list_projects() O(n) 读盘；
        结构性增删（create 首现/delete 移除）仍由对应方法保证索引正确。
        """
        with _META_LOCK:
            path = self.workspace_root / "_index.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                data = None
            if not isinstance(data, dict):
                data = {"version": 1, "projects": []}
            projects = data.get("projects")
            if not isinstance(projects, list):
                projects = []
            entry = {"id": project.id, "title": project.title, "updated_at": project.updated_at}
            for i, e in enumerate(projects):
                if isinstance(e, dict) and e.get("id") == project.id:
                    projects[i] = entry  # 保序替换
                    break
            else:
                projects.append(entry)
            data["projects"] = projects
            try:
                safe_write_json(path, data)
            except OSError:
                logger.warning("写入索引失败: %s", project.id)

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
