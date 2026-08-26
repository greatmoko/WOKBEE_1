"""项目磁盘目录套约。"""

from __future__ import annotations

from pathlib import Path

# 每个项目目录下的标准子目录
# deliverables：项目交付物（归档时一并归档并清空）
# uploads：用户上传文件（Agent 可读取；归档时一并归档并清空）
# archives：归档快照（自身不再被归档）
# scripts：可本地复用脚本（不参与归档）
# references：可复用外部材料（第三方代码/登录/环境参数/用到的 Skills 快照；不参与归档）
PROJECT_SUBDIRS = (
    "memory",
    "artifacts",  # 兼容旧路径；新交付请写入 deliverables/
    "deliverables",
    "uploads",
    "lessons",
    "runs",
    "workspace",
    "archives",
    "scripts",
    "references",
)

PROJECT_META = "project.json"
EVENTS_REL = "runs/events.jsonl"
ARCHIVES_DIR = "archives"
SCRIPTS_DIR = "scripts"
DELIVERABLES_DIR = "deliverables"
UPLOADS_DIR = "uploads"
REFERENCES_DIR = "references"

# 归档时复制后清空（不含 archives / memory / scripts / references）
ARCHIVABLE_DIRS = (
    "runs",
    "workspace",
    "artifacts",
    "deliverables",
    "uploads",
)


def project_dir(workspace_root: Path, project_id: str) -> Path:
    return workspace_root / project_id


def ensure_project_layout(root: Path) -> None:
    """创建项目根目录及标准子目录。"""
    root.mkdir(parents=True, exist_ok=True)
    for name in PROJECT_SUBDIRS:
        (root / name).mkdir(parents=True, exist_ok=True)
    # 兼容：旧 artifacts 有内容而 deliverables 为空时，提示性 README
    deliverables = root / DELIVERABLES_DIR
    readme = deliverables / "README.txt"
    if not readme.exists():
        try:
            readme.write_text(
                "本目录存放项目交付物（最终成果）。\n"
                "归档时会随本次会话一并归档并清空。\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    uploads = root / UPLOADS_DIR
    ureadme = uploads / "README.txt"
    if not ureadme.exists():
        try:
            ureadme.write_text(
                "本目录存放用户上传的文件，Agent 可直接读取调用。\n"
                "归档时会随本次会话一并归档并清空。\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    references = root / REFERENCES_DIR
    rreadme = references / "README.txt"
    if not rreadme.exists():
        try:
            rreadme.write_text(
                "本目录保存可复用的参考材料：第三方代码/脚本、登录与密钥配置、环境参数、"
                "以及本次用到的全局 Skills 快照。\n"
                "用于保证复杂任务下次能稳定复跑。\n"
                "归档时**不会**归档本目录，会长期保留。\n"
                "注意：登录/密钥等敏感信息仅供本机复跑，切勿外发。\n",
                encoding="utf-8",
            )
        except OSError:
            pass


def meta_path(project_root: Path) -> Path:
    return project_root / PROJECT_META


def events_path(project_root: Path) -> Path:
    return project_root / EVENTS_REL


def artifacts_dir(project_root: Path) -> Path:
    """兼容旧名；新代码请优先用 deliverables_dir。"""
    return project_root / "artifacts"


def deliverables_dir(project_root: Path) -> Path:
    return project_root / DELIVERABLES_DIR


def uploads_dir(project_root: Path) -> Path:
    return project_root / UPLOADS_DIR


def memory_dir(project_root: Path) -> Path:
    return project_root / "memory"


def workspace_sandbox(project_root: Path) -> Path:
    return project_root / "workspace"


def archives_dir(project_root: Path) -> Path:
    return project_root / ARCHIVES_DIR


def scripts_dir(project_root: Path) -> Path:
    return project_root / SCRIPTS_DIR


def references_dir(project_root: Path) -> Path:
    return project_root / REFERENCES_DIR


def list_deliverable_names(project_root: Path, limit: int = 8) -> list[str]:
    """交付物文件名（忽略 README）。"""
    names: list[str] = []
    for folder in (deliverables_dir(project_root), artifacts_dir(project_root)):
        if not folder.exists():
            continue
        for p in folder.iterdir():
            if not p.is_file():
                continue
            if p.name.lower() in ("readme.txt", "readme.md"):
                continue
            names.append(p.name)
            if len(names) >= limit:
                return names
    return names
