"""微信通道：基于腾讯 iLink 协议（个人微信账号，手机扫码登录，HTTPS 长轮询）。

与 dsh-im 同一协议。凭据 = 一次扫码签发的一套 token（`wechat_bot_token` 等四字段），
存进 `~/.wokbee/config.json`，重启自动重连；但 token 会过期（session -14），过期需重新扫码。

⚠️ 设计要点（均读 SDK 源码核过，非猜）：
- 不要用 `bot.messages()`：它吞掉一切 poll 异常、en 错误码 -14 时 `sleep(3600)`、且只在循环
  顶部检查 `_stop` → 无法及时感知会话过期/停止。本项目**自己驱动 `bot.client.poll()`**。
- **必须传 `cursor_file`**：iLink 不按消息 id 去重，靠 `get_updates_buf` 游标推进；不持久化
  重启后会重放最近一次窗口的消息。依赖 SDK 在每次 poll 后把游标写到该文件、构造时读回。
- 收紧 `long_poll_timeout=10s`，让 `stop()` 在 ~10s 内生效（SDK 默认 35s）。
- iLink 是**私聊（DM）**：会话对象就是 `from_user`；回复必须带 `context_token`（定向）。
- SDK 在方法内懒加载，未安装 `weixin-ilink` 时本模块仍可被 import。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from wokbee.gateway.base import Channel, ChannelMessage, ChannelStatus
from wokbee.gateway.store import GatewayChannelConfig, default_wechat_cursor_path

logger = logging.getLogger("wokbee")

_WECHAT_NETWORK_ERR = "未安装 weixin-ilink（pip install weixin-ilink>=0.3.5），无法连接微信。"

# 会话过期错误码（SDK：SESSION_EXPIRED_ERRCODE）
_SESSION_EXPIRED = -14
# 出站文本按 1800 大块切分（dsh-im 同款，比 SDK 默认 4000 更稳妥，避免超长消息受限）。
_WEIXIN_CHUNK = 1800

# 这些迹象代表「鉴权/会话失效」（token 过期、401/403、服务端报 -14）→ 应停下并提示重新扫码；
# 其余异常（网络瞬断、服务波动）只标记为临时失败，退避重试而不是杀轮询线程。
_SESSION_EXPIRED_MARKERS = ("401", "403", "unauthor", "session expired", "expired", "-14")


class WeChatChannel(Channel):
    """通过 iLink 长轮询接收微信消息，并回文本。"""

    def __init__(self, config: GatewayChannelConfig | None = None, bot=None):
        self._cfg = config or GatewayChannelConfig()
        self._bot = bot  # 测试可注入假 bot；None 时在 start() 懒建
        self._bot_info: dict = {}  # 建 _bot 时用的凭据，用于检测凭据已变（重新扫码）后重建
        self._status = ChannelStatus.IDLE
        self._cb: Callable[[ChannelMessage], None] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_requested = False
        self._last_err = ""

    @property
    def status(self) -> ChannelStatus:
        return self._status

    def _set_status(self, s: ChannelStatus):
        with self._lock:
            self._status = s
        self._emit_status(s)

    def on_message(self, callback: Callable[[ChannelMessage], None]) -> None:
        self._cb = callback

    # ── 生命周期 ─────────────────────────────
    def start(self) -> None:
        info = self._cfg.wechat_info()
        if not info:
            self._last_err = "未配置微信凭据（请先扫码登录）"
            self._set_status(ChannelStatus.ERROR)
            return
        if self._bot is None or self._bot_info != info:
            if not self._build_bot(info):
                self._set_status(ChannelStatus.ERROR)
                return
        if self._thread is not None and self._thread.is_alive():
            # 旧轮询线程仍在退出中（在途 ~10s 长轮询）；避免同 token 双轮询器。
            logger.warning("微信轮询线程仍在退出中，忽略本次 start")
            self._set_status(ChannelStatus.CONNECTING)
            return
        self._stop_requested = False
        self._set_status(ChannelStatus.CONNECTING)
        self._thread = threading.Thread(
            target=self._run_loop, name="wechat-poll", daemon=True
        )
        self._thread.start()

    def _build_bot(self, info: dict) -> bool:
        try:
            from weixin_ilink import WeixinBot
        except Exception as e:  # noqa: BLE001
            logger.error("导入 weixin-ilink 失败: %s", e)
            self._last_err = _WECHAT_NETWORK_ERR
            return False
        cursor = self._cfg.wechat_cursor_file or str(default_wechat_cursor_path())
        try:
            Path(cursor).parent.mkdir(parents=True, exist_ok=True)
            self._bot = WeixinBot(credentials=info, cursor_file=cursor, auto_save_cursor=True)
        except Exception as e:  # noqa: BLE001
            logger.exception("构造 WeixinBot 失败")
            self._last_err = str(e)
            return False
        # 收紧长轮询超时（SDK 默认 35s）：stop() 得以在 ~10s 内生效。
        try:
            self._bot.client.opts.long_poll_timeout = 10.0
        except Exception:  # noqa: BLE001
            pass
        self._bot_info = info
        return True

    def _run_loop(self) -> None:
        """自己驱动 `poll()`（不用 `bot.messages()`），便于感知 -14 / 及时停止。

        关键区分两类失败，避免「连续 3 次」误判把正常通道杀掉：
        - **鉴权/会话失效**（token 过期 → HTTP 401/403，或 SDK 报 ret=-14）：直接停下，
          `_last_err` 给出「请重新扫码」的可执行提示。
        - **临时网络/服务错误**（短时断连、服务波动）：只把状态标成 ERROR 让 UI 可见，
          退避后继续轮询（网络恢复即自动回 CONNECTED），**不**杀线程、不退出。
        """
        bot, cb = self._bot, self._cb
        transient = 0  # 连续临时失败计数（用于退避 / 首次置 ERROR）
        while not self._stop_requested:
            try:
                resp = bot.client.poll()
            except Exception as e:  # noqa: BLE001
                transient += 1
                if self._is_session_expired(str(e)):
                    self._last_err = "会话已过期或凭据失效，请重新扫码登录"
                    logger.error("微信鉴权/会话失效，停止：%s", e)
                    self._set_status(ChannelStatus.ERROR)
                    return
                self._last_err = str(e)
                # 临时失败：显示 ERROR（含原因），但退避后继续重试（网络恢复自动回连）
                if transient >= 3:
                    logger.warning("微信 poll 连续失败 %s 次（临时错误，正重试）：%s", transient, e)
                    self._set_status(ChannelStatus.ERROR)
                time.sleep(min(2 * transient, 15))
                continue
            ret = resp.get("ret") or 0
            errcode = resp.get("errcode") or 0
            if ret or errcode:
                if ret == _SESSION_EXPIRED or errcode == _SESSION_EXPIRED:
                    self._last_err = "会话已过期，请重新扫码登录"
                    logger.error("微信会话已过期（%s），请重新扫码登录", _SESSION_EXPIRED)
                    self._set_status(ChannelStatus.ERROR)
                    return
                # 其它业务错误码（限流/服务波动）→ 同临时失败：退避重试，不杀线程
                transient += 1
                self._last_err = f"ret={ret} errcode={errcode}"
                if transient >= 3:
                    self._set_status(ChannelStatus.ERROR)
                time.sleep(min(2 * transient, 15))
                continue
            transient = 0  # 干净成功的空轮询/收到消息 → 清零临时计数
            # 持久化游标（镜像 SDK messages() 的 auto_save_cursor）：重启后不重放上一窗口
            cursor_file = getattr(bot, "_cursor_file", None)
            if cursor_file is not None and getattr(bot, "_auto_save_cursor", True):
                try:
                    cursor_file.write_text(bot.client.cursor, encoding="utf-8")
                except OSError:
                    pass
            if self._status != ChannelStatus.CONNECTED:
                self._set_status(ChannelStatus.CONNECTED)
            for m in resp.get("msgs") or []:
                try:
                    if (m.get("message_type") or 0) != 1:  # MessageType.USER
                        continue
                    from_user = m.get("from_user_id") or ""
                    if not from_user:
                        continue
                    if m.get("context_token"):
                        bot._ctx_cache[from_user] = m["context_token"]
                    for item in m.get("item_list") or []:
                        im = self._make_im(m, item, bot)
                        cm = self._to_channel_message(im)
                        if cm is None:
                            continue
                        if cb:
                            try:
                                cb(cm)
                            except Exception:  # noqa: BLE001
                                logger.exception("微信消息回调失败")
                except Exception:  # noqa: BLE001
                    logger.exception("解析微信消息失败")
        self._set_status(ChannelStatus.STOPPED)

    @staticmethod
    def _is_session_expired(msg: str) -> bool:
        """从异常文本判断是否属于「鉴权/会话失效」，以便给出可执行的重新扫码提示。

        SDK 的 `api_post` 在非 200 时抛出 `RuntimeError(f"{endpoint} {status_code}: {text}")`，
        鉴权失效往往以 HTTP 401/403 形式出现（而非返回 ret=-14），故按文本关键字判定。
        """
        low = (msg or "").lower()
        return any(m in low for m in _SESSION_EXPIRED_MARKERS)

    @staticmethod
    def _make_im(m: dict, item: dict, bot):
        from weixin_ilink import IncomingMessage
        return IncomingMessage(raw_message=m, raw_item=item, _bot=bot)

    def stop(self) -> None:
        self._stop_requested = True
        bot = self._bot
        if bot is not None:
            try:
                bot.stop()
            except Exception:  # noqa: BLE001
                logger.exception("停止 WeixinBot 失败")
        # 不 join：在途 ~10s 长轮询会自然返回，daemon 线程自行退出（契约：不阻塞 UI 线程）。
        self._set_status(ChannelStatus.STOPPED)

    def send_text(self, msg: ChannelMessage, text: str) -> tuple[bool, str]:
        if not text:
            text = "（空回复）"
        bot = self._bot
        if bot is None:
            return False, self._last_err or "未连接"
        # context_token：优先取入站消息携带的，兜底用 bot 的会话缓存（DM 定向必需）。
        ctx = (msg.channel_meta or {}).get("context_token", "")
        if not ctx:
            ctx = getattr(bot, "_ctx_cache", {}).get(msg.sender_id, "")
        if not ctx:
            return False, "缺少 context_token，无法定向回复（请先给机器人发一条消息）"
        try:
            from weixin_ilink.markdown import filter_markdown
            cleaned = filter_markdown(text)  # 剥掉微信客户端不渲染的 MD 语法，回复更可读
        except Exception:  # noqa: BLE001
            cleaned = text
        try:
            bot.client.send_text_chunked(msg.sender_id, cleaned, ctx, max_length=_WEIXIN_CHUNK)
            return True, "ok"
        except Exception as e:  # noqa: BLE001
            logger.exception("发送微信消息失败")
            return False, str(e)

    def test_connection(self) -> tuple[bool, str]:
        info = self._cfg.wechat_info()
        if not info:
            return False, "未配置微信凭据（请先扫码登录）"
        try:
            from weixin_ilink.client import ILinkClient

            cli = ILinkClient(
                base_url=info["baseUrl"], token=info["botToken"],
                long_poll_timeout=2.0, api_timeout=4.0,
            )
            resp = cli.poll()
            ret = resp.get("ret") or 0
            errcode = resp.get("errcode") or 0
            if ret == 0 and errcode == 0:
                return True, "连接成功"
            if ret == _SESSION_EXPIRED or errcode == _SESSION_EXPIRED:
                return False, "会话已过期，请重新扫码登录"
            return False, f"ret={ret} errcode={errcode}"
        except Exception as e:  # noqa: BLE001
            if self._is_session_expired(str(e)):
                return False, "会话已过期或凭据失效，请重新扫码登录"
            return False, f"连接失败：{e}"

    # ── 消息归一化 ─────────────────────────────
    @staticmethod
    def _to_channel_message(im) -> ChannelMessage | None:
        from_user = getattr(im, "from_user", "") or ""
        if not from_user:
            return None
        mid = str(im.message_id) if getattr(im, "message_id", None) is not None else ""
        cm = ChannelMessage(
            channel="wechat",
            sender_id=from_user,
            text=im.text or "",
            conversation_id=from_user,  # iLink 为 DM：会话对象即 from_user
            message_id=mid,
            reply_to_message_id=mid,
            is_group=False,
            channel_meta={
                "context_token": getattr(im, "context_token", "") or "",
                "item_type": getattr(im, "item_type", 0),
                "session_id": getattr(im, "session_id", "") or "",
            },
        )
        if getattr(im, "is_image", False):
            cm.channel_meta["unsupported_type"] = "image"
        elif getattr(im, "is_file", False):
            cm.channel_meta["unsupported_type"] = "file"
        elif getattr(im, "is_video", False):
            cm.channel_meta["unsupported_type"] = "video"
        elif getattr(im, "is_voice", False):
            cm.channel_meta["voice_asr"] = True  # text 已是 ASR 转写
        return cm
