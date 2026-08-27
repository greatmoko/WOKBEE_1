"""项目沙箱越过：限制 Agent 仅默认访问当前项目目录，跨目录需授权。"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import (
    DeleteResult,
    EditResult,
    ExecuteResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)

from wokbee.engine.archive_guard import (
    _WIN_ABS_RE,
    normalize_agent_path,
    path_touches_archives,
)

logger = logging.getLogger("wokbee")

EscapeCallback = Callable[[dict[str, Any]], None]

_WRITE_OPS = frozenset({"write", "edit", "delete"})

# 用户消息中明确表示允许越过沙箱的短语（保守匹配，需同时含「授权/允许」类词）
_SANDBOX_PREAUTH_STRONG = (
    "越过沙箱",
    "脱离沙箱",
    "沙箱外",
    "沙箱外运行",
    "申请权限",
    "申请脱离",
    "直接访问",
    "授权访问",
    "允许访问",
    "允许直接",
    "真实路径",
)
_SANDBOX_PREAUTH_WEAK = (
    "可以",
    "允许",
    "授权",
    "同意",
    "批准",
    "是的",
)


def user_message_indicates_sandbox_preauth(text: str) -> bool:
    """检测用户是否在消息中明确授权越过项目沙箱。"""
    msg = (text or "").strip().lower()
    if not msg:
        return False
    if not any(k in msg for k in _SANDBOX_PREAUTH_STRONG):
        return False
    return any(k in msg for k in _SANDBOX_PREAUTH_WEAK)


def _strip_extended_prefix(path: Path) -> Path:
    raw = str(path)
    if raw.startswith("\\\\?\\"):
        return Path(raw[4:])
    return path


def _is_under(child: Path, parent: Path) -> bool:
    try:
        _strip_extended_prefix(child.resolve()).relative_to(
            _strip_extended_prefix(parent.resolve())
        )
        return True
    except ValueError:
        return False


def resolve_context_path(path: str | None, context_root: Path) -> Path | None:
    """把虚拟路径或 Windows 绝对路径解析为真实 Path。"""
    if not path:
        return None
    raw = str(path).strip()
    if not raw:
        return None
    if _WIN_ABS_RE.match(raw):
        try:
            return _strip_extended_prefix(Path(raw).resolve())
        except OSError:
            return None
    vpath = raw if raw.startswith("/") else f"/{raw}"
    try:
        return _strip_extended_prefix((context_root / vpath.lstrip("/")).resolve())
    except OSError:
        return None


class SandboxEscapeGuard:
    """单次运行内的沙箱越过审批状态（线程安全）。"""

    def __init__(
        self,
        project_root: Path | str,
        *,
        allow_escape: bool = False,
        on_escape_needed: EscapeCallback | None = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.allow_escape = bool(allow_escape)
        self.on_escape_needed = on_escape_needed
        self.session_granted = False
        self._lock = threading.RLock()
        self._event = threading.Event()
        self._decision: tuple[bool, bool] | None = None  # approved, grant_run

    def reset_session(self) -> None:
        with self._lock:
            self.session_granted = False

    def resolve(self, approved: bool, *, grant_run: bool = False) -> None:
        with self._lock:
            self._decision = (approved, grant_run)
            if approved and grant_run:
                self.session_granted = True
            self._event.set()

    def needs_escape(
        self,
        path: str | None,
        *,
        context_root: Path | None = None,
        operation: str = "read",
        skills_mount: bool = False,
    ) -> bool:
        """True = 路径落在当前项目目录外，需越过沙箱权限。"""
        root = context_root or self.project_root
        resolved = resolve_context_path(path, root)
        if resolved is None:
            return False
        if _is_under(resolved, self.project_root):
            return False
        if skills_mount:
            if operation not in _WRITE_OPS:
                # /skills/ 挂载点：读/list 不算越过沙箱
                return False
            # /skills/ 内 write/edit/delete 落在 skills 根目录，走写审批即可
            if resolved and _is_under(resolved, root):
                return False
        return True

    def _wait_decision(self, payload: dict[str, Any]) -> tuple[bool, bool]:
        if self.allow_escape:
            return True, False
        with self._lock:
            if self.session_granted:
                return True, False
        if not self.on_escape_needed:
            return False, False
        with self._lock:
            self._decision = None
            self._event.clear()
        self.on_escape_needed(payload)
        self._event.wait()
        with self._lock:
            decision = self._decision or (False, False)
            self._decision = None
            return decision

    def _deny_read(self, msg: str) -> ReadResult:
        return ReadResult(error=msg)

    def _deny_write(self, msg: str) -> WriteResult:
        return WriteResult(error=msg)

    def _deny_edit(self, msg: str) -> EditResult:
        return EditResult(error=msg)

    def _deny_delete(self, msg: str) -> DeleteResult:
        return DeleteResult(error=msg)

    def _deny_ls(self, msg: str) -> LsResult:
        return LsResult(error=msg)

    def _deny_glob(self, msg: str) -> GlobResult:
        return GlobResult(error=msg)

    def _deny_grep(self, msg: str) -> GrepResult:
        return GrepResult(error=msg)

    def _escape_fs(self) -> FilesystemBackend:
        return FilesystemBackend(virtual_mode=False)

    def _check(
        self,
        path: str | None,
        operation: str,
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
    ) -> tuple[bool, Path | None]:
        """返回 (允许, 沙箱外绝对路径或 None)。"""
        root = context_root or self.project_root
        if not self.needs_escape(
            path, context_root=root, operation=operation, skills_mount=skills_mount,
        ):
            return True, None
        resolved = resolve_context_path(path, root)
        display = str(resolved or path or "")
        approved, grant_run = self._wait_decision({
            "path": display,
            "operation": operation,
            "virtual_path": path,
        })
        if not approved:
            return False, resolved
        if grant_run:
            with self._lock:
                self.session_granted = True
        return True, resolved

    def gate_read(
        self,
        path: str,
        inner: Callable[[], ReadResult],
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
    ) -> ReadResult:
        ok, abs_path = self._check(
            path, "read", context_root=context_root, skills_mount=skills_mount,
        )
        if not ok:
            return self._deny_read(
                f"已拒绝访问项目沙箱外路径：{path}（可在审核策略中开启「越过沙箱」或运行时授权）"
            )
        if abs_path is not None:
            if path_touches_archives(str(abs_path)):
                return self._deny_read("禁止访问 archives/ 归档目录。")
            return self._escape_fs().read(str(abs_path))
        return inner()

    def gate_write(
        self,
        path: str,
        content: str,
        inner: Callable[[], WriteResult],
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
    ) -> WriteResult:
        ok, abs_path = self._check(
            path, "write", context_root=context_root, skills_mount=skills_mount,
        )
        if not ok:
            return self._deny_write(f"已拒绝写入项目沙箱外路径：{path}")
        if abs_path is not None:
            if path_touches_archives(str(abs_path)):
                return self._deny_write("禁止写入 archives/ 归档目录。")
            return self._escape_fs().write(str(abs_path), content)
        return inner()

    def gate_edit(
        self,
        path: str,
        old: str,
        new: str,
        replace_all: bool,
        inner: Callable[[], EditResult],
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
    ) -> EditResult:
        ok, abs_path = self._check(
            path, "edit", context_root=context_root, skills_mount=skills_mount,
        )
        if not ok:
            return self._deny_edit(f"已拒绝编辑项目沙箱外路径：{path}")
        if abs_path is not None:
            if path_touches_archives(str(abs_path)):
                return self._deny_edit("禁止编辑 archives/ 归档目录。")
            return self._escape_fs().edit(str(abs_path), old, new, replace_all)
        return inner()

    def gate_delete(
        self,
        path: str,
        inner: Callable[[], DeleteResult],
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
    ) -> DeleteResult:
        ok, abs_path = self._check(
            path, "delete", context_root=context_root, skills_mount=skills_mount,
        )
        if not ok:
            return self._deny_delete(f"已拒绝删除项目沙箱外路径：{path}")
        if abs_path is not None:
            if path_touches_archives(str(abs_path)):
                return self._deny_delete("禁止删除 archives/ 归档目录。")
            return self._escape_fs().delete(str(abs_path))
        return inner()

    def gate_ls(
        self,
        path: str,
        inner: Callable[[], LsResult],
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
    ) -> LsResult:
        ok, abs_path = self._check(
            path, "read", context_root=context_root, skills_mount=skills_mount,
        )
        if not ok:
            return self._deny_ls(f"已拒绝列出项目沙箱外路径：{path}")
        if abs_path is not None:
            return self._escape_fs().ls(str(abs_path))
        return inner()

    def gate_glob(
        self,
        pattern: str,
        path: str | None,
        inner: Callable[[], GlobResult],
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
    ) -> GlobResult:
        ok, _ = self._check(
            path or pattern, "read", context_root=context_root, skills_mount=skills_mount,
        )
        if not ok:
            return self._deny_glob(f"已拒绝在项目沙箱外 glob：{pattern}")
        return inner()

    def gate_grep(
        self,
        pattern: str,
        path: str | None,
        glob: str | None,
        inner: Callable[[], GrepResult],
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
        **kwargs: Any,
    ) -> GrepResult:
        ok, _ = self._check(
            path or glob or pattern,
            "read",
            context_root=context_root,
            skills_mount=skills_mount,
        )
        if not ok:
            return self._deny_grep(f"已拒绝在项目沙箱外 grep：{pattern}")
        return inner()


class SandboxEscapeBackend:
    """包装文件后端：沙箱外路径需授权或使用「越过沙箱」策略。"""

    def __init__(
        self,
        inner: Any,
        guard: SandboxEscapeGuard,
        *,
        context_root: Path | None = None,
        skills_mount: bool = False,
    ):
        self._inner = inner
        self._guard = guard
        self._context_root = context_root
        self._skills_mount = skills_mount

    def _coerce(self, path: str | None) -> str | None:
        if path is None:
            return None
        if self._skills_mount:
            return path
        return normalize_agent_path(path, self._guard.project_root) or path

    def ls(self, path: str) -> LsResult:
        path = self._coerce(path) or path
        return self._guard.gate_ls(
            path,
            lambda: self._inner.ls(path),
            context_root=self._context_root,
            skills_mount=self._skills_mount,
        )

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        file_path = self._coerce(file_path) or file_path
        return self._guard.gate_read(
            file_path,
            lambda: self._inner.read(file_path, offset=offset, limit=limit),
            context_root=self._context_root,
            skills_mount=self._skills_mount,
        )

    def write(self, file_path: str, content: str) -> WriteResult:
        file_path = self._coerce(file_path) or file_path
        return self._guard.gate_write(
            file_path,
            content,
            lambda: self._inner.write(file_path, content),
            context_root=self._context_root,
            skills_mount=self._skills_mount,
        )

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        file_path = self._coerce(file_path) or file_path
        return self._guard.gate_edit(
            file_path,
            old_string,
            new_string,
            replace_all,
            lambda: self._inner.edit(file_path, old_string, new_string, replace_all),
            context_root=self._context_root,
            skills_mount=self._skills_mount,
        )

    def delete(self, file_path: str) -> DeleteResult:
        file_path = self._coerce(file_path) or file_path
        return self._guard.gate_delete(
            file_path,
            lambda: self._inner.delete(file_path),
            context_root=self._context_root,
            skills_mount=self._skills_mount,
        )

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        path = self._coerce(path)
        pattern = self._coerce(pattern) or pattern
        return self._guard.gate_glob(
            pattern,
            path,
            lambda: self._inner.glob(pattern, path),
            context_root=self._context_root,
            skills_mount=self._skills_mount,
        )

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
        context_lines: int = 0,
    ) -> GrepResult:
        path = self._coerce(path)
        glob_pat = self._coerce(glob)
        return self._guard.gate_grep(
            pattern,
            path,
            glob_pat,
            lambda: self._inner.grep(
                pattern, path, glob_pat,
                max_count=max_count, context_lines=context_lines,
            ),
            context_root=self._context_root,
            skills_mount=self._skills_mount,
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._inner.execute(command, timeout=timeout)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)
