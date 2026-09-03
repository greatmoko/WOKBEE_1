"""飞书通道：基于官方 `lark-oapi` 的 WebSocket 长连接（免公网 IP / 免回调 / 免 SSL）。

⚠️ 包名是 `lark-oapi`，import 模块是 `lark_oapi`——`from lark.ws import Client`
会误引无关的 `lark` 解析包。SDK 在方法内懒加载，未安装 lark-oapi 时该模块仍可被 import。
"""

from __future__ import annotations

import asyncio
import json
import threading
import logging
from typing import Callable

from wokbee.gateway.base import Channel, ChannelMessage, ChannelStatus, strip_mentions
from wokbee.gateway.store import GatewayChannelConfig

logger = logging.getLogger("wokbee")

_LARK_ERR = "未安装 lark-oapi（pip install lark-oapi>=1.5.5），无法连接飞书。"


class FeishuChannel(Channel):
    """通过 `lark.ws.Client` 以长连接接收飞书消息，并回文本。"""

    def __init__(self, config: GatewayChannelConfig | None = None):
        self._cfg = config or GatewayChannelConfig()
        self._status = ChannelStatus.IDLE
        self._cb: Callable[[ChannelMessage], None] | None = None
        self._ws_thread: threading.Thread | None = None
        self._client = None          # WS 长连接客户端（收事件）
        self._rest_client = None     # 独立 REST 客户端（发消息）
        self._loop = None            # 本频道专用 asyncio 事件循环
        self._lock = threading.Lock()
        self._stop_requested = False

    @property
    def status(self) -> ChannelStatus:
        return self._status

    def _set_status(self, s: ChannelStatus):
        with self._lock:
            self._status = s
        self._emit_status(s)

    def on_message(self, callback: Callable[[ChannelMessage], None]) -> None:
        self._cb = callback

    def start(self) -> None:
        try:
            from lark_oapi.ws import Client as LarkWsClient
        except Exception as e:  # noqa: BLE001
            logger.error("导入 lark-oapi 失败: %s", e)
            self._set_status(ChannelStatus.ERROR)
            return
        if not (self._cfg.feishu_app_id and self._cfg.feishu_app_secret):
            self._set_status(ChannelStatus.ERROR)
            return
        # 事件订阅方式必须是「长连接」：addons 无法设置订阅方式，若应用配置的是 webhook
        # 长连接将收不到事件。此处按长连接客户端启动；订阅方式错误表现为「连上但收不到」。
        self._client = LarkWsClient(
            self._cfg.feishu_app_id,
            self._cfg.feishu_app_secret,
            event_handler=self._build_handler(),
        )
        # 复用 SDK 自带的重连钩子：失联时降级为 CONNECTING，恢复时回 CONNECTED。
        self._client.on_reconnecting = lambda: self._set_status(ChannelStatus.CONNECTING)
        self._client.on_reconnected = lambda: self._set_status(ChannelStatus.CONNECTED)
        self._stop_requested = False
        self._set_status(ChannelStatus.CONNECTING)
        self._ws_thread = threading.Thread(
            target=self._run_ws, name="feishu-ws", daemon=True
        )
        self._ws_thread.start()

    def _run_ws(self) -> None:
        """运行长连接并监控状态，让 UI 显示真实连接状态。

        SDK 所有异步方法都引用模块级单例事件循环 `lark_oapi.ws.client.loop`。若直接
        复用该单例，`stop()` 无法干净退出：旧任务/旧 WS 残留，重启会撞上
        「event loop is already running」并误报 ERROR，甚至让旧连接继续投递幽灵消息。

        这里为每个频道创建**独立事件循环**，并把 SDK 的模块级 loop 指针改到本频道
        的循环上（进程内同一时刻只有一个飞书频道，改动安全）。`stop()` 会关闭 WS、
        取消任务并关闭本循环，彻底断开、可安全重启。
        """
        client = self._client
        try:
            from lark_oapi.ws import client as _ws_client_cls
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            _ws_client_cls.loop = loop  # 让 SDK 的异步调度落到本频道专用循环
            self._loop = loop
        except Exception:  # noqa: BLE001
            logger.exception("无法创建飞书连接事件循环")
            self._set_status(ChannelStatus.ERROR)
            return

        try:
            loop.run_until_complete(client._connect())
        except Exception:  # noqa: BLE001
            if not self._stop_requested:
                logger.exception("飞书首次连接失败")
                self._set_status(ChannelStatus.ERROR)
            self._close_loop(loop)
            return

        # 连上了 —— SDK 无首连回调，这里显式置 CONNECTED。
        self._set_status(ChannelStatus.CONNECTED)
        logger.info("飞书长连接已建立：%s", getattr(client, "_conn_url", ""))

        # 维持心跳：启动 SDK 的 ping 常驻任务，然后 run_forever 直到 stop() 调 loop.stop()。
        try:
            loop.create_task(client._ping_loop())
        except Exception:  # noqa: BLE001
            logger.exception("启动飞书心跳失败")
        try:
            loop.run_forever()
        except Exception:  # noqa: BLE001
            logger.debug("飞书连接循环被停止")
        if self._stop_requested:
            self._set_status(ChannelStatus.STOPPED)
        else:
            # 未请求停止却退出（断线且重连耗尽）→ 失败
            self._set_status(ChannelStatus.ERROR)
        self._close_loop(loop)

    def _close_loop(self, loop) -> None:
        """取消剩余任务并关闭本频道专用事件循环，避免任务/资源残留。"""
        self._loop = None
        if loop is None or loop.is_closed():
            return
        try:
            tasks = [t for t in asyncio.all_tasks(loop)]
            for t in tasks:
                t.cancel()
            if tasks:
                loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # noqa: BLE001
            logger.exception("清理飞书事件循环任务失败")
        finally:
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._stop_requested = True
        client = self._client
        loop = self._loop
        if client is not None:
            try:
                client._auto_reconnect = False  # 关闭内部自动重连避免阻塞
                if loop is not None and loop.is_running() and not loop.is_closed():
                    async def _shutdown():
                        # 关闭 WS 长连接，让收发任务尽快结束；剩余任务由 _close_loop 取消。
                        try:
                            await client._disconnect()
                        except Exception:  # noqa: BLE001
                            pass
                    loop.call_soon_threadsafe(loop.create_task, _shutdown())
                    loop.call_soon_threadsafe(loop.stop)
                elif loop is not None and not loop.is_running() and not loop.is_closed():
                    # 尚处于连接阶段就被 stop：直接断开连接并关闭循环。
                    try:
                        loop.run_until_complete(client._disconnect())
                    except Exception:  # noqa: BLE001
                        pass
                    self._close_loop(loop)
            except Exception:  # noqa: BLE001
                logger.exception("停止飞书长连接失败")
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=5.0)
            self._ws_thread = None
        self._client = None
        self._set_status(ChannelStatus.STOPPED)

    def send_text(self, msg: ChannelMessage, text: str) -> tuple[bool, str]:
        if not text:
            text = "（空回复）"
        if not (self._cfg.feishu_app_id and self._cfg.feishu_app_secret):
            return False, "未配置 app_id/app_secret"
        content = json.dumps({"text": text}, ensure_ascii=False)

        # 发送走独立的 REST 客户端：WS `Client` 没有 `.im` 接口，只能收发事件。
        # 复用已有实例（懒建），避免每次发消息都新建连接。
        rest = self._get_rest_client()
        if rest is None:
            return False, "REST 客户端初始化失败"

        def _reply_once() -> tuple[bool, str]:
            """先按消息回复，失败/无 message_id 再退化为按会话/私聊创建消息（更稳）。"""
            if msg.reply_to_message_id:
                try:
                    from lark_oapi.api.im.v1 import (
                        ReplyMessageRequest,
                        ReplyMessageRequestBody,
                    )
                    body = (
                        ReplyMessageRequestBody.builder()
                        .content(content)
                        .msg_type("text")
                        .build()
                    )
                    req = (
                        ReplyMessageRequest.builder()
                        .message_id(msg.reply_to_message_id)
                        .request_body(body)
                        .build()
                    )
                    resp = rest.im.v1.message.reply(req)
                    if resp is not None and getattr(resp, "code", -1) == 0:
                        return True, getattr(resp, "msg", "") or "ok"
                except Exception:  # noqa: BLE001
                    logger.exception("按消息回复失败，退化为按会话发送")
            if msg.conversation_id:
                ok, err = _create_text(rest, content, "chat_id", msg.conversation_id)
                if ok:
                    return True, err
                return False, err
            return False, "无 message_id 也无会话，无法回复"

        def _create_text(rest, content, receive_id_type, receive_id) -> tuple[bool, str]:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .content(content)
                .msg_type("text")
                .build()
            )
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)  # 注意：receive_id_type 在 Request 上
                .request_body(body)
                .build()
            )
            resp = rest.im.v1.message.create(req)
            if resp is not None and getattr(resp, "code", -1) == 0:
                return True, getattr(resp, "msg", "") or "ok"
            return False, getattr(resp, "msg", "") or f"code={getattr(resp, 'code', None)}"

        try:
            ok, err = _reply_once()
            if ok:
                return ok, err
            # 再兜底一次：用发送者 open_id 直接私聊（若会话建不了）
            if msg.sender_id:
                ok, err2 = _create_text(rest, content, "open_id", msg.sender_id)
                if ok:
                    return ok, err2
                err = err2 or err
            return False, err
        except Exception as e:  # noqa: BLE001
            logger.exception("发送飞书消息失败")
            return False, str(e)

    def _get_rest_client(self):
        """懒建独立的 REST 客户端用于发送消息（WS 客户端无 `.im`）。"""
        if self._rest_client is not None:
            return self._rest_client
        try:
            from lark_oapi import Client as RestClient
            self._rest_client = (
                RestClient.builder()
                .app_id(self._cfg.feishu_app_id)
                .app_secret(self._cfg.feishu_app_secret)
                .build()
            )
            return self._rest_client
        except Exception as e:  # noqa: BLE001
            logger.exception("初始化飞书 REST 客户端失败: %s", e)
            return None

    def test_connection(self) -> tuple[bool, str]:
        from wokbee.gateway.store import GatewayStore
        return GatewayStore().test_connection(self._cfg)

    # ── 内部 ─────────────────────────────
    def _build_handler(self):
        from lark_oapi import EventDispatcherHandler

        def _on_event(data):
            self._on_message_v1(data)

        return (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_event)
            .build()
        )

    def _on_message_v1(self, data) -> None:
        """飞书收线程回调：最小化处理（<3s ack），只组 ChannelMessage 入队，绝不跑 Agent。"""
        try:
            ev = data.event
            sender = getattr(ev, "sender", None)
            sender_id = ""
            if sender is not None:
                sid = getattr(sender, "sender_id", None)
                if sid is not None:
                    sender_id = getattr(sid, "open_id", "") or getattr(sid, "user_id", "") \
                        or getattr(sid, "union_id", "") or ""
            message = getattr(ev, "message", None)
            if message is None:
                return
            chat_id = getattr(message, "chat_id", "") or ""
            message_id = getattr(message, "message_id", "") or ""
            msg_type = getattr(message, "message_type", "") or ""
            chat_type = getattr(message, "chat_type", "") or "p2p"
            is_group = chat_type in ("group",)
            content_str = getattr(message, "content", "") or "{}"
            mentions = getattr(message, "mentions", None) or []
            text = self._extract_text(msg_type, content_str)

            cm = ChannelMessage(
                channel="feishu",
                sender_id=sender_id or "",
                text=text,
                conversation_id=chat_id or "",
                reply_to_message_id=message_id,
                message_id=message_id,
                is_group=is_group,
                channel_meta={"message_type": msg_type, "raw": str(content_str)},
            )
            if msg_type and msg_type not in ("text", "post"):
                cm.channel_meta["unsupported_type"] = msg_type
            if self._cb:
                try:
                    self._cb(cm)
                except Exception:
                    logger.exception("飞书消息回调失败")
        except Exception:
            logger.exception("解析飞书消息失败")

    @staticmethod
    def _extract_text(msg_type: str, content_str: str) -> str:
        """从飞书消息 content 里抠文本；图片/语音/文件等不支持。"""
        if msg_type == "text":
            try:
                body = json.loads(content_str or "{}")
                return strip_mentions(body.get("text", ""))
            except json.JSONDecodeError:
                return ""
        if msg_type == "post":
            try:
                body = json.loads(content_str or "{}")
                chunks = []
                for line in body.get("content") or []:
                    for seg in line if isinstance(line, list) else []:
                        if isinstance(seg, dict) and seg.get("text"):
                            chunks.append(seg["text"])
                return strip_mentions("".join(chunks)) if chunks else ""
            except json.JSONDecodeError:
                return ""
        # 媒体类型：v1 不支持，交由 manager 回执提示
        return ""
