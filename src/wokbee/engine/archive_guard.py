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
# normalize_agent_path 只处理 Windows 盘符绝对路径（语义不变）
_WIN_ABS_RE = re.compile(r"^[a-zA-Z]:[/\\]")
# 守卫用：盘符绝对 / UNC / 根化的 /… / drive-relative（C:），决定是否按绝对路径解析
_ABS_RE = re.compile(r"^[a-zA-Z]:[/\\]|^\\\\|^/|^[a-zA-Z]:$")


def _looks_abs(p: str) -> bool:
    return bool(_ABS_RE.match(p))


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


def path_touches_archives(path: str | None, project_root=None) -> bool:
    """项目根下是否触及 archives/。

    归档守卫的边界是**本项目根目录下的 archives 目录**（含其整棵子树），不是任何名子里
    含 archives 的路径：workspace/archives、/ext/archives/… 这些「别处恰好叫 archives」
    的目录放行——若按裸组件名拦截，恰好访问一个叫 archives 的已授权外部目录，或项目内
    无关子目录里的同名文件夹，都会被误杀（对外部目录尤其致命）。
    """
    if not path:
        return False
    if project_root is not None:
        try:
            root = Path(project_root).resolve()
        except (OSError, ValueError):
            root = None
        if root is not None:
            try:
                p = str(path)
                if _looks_abs(p):
                    target = Path(p).resolve()
                else:
                    target = (root / p).resolve()
                archive_root = root / "archives"
                if target == archive_root or archive_root in target.parents:
                    return True
                return False
            except (OSError, ValueError):
                pass  # 解析失败 → 退回保守检查
    # 无 project_root 或解析失败：退化为「多级路径含 archives 组件」的保守判断
    norm = str(path).replace("\\", "/").strip()
    return _ARCHIVES_RE.search(norm) is not None


def _iter_command_tokens(command: str) -> list[str]:
    """粗切 shell 命令为 token：引号内容作整体（含空格路径），其余按空白切。"""
    tokens: list[str] = []
    for m in re.finditer(r'"([^"]*)"|\'([^\']*)\'|`([^`]*)`', command):
        tokens.append(m.group(1) or m.group(2) or m.group(3) or "")
    rest = re.sub(r'"[^"]*"|\'[^\']*\'|`[^`]*`', " ", command)
    rest = re.sub(r"[<>|&;]", " ", rest)
    for tok in rest.split():
        tokens.append(tok)
    return tokens


def _token_touches_archives(token: str) -> bool:
    """单个 token 是否指向项目 archives（路径解析/dir 视角），而非仅提到该字样。"""
    t = token
    # 去命令替换/变量引用（把 $(…) 内容并过来，尽量挡住 archiv$( 'e')s 类拼接）
    t = re.sub(r"\$\{[^}]*\}|\$\([^)]*\)", "", t)
    t = t.replace("`", "").replace("'", "").replace('"', "")
    t = t.replace("\\", "/")
    has_glob = any(c in t for c in "*?[")
    # 展开单层 ./ 与 ../，归一化后再看组件
    parts = t.split("/")
    collapsed: list[str] = []
    for x in parts:
        if x == "..":
            if collapsed:
                collapsed.pop()
        elif x == ".":
            continue
        else:
            collapsed.append(x)
    norm = "/".join(collapsed).lstrip("/")
    if has_glob:
        low = t.lower()
        # glob 展开后可能就是 archives（archiv*、*chives）
        if "chives" in low or "archiv" in low:
            return True
    # 仅当「archives」是独立路径组件才算命中；my-archives-backup、archive_summary.py 放行
    return _ARCHIVES_RE.search(norm) is not None


def command_touches_archives(command: str | None) -> bool:
    """shell 命令是否触及项目 archives。

    用「路径解析」替代硬子串匹配：处理引号（含空格路径）、`..`/glob，只拦指向 archives
    目录的 token，从而放行 `dir my-archives-backup`、`python archive_summary.py` 这类
    仅提到 archives 字样的正常命令。（命令替换/变量拼接属 shell 语义，此处尽力解析，非
    完美沙箱。）
    """
    if not command or not isinstance(command, str):
        return False
    for tok in _iter_command_tokens(command):
        if _token_touches_archives(tok):
            return True
    return False


from wokbee.engine.runtime_env import enrich_shell_env, build_execute_invocation
from tokbee.core.subprocess_util import nowin


def _shell_env(base: dict | None = None, *, project_root: str | Path | None = None) -> dict[str, str]:
    return enrich_shell_env(base, project_root=project_root)


class ArchiveDeniedBackend(LocalShellBackend):
    """LocalShellBackend 包装：拒绝一切触及 archives/ 的文件与 shell 操作。

    注意：`execute` 是**有意 fork** 的上游 `LocalShellBackend.execute`（见方法内注释），
    不是透传。上游升级时需手工比对同步差异，勿直接覆盖或删除本实现，否则归档守卫失效。
    """

    def _coerce_path(self, path: str | None) -> str | None:
        if path is None:
            return None
        return normalize_agent_path(path, getattr(self, "cwd", None))

    def _archive_hit(self, path: str | None) -> bool:
        """按本项目根限定归档边界（不是任何名字含 archives 的路径）。"""
        return path_touches_archives(path, getattr(self, "root_dir", None))

    def ls(self, path: str) -> LsResult:
        path = self._coerce_path(path) or path
        if self._archive_hit(path):
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
            if name.lower() == "archives" or self._archive_hit(p):
                continue
            filtered.append(e)
        return LsResult(error=None, entries=filtered)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        file_path = self._coerce_path(file_path) or file_path
        if self._archive_hit(file_path):
            return ReadResult(error=_DENY_MSG)
        return super().read(file_path, offset=offset, limit=limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        file_path = self._coerce_path(file_path) or file_path
        if self._archive_hit(file_path):
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
        if self._archive_hit(file_path):
            return EditResult(error=_DENY_MSG)
        return super().edit(file_path, old_string, new_string, replace_all=replace_all)

    def delete(self, file_path: str) -> DeleteResult:
        file_path = self._coerce_path(file_path) or file_path
        if self._archive_hit(file_path):
            return DeleteResult(error=_DENY_MSG)
        return super().delete(file_path)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        path = self._coerce_path(path)
        pattern = self._coerce_path(pattern) or pattern
        if self._archive_hit(path) or self._archive_hit(pattern):
            return GlobResult(error=_DENY_MSG)
        result = super().glob(pattern, path)
        if result.error or not result.matches:
            return result
        matches = []
        for m in result.matches:
            p = str(m.get("path") if isinstance(m, dict) else getattr(m, "path", m) or "")
            if not self._archive_hit(p):
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
        if self._archive_hit(path) or self._archive_hit(glob):
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
            if not self._archive_hit(p):
                matches.append(m)
        return GrepResult(
            error=None,
            matches=matches,
            truncated=bool(getattr(result, "truncated", False)),
        )

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """执行 shell；强制 UTF-8 解码，避免 Windows 默认 GBK 读 UTF-8 输出崩溃。

        这是对 `LocalShellBackend.execute` 的**有意 fork**（长驻实现）：在透传前注入
        归档守卫（`command_touches_archives`），并把输出统一 UTF-8 解码加截断。上游
        deepagents 升级会改动它的 execute（run/echo/os.environ 等）时，需同步本方法；
        否则归档守卫会随漂移而失效。改动前先diff上游 `backends/filesystem.py`。
        """
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
                creationflags=nowin(),
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
