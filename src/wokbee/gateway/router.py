"""入站消息 → 项目 Agent 的路由与白名单。

路由规则（与社区一致）：
- 空文本 → EMPTY_MESSAGE；发送者不在 allow_from → DENIED_SENDER。
- 符号约定：`@` **专用于切换/指定项目（仅按项目ID**，如 `@prj_xxx`）；`#` **专用于操作指令**
  （`#new` / `#list` / `#run` / `#help`，由 GatewayManager 拦截，不进路由）。
- `@项目ID` 按项目 id 精确匹配；无前缀回落该频道的默认项目（default_project_for(channel)）。
- 匹配到项目后，剥离路由前缀，`clean_text` 给 Agent（`@项目ID 文字` → 切换并运行文字）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from wokbee.core.project_store import ProjectStore
from wokbee.gateway.base import ChannelMessage
from wokbee.gateway.store import GatewayChannelConfig


class RouteOutcome(str, Enum):
    OK = "ok"
    DENIED_SENDER = "denied_sender"
    UNKNOWN_PROJECT = "unknown_project"
    NO_DEFAULT = "no_default"
    EMPTY_MESSAGE = "empty_message"


@dataclass
class RouteResult:
    outcome: RouteOutcome
    project_id: str = ""
    clean_text: str = ""
    reply: str = ""  # outcome != OK 时的预置回复
    token: str = ""  # 用户用到的路由前缀（@名称/#id）；空表示无前缀


class MessageRouter:
    def __init__(self, project_store: ProjectStore | None = None, settings=None):
        self._store = project_store or ProjectStore()

    def route(self, msg: ChannelMessage, cfg: GatewayChannelConfig) -> RouteResult:
        text = (msg.text or "").strip()
        if not text:
            return RouteResult(RouteOutcome.EMPTY_MESSAGE,
                               reply="（收到空消息，未处理）")

        # 允许列表空 = 默认放行（个人使用）；仅在用户显式填写了若干发送者时才限流。
        allow = [s for s in (cfg.allow_from or []) if s]
        if allow and msg.sender_id not in allow:
            return RouteResult(
                RouteOutcome.DENIED_SENDER,
                reply=(
                    "你暂未获得网关使用权限：当前允许列表已启用，仅限指定用户。\n"
                    + (msg.sender_id or "<未知>")
                    + "\n\n如需放行，请到 WokBee「消息网关」的允许列表添加该账号。"
                ),
            )

        token, rest = self.parse_route_prefix(text)
        # `#` 是指令域（#new/#list/#run/#help），已由 GatewayManager 拦截。落到路由里的
        # `#` 是未知指令，不应被当作项目路由误判为「无前缀走默认项目」，直接给出说明。
        if token.startswith("#"):
            return RouteResult(
                RouteOutcome.UNKNOWN_PROJECT,
                reply=f"未知指令：{token}。\n" + self._command_help(),
            )
        project_id, reply = self._resolve_project(token, cfg, msg.channel)
        if not project_id:
            outcome = RouteOutcome.UNKNOWN_PROJECT if token else RouteOutcome.NO_DEFAULT
            return RouteResult(outcome, reply=reply, token=token)
        return RouteResult(RouteOutcome.OK, project_id=project_id, clean_text=rest, token=token)

    def _project_listing(self) -> str:
        """无默认项目/项目ID找不到时，把现有项目清单发给用户，让其在 IM 里用 @项目ID 直接选。"""
        projects = self._store.list_projects()
        if not projects:
            return "还没有任何项目。请先在 WokBee 里新建一个项目。"
        lines = ["目前未绑定默认项目。请在消息里发送 @项目ID 指定要用的项目（指定后即为该频道默认，可随时再切换）："]
        for i, p in enumerate(projects, 1):
            lines.append(f"{i}. {p.title}（@{p.id}）")
        lines.append("发送 #help 查看全部可用指令。")
        return "\n".join(lines)

    # ---- 内部 ----
    def _command_help(self) -> str:
        """可与 WokBee 交互的系统指令说明（#help；未知 # 指令也回这段）。"""
        return (
            "可与 WokBee 交互的系统指令：\n"
            "· @项目ID —— 切换默认项目到该 ID（仅按项目ID，如 @prj_xxx）。\n"
            "  `@项目ID 内容` 则切换并立刻运行该内容。\n"
            "· #new 描述 —— 新建项目并把描述设为项目目标，绑定为当前频道默认。\n"
            "· #list —— 列出所有项目及各自的项目ID。\n"
            "· #run —— 用当前默认项目的「目标」运行 Agent。\n"
            "· #help —— 显示本说明。\n\n"
            "直接发消息（无前缀）会路由到当前默认项目。"
        )

    def _resolve_project(self, token: str, cfg: GatewayChannelConfig, channel: str) -> tuple[str, str]:
        """返回 (project_id, 失败时的提示)。project_id 为空表示未找到。"""
        if token.startswith("@"):
            # `@` 专用于切换项目，且**只能用项目ID**（不再按标题/名称匹配）
            pid = token[1:].strip()
            if pid and self._store.get(pid):
                return pid, ""
            return "", f"未找到项目：{token}。\n" + self._project_listing()
        # 无前缀 → 按频道取默认项目（不同频道各绑各的，issue 3）
        did = cfg.default_project_for(channel)
        if did and self._store.get(did):
            return did, ""
        if did:
            return "", "默认项目不存在，请到设置中重新绑定。\n" + self._project_listing()
        return "", self._project_listing()

    @staticmethod
    def parse_route_prefix(text: str) -> tuple[str, str]:
        """拆解路由前缀：返回 (token, rest)。token 为 "" | "@项目ID" | "#指令"。

        `@` 只用于**切换项目（按项目ID）**；`#` 专用于操作指令（#new/#list/#run/#help，
        由 GatewayManager 拦截）。二者都从正文剥离，rest 为剩余内容。
        """
        t = (text or "").strip()
        m = re.match(r"^([@#][^\s#@]{1,64})", t)
        if not m:
            return "", t
        token = m.group(1)
        return token, t[m.end():].strip()
