"""强制文件工具使用虚拟路径，并对已获批的真实路径做自动改写。

背景：文件工具实际有两道门。
1. deepagents 的 `validate_path`（middleware/filesystem.py）已拒绝真正的 Windows 绝对路径
   `C:\\...`（utils.py:687），所以真实的 `C:\\...` 根本到不了后端。
2. 但 Agent 常把真实路径**前面加 `/`** 写成 `/C:/Users/...` —— 这种通过 validate_path，
   会到达后端。本包装负责收尾：把「已获批附加目录」里的真实路径**自动改写**成
   `/ext/<slug>/…`，未获批的真实路径则返回**教学式错误**，引导其走 request_access。

处理表（`_prep` 返回值成对：`(error, use_path)`）：
- 非主机路径（workspace/…、/ext/<slug>/…、/skills/…、相对虚拟路径）→ 原样透传；
- 主机路径，且在某个已获批目录下 → 自动改写为 `/ext/<slug>/<相对>`，直接成功；
- 主机路径，但未获批 → 返回教学式错误（教 Agent 去 request_access）；
- `execute` 原样透传（本就允许真实路径）。

`SandboxBackendProtocol` 的 async 方法都经 `asyncio.to_thread(self.<sync>, ...)` 委托
（protocol.py），因此只需覆写 sync 方法即可同步覆盖 async；图用 `agent.stream`（sync）跑。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
)

_DRIVE_RE = re.compile(r"^/?[a-zA-Z]:[/\\]")
_DRIVE_NORM_RE = re.compile(r"^([a-zA-Z]:)[/\\](.*)$")
_WILDCARD_CHARS = "*?["

_GUIDE_MSG = (
    "错误：你传给文件工具的路径「{path}」看起来是真实主机路径，但该目录未授权。"
    "文件工具只接受虚拟路径：项目内请用 workspace/…、deliverables/…、uploads/… 等；"
    "项目外请先调用 request_access(path, reason) 申请加入白名单，"
    "获批后用返回的 /ext/<slug>/… 虚拟路径。只有 execute 才接受真实路径。"
)


def _has_wildcard(s: str | None) -> bool:
    return bool(s) and any(ch in s for ch in _WILDCARD_CHARS)


def _normalize_host(path: str) -> str:
    """把 `/C:/Users/x/y` 或 `C:\\Users\\x\\y` 统一成 Windows 绝对路径（`C:\\Users\\x\\y`）。"""
    p = str(path)
    while p.startswith("/"):
        p = p[1:]
    m = _DRIVE_NORM_RE.match(p)
    if not m:
        return p
    rest = m.group(2).replace("/", "\\")
    return f"{m.group(1)}\\{rest}"


class AccessCoerceBackend(SandboxBackendProtocol):
    """CompositeBackend 外层：强制虚拟路径 + 已获批真实路径自动改写 + execute 透传。"""

    def __init__(self, backend, *, project_root: str | Path | None = None, registry=None):
        self._inner = backend
        self._project_root = str(Path(project_root).resolve()) if project_root else None
        self._registry = registry

    @property
    def id(self) -> str:
        return getattr(self._inner, "id", str(type(self._inner).__name__))

    def _coerce_real(self, path: str) -> str | None:
        """主机真实路径 → /ext/<slug>/…；无匹配返回 None。"""
        if self._registry is None:
            return None
        norm = _normalize_host(path)
        n = os.path.normcase(os.path.normpath(norm))
        for e in self._registry.entries():
            real = os.path.normcase(os.path.normpath(e["real_dir"]))
            if n == real:
                return e["prefix"]
            sep = os.sep
            if n.startswith(real + sep):
                rel = n[len(real) + 1 :]
                return e["prefix"] + rel.replace(sep, "/")
        return None

    def _prep(self, path: str | None) -> tuple[str | None, str | None]:
        """返回 (error, use_path)。error 非空则直接用该错误；否则 use_path 是实际路径。"""
        if not path:
            return None, path
        p = str(path)
        if not _DRIVE_RE.match(p):
            return None, p  # 非主机路径 → 透传
        coerced = self._coerce_real(p)
        if coerced is not None:
            return None, coerced  # 已获批 → 自动改写为 /ext/<slug>/…
        return _GUIDE_MSG.format(path=p), p  # 未获批 → 教学式错误

    # ---- file tools：先 _prep（改写到 /ext/ 或拦下），再转内层 ----
    def ls(self, path: str) -> LsResult:
        err, use_path = self._prep(path)
        if err:
            return LsResult(error=err)
        return self._inner.ls(use_path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        err, use_path = self._prep(file_path)
        if err:
            return ReadResult(error=err)
        return self._inner.read(use_path, offset=offset, limit=limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        err, use_path = self._prep(file_path)
        if err:
            return WriteResult(error=err)
        return self._inner.write(use_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        err, use_path = self._prep(file_path)
        if err:
            return EditResult(error=err)
        return self._inner.edit(use_path, old_string, new_string, replace_all=replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        err, use_path = self._prep(file_path)
        if err:
            return DeleteResult(error=err)
        return self._inner.delete(use_path)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        err, use_path = self._prep(path)
        if err:
            return GlobResult(error=err)
        if not _has_wildcard(pattern):
            perr, use_pattern = self._prep(pattern)
            if perr:
                return GlobResult(error=perr)
            pattern = use_pattern
        return self._inner.glob(pattern, use_path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        err, use_path = self._prep(path)
        if err:
            return GrepResult(error=err)
        if glob is not None and not _has_wildcard(glob):
            gerr, use_glob = self._prep(glob)
            if gerr:
                return GrepResult(error=gerr)
            glob = use_glob
        return self._inner.grep(
            pattern, use_path, glob, max_count=max_count, context_lines=context_lines
        )

    # ---- upload/download：转内层（已由 middleware 校验），只保证不中断 ----
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return self._inner.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return self._inner.download_files(paths)

    # ---- execute：透传（execute 本就接受真实路径）----
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._inner.execute(command, timeout=timeout)
