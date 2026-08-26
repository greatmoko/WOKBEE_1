"""阻止 Agent 读写/列举/在命令中访问 archives/ 归档目录。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

from deepagents.backends import LocalShellBackend
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

_DENY_MSG = (
    "禁止访问 archives/：归档文档与归档数据不能作为当前运行的数据来源。"
    "请仅使用 workspace/、uploads/、deliverables/、memory/（最新经验）、scripts/、runs/。"
)

_ARCHIVES_RE = re.compile(r"(^|[/\\])archives([/\\]|$)", re.IGNORECASE)
_CMD_ARCHIVES_RE = re.compile(
    r"(^|[^\w])archives([^\w]|$)|[/\\]archives([/\\]|$)",
    re.IGNORECASE,
)


_WIN_ABS_RE = re.compile(r"^[a-zA-Z]:[/\\]")


def normalize_agent_path(path: str | None, project_root: Path | str | None) -> str | None:
    """将项目内的 Windows 绝对路径转为虚拟相对路径（workspace/…）。"""
    if not path or not project_root:
        return path
    raw = str(path).strip()
    if not _WIN_ABS_RE.match(raw):
        return path
    try:
        resolved = Path(raw).resolve()
        rel = resolved.relative_to(Path(project_root).resolve())
        return rel.as_posix()
    except (ValueError, OSError):
        return path


def path_touches_archives(path: str | None) -> bool:
    if not path:
        return False
    norm = str(path).replace("\\", "/").strip()
    # 虚拟路径或相对路径
    if _ARCHIVES_RE.search(norm):
        return True
    try:
        parts = {p.lower() for p in PurePosixPath(norm.lstrip("/")).parts}
    except Exception:
        return "archives" in norm.lower()
    return "archives" in parts


def command_touches_archives(command: str | None) -> bool:
    if not command:
        return False
    return bool(_CMD_ARCHIVES_RE.search(command))


from wokbee.engine.runtime_env import enrich_shell_env, build_execute_invocation


def _shell_env(base: dict | None = None, *, project_root: str | Path | None = None) -> dict[str, str]:
    return enrich_shell_env(base, project_root=project_root)


class ArchiveDeniedBackend(LocalShellBackend):
    """LocalShellBackend 包装：拒绝一切触及 archives/ 的文件与 shell 操作。"""

    def _coerce_path(self, path: str | None) -> str | None:
        if path is None:
            return None
        return normalize_agent_path(path, getattr(self, "cwd", None))

    def ls(self, path: str) -> LsResult:
        path = self._coerce_path(path) or path
        if path_touches_archives(path):
            return LsResult(error=_DENY_MSG)
        result = super().ls(path)
        if result.error or not result.entries:
            return result
        filtered = []
        for e in result.entries:
            p = ""
            if isinstance(e, dict):
                p = str(e.get("path") or "")
                name = PurePosixPath(p.replace("\\", "/")).name if p else ""
            else:
                p = str(getattr(e, "path", "") or "")
                name = PurePosixPath(p.replace("\\", "/")).name if p else str(getattr(e, "name", "") or "")
            if name.lower() == "archives" or path_touches_archives(p):
                continue
            filtered.append(e)
        return LsResult(error=None, entries=filtered)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        file_path = self._coerce_path(file_path) or file_path
        if path_touches_archives(file_path):
            return ReadResult(error=_DENY_MSG)
        return super().read(file_path, offset=offset, limit=limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        file_path = self._coerce_path(file_path) or file_path
        if path_touches_archives(file_path):
            return WriteResult(error=_DENY_MSG)
        return super().write(file_path, content)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        file_path = self._coerce_path(file_path) or file_path
        if path_touches_archives(file_path):
            return EditResult(error=_DENY_MSG)
        return super().edit(file_path, old_string, new_string, replace_all=replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        file_path = self._coerce_path(file_path) or file_path
        if path_touches_archives(file_path):
            return DeleteResult(error=_DENY_MSG)
        return super().delete(file_path)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        path = self._coerce_path(path)
        pattern = self._coerce_path(pattern) or pattern
        if path_touches_archives(path) or path_touches_archives(pattern):
            return GlobResult(error=_DENY_MSG)
        result = super().glob(pattern, path)
        if result.error or not result.matches:
            return result
        matches = []
        for m in result.matches:
            p = str(m.get("path") if isinstance(m, dict) else getattr(m, "path", m) or "")
            if not path_touches_archives(p):
                matches.append(m)
        return GlobResult(
            error=None,
            matches=matches,
            truncated=bool(getattr(result, "truncated", False)),
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
        path = self._coerce_path(path)
        if path_touches_archives(path) or path_touches_archives(glob):
            return GrepResult(error=_DENY_MSG)
        result = super().grep(
            pattern,
            path,
            glob,
            max_count=max_count,
            context_lines=context_lines,
        )
        if result.error or not result.matches:
            return result
        matches = []
        for m in result.matches:
            p = str(m.get("path") if isinstance(m, dict) else getattr(m, "path", "") or "")
            if not path_touches_archives(p):
                matches.append(m)
        return GrepResult(
            error=None,
            matches=matches,
            truncated=bool(getattr(result, "truncated", False)),
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """执行 shell；强制 UTF-8 解码，避免 Windows 默认 GBK 读 UTF-8 输出崩溃。"""
        if command_touches_archives(command):
            return ExecuteResponse(output=_DENY_MSG, exit_code=1, truncated=False)
        if not command or not isinstance(command, str):
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
                truncated=False,
            )

        effective_timeout = timeout if timeout is not None else getattr(
            self, "_default_timeout", 120
        )
        if effective_timeout <= 0:
            raise ValueError(f"timeout must be positive, got {effective_timeout}")

        max_bytes = int(getattr(self, "_max_output_bytes", 100_000) or 100_000)
        env = _shell_env(
            getattr(self, "_env", None),
            project_root=getattr(self, "root_dir", None),
        )
        cwd = str(getattr(self, "cwd", os.getcwd()))

        try:
            argv, shell_mode = build_execute_invocation(command)
            if argv:
                run_kw: dict = {
                    "args": argv,
                    "shell": False,
                }
            else:
                run_kw = {
                    "args": command,
                    "shell": True,
                }
            result = subprocess.run(  # noqa: S602
                check=False,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                env=env,
                cwd=cwd,
                **run_kw,
            )
            output_parts: list[str] = []
            if result.stdout:
                output_parts.append(result.stdout)
            if result.stderr:
                stderr_lines = result.stderr.strip().split("\n")
                output_parts.extend(f"[stderr] {line}" for line in stderr_lines)
            output = "\n".join(output_parts) if output_parts else "<no output>"
            truncated = False
            if len(output) > max_bytes:
                output = output[:max_bytes]
                output += f"\n\n... Output truncated at {max_bytes} bytes."
                truncated = True
            if result.returncode != 0:
                output = f"{output.rstrip()}\n\nExit code: {result.returncode}"
            return ExecuteResponse(
                output=output,
                exit_code=result.returncode,
                truncated=truncated,
            )
        except subprocess.TimeoutExpired:
            if timeout is not None:
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds "
                    "(custom timeout). The command may be stuck or require more time."
                )
            else:
                msg = (
                    f"Error: Command timed out after {effective_timeout} seconds. "
                    "For long-running commands, re-run using the timeout parameter."
                )
            return ExecuteResponse(output=msg, exit_code=124, truncated=False)
        except Exception as e:  # noqa: BLE001
            return ExecuteResponse(
                output=f"Error executing command ({type(e).__name__}): {e}",
                exit_code=1,
                truncated=False,
            )
