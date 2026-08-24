"""后台线程运行 AgentRunner，并通过信号回传 UI。"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from autobee.engine.runner import AgentRunner, RunRequest, RunResult


class AgentWorker(QThread):
    event_emitted = Signal(str, str, object)  # kind, content, meta
    approval_needed = Signal(object)  # list[dict]
    finished_result = Signal(object)  # RunResult

    def __init__(self, runner: AgentRunner, request: RunRequest, parent=None):
        super().__init__(parent)
        self.runner = runner
        self.request = request

    def run(self):
        self.runner.on_event = self._on_event
        self.runner.on_approval_needed = self._on_approval

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
