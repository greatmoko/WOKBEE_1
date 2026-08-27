"""AutoBee 任务持久化：单文件 JSON + 线程锁。"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from tokbee.core.config import default_data_dir
from tokbee.core.safe_io import safe_write_json

from autobee.core.models import JobLog, ScheduledTask

logger = logging.getLogger("autobee")

# 每任务最多保留的日志条数（记录最近 10 次运行）
MAX_LOGS_PER_TASK = 10


class AutoBeeStore:
    """管理定时任务与运行日志，存于 ~/.wokbee/autobee.json。"""

    def __init__(self, config_path: str | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = default_data_dir() / "autobee.json"
        self._lock = threading.RLock()
        self._tasks: dict[str, ScheduledTask] = {}
        self._logs: dict[str, list[JobLog]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            tasks = data.get("tasks") or []
            logs = data.get("logs") or {}
            for item in tasks:
                if isinstance(item, dict):
                    task = ScheduledTask.from_dict(item)
                    self._tasks[task.id] = task
            for tid, items in logs.items():
                self._logs[str(tid)] = [
                    JobLog.from_dict(it) for it in items if isinstance(it, dict)
                ]
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("读取 autobee.json 失败: %s", e)
            self._tasks = {}
            self._logs = {}

    def save(self) -> None:
        with self._lock:
            data = {
                "version": 1,
                "tasks": [t.to_dict() for t in self._tasks.values()],
                "logs": {
                    tid: [log.to_dict() for log in logs]
                    for tid, logs in self._logs.items()
                },
            }
            safe_write_json(self._path, data)

    def list_tasks(self) -> list[ScheduledTask]:
        with self._lock:
            items = sorted(
                self._tasks.values(),
                key=lambda t: (not t.enabled, t.updated_at),
                reverse=False,
            )
            # 启用在前，禁用在后；各自按更新时间倒序
            enabled = sorted((t for t in items if t.enabled), key=lambda t: t.updated_at, reverse=True)
            disabled = sorted((t for t in items if not t.enabled), key=lambda t: t.updated_at, reverse=True)
            return enabled + disabled

    def get(self, task_id: str) -> ScheduledTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return task

    def save_task(self, task: ScheduledTask) -> None:
        with self._lock:
            task.touch()
            self._tasks[task.id] = task
            self.save()

    def create(self, **fields) -> ScheduledTask:
        task = ScheduledTask(**fields)
        self.save_task(task)
        return task

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)
            self._logs.pop(task_id, None)
            self.save()

    def set_enabled(self, task_id: str, enabled: bool) -> ScheduledTask | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            task.enabled = bool(enabled)
            task.touch()
            self.save()
            return task

    def set_ai(self, task_id: str, *, gen: tuple[str, str, str] | None = None,
               exec_: tuple[str, str, str] | None = None) -> ScheduledTask | None:
        """gen/exec 各为 (provider_id, model_id, label)。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            if gen is not None:
                task.gen_provider, task.gen_model_id, task.gen_model_label = gen
            if exec_ is not None:
                task.exec_provider, task.exec_model_id, task.exec_model_label = exec_
            task.touch()
            self.save()
            return task

    def append_log(self, log: JobLog) -> None:
        with self._lock:
            if log.task_id not in self._logs:
                self._logs[log.task_id] = []
            self._logs[log.task_id].append(log)
            if len(self._logs[log.task_id]) > MAX_LOGS_PER_TASK:
                self._logs[log.task_id] = self._logs[log.task_id][-MAX_LOGS_PER_TASK:]
            self.save()

    def update_log(self, log: JobLog) -> None:
        """按 log.id 更新已有日志（用于运行中 → 终态）。"""
        with self._lock:
            logs = self._logs.get(log.task_id)
            if not logs:
                self.append_log(log)
                return
            for i, existing in enumerate(logs):
                if existing.id == log.id:
                    logs[i] = log
                    self.save()
                    return
            self.append_log(log)

    def list_logs(self, task_id: str, limit: int = MAX_LOGS_PER_TASK) -> list[JobLog]:
        with self._lock:
            logs = self._logs.get(task_id, [])
            if limit > 0 and len(logs) > limit:
                return logs[-limit:]
            return list(logs)

    def clear_logs(self, task_id: str) -> None:
        with self._lock:
            self._logs.pop(task_id, None)
            self.save()
