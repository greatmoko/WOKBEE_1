"""后台线程运行 AgentRunner，并通过信号回传 UI。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QThread, Signal

from wokbee.engine.runner import AgentRunner, RunRequest, RunResult


class AgentWorker(QThread):
    event_emitted = Signal(str, str, object)  # kind, content, meta
    approval_needed = Signal(object)  # list[dict]
    finished_result = Signal(object)  # RunResult

    def __init__(
        self,
        runner: AgentRunner,
        request: RunRequest,
        parent=None,
        *,
        mode: str = "run",
    ):
        super().__init__(parent)
        self.runner = runner
        self.request = request
        self.mode = mode  # run | chat

    def run(self):
        self.runner.on_event = self._on_event
        self.runner.on_approval_needed = self._on_approval

        if self.mode == "chat":
            result = self.runner.run_chat(self.request)
        else:
            result = self.runner.run(self.request)
        self.finished_result.emit(result)

    def _on_event(self, kind: str, content: str, meta: dict):
        self.event_emitted.emit(kind, content, meta)

    def _on_approval(self, pending: list):
        self.approval_needed.emit(pending)

    def cancel(self):
        self.runner.request_cancel()

    def approve_all(self):
        # 决策数量由 runner 侧 pending 决定；这里先发一批 approve，runner 会截断/补齐
        self.runner.resolve_approval([{"type": "approve"}] * 16)

    def reject_all(self, message: str = "用户拒绝"):
        self.runner.resolve_approval(
            [{"type": "reject", "message": message}] * 16
        )


class LessonWorker(QThread):
    """后台总结经验，避免阻塞 UI 主线程。"""

    event_emitted = Signal(str, str, object)  # kind, content, meta
    finished_lesson = Signal(object)  # Lesson | None
    failed = Signal(str)

    def __init__(
        self,
        runner: AgentRunner,
        request: RunRequest,
        *,
        outcome: str,
        summary: str,
        errors: str = "",
        success_path: str = "",
        notes: str = "",
        events: list[Any] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.runner = runner
        self.request = request
        self.outcome = outcome
        self.summary = summary
        self.errors = errors
        self.success_path = success_path
        self.notes = notes
        self.events = events

    def run(self):
        self.runner.on_event = self._on_event
        try:
            lesson = self.runner.write_lesson_manual(
                self.request,
                self.outcome,
                self.summary,
                self.errors,
                success_path=self.success_path,
                notes=self.notes,
                events=self.events,
            )
            self.finished_lesson.emit(lesson)
        except Exception as e:
            self.failed.emit(str(e))

    def _on_event(self, kind: str, content: str, meta: dict):
        self.event_emitted.emit(kind, content, meta or {})
