"""动态「越白名单」：request_access 工具 + /ext/<slug>/ 虚拟路由挂载。

白名单制挂载做成动态——Agent 要用项目外路径时先 request_access 申请，经人工高危审批
（HIGH_RISK_TOOLS，见 approval_policy）后把该目录实时挂成 CompositeBackend 的一条
```console
/ext/<slug>/ -> FilesystemBackend(root_dir=<dir>, virtual_mode=True)
```
虚拟路由，并持久化到全局 `~/.wokbee/config.json` 的 `wokbee.additional_directories`
（用户选定「全局共享」）。拒绝则工具体不运行、零残留。

与旧版越沙箱（sandbox_escape.py，已删）的关键区别：本实现把挂载副作用放在工具体内，
由框架自带的非阻塞 interrupt_on 审批驱动（同 execute），绝不在文件工具内部阻塞线程。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from deepagents.backends import FilesystemBackend
from langchain_core.tools import tool

logger = logging.getLogger("wokbee")

_EXT_PREFIX = "/ext/"


class ApprovedDirRegistry:
    """会话级已授权附加目录注册表（真实目录 ↔ /ext/<slug>/ 虚拟路由）。"""

    def __init__(self) -> None:
        self._entries: list[dict[str, str]] = []

    def add(self, name: str, real_dir: str, prefix: str) -> None:
        for e in self._entries:
            if e["real_dir"] == real_dir:
                e["name"] = name or e.get("name") or Path(real_dir).name
                e["prefix"] = prefix
                return
        self._entries.append(
            {"name": name or Path(real_dir).name, "real_dir": real_dir, "prefix": prefix}
        )

    def entries(self) -> list[dict[str, str]]:
        return list(self._entries)

    def find(self, real_dir: str) -> dict[str, str] | None:
        for e in self._entries:
            if e["real_dir"] == real_dir:
                return e
        return None

    def prefix_for(self, real_dir: str) -> str | None:
        e = self.find(real_dir)
        return e["prefix"] if e else None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(name)).strip("-")
    return slug or "ext"


def _used_slugs(routes: dict) -> set[str]:
    used: set[str] = set()
    for prefix in routes:
        parts = (prefix or "").strip("/").split("/")
        if len(parts) > 1 and parts[0] == "ext":
            used.add(parts[1])
    return used


def _unique_slug(routes: dict, base: str) -> str:
    used = _used_slugs(routes)
    slug = base
    i = 2
    while slug in used:
        slug = f"{base}-{i}"
        i += 1
    return slug


def mount_dir(
    routes: dict,
    registry: ApprovedDirRegistry,
    dir_path: str | Path,
    *,
    name: str = "",
    persist: bool = False,
    settings=None,
) -> str | None:
    """把目录挂成 /ext/<slug>/ 路由并登记到 registry。返回 prefix；目录不存在返回 None。

    persist=True 时才写入全局 settings（用于动态 request_access；静态预挂载不写）。
    """
    try:
        rp = Path(str(dir_path)).expanduser().resolve()
    except OSError:
        return None
    if not rp.is_dir():
        return None
    key = str(rp)
    existing = registry.find(key)
    if existing:
        return existing["prefix"]
    slug = _unique_slug(routes, _slugify(name or rp.name))
    prefix = f"{_EXT_PREFIX}{slug}/"
    routes[prefix] = FilesystemBackend(root_dir=key, virtual_mode=True)
    registry.add(name or rp.name, key, prefix)
    if persist and settings is not None:
        settings.add_additional_directory(name or rp.name, key)
    return prefix


def register_access_route(composite, registry, dir_path, settings=None, *, persist: bool = True) -> str | None:
    """往「已构建好的」CompositeBackend 实时注册一条 /ext/<slug>/ 路由。

    deepagents 的 `sorted_routes` 只在 CompositeBackend.__init__ 计算一次
    （composite.py:233），必须先写 routes 再重算 sorted_routes，新路由才会生效。
    """
    prefix = mount_dir(composite.routes, registry, dir_path, persist=persist, settings=settings)
    if prefix:
        composite.sorted_routes = sorted(
            composite.routes.items(), key=lambda x: len(x[0]), reverse=True
        )
    return prefix


ACCESS_TOOL_DESCRIPTION = """当你要用文件工具（read_file/write_file/edit_file/ls/glob/grep/delete）访问**项目文件夹之外**的路径时，必须先调用本工具申请授权。

规则：
- 传入要访问的目录或文件路径（文件会自动取其父目录）；reason 说明用途（如“需要修改共享材料”）。
- 获批后请**一律用返回的虚拟路径**（如 /ext/<slug>/…）调用文件工具，不要再传真实路径。
- 只有 execute 可用真实路径；项目内 workspace/… 等虚拟路径无需申请。
"""


def _grant_msg(prefix: str, real_dir: str) -> str:
    return (
        f"已授权访问：{real_dir}\n"
        f"请用以下**虚拟路径**访问该目录（文件工具只接受虚拟路径）：\n{prefix}\n"
        f"例如：{prefix}文件名。切勿再用真实路径（C:\\\\... 等）调用文件工具；"
        "只有 execute 才接受真实路径。"
    )


def build_access_request_tool(composite, registry, settings, *, emit):
    """构造可挂到 create_deep_agent(tools=[...]) 的 request_access 工具。"""

    @tool(description=ACCESS_TOOL_DESCRIPTION)
    def request_access(path: str, reason: str = "") -> str:
        """申请授权访问项目外目录（人工高危审批）。获批后可经 /ext/<slug>/ 虚拟路径读写。"""
        raw = (path or "").strip()
        if not raw:
            return "错误：path 不能为空。请传入需要访问的目录（或文件路径，会自动取父目录）。"
        rp = Path(raw).expanduser()
        if rp.is_file():
            rp = rp.parent
        try:
            rp = rp.resolve()
        except OSError as e:
            return f"错误：路径无法解析：{e}"
        if not rp.is_dir():
            return f"错误：路径不存在或不是目录：{rp}"
        key = str(rp)
        existing = registry.find(key)
        if existing:
            emit("info", f"附加目录已在白名单：{key}（{existing['prefix']}）")
            return _grant_msg(existing["prefix"], key)
        prefix = register_access_route(composite, registry, rp, settings, persist=True)
        if not prefix:
            return f"错误：无法为 {key} 注册访问路由。"
        emit("info", f"已授权附加目录：{key}（虚拟路径 {prefix}）")
        return _grant_msg(prefix, key)

    request_access.name = "request_access"
    return request_access
