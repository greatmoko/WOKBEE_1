"""消息网关配置存储：镜像 `McpStore`，用 `Config` 点路径存到 `~/.wokbee/config.json`。

不单开文件，避免多一条加载路径、多一份单例管理。`feishu_app_id/secret` 两个来源：
扫码创建（provision.py 的 `register_app` 设备流）自动回填，或用户手动粘贴。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tokbee.core.config import Config

logger = logging.getLogger("wokbee")

# 微信 iLink 凭据必备的四键（与 SDK 的 info_json 一一对应）；userId 可为空。
_WECHAT_REQUIRED = ("botToken", "accountId", "baseUrl")


def default_wechat_cursor_path() -> Path:
    """微信消息去重游标的默认保存位置（`~/.wokbee/wechat_cursor.txt`）。

    iLink 协议不按消息 id 自行去重，靠 `get_updates_buf` 游标推进；若不持久化，重启后会
    重复推最近一个窗口的消息。靠 `cursor_file` 才能做到「重启不重放」。
    """
    return Path.home() / ".wokbee" / "wechat_cursor.txt"


def wechat_creds_ok(creds: dict | None) -> bool:
    """判断微信 iLink 凭据是否齐全（botToken / accountId / baseUrl 三者非空才可连接）。"""
    if not creds:
        return False
    return all(creds.get(k) for k in _WECHAT_REQUIRED)


@dataclass
class GatewayChannelConfig:
    """网关整体配置（多频道可**同时**启用：每个频道各一个开关，互不影响，不再只跑一条）。

    `enabled` 保留但改义为「任一频道开启」的总标记（兼容旧判断/显示）；
    `channel` 只作为工作区默认显示的频道（不再用它决定哪条能跑）。真正决定某频道是否
    连接的是 `feishu_enabled` / `wechat_enabled`。
    """

    enabled: bool = False  # 总启用标记 = 任一频道开启
    channel: str = "feishu"  # 主显示频道（工作区默认显示哪条）
    feishu_enabled: bool = False  # 飞书频道独立启用开关
    wechat_enabled: bool = False  # 微信频道独立启用开关
    default_project_id: str = ""  # 通用默认项目（历史兜底；新数据优先用频道专属）
    feishu_default_project_id: str = ""  # 飞书频道各自绑定的默认项目
    wechat_default_project_id: str = ""  # 微信频道各自绑定的默认项目
    allow_from: list[str] = field(default_factory=list)  # 允许触发的 sender_id 列表
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    # 微信 iLink（不再用 app_id/secret；凭据为一次扫码签发的一套 token）
    wechat_bot_token: str = ""
    wechat_account_id: str = ""
    wechat_base_url: str = ""  # 例如 https://ilinkai.weixin.qq.com
    wechat_user_id: str = ""  # 可选
    wechat_cursor_file: str = ""  # 可选覆盖；空 -> default_wechat_cursor_path()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> GatewayChannelConfig:
        d = data or {}
        enabled = bool(d.get("enabled", False))
        channel = str(d.get("channel") or "feishu")
        # 多频道迁移：旧配置只存 enabled + channel（单频道模型，一次只能跑一条）。
        # 首次读到带多频道开关的配置时，把「旧时代启用的那条」映射到对应频道开关，
        # 其余频道默认关——避免把两条都意外打开。
        # 旧配置的全局默认项目 default_project_id：在还没有频道专属字段时，把它迁移到
        # 各频道的专属字段（仅当该频道专属字段缺席时），之后 default_project_for 只认专属字段。
        legacy_default = str(d.get("default_project_id") or "")
        return cls(
            enabled=enabled,
            channel=channel,
            feishu_enabled=bool(d.get("feishu_enabled", enabled and channel == "feishu")),
            wechat_enabled=bool(d.get("wechat_enabled", enabled and channel == "wechat")),
            default_project_id=legacy_default,
            feishu_default_project_id=str(d.get("feishu_default_project_id", legacy_default) or ""),
            wechat_default_project_id=str(d.get("wechat_default_project_id", legacy_default) or ""),
            allow_from=[str(x) for x in (d.get("allow_from") or [])],
            feishu_app_id=str(d.get("feishu_app_id") or ""),
            feishu_app_secret=str(d.get("feishu_app_secret") or ""),
            wechat_bot_token=str(d.get("wechat_bot_token") or ""),
            wechat_account_id=str(d.get("wechat_account_id") or ""),
            wechat_base_url=str(d.get("wechat_base_url") or ""),
            wechat_user_id=str(d.get("wechat_user_id") or ""),
            wechat_cursor_file=str(d.get("wechat_cursor_file") or ""),
        )

    def default_project_for(self, channel: str) -> str:
        """按频道取无前缀消息的默认项目 id（各频道各绑各的，**不再回落全局默认**）。

        之前会 `or self.default_project_id` 回落到历史全局默认，导致「微信取消绑定后仍路由到旧
        项目」。现在：某频道自行绑定为空即「无默认」，应返回项目清单让用户用 @项目 指定。
        旧配置里的全局默认已在 from_dict 迁移进各频道专属字段（见 from_dict）。
        """
        if channel == "wechat":
            return self.wechat_default_project_id
        if channel == "feishu":
            return self.feishu_default_project_id
        return self.default_project_id

    def set_default_project(self, channel: str, project_id: str) -> None:
        """把某频道的默认项目设成 `project_id`（IM 里 @项目ID 命中后即切换为该频道默认，可多次切换）。"""
        if channel == "wechat":
            self.wechat_default_project_id = project_id
        elif channel == "feishu":
            self.feishu_default_project_id = project_id
        else:
            self.default_project_id = project_id

    def channel_enabled(self, channel: str) -> bool:
        """某频道是否被启用（多频道互不影响；未知频道回落总开关）。"""
        if channel == "wechat":
            return bool(self.wechat_enabled)
        if channel == "feishu":
            return bool(self.feishu_enabled)
        return bool(self.enabled)

    def set_channel_enabled(self, channel: str, enable: bool) -> None:
        """开启/关闭某频道；总开关 `enabled` 同步为「任一频道开启」。"""
        if channel == "wechat":
            self.wechat_enabled = bool(enable)
        elif channel == "feishu":
            self.feishu_enabled = bool(enable)
        else:
            self.enabled = bool(enable)
        self.enabled = bool(self.feishu_enabled or self.wechat_enabled)

    def wechat_info(self) -> dict:
        """组装成 SDK 需要的 info_json。缺 botToken/accountId/baseUrl 时视为未配置。"""
        if not wechat_creds_ok(
            {"botToken": self.wechat_bot_token, "accountId": self.wechat_account_id,
             "baseUrl": self.wechat_base_url}
        ):
            return {}
        return {
            "botToken": self.wechat_bot_token,
            "accountId": self.wechat_account_id,
            "baseUrl": self.wechat_base_url,
            "userId": self.wechat_user_id or "",
        }


class GatewayStore:
    """读写 `wokbee.gateway` 配置键。"""

    _KEY = "wokbee.gateway"

    def __init__(self, config: Config | None = None):
        self._config = config or Config()
        if self._config.get(self._KEY) is None:
            self._config.set(self._KEY, GatewayChannelConfig().to_dict())
            self._config.save()

    def get_config(self) -> GatewayChannelConfig:
        raw = self._config.get(self._KEY) or {}
        if not isinstance(raw, dict):
            raw = {}
        return GatewayChannelConfig.from_dict(raw)

    def save_config(self, cfg: GatewayChannelConfig) -> None:
        self._config.set(self._KEY, cfg.to_dict())
        self._config.save()

    def set_enabled(self, flag: bool) -> None:
        cfg = self.get_config()
        cfg.enabled = bool(flag)
        self.save_config(cfg)

    def test_connection(
        self,
        cfg: GatewayChannelConfig | None = None,
        timeout: float = 8.0,
    ) -> tuple[bool, str]:
        """按 `cfg.channel` 分派连通性校验，不真正建立常驻连接。

        - 飞书：请求租户 access_token（校验 app_id/app_secret）。
        - 微信：构造一个独立的 `ILinkClient` 向 `getupdates` 发一次性请求（校验 botToken）。
        空凭据直接返回未配置，不发网络请求；用线程 + asyncio.wait_for 带超时，
        避免没有超时的网络调用卡死界面。
        """
        cfg = cfg or self.get_config()
        if cfg.channel == "wechat":
            return self._test_wechat(cfg, timeout)
        return self._test_feishu(cfg, timeout)

    @staticmethod
    def _test_feishu(
        cfg: GatewayChannelConfig, timeout: float
    ) -> tuple[bool, str]:
        if not (cfg.feishu_app_id and cfg.feishu_app_secret):
            return False, "未配置 app_id/app_secret（可先扫码创建机器人）"
        result: dict = {}

        def _worker():
            try:
                import httpx

                resp = httpx.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": cfg.feishu_app_id, "app_secret": cfg.feishu_app_secret},
                    timeout=timeout,
                )
                body = resp.json() if resp.status_code == 200 else {}
                result["code"] = body.get("code")
                result["msg"] = body.get("msg", f"HTTP {resp.status_code}")
            except Exception as e:  # noqa: BLE001
                result["error"] = str(e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        try:
            asyncio.run(_wait_thread(t, timeout))
        except TimeoutError:
            return False, f"连接超时（> {timeout:.0f} 秒）"
        if "error" in result:
            return False, result["error"]
        if result.get("code") == 0:
            return True, result.get("msg") or "连接成功"
        return False, result.get("msg") or f"code={result.get('code')}"

    @staticmethod
    def _test_wechat(
        cfg: GatewayChannelConfig, timeout: float
    ) -> tuple[bool, str]:
        info = cfg.wechat_info()
        if not info:
            return False, "未配置微信凭据（请先扫码登录）"
        result: dict = {}

        def _worker():
            try:
                from weixin_ilink.client import ILinkClient

                # 独立客户端、短轮询：只探测 token 是否有效，不碰正在跑的轮询线程。
                cli = ILinkClient(
                    base_url=info["baseUrl"], token=info["botToken"],
                    long_poll_timeout=2.0, api_timeout=timeout,
                )
                resp = cli.poll()
                ret = resp.get("ret") or 0
                errcode = resp.get("errcode") or 0
                if ret == 0 and errcode == 0:
                    result["ok"] = True
                elif ret == -14 or errcode == -14:
                    result["msg"] = "会话已过期，请重新扫码登录"
                else:
                    result["msg"] = f"ret={ret} errcode={errcode}"
            except Exception as e:  # noqa: BLE001
                result["error"] = str(e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        try:
            asyncio.run(_wait_thread(t, timeout))
        except TimeoutError:
            return False, f"连接超时（> {timeout:.0f} 秒）"
        if "error" in result:
            return False, result["error"]
        if result.get("ok"):
            return True, "连接成功"
        return False, result.get("msg") or "连接失败"


async def _wait_thread(t: threading.Thread, timeout: float):
    """等线程结束，超时抛 TimeoutError。"""
    deadline = time.monotonic() + timeout
    while t.is_alive():
        if time.monotonic() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.02)
