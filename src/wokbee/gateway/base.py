"""消息网关的通道抽象：统一飞书 / 微信（iLink）的收发接口。

各通道只需实现这份最小契约即可接入 `GatewayManager`。路由、白名单、项目分发
不在通道职责内——通道只负责「扫码/连接 → 收到消息回调 → 发送文本回执」。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

# 供外部复用的解析工具（剥离群聊 @ 占位符）
_AT_PLACEHOLDER_RE = None  # 惰性编译，避免模块导入即拉高开销


def _compile_at_re():
    global _AT_PLACEHOLDER_RE
    if _AT_PLACEHOLDER_RE is None:
        import re

        # 飞书 mentions 里的两种占位写法：
        #   <at user_id=ou_xxx></at>  /  <at user_id=ou_xxx>名字</at>  /  @_user_1
        _AT_PLACEHOLDER_RE = re.compile(
            r"<at\s+[^>]*>.*?</at>|<at\s+[^>]*/>|@_user_\d+"
        )
    return _AT_PLACEHOLDER_RE


def strip_mentions(text: str) -> str:
    """把消息里的 @ 占位符/原样 @ 从群聊消息文本中剔除，只保留正文。

    仅针对飞书等会内联 `@_user_1` 或 `<at ...>` 占位符的来源；普通文本不受影响。
    """
    if not text:
        return ""
    return _compile_at_re().sub("", text).strip()


class ChannelStatus(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class ChannelMessage:
    """一条从手机 IM 归一的入站消息。"""

    channel: str  # "feishu" | "wechat" | "fake"
    sender_id: str  # 飞书 app 级 open_id
    text: str  # 已剥离 @ 的净文本
    conversation_id: str  # chat_id / 会话标识
    reply_to_message_id: str = ""  # 复读用（飞书 message_id）
    message_id: str = ""  # 去重 key（用 message_id，非 event_id）
    is_group: bool = False
    channel_meta: dict = field(default_factory=dict)


class Channel(ABC):
    """IM 通道契约。所有方法都不能阻塞/操作 UI 线程。"""

    @property
    @abstractmethod
    def status(self) -> ChannelStatus: ...

    @abstractmethod
    def start(self) -> None:
        """非阻塞启动：频道自行在 daemon 线程建立连接/轮询。"""

    @abstractmethod
    def stop(self) -> None:
        """停止并回收后台线程。"""

    @abstractmethod
    def on_message(self, callback: Callable[[ChannelMessage], None]) -> None:
        """注册消息回调。在频道收线程上触发，必须快速返回（只入队）。"""

    @abstractmethod
    def send_text(self, msg: ChannelMessage, text: str) -> tuple[bool, str]:
        """向 `msg` 的会话回一条文本。返回 (ok, 错误信息)。"""

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """校验凭据可用的连通性测试（不真正连接常驻通道）。"""

    def set_status_cb(self, callback: Callable[[ChannelStatus], None]) -> None:
        """可选：注册状态变化回调（如 `ConnectionError` 让 UI 显示真实连接失败）。"""
        self._status_cb = callback

    def _emit_status(self, status: ChannelStatus) -> None:
        cb = getattr(self, "_status_cb", None)
        if cb:
            try:
                cb(status)
            except Exception:
                pass


def _json_dumps(obj) -> str:
    """带非 ASCII 转义的安全 JSON 序列化，避免中文被占位符问题。"""
    return json.dumps(obj, ensure_ascii=False)
