"""WokBee 网页聊天界面：QWebEngineView + QWebChannel 桥接层。

以现代化 HTML/CSS/JS 聊天气泡替换旧的 Qt 时间线（_Timeline）。消息渲染、流式打字、
思考卡片、工具步骤行、系统轻提示全部在网页内完成；本模块负责：

- 把 `ProjectEvent` 序列化并经 QWebChannel 推送给前端（renderEvents / appendEvent /
  appendStream / beginRun / endRun / 状态条 / 审批 / 全局收拢）；
- 处理前端回传的 `page_ready`（页面就绪后补发缓冲事件）与 `load_older`（上翻加载更早记录）；
- 维护「页面未就绪」期间的待发缓存，保证切项目/首次进入不丢消息。

输入框、运行/暂停、审批按钮仍由 Qt 操作栏（_ActionBar）承担，聊天界面只负责展示与
消息流实时更新。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFrame, QVBoxLayout

from tokbee.ui.styles.theme import Theme

from wokbee.core.models import ProjectEvent
from wokbee.ui.timeline import (
    LOAD_OLDER_BATCH,
    _event_ui_role,
    _redact_display,
    _tool_event_display_text,
)

logger = logging.getLogger("wokbee")

_HTML_PATH = Path(__file__).parent / "webchat_assets" / "chat.html"


def _event_to_dict(ev: ProjectEvent) -> dict:
    """把事件转成前端可渲染的 dict（含工具行配对信息与脱敏后的展示文本）。"""
    meta = ev.meta if isinstance(ev.meta, dict) else {}
    phase = str(meta.get("phase") or "")
    if ev.kind == "tool":
        body = _tool_event_display_text(ev)
    else:
        body = ev.content or ""
    d: dict = {
        "kind": ev.kind,
        "role": _event_ui_role(ev.kind),
        "phase": phase,
        "time": ev.created_at,
        "content": _redact_display(body),
    }
    if ev.kind == "tool":
        d["tool"] = {
            "id": str(meta.get("tool_call_id") or ""),
            "name": str(meta.get("tool") or "tool"),
            "phase": str(meta.get("phase") or "").lower(),
            "status": str(meta.get("status") or "success").lower(),
        }
    return d


class _ChatBridge(QObject):
    """注册为 QWebChannel 对象「backend」，前端通过它收发消息。

    Python → 前端：信号（前端在 JS 里 connect）。
    前端 → Python：槽（前端直接调用）。
    """

    render_events = Signal(str, int)      # events_json, older_remaining
    prepend_events = Signal(str, int)     # older_events_json, older_remaining
    append_event = Signal(str)            # event_json
    append_stream = Signal(str, str)      # target("reasoning"/"text"), delta
    begin_run = Signal()
    end_run = Signal()
    set_status = Signal(str)
    clear_status = Signal()
    set_agent_running = Signal(bool)
    approval_pending = Signal()
    resume_approval = Signal(bool)
    show_empty = Signal(str)

    def __init__(self, host, parent=None):
        super().__init__(parent)
        self._host = host

    @Slot()
    def page_ready(self):
        self._host._on_page_ready()

    @Slot()
    def load_older(self):
        self._host._on_load_older()


class _WebChat(QFrame):
    """现代化聊天窗口：内部承载 QWebEngineView，对外暴露与 _Timeline 兼容的接口。"""

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self._ready = False
        self._pending: list[tuple[str, tuple]] = []
        # 当前已加载的事件窗口（oldest→newest），供上翻加载更早记录
        self._all_events: list[ProjectEvent] = []
        self._older_remaining = 0
        self._events_loader = None
        self._has_rendered = False
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._view = QWebEngineView(self)
        layout.addWidget(self._view)
        self._bridge = _ChatBridge(self)
        channel = QWebChannel(self._view.page())
        channel.registerObject("backend", self._bridge)
        self._view.page().setWebChannel(channel)
        self._view.setUrl(QUrl.fromLocalFile(str(_HTML_PATH)))
        # 兜底：前端 setInterval + 本侧在加载完成后的定时 ping 双保险，保证 page_ready 一定到达。
        self._view.loadFinished.connect(self._on_load_finished)

    def _on_load_finished(self, ok: bool):
        # 前端 setInterval 每秒重试；本侧再按递增间隔 ping 几次，适配定时器被
        # 节流/GPU 回退等环境（offscreen、无独显），确保事件缓存不悬空。
        for delay in (0, 300, 800, 1600, 3000):
            QTimer.singleShot(delay, self._ping_page_ready)

    def _ping_page_ready(self):
        if self._ready:
            return
        try:
            self._view.page().runJavaScript(
                "if (typeof bridge !== 'undefined' && bridge) "
                "{ try { bridge.page_ready(); } catch (e) {} }"
            )
        except Exception:
            logger.exception("网页 page_ready 兜底触发失败")

    # ── 内部：缓存与补发 ─────────────────────────────────────────

    def _emit(self, name: str, *args):
        """信号安全发送：页面未就绪时先入缓存，就绪后按序补发。"""
        if self._ready:
            getattr(self._bridge, name).emit(*args)
        else:
            self._pending.append((name, args))

    def _on_page_ready(self):
        if self._ready:
            return
        self._ready = True
        pending, self._pending = self._pending, []
        for name, args in pending:
            try:
                getattr(self._bridge, name).emit(*args)
            except Exception:
                logger.exception("补发网页事件 %s 失败", name)

    def _on_load_older(self):
        if self._events_loader is None or self._older_remaining <= 0:
            return
        try:
            events, remaining = self._events_loader(len(self._all_events), LOAD_OLDER_BATCH)
        except Exception:
            return
        if not events:
            self._older_remaining = 0
            return
        self._all_events = list(events) + self._all_events
        self._older_remaining = remaining
        payload = json.dumps(
            [_event_to_dict(e) for e in events], ensure_ascii=False
        )
        self._emit("prepend_events", payload, remaining)

    # ── 对外接口（与 _Timeline 兼容） ─────────────────────────────

    @property
    def _bubbles(self):
        """时间线是否已有内容（workspace 用它判断是否整表重绘）。"""
        return self._all_events if self._has_rendered else []

    def show_empty(self, text: str = "选择或新建一个项目开始。"):
        self._all_events = []
        self._older_remaining = 0
        self._has_rendered = True
        self._emit("show_empty", text)

    def render_events(
        self,
        events: list[ProjectEvent],
        *,
        older_remaining: int = 0,
        loader=None,
    ):
        self._all_events = list(events)
        self._older_remaining = max(0, int(older_remaining or 0))
        self._events_loader = loader
        if not events:
            self.show_empty("尚无执行记录。在下方输入目标或指令，然后点击运行。")
            return
        self._has_rendered = True
        payload = json.dumps(
            [_event_to_dict(e) for e in events], ensure_ascii=False
        )
        self._emit("render_events", payload, self._older_remaining)

    def append_event(self, ev: ProjectEvent):
        self._all_events.append(ev)
        self._has_rendered = True
        self._emit("append_event", json.dumps(_event_to_dict(ev), ensure_ascii=False))

    def append_stream(self, target: str, delta: str):
        if not (delta or ""):
            return
        self._has_rendered = True
        self._emit("append_stream", "reasoning" if target == "reasoning" else "text", delta)

    def begin_run(self):
        self._emit("begin_run")

    def end_run(self):
        self._emit("end_run")
        self._emit("clear_status")

    def set_agent_running(self, running: bool):
        self._emit("set_agent_running", bool(running))

    def reset_scroll_anchor(self):
        # 网页端自动贴底滚动；切项目后下一次渲染由 render_events 全量重绘，无需干预
        pass

    def on_approval_pending(self):
        self._emit("approval_pending")

    def resume_after_approval(self, approved: bool):
        self._emit("resume_approval", bool(approved))

    def _status(self, text: str, *, pulse: bool = True):
        self._emit("set_status", text or "")

    def _schedule_scroll_to_bottom(self, *, force: bool = False):
        # 网页端自动贴底滚动，无需前端干预
        pass

    def _refresh_essentials(self):
        pass  # 兼容旧接口；顶栏要素由 workspace 的 Qt 部件负责

    def _scroll_to_bottom(self):
        pass
