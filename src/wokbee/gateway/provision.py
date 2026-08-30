"""扫码一键创建飞书自建应用（OAuth 2.0 设备流 / RFC 8628）。

用户用飞书 App 扫一次码，自动注册应用、开通机器人能力、预配权限与事件订阅，
凭证（client_id / client_secret）自动下发 —— 用户无需碰 app_id/app_secret。

⚠️ 约束：
- `register_app` 同步阻塞轮询，必须放在后台线程；`on_qr_code`/`on_status_change`
  在该后台线程触发，需经 Qt 信号桥回主线程渲染，不能在回调里碰 Qt 控件。
- `addons` 只能预配权限与事件订阅，**不能设置事件订阅方式（长连接/回调）**。
  跑 `lark.ws.Client` 长连接需确保应用事件订阅为「长连接」。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger("wokbee")

# 预置权限 + 事件订阅（智能体 preset 会在此基础上叠加，一般已含这两项）。
# ⚠️ lark_oapi 的 addons 形状：scopes 是 {tenant|user: [..]}；events.items 也必须是
#   {tenant|user: [..]}（嵌套对象），不能是扁平 [..]，否则 _normalize_addons 直接抛
#   “events.items must be an object”，on_qr_code 根本不会被调用。
DEFAULT_ADDONS = {
    "scopes": {"tenant": ["im:message:send_as_bot"]},
    "events": {"items": {"tenant": ["im.message.receive_v1"]}},
}


class FeishuProvisioner:
    """驱动飞书设备流，产出应用凭据。回调均在后台线程触发。"""

    def __init__(
        self,
        *,
        on_qr_code: Callable[[dict], None] | None = None,
        on_status_change: Callable[[dict], None] | None = None,
        name: str = "WokBee",
        desc: str = "手机遥控电脑上的 WokBee Agent",
        only_new: bool = False,
    ):
        self._on_qr = on_qr_code or (lambda info: None)
        self._on_status = on_status_change or (lambda info: None)
        self._app_preset = {"name": name, "desc": desc}
        self._only_new = only_new
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """中止同步轮询（窗口关闭 / 用户取消时调用）。"""
        self._cancel.set()

    def run(self) -> dict:
        """阻塞运行设备流，返回 {'client_id','client_secret','user_info'}。"""
        import lark_oapi as lark

        logger.info("开始飞书扫码创建应用（only_new=%s）", self._only_new)
        result = lark.register_app(
            on_qr_code=self._on_qr,
            on_status_change=self._on_status,
            addons=DEFAULT_ADDONS,
            create_only=self._only_new,
            app_preset=self._app_preset,
            cancel_event=self._cancel,
        )
        logger.info("飞书应用创建完成：%s", result.get("client_id"))
        return result
