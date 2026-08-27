"""WokBee 领域模型。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 项目名称硬上限（列表/顶栏展示用，尽量简短）
MAX_PROJECT_TITLE_LEN = 15


def new_project_id() -> str:
    return f"prj_{uuid.uuid4().hex[:12]}"


class ProjectStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    FAILED = "failed"
    DONE = "done"


class RiskLevel(str, Enum):
    """工具/操作风险等级，用于对照审批勾选项。"""

    READ = "read"
    WRITE = "write"
    ROUTINE = "routine"
    HIGH_RISK = "high_risk"


@dataclass
class ApprovalFlags:
    """审核策略勾选项：勾选 = 该级别免审。"""

    skip_read: bool = True  # 读免审
    skip_write: bool = False  # 写免审
    skip_routine: bool = False  # 常规操作免审
    skip_high_risk: bool = False  # 高危操作免审
    allow_sandbox_escape: bool = False  # 越过沙箱：可访问其他项目/目录外文件

    def to_dict(self) -> dict:
        return {
            "skip_read": bool(self.skip_read),
            "skip_write": bool(self.skip_write),
            "skip_routine": bool(self.skip_routine),
            "skip_high_risk": bool(self.skip_high_risk),
            "allow_sandbox_escape": bool(self.allow_sandbox_escape),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ApprovalFlags:
        data = data or {}
        return cls(
            skip_read=bool(data.get("skip_read", True)),
            skip_write=bool(data.get("skip_write", False)),
            skip_routine=bool(data.get("skip_routine", False)),
            skip_high_risk=bool(data.get("skip_high_risk", False)),
            allow_sandbox_escape=bool(data.get("allow_sandbox_escape", False)),
        )

    def copy(self) -> ApprovalFlags:
        return ApprovalFlags.from_dict(self.to_dict())

    def needs_approval(self, risk: RiskLevel | str) -> bool:
        """返回 True 表示该风险级别仍需人工审批。"""
        key = risk.value if isinstance(risk, RiskLevel) else str(risk)
        mapping = {
            RiskLevel.READ.value: self.skip_read,
            RiskLevel.WRITE.value: self.skip_write,
            RiskLevel.ROUTINE.value: self.skip_routine,
            RiskLevel.HIGH_RISK.value: self.skip_high_risk,
        }
        skip = mapping.get(key, False)
        return not skip

    def summary(self) -> str:
        labels = []
        if self.skip_read:
            labels.append("读")
        if self.skip_write:
            labels.append("写")
        if self.skip_routine:
            labels.append("常规")
        if self.skip_high_risk:
            labels.append("高危")
        if self.allow_sandbox_escape:
            labels.append("越沙箱")
        if not labels:
            return "全部需审"
        if len(labels) == 5:
            return "全部免审"
        return "免审：" + "/".join(labels)

    @classmethod
    def from_legacy(cls, data: dict) -> ApprovalFlags:
        """兼容旧字段 policy / trust_yolo。"""
        if isinstance(data.get("approval"), dict):
            return cls.from_dict(data["approval"])
        if data.get("trust_yolo"):
            return cls(
                skip_read=True,
                skip_write=True,
                skip_routine=True,
                skip_high_risk=True,
                allow_sandbox_escape=True,
            )
        policy = str(data.get("policy") or "graded")
        if policy == "readonly":
            return cls(
                skip_read=True,
                skip_write=False,
                skip_routine=False,
                skip_high_risk=False,
            )
        if policy == "yolo":
            return cls(
                skip_read=True,
                skip_write=True,
                skip_routine=True,
                skip_high_risk=True,
                allow_sandbox_escape=True,
            )
        # graded 默认：仅读免审
        return cls(
            skip_read=True,
            skip_write=False,
            skip_routine=False,
            skip_high_risk=False,
        )


@dataclass
class ProjectEvent:
    """执行时间线事件。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    kind: str = "info"  # info | user | agent | tool | approval | error | lesson
    content: str = ""
    created_at: str = field(default_factory=_now)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ProjectEvent:
        return cls(
            id=data.get("id", uuid.uuid4().hex[:10]),
            kind=data.get("kind", "info"),
            content=data.get("content", ""),
            created_at=data.get("created_at", _now()),
            meta=data.get("meta") or {},
        )


@dataclass
class Project:
    """项目实体；磁盘目录名 = id。"""

    id: str = field(default_factory=new_project_id)
    title: str = "未命名项目"
    goal: str = ""
    status: ProjectStatus = ProjectStatus.IDLE
    approval: ApprovalFlags = field(default_factory=ApprovalFlags)
    progress_done: int = 0
    progress_total: int = 0
    current_step: str = ""
    artifacts_summary: str = ""
    provider: str = ""
    model_id: str = ""
    pinned: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def progress_text(self) -> str:
        if self.progress_total <= 0:
            return "未开始"
        return f"{self.progress_done}/{self.progress_total}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status.value,
            "approval": self.approval.to_dict(),
            "progress_done": self.progress_done,
            "progress_total": self.progress_total,
            "current_step": self.current_step,
            "artifacts_summary": self.artifacts_summary,
            "provider": self.provider,
            "model_id": self.model_id,
            "pinned": bool(self.pinned),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Project:
        status = data.get("status", ProjectStatus.IDLE.value)
        try:
            status_e = ProjectStatus(status)
        except ValueError:
            status_e = ProjectStatus.IDLE
        return cls(
            id=data.get("id") or new_project_id(),
            title=(
                (str(data.get("title") or "未命名项目")).replace("\x00", "").strip()
                or "未命名项目"
            )[:MAX_PROJECT_TITLE_LEN],
            goal=(str(data.get("goal") or "")).replace("\x00", "").strip(),
            status=status_e,
            approval=ApprovalFlags.from_legacy(data),
            progress_done=int(data.get("progress_done") or 0),
            progress_total=int(data.get("progress_total") or 0),
            current_step=data.get("current_step") or "",
            artifacts_summary=data.get("artifacts_summary") or "",
            provider=data.get("provider") or "",
            model_id=data.get("model_id") or "",
            pinned=bool(data.get("pinned", False)),
            created_at=data.get("created_at") or _now(),
            updated_at=data.get("updated_at") or _now(),
        )
