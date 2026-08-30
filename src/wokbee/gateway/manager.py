"""网关管理器：拥有**多个并发的频道** + 收件队列 + 分发线程池，把手机消息路由到项目 Agent。

在 `ServiceRegistry` 中作为单例持有。`QObject` 在 UI 线程创建，worker 线程经
`GatewayNotifier` 信号桥把状态/日志回 UI（仿 `SchedulerNotifier`）。

**多频道模型（issue：不同 IM 不能共用一个消息网络）**：飞书与微信各自独立长连接、同时运行，
互不影响。`_channels` 是按频道 key 的字典；每个频道有自己的状态回调，状态按 key 回给 UI，
所以飞书面板只显示飞书状态、微信面板只显示微信状态，不再「等待微信推送」串台。

线程模型：
- 各通道收线程 → `on_message`（快）→ 丢进 `_inbox` 队列。
- `_dispatch_loop`（daemon 线程）读队列 → `ThreadPoolExecutor` 提交 `_handle`。
- `_handle`（池线程）内做路由 + `_project_lock` 串行化 + `dispatcher.run_chat`（无头 Agent）。
  **绝不在 UI 线程或通道收线程里 import 引擎/跑 Agent。**
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from wokbee.core.models import ProjectEvent, MAX_PROJECT_TITLE_LEN
from wokbee.core.project_store import ProjectStore
from wokbee.core.settings import WokBeeSettings
from tokbee.core.provider_store import ProviderStore

from wokbee.gateway.base import Channel, ChannelMessage, ChannelStatus
from wokbee.gateway.dispatcher import GatewayDispatcher
from wokbee.gateway.router import MessageRouter, RouteOutcome
from wokbee.gateway.store import GatewayChannelConfig, GatewayStore

logger = logging.getLogger("wokbee")

_SENTINEL = object()  # 队列停止哨兵

# 通道状态 → 人类可读文本（用 `{chan}` 占位，运行时按频道名填充）
_CHANNEL_STATUS_TEXT = {
    ChannelStatus.IDLE: "已就绪",
    ChannelStatus.CONNECTING: "连接{chan}中…",
    ChannelStatus.CONNECTED: "已连接（等待{chan}推送）",
    ChannelStatus.ERROR: "{chan}连接失败（检查网络与凭据；若微信提示过期请重新扫码）",
    ChannelStatus.STOPPED: "已停止",
}

# 频道 id → 显示名（用于日志/状态文案）
_CHANNEL_DISPLAY = {"feishu": "飞书", "wechat": "微信"}

# IM 管理指令（`#` 前缀；大小写不敏感，先于项目路由被拦截）：
#   #new  描述   —— 新建项目并绑定为当前频道默认；描述字串设为项目目标
#   #list        —— 列出所有项目（与「未绑定默认项目」时的清单一致；含各自项目ID）
#   #run         —— 用当前频道默认项目的「目标」运行 Agent
#   #help        —— 返回可与 WokBee 交互的系统指令说明
_COMMAND_TOKENS = frozenset({"#new", "#list", "#run", "#help"})


class GatewayNotifier(QObject):
    """网关 → UI 的 Qt 信号桥（跨线程自动 QueuedConnection）。"""

    status_changed = Signal(str, str)       # channel_key, 人类可读状态
    log_line = Signal(str)                  # 实时日志
    message_done = Signal(str, str, str)    # project_id, sender_id, reply-brief
    event_written = Signal(str, str, str, object)  # project_id, kind, content, meta（实时流）
    error = Signal(str, str)                # channel_key, 严重错误提示


def feishu_factory(cfg: GatewayChannelConfig) -> Channel:
    from wokbee.gateway.feishu import FeishuChannel
    return FeishuChannel(cfg)


def wechat_factory(cfg: GatewayChannelConfig) -> Channel:
    from wokbee.gateway.wechat import WeChatChannel
    return WeChatChannel(cfg)


# 频道 id → 通道构造工厂（每个启用的频道各建各的；不在按 cfg.channel 选一条）
_CHANNEL_FACTORIES = {
    "feishu": feishu_factory,
    "wechat": wechat_factory,
}


class GatewayManager(QObject):
    """进程内消息网关（多频道并发）。"""

    def __init__(
        self,
        store: GatewayStore | None = None,
        settings: WokBeeSettings | None = None,
        project_store: ProjectStore | None = None,
        provider_store: ProviderStore | None = None,
        dispatcher: GatewayDispatcher | None = None,
        channel_factory: Callable[[GatewayChannelConfig], Channel] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.store = store or GatewayStore()
        self.settings = settings or WokBeeSettings()
        self.project_store = project_store or ProjectStore()
        self.provider_store = provider_store or ProviderStore()
        self.dispatcher = dispatcher or GatewayDispatcher(
            self.settings, self.provider_store, self.project_store
        )
        # 让分发器把每条落盘事件转交回 manager → UI 实时刷新时间线（Fake dispatcher 无此属性）
        if hasattr(self.dispatcher, "event_sink"):
            self.dispatcher.event_sink = self._on_event_written
        # 允许外部注入 factory（冒烟测试用）；为空时按频道 key 从 _CHANNEL_FACTORIES 选。
        self._channel_factory = channel_factory
        self.notifier = GatewayNotifier()
        self._router = MessageRouter(self.project_store, self.settings)
        self._inbox: queue.Queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=2)
        self._dispatcher_thread: threading.Thread | None = None
        self._channels: dict[str, Channel] = {}  # channel_key -> Channel（多频道并发）
        self._project_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._in_flight: set[str] = set()
        self._in_flight_guard = threading.Lock()

    # ── 生命周期 ─────────────────────────────
    def _factory_for(self, key: str) -> Callable[[GatewayChannelConfig], Channel]:
        if self._channel_factory is not None:
            return self._channel_factory
        return _CHANNEL_FACTORIES.get(key, feishu_factory)

    @staticmethod
    def _creds_present(key: str, cfg: GatewayChannelConfig) -> bool:
        """某频道的连接凭据是否齐全（决定能否实际建立长连接）。"""
        if key == "wechat":
            return bool(cfg.wechat_bot_token and cfg.wechat_account_id and cfg.wechat_base_url)
        return bool(cfg.feishu_app_id and cfg.feishu_app_secret)

    def start(self) -> None:
        """启动所有已启用且已有凭据的频道（幂等；Feishu / WeChat 可同时在线）。"""
        cfg = self.store.get_config()
        for key in _CHANNEL_FACTORIES:
            self.sync_channel(key)
        if not any(cfg.channel_enabled(k) for k in _CHANNEL_FACTORIES):
            self.notifier.log_line.emit("网关未启用任何频道——可在「消息网关」勾选「启用网关」。")

    def sync_channel(self, key: str) -> None:
        """按当前配置把某频道拉到期望状态：启用+有凭据则连接，否则停止。

        面板勾选/取消「启用网关」与启动时统一走这里，保证「勾选即连、取消即停」，且
        只影响本频道、不动其它频道（issue：不同 IM 互不干扰）。
        """
        cfg = self.store.get_config()
        disp = _CHANNEL_DISPLAY.get(key, key)
        if not cfg.channel_enabled(key):
            self._stop_channel(key)
            return
        if not self._creds_present(key, cfg):
            self.notifier.log_line.emit(
                f"「{disp}」已启用但未配置凭据——请扫码创建/登录，或填写凭据后进入再勾选启用。"
            )
            return
        self._ensure_channel(key, cfg)

    def _ensure_channel(self, key: str, cfg: GatewayChannelConfig) -> None:
        """建一条频道长连接（若已存在则忽略）。"""
        if key in self._channels:
            return
        chan = self._factory_for(key)(cfg)
        self._channels[key] = chan
        chan.on_message(self._on_incoming)
        chan.set_status_cb(lambda status, _k=key: self._on_channel_status(_k, status))
        disp = _CHANNEL_DISPLAY.get(key, key)
        self.notifier.log_line.emit(f"启动{disp}长连接（等待{disp}推流）…")
        try:
            chan.start()
        except Exception:
            logger.exception("启动通道失败")
            self._channels.pop(key, None)
            self.notifier.error.emit(key, f"启动{disp}长连接失败，请检查配置并查看日志。")
            return
        self._ensure_dispatch_loop()

    def _ensure_dispatch_loop(self) -> None:
        if self._dispatcher_thread is None or not self._dispatcher_thread.is_alive():
            self._dispatcher_thread = threading.Thread(
                target=self._dispatch_loop, name="gw-dispatch", daemon=True
            )
            self._dispatcher_thread.start()

    def _stop_channel(self, key: str) -> None:
        chan = self._channels.pop(key, None)
        if chan is None:
            return
        try:
            chan.stop()
        except Exception:
            logger.exception("停止通道失败")
        self.notifier.status_changed.emit(key, self._status_text(ChannelStatus.STOPPED, key))

    def shutdown(self, wait: bool = False) -> None:
        for key in list(self._channels):
            self._stop_channel(key)
        self._inbox.put(_SENTINEL)
        if (
            self._dispatcher_thread is not None
            and self._dispatcher_thread is not threading.current_thread()
        ):
            self._dispatcher_thread.join(timeout=None if wait else 3.0)
            self._dispatcher_thread = None
        self._pool.shutdown(wait=wait, cancel_futures=True)

    @property
    def running(self) -> bool:
        return bool(self._channels)

    def _on_channel_status(self, key: str, status: ChannelStatus) -> None:
        self.notifier.status_changed.emit(key, self._status_text(status, key))

    def _status_text(self, status: ChannelStatus, key: str) -> str:
        """把某频道的状态转成人类可读文案；失败时附加 `_last_err` 具体原因。"""
        chan_name = _CHANNEL_DISPLAY.get(key, key)
        text = _CHANNEL_STATUS_TEXT.get(status, status.value)
        try:
            text = text.format(chan=chan_name)
        except Exception:  # noqa: BLE001
            pass
        if status == ChannelStatus.ERROR:
            chan = self._channels.get(key)
            err = getattr(chan, "_last_err", "")
            if err:
                text = f"{chan_name}连接失败：{err}"
        return text

    def status_text(self, key: str) -> str:
        """某频道当前状态的文本（面板按自己频道读取，不串台）。"""
        chan = self._channels.get(key)
        if chan is not None:
            return self._status_text(chan.status, key)
        return "未启动"

    # ── 消息流 ─────────────────────────────
    def _on_incoming(self, msg: ChannelMessage) -> None:
        """通道收线程回调：只去重 + 入队，快速返回。"""
        self.notifier.log_line.emit(
            f"收到 {msg.channel} 消息（{msg.sender_id}）：{(msg.text or '')[:80]}"
        )
        mid = msg.message_id
        if mid:
            with self._in_flight_guard:
                if mid in self._in_flight:
                    return
                self._in_flight.add(mid)
        self._inbox.put(msg)

    def _dispatch_loop(self) -> None:
        while True:
            msg = self._inbox.get()
            if msg is _SENTINEL:
                break
            self._pool.submit(self._handle, msg)

    def _handle(self, msg: ChannelMessage) -> None:
        # 优先拦截 IM 管理指令（#new / #list / #run / #help），不路由到项目 Agent
        token, rest = MessageRouter.parse_route_prefix(msg.text or "")
        cmd = (token or "").lower()
        if cmd in _COMMAND_TOKENS:
            # 指令能建项目/跑 Agent，同样受允许列表约束（空列表=默认放行）
            allow = [s for s in (self.store.get_config().allow_from or []) if s]
            if allow and msg.sender_id not in allow:
                self._reply(msg, "你暂未获得使用指令的权限：当前允许列表已启用，仅限指定用户。")
                return
            self._run_command(cmd, rest, msg)
            return

        cfg = self.store.get_config()
        res = self._router.route(msg, cfg)
        if res.outcome != RouteOutcome.OK:
            if res.reply:
                self._reply(msg, res.reply)
            # 允许列表为空=默认放行，DENIED_SENDER 只在用户显式限定发送者时出现
            if res.outcome == RouteOutcome.DENIED_SENDER:
                self.notifier.log_line.emit(
                    f"[限流] 发送者 {msg.sender_id} 未在允许列表 → 请到「消息网关→允许列表」添加"
                )
            else:
                self.notifier.log_line.emit(f"[{res.outcome}] {msg.sender_id} {res.reply}")
            return

        project_id = res.project_id
        # IM 里用了 @项目ID 显式指定 → 把它设为该频道的默认项目（允许再次 @ 切换，issue 1）
        if res.token:
            cfg = self.store.get_config()
            cfg.set_default_project(msg.channel, project_id)
            self.store.save_config(cfg)
            self.notifier.log_line.emit(
                f"已将 {msg.channel} 的默认项目切换为 {project_id}（后续可再 @项目ID 更换）"
            )
            # 只有 @项目ID、没有正文 → 纯「切换默认项目」：只回执确认，**不跑 Agent**、
            # 也不把空内容交给项目（否则会报「提问内容为空」）。
            if not (res.clean_text or "").strip():
                project = self.project_store.get(project_id)
                name = project.title if project else project_id
                self._reply(msg, (
                    f"✔ 已切换当前频道默认项目：{name}（{project_id}）\n"
                    "之后直接发消息就会路由到该项目；想让它干活可发 #run。"
                ))
                return
        self._run_chat(msg, project_id, res.clean_text)

    def _run_chat(self, msg: ChannelMessage, project_id: str, content: str) -> None:
        """把清理过的用户输入交给默认项目 Agent 跑一遍，并实时落盘事件/回执。"""
        with self._project_lock(project_id):
            self.notifier.log_line.emit(f"开始处理 -> {project_id}: {content[:80]}")
            self._append_event(
                project_id, ProjectEvent(
                    kind="user", content=content,
                    meta={"channel": msg.channel, "sender": msg.sender_id},
                )
            )
            project = self.project_store.get(project_id)
            if project is None:
                self._reply(msg, "项目不存在或已删除。")
                return
            try:
                result = self.dispatcher.run_chat(project, content)
            except Exception:
                logger.exception("网关分发失败")
                result = SimpleNamespace(
                    ok=False, outcome="failed", final_text="",
                    error=str(sys.exc_info()[1]),
                )
            reply = self.dispatcher.reply_for(result)
            self._reply(msg, reply)
            self.notifier.message_done.emit(project_id, msg.sender_id, reply[:120])
            self._append_event(
                project_id, ProjectEvent(
                    kind="info",
                    content=f"来自手机（{msg.channel}）的回复已推送。",
                    meta={"sender": msg.sender_id},
                )
            )

    def _reply(self, msg: ChannelMessage, text: str) -> None:
        chan = self._channels.get(msg.channel)
        if chan is None:
            self.notifier.log_line.emit("回执失败：通道未连接")
            return
        try:
            ok, err = chan.send_text(msg, text)
        except Exception:
            logger.exception("发送回执失败")
            ok, err = False, "发送异常"
        if not ok:
            self.notifier.log_line.emit(f"回执失败（{msg.channel}）：{err}")

    # ── IM 管理指令（#new / #list / #run / #help） ────────────────────
    def _run_command(self, cmd: str, arg: str, msg: ChannelMessage) -> None:
        if cmd == "#new":
            self._cmd_new(arg, msg)
        elif cmd == "#list":
            self._cmd_list(msg)
        elif cmd == "#run":
            self._cmd_run(msg)
        elif cmd == "#help":
            self._cmd_help(msg)

    def _cmd_new(self, goal: str, msg: ChannelMessage) -> None:
        """`#new 描述`：新建项目并把背景描述设为项目目标，同时绑定为当前频道默认。"""
        goal = (goal or "").strip()
        proj = self.project_store.create(title=self._title_from_goal(goal), goal=goal)
        pid = proj.id
        cfg = self.store.get_config()
        cfg.set_default_project(msg.channel, pid)
        self.store.save_config(cfg)
        self.notifier.log_line.emit(f"[#new] 已创建项目 {proj.title}（{pid}）并绑定为 {msg.channel} 默认")
        goal_line = goal if goal else "（未设置目标；到桌面详情里补充，或用 #new 描述 直接带上）"
        self._reply(msg, (
            "✔ 已创建新项目，并绑定为当前公众号的默认项目：\n"
            f"名称：{proj.title}\n标识：{pid}\n目标：{goal_line}\n\n"
            "想让它干活，发送 #run 即可按目标运行。"
        ))

    def _cmd_list(self, msg: ChannelMessage) -> None:
        """`#list`：返回全部项目清单（与「未绑定默认项目」时提示一致；含各自项目ID）。"""
        self._reply(msg, self._router._project_listing())

    def _cmd_run(self, msg: ChannelMessage) -> None:
        """`#run`：用当前频道默认项目的「目标」作为提示词运行 Agent。"""
        cfg = self.store.get_config()
        pid = cfg.default_project_for(msg.channel)
        if not pid:
            self._reply(msg, "当前频道还没有绑定默认项目。\n" + self._router._project_listing())
            return
        project = self.project_store.get(pid)
        if project is None:
            self._reply(msg, f"默认项目不存在或已删除：{pid}。\n" + self._router._project_listing())
            return
        goal = (project.goal or "").strip()
        if not goal:
            self._reply(msg, f"项目「{project.title}」还没有目标，无法运行。请用 #new 描述 建立带目标的项目。")
            return
        self.notifier.log_line.emit(f"[#run] 用 {msg.channel} 默认项目「{project.title}」的目标运行")
        self._run_chat(msg, pid, goal)

    def _cmd_help(self, msg: ChannelMessage) -> None:
        """`#help`：返回可与 WokBee 交互的系统指令说明。"""
        self._reply(msg, self._router._command_help())

    @staticmethod
    def _title_from_goal(goal: str) -> str:
        """`#new` 未显式给名称时：取目标前 N 字作标题（更好认），否则给默认名。"""
        goal = (goal or "").strip()
        if goal:
            return goal[:MAX_PROJECT_TITLE_LEN]
        return "手机新建项目"

    def _on_event_written(self, project_id: str, kind: str, content: str, meta: dict) -> None:
        """分发器每条事件落盘后转交（worker 线程）：以 QueuedConnection 回 UI 实时刷新。"""
        self.notifier.event_written.emit(project_id, kind, content, meta)

    def _append_event(self, project_id: str, event: ProjectEvent) -> None:
        try:
            self.project_store.append_event(project_id, event)
        except Exception:
            logger.exception("写入项目事件失败")
        self.notifier.event_written.emit(
            project_id, event.kind, event.content or "", dict(event.meta or {})
        )

    def _project_lock(self, project_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._project_locks.get(project_id)
            if lock is None:
                lock = threading.Lock()
                self._project_locks[project_id] = lock
            return lock
