"""定时任务调度：包装 APScheduler BackgroundScheduler，后台线程运行，不阻塞 UI。"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from autobee.core.models import JobLog, ScheduledTask, TaskRunStatus, _now
from autobee.core.store import AutoBeeStore
from autobee.engine.executor import TaskExecutor

logger = logging.getLogger("autobee")

# 失联宽限（秒）：电脑睡眠到点后仍能在宽限内补跑，避免默认 1s 静默丢弃
_MISFIRE_GRACE = 3600


def describe_cron(expr: str) -> str:
    """把常见 5 段 cron 转成人类可读说明；无法识别时返回空串。"""
    fields = (expr or "").split()
    if len(fields) != 5:
        return ""
    minute, hour, day, month, dow = fields
    try:
        minute_i, hour_i = int(minute), int(hour)
    except ValueError:
        minute_i = hour_i = None
    # 分钟精度 / 固定时刻
    if minute == "*" and hour == "*" and day == "*" and month == "*" and dow == "*":
        return "每分钟"
    if minute.startswith("*/") and hour == "*" and _all_star(day, month, dow):
        try:
            return f"每 {int(minute[2:])} 分钟"
        except ValueError:
            pass
    if _all_star(minute, day, month, dow) and hour == "*":
        return "每小时"
    if _all_star(minute, hour, day, month, dow):
        return "每天任意时刻"
    # 每天固定时刻
    if _all_star(day, month, dow) and minute_i is not None and hour_i is not None:
        hm = f"{hour_i:02d}:{minute_i:02d}"
        return f"每天 {hm}"
    # 工作日固定时刻
    if day == "*" and month == "*" and dow == "1-5" and minute_i is not None and hour_i is not None:
        return f"每个工作日 {hour_i:02d}:{minute_i:02d}"
    if month == "*" and dow == "*" and _is_int(day) and _is_int(hour) and _is_int(minute):
        return f"每月 {int(day)} 号 {int(hour):02d}:{int(minute):02d}"
    return ""


def _all_star(*fields: str) -> bool:
    return all(f == "*" for f in fields)


def _is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except (TypeError, ValueError):
        return False


class SchedulerService:
    """管理任务注册到 APScheduler，并记录运行日志。"""

    def __init__(self, store: AutoBeeStore | None = None, executor: TaskExecutor | None = None):
        self.store = store or AutoBeeStore()
        self.executor = executor or TaskExecutor(self.store)
        self._started = False
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._scheduler = BackgroundScheduler(
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": _MISFIRE_GRACE,
            },
        )
        self._scheduler.add_listener(self._on_scheduler_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED)

    # ── 生命周期 ───────────────────────────────────────────
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            for task in self.store.list_tasks():
                self._register(task)
            self._scheduler.start()
            self._started = True

    def shutdown(self, *, wait: bool = False) -> None:
        with self._lock:
            if not self._started:
                return
            try:
                self._scheduler.shutdown(wait=wait)
            except Exception:
                logger.exception("关闭调度器失败")
            self._started = False

    @property
    def running(self) -> bool:
        return self._started

    # ── 任务注册 ───────────────────────────────────────────
    def _register(self, task: ScheduledTask) -> None:
        """按 id 注册/更新 job；禁用任务补 pause。replace_existing 会重建激活，须补 pause。"""
        try:
            trigger = CronTrigger.from_crontab(task.schedule)
        except ValueError as e:
            logger.warning("任务 %s 的 cron 无效：%s", task.id, e)
            return
        self._scheduler.add_job(
            self._run_job, trigger, args=[task.id], id=task.id, replace_existing=True,
        )
        if not task.enabled:
            try:
                self._scheduler.pause_job(task.id)
            except Exception:
                logger.exception("暂停任务 %s 失败", task.id)

    def add_or_update(self, task: ScheduledTask) -> None:
        with self._lock:
            self._register(task)

    def remove(self, task_id: str) -> None:
        with self._lock:
            try:
                self._scheduler.remove_job(task_id)
            except Exception:
                pass

    def pause(self, task_id: str) -> None:
        with self._lock:
            self.store.set_enabled(task_id, False)
            try:
                self._scheduler.pause_job(task_id)
            except Exception:
                logger.exception("暂停任务 %s 失败", task_id)

    def resume(self, task_id: str) -> None:
        with self._lock:
            self.store.set_enabled(task_id, True)
            try:
                self._scheduler.resume_job(task_id)
            except Exception:
                logger.exception("恢复任务 %s 失败", task_id)

    def run_now(self, task_id: str) -> None:
        """立即执行（用自管线程池投递，避开 APScheduler 无公开 submit）。"""
        task = self.store.get(task_id)
        if task is None:
            return
        self._pool.submit(self._run_job, task_id)

    # ── 查询 ───────────────────────────────────────────────
    def next_run_time(self, task_id: str) -> str:
        job = self._scheduler.get_job(task_id)
        if job is None:
            return "未调度"
        next_t = job.next_run_time
        if next_t is None:
            return "已暂停"
        return next_t.astimezone().strftime("%Y-%m-%d %H:%M")

    def is_paused(self, task_id: str) -> bool:
        job = self._scheduler.get_job(task_id)
        return job is None or job.next_run_time is None

    # ── 执行与记录 ─────────────────────────────────────────
    def _run_job(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is None:
            return
        log = JobLog(task_id=task_id, status=TaskRunStatus.RUNNING)
        started_dt = datetime.now()
        try:
            result = self.executor.run(task)
            ok = bool(result.get("ok"))
            log.status = TaskRunStatus.SUCCESS if ok else TaskRunStatus.FAILED
            log.summary = str(result.get("message") or "")
            log.error = str(result.get("error") or "")
            task.last_status = log.status.value
            task.last_message = log.summary or log.error
        except Exception as e:
            logger.exception("任务 %s 执行异常", task_id)
            log.status = TaskRunStatus.FAILED
            log.error = str(e)
            task.last_status = TaskRunStatus.FAILED.value
            task.last_message = str(e)
        finally:
            log.finished_at = _now()
            log.duration_s = max(0.0, (datetime.now() - started_dt).total_seconds())
            self.store.append_log(log)
            task.last_run = log.started_at
            self.store.save_task(task)

    def _on_scheduler_event(self, event) -> None:
        try:
            job_id = getattr(event, "job_id", "")
            if event.code == EVENT_JOB_MISSED:
                log = JobLog(task_id=job_id, status=TaskRunStatus.MISSED, summary="错过执行")
                self.store.append_log(log)
                task = self.store.get(job_id)
                if task:
                    task.last_status = TaskRunStatus.MISSED.value
                    task.last_message = "错过执行（可能当时电脑未开机）"
                    self.store.save_task(task)
            elif event.code == EVENT_JOB_ERROR:
                exc = getattr(event, "exception", None)
                log = JobLog(
                    task_id=job_id, status=TaskRunStatus.FAILED,
                    summary=task_summary(self.store, job_id),
                    error=getattr(exc, "str", None) or str(exc)[:300],
                )
                self.store.append_log(log)
        except Exception:
            logger.exception("写入调度事件日志失败")


def task_summary(store: AutoBeeStore, task_id: str) -> str:
    task = store.get(task_id)
    return f"{task.name} 执行出错" if task else "执行出错"
