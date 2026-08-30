"""WokBee 消息网关：把手机 IM（飞书首发）消息路由到本地项目 Agent。

本包只做网关自身；引擎（deepagents 等）在 `wokbee.engine`，由 `GatewayDispatcher`
在后台线程按需加载（与 `autobee.engine.executor` 相同契约，绝不在 UI 线程 import）。
"""

from __future__ import annotations

from wokbee.gateway.base import Channel, ChannelMessage, ChannelStatus
from wokbee.gateway.store import GatewayChannelConfig, GatewayStore
from wokbee.gateway.router import MessageRouter, RouteOutcome, RouteResult
from wokbee.gateway.dispatcher import GatewayDispatcher
from wokbee.gateway.feishu import FeishuChannel
from wokbee.gateway.provision import FeishuProvisioner
from wokbee.gateway.manager import GatewayManager, GatewayNotifier

__all__ = [
    "Channel",
    "ChannelMessage",
    "ChannelStatus",
    "GatewayChannelConfig",
    "GatewayStore",
    "MessageRouter",
    "RouteOutcome",
    "RouteResult",
    "GatewayDispatcher",
    "FeishuChannel",
    "FeishuProvisioner",
    "GatewayManager",
    "GatewayNotifier",
]
