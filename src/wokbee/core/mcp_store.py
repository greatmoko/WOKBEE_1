"""MCP 服务器配置存储。"""

from __future__ import annotations

import asyncio
import logging
import shlex
import uuid
from dataclasses import dataclass, field
from typing import Any

from tokbee.core.config import Config

logger = logging.getLogger("wokbee")


@dataclass
class McpServerConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    enabled: bool = True
    transport: str = "stdio"  # stdio | sse | streamable_http
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "transport": self.transport,
            "command": self.command,
            "args": list(self.args),
            "url": self.url,
            "env": dict(self.env),
            "cwd": self.cwd,
        }

    @classmethod
    def from_dict(cls, data: dict) -> McpServerConfig:
        args = data.get("args") or []
        if isinstance(args, str):
            args = shlex.split(args, posix=False)
        env = data.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
            name=str(data.get("name") or "mcp"),
            enabled=bool(data.get("enabled", True)),
            transport=str(data.get("transport") or "stdio"),
            command=str(data.get("command") or ""),
            args=[str(a) for a in args],
            url=str(data.get("url") or ""),
            env={str(k): str(v) for k, v in env.items()},
            cwd=str(data.get("cwd") or ""),
        )

    def to_connection(self) -> dict[str, Any] | None:
        """转为 MultiServerMCPClient connections 条目。"""
        t = (self.transport or "stdio").strip().lower()
        if t == "stdio":
            if not self.command.strip():
                return None
            conn: dict[str, Any] = {
                "transport": "stdio",
                "command": self.command.strip(),
                "args": list(self.args),
            }
            if self.env:
                conn["env"] = dict(self.env)
            if self.cwd.strip():
                conn["cwd"] = self.cwd.strip()
            return conn
        if t in ("sse", "streamable_http", "http"):
            if not self.url.strip():
                return None
            transport = "sse" if t == "sse" else "streamable_http"
            if t == "http":
                transport = "streamable_http"
            return {"transport": transport, "url": self.url.strip()}
        return None


class McpStore:
    def __init__(self, config: Config | None = None):
        self._config = config or Config()
        if self._config.get("wokbee.mcp_servers") is None:
            self._config.set("wokbee.mcp_servers", [])
            self._config.save()

    def list_servers(self) -> list[McpServerConfig]:
        raw = self._config.get("wokbee.mcp_servers") or []
        if not isinstance(raw, list):
            return []
        return [McpServerConfig.from_dict(x) for x in raw if isinstance(x, dict)]

    def list_enabled(self) -> list[McpServerConfig]:
        return [s for s in self.list_servers() if s.enabled]

    def save_all(self, servers: list[McpServerConfig]) -> None:
        self._config.set("wokbee.mcp_servers", [s.to_dict() for s in servers])
        self._config.save()

    def upsert(self, server: McpServerConfig) -> None:
        items = self.list_servers()
        found = False
        for i, s in enumerate(items):
            if s.id == server.id:
                items[i] = server
                found = True
                break
        if not found:
            items.append(server)
        self.save_all(items)

    def delete(self, server_id: str) -> None:
        self.save_all([s for s in self.list_servers() if s.id != server_id])

    def set_enabled(self, server_id: str, enabled: bool) -> None:
        items = self.list_servers()
        for s in items:
            if s.id == server_id:
                s.enabled = enabled
        self.save_all(items)

    def build_connections(self) -> dict[str, dict]:
        conns: dict[str, dict] = {}
        for s in self.list_enabled():
            c = s.to_connection()
            if not c:
                continue
            key = re_key(s.name) or s.id
            # 避免重名覆盖
            base = key
            i = 2
            while key in conns:
                key = f"{base}_{i}"
                i += 1
            conns[key] = c
        return conns

    def load_tools(self) -> list:
        """同步加载已启用 MCP 的工具列表。"""
        connections = self.build_connections()
        if not connections:
            return []
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            logger.error("未安装 langchain-mcp-adapters，无法加载 MCP")
            return []

        async def _load():
            client = MultiServerMCPClient(connections)
            return await client.get_tools()

        try:
            return list(asyncio.run(_load()))
        except Exception:
            logger.exception("加载 MCP 工具失败")
            raise


def re_key(name: str) -> str:
    import re

    key = re.sub(r"[^\w\-]+", "_", (name or "").strip())
    return key.strip("_") or ""
