"""微信扫码登录（iLink / dsh-im 同款）：扫一次，签发一套 botToken 凭据。

⚠️ 约束（读 `weixin_ilink.auth.login_with_qr` / `bot.login` 源码）：
- `weixin_ilink.login(...)` **同步阻塞**，最长 ~8 分钟（LOGIN_TIMEOUT），**没有 cancel 参数**。
  必须放后台线程跑；窗口关闭/取消只能设标志忽略结果（线程为 daemon，随进程结束）。
- `on_qrcode(url)` 可能被**多次**调用（二维码自动过期/刷新，最多 3 次），UI 需每次重绘。
- 无 `expire_in` 倒计时；用「长时间未扫码」提示即可，SDK 会自动刷新。

返回值为 SDK 的 info_json dict：`{"botToken","accountId","baseUrl","userId"}`。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger("wokbee")


class WeChatProvisioner:
    """驱动微信扫码登录，产出 info_json 凭据。回调均在调用线程（后台）触发。"""

    def __init__(
        self,
        *,
        on_qrcode: Callable[[str], None] | None = None,
        on_status_change: Callable[[str], None] | None = None,
        base_url: str | None = None,
    ):
        self._on_qr = on_qrcode or (lambda url: None)
        self._on_status = on_status_change or (lambda s: None)
        self._base_url = base_url
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """设置取消标志。SDK 无原生取消，仅让 `run` 结束后丢弃结果。"""
        self._cancelled.set()

    def run(self) -> dict:
        """阻塞运行扫码登录，返回 info_json dict。已取消则抛 InterruptedError。"""
        import weixin_ilink

        logger.info("开始微信扫码登录（base_url=%s）", self._base_url)
        info = weixin_ilink.login(
            on_qrcode=self._on_qr,
            on_status_change=self._on_status,
            base_url=self._base_url,
        )
        if self._cancelled.is_set():
            raise InterruptedError("微信扫码已取消")
        logger.info("微信登录成功：accountId=%s", info.get("accountId"))
        return info
