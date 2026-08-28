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


def _drive_relative_input(raw: str) -> bool:
    """`C:` / `C:foo` —— 无明确绝对锚点，`Path(...).resolve()` 会落到该盘 cwd，属静默错路由，拒。"""
    s = (raw or "").strip()
    if re.match(r"^[a-zA-Z]:?$", s):
        return True
    return bool(re.match(r"^[a-zA-Z]:[^\\/]", s))


def _is_drive_root(p: Path) -> bool:
    """盘符根（C:\\）或 UNC 根（\\\\server\\share）—— `parts` 只有 1 段且带 anchor。"""
    anchor = getattr(p, "anchor", "") or ""
    return anchor != "" and len(p.parts) == 1


def _mount_problem(rp: Path, project_root) -> str | None:
    """该目录能否安全挂成 /ext/ 路由。返回问题原因；None 表示可挂。

    安全边界：/ext/ 路由是裸 FilesystemBackend，**不经过** ArchiveDeniedBackend
    （那层只包 composite 的 default=项目根）。因此任何会让 `/ext/<slug>/archives/…`
    碰到归档的挂载都必须拒绝：
    - 盘根 / UNC 根（整盘、整共享）→ 拒绝，要求精确到子目录；
    - 与项目根重叠（项目根本身、其祖先、其子目录）→ 拒绝，否则归档守护被整体绕过；
    - 路径含 archives 段 → 拒绝，避免把归档树直接暴露在虚拟路由下。
    """
    if _is_drive_root(rp):
        return "不能挂载整个盘符/共享根目录，请精确到具体子目录。"
    if any((part or "").lower() == "archives" for part in rp.parts):
        return (
            "该目录路径含 archives 段，属于归档区域，禁止挂载到虚拟路径"
            "（/ext/ 路由不经归档守卫）。"
        )
    if project_root is not None:
        try:
            root = Path(project_root).resolve()
        except OSError:
            root = None
        if root is not None:
            try:
                rr = rp.resolve()
            except OSError:
                return f"目录无法解析：{rp}"
            # 两侧都 resolve（realpath 处理 junction/8.3），再判断重叠
            if rr == root or root in rr.parents or rr in root.parents:
                return (
                    f"该目录 {rp} 与项目根重叠（是项目根、其祖先或其子目录），"
                    "挂载后会经 /ext/ 访问项目 archives，禁止挂载。"
                    "请仅用项目内虚拟路径，或在项目外挑选不重叠的目录。"
                )
    return None


def mount_dir(
    routes: dict,
    registry: ApprovedDirRegistry,
    dir_path: str | Path,
    *,
    name: str = "",
    persist: bool = False,
    settings=None,
    slug: str = "",
    project_root=None,
) -> str | None:
    """把目录挂成 /ext/<slug>/ 路由并登记到 registry。返回 prefix；目录不存在或被拒返回 None。

    persist=True 时才写入全局 settings（用于动态 request_access；静态预挂载不写）。
    传入 slug 时优先使用它（如从持久化配置恢复），否则按 name/目录名现推 —— 保证跨会话
    对同一目录使用同一个虚拟路径，避免旧 /ext/<slug>/ 引用失效或串路由。
    project_root 用于拒绝与项目根重叠/含 archives 段/盘根的危险挂载 —— /ext/ 路由不经归档
    守卫，不能让挂载点把项目归档暴露出来。
    """
    try:
        rp = Path(str(dir_path)).expanduser().resolve()
    except OSError:
        return None
    if not rp.is_dir():
        return None
    problem = _mount_problem(rp, project_root)
    if problem:
        logger.warning("拒绝挂载附加目录 %s：%s", rp, problem)
        return None
    key = str(rp)
    existing = registry.find(key)
    if existing:
        return existing["prefix"]
    base_slug = slug or _slugify(name or rp.name)
    selected = _unique_slug(routes, base_slug)
    prefix = f"{_EXT_PREFIX}{selected}/"
    routes[prefix] = FilesystemBackend(root_dir=key, virtual_mode=True)
    registry.add(name or rp.name, key, prefix)
    if persist and settings is not None:
        settings.add_additional_directory(name or rp.name, key, slug=selected)
    return prefix


def register_access_route(
    composite, registry, dir_path, settings=None, *, persist: bool = True, project_root=None
) -> str | None:
    """往「已构建好的」CompositeBackend 实时注册一条 /ext/<slug>/ 路由。

    deepagents 的 `sorted_routes` 只在 CompositeBackend.__init__ 计算一次
    （composite.py:233），必须先写 routes 再重算 sorted_routes，新路由才会生效。
    """
    prefix = mount_dir(
        composite.routes, registry, dir_path, persist=persist, settings=settings,
        project_root=project_root,
    )
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


def build_access_request_tool(composite, registry, settings, *, emit, project_root=None):
    """构造可挂到 create_deep_agent(tools=[...]) 的 request_access 工具。

    project_root 用于拒绝与项目根重叠/含 archives 段/盘根的危险挂载（/ext/ 路由不经归档守卫）。
    """

    @tool(description=ACCESS_TOOL_DESCRIPTION)
    def request_access(path: str, reason: str = "") -> str:
        """申请授权访问项目外目录（人工高危审批）。获批后可经 /ext/<slug>/ 虚拟路径读写。"""
        raw = (path or "").strip()
        if not raw:
            return "错误：path 不能为空。请传入需要访问的目录（或文件路径，会自动取父目录）。"
        if _drive_relative_input(raw):
            return (
                "错误：请传入明确的绝对路径（如 C:\\data\\materials）。"
                "盘符根或相对盘符路径（C: 或 C:foo）不允许挂载——它没有明确锚点，"
                "会被解析到磁盘的当前工作目录，容易误触整盘。"
            )
        rp = Path(raw).expanduser()
        if rp.is_file():
            rp = rp.parent
        try:
            rp = rp.resolve()
        except OSError as e:
            return f"错误：路径无法解析：{e}"
        if not rp.is_dir():
            return f"错误：路径不存在或不是目录：{rp}"
        problem = _mount_problem(rp, project_root)
        if problem:
            return f"错误：{problem}"
        key = str(rp)
        existing = registry.find(key)
        if existing:
            emit("info", f"附加目录已在白名单：{key}（{existing['prefix']}）")
            return _grant_msg(existing["prefix"], key)
        prefix = register_access_route(
            composite, registry, rp, settings, persist=True, project_root=project_root
        )
        if not prefix:
            return f"错误：无法为 {key} 注册访问路由。"
        emit("info", f"已授权附加目录：{key}（虚拟路径 {prefix}）")
        return _grant_msg(prefix, key)

    request_access.name = "request_access"
    return request_access
