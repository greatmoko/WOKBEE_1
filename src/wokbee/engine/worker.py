"""后台线程运行 AgentRunner，并通过信号回传 UI。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QThread, Signal

from wokbee.engine.runner import AgentRunner, RunRequest, RunResult


class AgentWorker(QThread):
    event_emitted = Signal(str, str, object)  # kind, content, meta
    approval_needed = Signal(object)  # list[dict]
    ask_user_needed = Signal(object)  # dict payload
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
        self._last_pending_count = 0  # 最近一次审批待决数量（由 _on_approval 填充）

    def run(self):
        self.runner.on_event = self._on_event
        self.runner.on_approval_needed = self._on_approval
        self.runner.on_ask_user_needed = self._on_ask_user

        if self.mode == "chat":
            result = self.runner.run_chat(self.request)
        else:
            result = self.runner.run(self.request)
        self.finished_result.emit(result)

    def _on_event(self, kind: str, content: str, meta: dict):
        self.event_emitted.emit(kind, content, meta)

    def _on_approval(self, pending: list):
        self._last_pending_count = len(pending) if pending else 0
        self.approval_needed.emit(pending)

    def _on_ask_user(self, payload: dict):
        self.ask_user_needed.emit(payload)

    def cancel(self):
        self.runner.request_cancel()

    def approve_all(self):
        # 决策长度与最近一次审批待决数量对齐（避免写死 16，单轮 pending>16 时后面决策无人批）
        n = self._last_pending_count or 16
        self.runner.resolve_approval([{"type": "approve"}] * n)

    def reject_all(self, message: str = "用户拒绝"):
        n = self._last_pending_count or 16
        self.runner.resolve_approval(
            [{"type": "reject", "message": message}] * n
        )

    def resolve_ask_user(self, answers: dict):
        self.runner.resolve_ask_user(answers)


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

    def cancel(self):
        self.runner.request_cancel()
