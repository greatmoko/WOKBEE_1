"""AutoBee 定时任务领域模型。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


def _now() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_task_id() -> str:
    return f"ab_{uuid.uuid4().hex[:12]}"


def _clean(value: str | None) -> str:
    return (value or "").replace("\x00", "").strip()


class TaskType(str, Enum):
    """定时任务执行动作类型（单一类型）。企业微信推送是推送渠道，不是动作类型。"""

    TEXT = "text"  # 文本（记录/生成正文）
    SCRIPT = "script"  # 脚本代码
    WOKBEE = "wokbee"  # WokBee 项目任务（project_id 关联）

    @property
    def label(self) -> str:
        return {
            TaskType.TEXT: "文本",
            TaskType.SCRIPT: "脚本",
            TaskType.WOKBEE: "WokBee 任务",
        }[self]


class TaskRunStatus(str, Enum):
    """单次运行结果状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    MISSED = "missed"
    RUNNING = "running"

    @property
    def label(self) -> str:
        return {
            TaskRunStatus.SUCCESS: "成功",
            TaskRunStatus.FAILED: "失败",
            TaskRunStatus.MISSED: "错过",
            TaskRunStatus.RUNNING: "运行中",
        }[self]


@dataclass
class ScheduledTask:
    """一个定时任务。"""

    id: str = field(default_factory=new_task_id)
    name: str = "未命名任务"
    description: str = ""  # 自然语言描述，供 NL 再生成
    task_type: TaskType = TaskType.TEXT
    schedule: str = "*/30 * * * *"  # 5 段 cron
    cron_text: str = ""
    enabled: bool = True

    # 类型负载（单一类型，各取所需）
    content: str = ""  # text 正文
    use_ai: bool = False  # text：用模型按 description 生成正文
    code: str = ""  # script Python 代码
    timeout_s: int = 120  # script
    project_id: str = ""  # wokbee
    user_message: str = ""  # wokbee 驱动 Agent 的话
    max_steps: int = 40  # wokbee

    # 推送渠道（企业微信，作用于任意类型的结果）
    push_wecom: bool = False
    webhook_url: str = ""  # 群机器人 webhook
    msgtype: str = "text"  # text | markdown
    mention: str = ""  # @ 内容（账号/手机号/@all）

    # AI 模型
    gen_provider: str = ""
    gen_model_id: str = ""
    gen_model_label: str = ""
    exec_provider: str = ""
    exec_model_id: str = ""
    exec_model_label: str = ""

    # 运行态
    last_run: str = ""
    last_status: str = ""  # success | failed | missed | running | ""
    last_message: str = ""
    next_run: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def touch(self) -> None:
        self.updated_at = _now()

    def _type(self) -> TaskType:
        return self.task_type

    def to_dict(self) -> dict:
        d = asdict(self)
        d["task_type"] = self.task_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> ScheduledTask:
        data = data or {}
        try:
            ttype = TaskType(str(data.get("task_type") or TaskType.TEXT.value))
        except ValueError:
            ttype = TaskType.TEXT
        return cls(
            id=str(data.get("id") or new_task_id()),
            name=_clean(data.get("name") or "") or "未命名任务",
            description=_clean(data.get("description")),
            task_type=ttype,
            schedule=_clean(data.get("schedule") or "") or "*/30 * * * *",
            cron_text=_clean(data.get("cron_text")),
            enabled=bool(data.get("enabled", True)),
            content=_clean(data.get("content")),
            use_ai=bool(data.get("use_ai", False)),
            code=_clean(data.get("code")),
            timeout_s=max(1, int(data.get("timeout_s") or 120)),
            project_id=_clean(data.get("project_id")),
            user_message=_clean(data.get("user_message")),
            max_steps=max(1, int(data.get("max_steps") or 40)),
            push_wecom=bool(data.get("push_wecom", False)),
            webhook_url=_clean(data.get("webhook_url")),
            msgtype=_clean(data.get("msgtype") or "") or "text",
            mention=_clean(data.get("mention")),
            gen_provider=_clean(data.get("gen_provider")),
            gen_model_id=_clean(data.get("gen_model_id")),
            gen_model_label=_clean(data.get("gen_model_label")),
            exec_provider=_clean(data.get("exec_provider")),
            exec_model_id=_clean(data.get("exec_model_id")),
            exec_model_label=_clean(data.get("exec_model_label")),
            last_run=_clean(data.get("last_run")),
            last_status=_clean(data.get("last_status")),
            last_message=_clean(data.get("last_message")),
            next_run=_clean(data.get("next_run")),
            created_at=_clean(data.get("created_at")) or _now(),
            updated_at=_clean(data.get("updated_at")) or _now(),
        )


@dataclass
class JobLog:
    """一次运行日志。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    task_id: str = ""
    status: TaskRunStatus = TaskRunStatus.RUNNING
    started_at: str = field(default_factory=_now)
    finished_at: str = ""
    duration_s: float = 0.0
    summary: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> JobLog:
        data = data or {}
        try:
            status = TaskRunStatus(str(data.get("status") or TaskRunStatus.RUNNING.value))
        except ValueError:
            status = TaskRunStatus.RUNNING
        try:
            duration = float(data.get("duration_s") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
            task_id=str(data.get("task_id") or ""),
            status=status,
            started_at=_clean(data.get("started_at")) or _now(),
            finished_at=_clean(data.get("finished_at")),
            duration_s=duration,
            summary=_clean(data.get("summary")),
            error=_clean(data.get("error")),
            meta=data.get("meta") if isinstance(data.get("meta"), dict) else {},
        )
