"""只读文件后端包装：拒绝对 /skills/（全局公共技能库）的写入。

背景：技能库是本机公共资源，并非本项目产物；系统提示把它描述为「只读挂载」，但裸
FilesystemBackend 实际允许 write/edit/delete。这里包一层只读 facade——read/ls/glob/grep
放行，write/edit/delete 一律返回教学式错误，防模型误改全局技能。
"""

from __future__ import annotations

import inspect

from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

_READONLY_MSG = (
    "错误：/skills/ 是本机公共 Skills 目录，只读挂载，禁止修改或删除。"
    "如需新增/调整技能，请到「AI 配置 → Skills」手动配置，不要用文件工具编辑全局技能库。"
)


def _inner_accepts_context_lines(backend) -> bool:
    """内层 grep 是否收 context_lines（FilesystemBackend 收，CompositeBackend 不收）。"""
    return "context_lines" in inspect.signature(backend.grep).parameters


class ReadOnlyBackend:
    """只读 facade：文件读/列/搜放行，写/改/删拒绝。"""

    def __init__(self, backend):
        self._inner = backend

    @property
    def id(self) -> str:
        return getattr(self._inner, "id", str(type(self._inner).__name__))

    # ---- 放行：只读操作 ----
    def ls(self, path: str) -> LsResult:
        return self._inner.ls(path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return self._inner.read(file_path, offset=offset, limit=limit)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return self._inner.glob(pattern, path)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        if _inner_accepts_context_lines(self._inner):
            return self._inner.grep(
                pattern, path, glob, max_count=max_count, context_lines=context_lines
            )
        return self._inner.grep(pattern, path, glob, max_count=max_count)

    # ---- 拦截：写入操作 ----
    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=_READONLY_MSG)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=_READONLY_MSG)

    def delete(self, file_path: str) -> DeleteResult:
        return DeleteResult(error=_READONLY_MSG)
