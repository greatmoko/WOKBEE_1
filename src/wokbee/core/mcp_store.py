"""MCP 服务器配置存储。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shlex
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from tokbee.core.config import Config

logger = logging.getLogger("wokbee")

# 进程级 MCP 工具缓存：以「已启用连接配置指纹」为 key。同一会话内多次 build_agent
# （chat 模式每轮重建 Agent）复用同一批工具对象，保证工具 JSON-Schema 字节级稳定，
# 否则 MCP 每轮重连重取可能导致字段顺序/内容抖动，直接破坏 DeepSeek 前缀缓存。
_MCP_TOOLS_CACHE: dict[str, list] = {}
_MCP_TOOLS_LOCK = threading.Lock()
_MCP_TOOLS_CACHE_MAX = 16


def _connections_fingerprint(connections: dict[str, dict]) -> str:
    """对连接配置做稳定 SHA-256：只依赖配置内容，不依赖插入顺序。"""
    h = hashlib.sha256()
    for key in sorted(connections or {}):
        h.update(key.encode("utf-8"))
        h.update(b"\0")
        try:
            payload = json.dumps(
                connections[key], sort_keys=True, ensure_ascii=False
            )
        except (TypeError, ValueError):
            payload = str(connections[key])
        h.update(payload.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


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

    def load_tools(self, *, use_cache: bool = True) -> list:
        """同步加载已启用 MCP 的工具列表。

        同一配置指纹的结果会缓存：chat 模式每轮重建 Agent 时复用同一批工具对象，
        保证工具 schema 字节级稳定，利于 DeepSeek 前缀缓存；仅在配置变化（启停/增删）
        时指纹改变、强制重连重取。
        """
        connections = self.build_connections()
        if not connections:
            return []
        fp = _connections_fingerprint(connections)
        if use_cache:
            with _MCP_TOOLS_LOCK:
                cached = _MCP_TOOLS_CACHE.get(fp)
                if cached is not None:
                    return list(cached)
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            logger.error("未安装 langchain-mcp-adapters，无法加载 MCP")
            return []

        async def _load():
            # async with 确保 aclose()：避免 stdio MCP 子进程每次加载后泄漏（B4）
            async with MultiServerMCPClient(connections) as client:
                return await client.get_tools()

        try:
            tools = list(asyncio.run(_load()))
        except Exception:
            logger.exception("加载 MCP 工具失败")
            raise
        if use_cache:
            with _MCP_TOOLS_LOCK:
                # 仅成功才写入；超上限时移除最早条目，避免长期运行无限增长
                if len(_MCP_TOOLS_CACHE) >= _MCP_TOOLS_CACHE_MAX:
                    _MCP_TOOLS_CACHE.pop(next(iter(_MCP_TOOLS_CACHE)), None)
                _MCP_TOOLS_CACHE[fp] = tools
        return tools


def re_key(name: str) -> str:
    import re

    key = re.sub(r"[^\w\-]+", "_", (name or "").strip())
    return key.strip("_") or ""
