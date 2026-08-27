"""本机运行环境探测 — 供 Agent 会话上下文与 shell 执行使用。

探测结果持久化在 ~/.wokbee/config.json 的 wokbee.runtime_env；
仅在环境信息为空时探测一次，后续 Agent 直接加载缓存。
"""

from __future__ import annotations

import locale
import logging
import os
import platform
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger("wokbee")

# (展示名, PATH 命令名, 版本参数；None 表示只记录路径)
_CLI_TOOLS: tuple[tuple[str, str, list[str] | None], ...] = (
    ("git", "git", ["--version"]),
    ("gh", "gh", ["--version"]),
    ("node", "node", ["--version"]),
    ("npm", "npm", ["--version"]),
    ("pnpm", "pnpm", ["--version"]),
    ("yarn", "yarn", ["--version"]),
    ("python", "python", ["--version"]),
    ("pip", "pip", ["--version"]),
    ("uv", "uv", ["--version"]),
    ("conda", "conda", ["--version"]),
    ("docker", "docker", ["--version"]),
    ("go", "go", ["version"]),
    ("rustc", "rustc", ["--version"]),
    ("cargo", "cargo", ["--version"]),
    ("dotnet", "dotnet", ["--version"]),
    ("java", "java", ["-version"]),
    ("mvn", "mvn", ["--version"]),
    ("gradle", "gradle", ["--version"]),
    ("make", "make", ["--version"]),
    ("cmake", "cmake", ["--version"]),
    ("curl", "curl", ["--version"]),
    ("wget", "wget", ["--version"]),
    ("ffmpeg", "ffmpeg", ["-version"]),
    ("bash", "bash", ["--version"]),
    ("sh", "sh", None),
    ("cmd", "cmd", None),
)

_mem_cache: RuntimeEnv | None = None
_probe_lock = threading.Lock()


@dataclass
class RuntimeEnv:
    """本机环境快照（不含项目目录；项目目录在 Agent 运行时叠加）。"""

    os_name: str = ""
    os_release: str = ""
    machine: str = ""
    cwd: str = ""
    project_root: str = ""
    python_exe: str = ""
    python_version: str = ""
    comspec: str = ""
    pwsh_exe: str = ""
    pwsh_version: str = ""
    powershell_exe: str = ""
    powershell_version: str = ""
    encoding: str = ""
    path_preview: str = ""
    tool_paths: dict[str, str] = field(default_factory=dict)
    tool_versions: dict[str, str] = field(default_factory=dict)
    probed_at: str = ""

    def to_dict(self) -> dict:
        return {
            "os_name": self.os_name,
            "os_release": self.os_release,
            "machine": self.machine,
            "cwd": self.cwd,
            "python_exe": self.python_exe,
            "python_version": self.python_version,
            "comspec": self.comspec,
            "pwsh_exe": self.pwsh_exe,
            "pwsh_version": self.pwsh_version,
            "powershell_exe": self.powershell_exe,
            "powershell_version": self.powershell_version,
            "encoding": self.encoding,
            "path_preview": self.path_preview,
            "tool_paths": dict(self.tool_paths),
            "tool_versions": dict(self.tool_versions),
            "probed_at": self.probed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> RuntimeEnv:
        return cls(
            os_name=str(raw.get("os_name") or ""),
            os_release=str(raw.get("os_release") or ""),
            machine=str(raw.get("machine") or ""),
            cwd=str(raw.get("cwd") or ""),
            python_exe=str(raw.get("python_exe") or ""),
            python_version=str(raw.get("python_version") or ""),
            comspec=str(raw.get("comspec") or ""),
            pwsh_exe=str(raw.get("pwsh_exe") or ""),
            pwsh_version=str(raw.get("pwsh_version") or ""),
            powershell_exe=str(raw.get("powershell_exe") or ""),
            powershell_version=str(raw.get("powershell_version") or ""),
            encoding=str(raw.get("encoding") or ""),
            path_preview=str(raw.get("path_preview") or ""),
            tool_paths=dict(raw.get("tool_paths") or {}),
            tool_versions=dict(raw.get("tool_versions") or {}),
            probed_at=str(raw.get("probed_at") or ""),
        )

    def with_project_root(self, project_root: str | Path | None) -> RuntimeEnv:
        root = str(Path(project_root).resolve()) if project_root else self.cwd
        return RuntimeEnv(
            os_name=self.os_name,
            os_release=self.os_release,
            machine=self.machine,
            cwd=self.cwd,
            project_root=root,
            python_exe=self.python_exe,
            python_version=self.python_version,
            comspec=self.comspec,
            pwsh_exe=self.pwsh_exe,
            pwsh_version=self.pwsh_version,
            powershell_exe=self.powershell_exe,
            powershell_version=self.powershell_version,
            encoding=self.encoding,
            path_preview=self.path_preview,
            tool_paths=dict(self.tool_paths),
            tool_versions=dict(self.tool_versions),
            probed_at=self.probed_at,
        )

    def powershell_argv_for_file(self, script_path: str | Path) -> list[str]:
        """运行 .ps1：优先 pwsh，无 pwsh 时回退 Windows PowerShell。"""
        script = str(script_path)
        if self.pwsh_exe:
            return [
                self.pwsh_exe,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script,
            ]
        exe = self.powershell_exe or "powershell"
        return [
            exe,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script,
        ]


def _run_version(argv: list[str], *, timeout: float = 1.2) -> str:
    try:
        r = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
        out = (r.stdout or r.stderr or "").strip()
        return out.splitlines()[0][:120] if out else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _which(name: str) -> str:
    p = shutil.which(name)
    return str(Path(p).resolve()) if p else ""


def _probe_cli_tools() -> tuple[dict[str, str], dict[str, str]]:
    paths: dict[str, str] = {}
    jobs: list[tuple[str, str, list[str]]] = []
    for label, cmd, ver_args in _CLI_TOOLS:
        exe = _which(cmd)
        if not exe:
            continue
        paths[label] = exe
        if ver_args:
            jobs.append((label, exe, ver_args))

    versions: dict[str, str] = {}
    if not jobs:
        return paths, versions

    def _one(job: tuple[str, str, list[str]]) -> tuple[str, str]:
        label, exe, ver_args = job
        return label, _run_version([exe, *ver_args])

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_one, job) for job in jobs]
        for fut in as_completed(futures, timeout=6):
            try:
                label, ver = fut.result()
                if ver:
                    versions[label] = ver
            except Exception:
                continue
    return paths, versions


def _do_probe_runtime_env() -> RuntimeEnv:
    """实际探测（耗时）；结果应持久化，勿在每次 Agent 运行时调用。"""
    tool_paths, tool_versions = _probe_cli_tools()
    env = RuntimeEnv(
        os_name=platform.system(),
        os_release=platform.release(),
        machine=platform.machine(),
        cwd=str(Path.cwd()),
        python_exe=str(Path(sys.executable).resolve()),
        python_version=platform.python_version(),
        comspec=os.environ.get("COMSPEC", ""),
        encoding=locale.getpreferredencoding(False) or "utf-8",
        tool_paths=tool_paths,
        tool_versions=tool_versions,
        probed_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    env.pwsh_exe = _which("pwsh")
    env.powershell_exe = _which("powershell") or _which("powershell.exe")

    shell_jobs: list[tuple[str, list[str]]] = []
    if env.pwsh_exe:
        shell_jobs.append(
            ("pwsh", [env.pwsh_exe, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"])
        )
    if env.powershell_exe:
        shell_jobs.append(
            (
                "ps5",
                [env.powershell_exe, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
            )
        )

    if shell_jobs:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = {pool.submit(_run_version, argv): key for key, argv in shell_jobs}
            for fut in as_completed(futs, timeout=4):
                key = futs[fut]
                try:
                    ver = fut.result()
                except Exception:
                    ver = ""
                if key == "pwsh":
                    env.pwsh_version = ver
                elif key == "ps5":
                    env.powershell_version = ver

    if env.pwsh_exe and not env.pwsh_version:
        env.pwsh_version = _run_version([env.pwsh_exe, "-NoProfile", "-v"])

    path_val = os.environ.get("PATH", "")
    if path_val:
        parts = path_val.split(os.pathsep)
        env.path_preview = f"{len(parts)} 目录（前 3：{'; '.join(parts[:3])}）"

    return env


def _settings_or_default(settings=None):
    if settings is not None:
        return settings
    from wokbee.core.settings import WokBeeSettings

    return WokBeeSettings()


def _is_valid_cached(raw) -> bool:
    return isinstance(raw, dict) and bool(str(raw.get("python_exe") or "").strip())


def get_runtime_env(settings=None) -> RuntimeEnv | None:
    """读取已缓存的本机环境（内存 → config.json），无缓存返回 None。"""
    global _mem_cache
    if _mem_cache is not None:
        return _mem_cache

    settings = _settings_or_default(settings)
    raw = settings.get("runtime_env")
    if not _is_valid_cached(raw):
        return None

    _mem_cache = RuntimeEnv.from_dict(raw)
    return _mem_cache


def save_runtime_env(settings, env: RuntimeEnv) -> None:
    """写入 config 并更新进程内缓存。"""
    global _mem_cache
    if not env.probed_at:
        env.probed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    settings.set("runtime_env", env.to_dict())
    settings.save()
    _mem_cache = env


def ensure_runtime_env(settings=None, *, force: bool = False) -> RuntimeEnv:
    """环境为空时探测并保存；已有缓存则直接返回。"""
    settings = _settings_or_default(settings)
    if not force:
        cached = get_runtime_env(settings)
        if cached is not None:
            return cached

    with _probe_lock:
        if not force:
            cached = get_runtime_env(settings)
            if cached is not None:
                return cached
        logger.info("本机运行环境为空或强制刷新，开始探测…")
        env = _do_probe_runtime_env()
        save_runtime_env(settings, env)
        logger.info("本机运行环境已保存（%s）", env.probed_at)
        return env


def ensure_runtime_env_async(
    settings=None,
    *,
    force: bool = False,
    on_done: Callable[[RuntimeEnv], None] | None = None,
) -> bool:
    """后台探测；若已有缓存且非 force 则跳过。返回是否启动了后台任务。"""
    settings = _settings_or_default(settings)
    if not force and get_runtime_env(settings) is not None:
        return False

    def _worker() -> None:
        try:
            env = ensure_runtime_env(settings, force=force)
            if on_done:
                on_done(env)
        except Exception:
            logger.exception("后台探测本机运行环境失败")

    threading.Thread(target=_worker, daemon=True, name="wokbee-runtime-env").start()
    return True


def collect_runtime_env(*, project_root: str | Path | None = None, settings=None) -> RuntimeEnv:
    """Agent 用：加载缓存环境并叠加项目目录；无缓存时同步探测一次。"""
    base = ensure_runtime_env(settings)
    return base.with_project_root(project_root)


def enrich_shell_env(base: dict[str, str] | None = None, *, project_root: str | Path | None = None) -> dict[str, str]:
    """subprocess / execute 用环境：UTF-8 + 注入工具路径，pwsh 目录优先入 PATH。"""
    env = dict(base or os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    rt = ensure_runtime_env()
    env["WOKBEE_PYTHON"] = rt.python_exe
    if rt.pwsh_exe:
        env["WOKBEE_PWSH"] = rt.pwsh_exe
        pwsh_dir = str(Path(rt.pwsh_exe).parent)
        path_key = "PATH"
        existing = env.get(path_key, "")
        if pwsh_dir.lower() not in existing.lower():
            env[path_key] = pwsh_dir + os.pathsep + existing if existing else pwsh_dir
    if project_root:
        env["WOKBEE_PROJECT_ROOT"] = str(Path(project_root).resolve())
    return env


def _format_tool_line(label: str, rt: RuntimeEnv) -> str | None:
    exe = rt.tool_paths.get(label)
    if not exe:
        return None
    ver = rt.tool_versions.get(label, "")
    return f"{label}={exe}" + (f" ({ver})" if ver else "")


def format_runtime_env_block(
    rt: RuntimeEnv,
    *,
    project_root: str = "",
    model: str = "",
    policy: str = "",
    extra: str = "",
) -> str:
    """将 RuntimeEnv 格式化为 Agent 可读块。"""
    lines = [
        "【运行环境】（WokBee 本机实测；execute 与 scripts 均在此环境执行）",
    ]
    if rt.probed_at:
        lines.append(f"- 环境快照时间：{rt.probed_at}（持久化缓存，非每次 Agent 重探）")
    if rt.os_name.lower() == "windows":
        lines.append(
            "- **平台：Windows（非 Linux/macOS）** — execute 勿用 head/tail/awk/sed/bash 语法；"
            "列目录用 ls 工具，或 pwsh 的 Get-ChildItem / Select-Object"
        )
    lines.extend([
        f"- OS：{rt.os_name} {rt.os_release} ({rt.machine})",
        f"- 项目目录（真实路径，**仅供 execute**）：{rt.project_root or project_root or '（未知）'}",
        f"- 当前工作目录：{rt.cwd}",
        "- 文件工具虚拟路径（read_file/write_file/ls/grep/glob 必用，禁止 C:\\\\...）：",
        "  · workspace/…  workspace 沙箱",
        "  · deliverables/…  交付物",
        "  · uploads/…  用户上传",
        "  · memory/…  经验（experiences/）",
        "  · scripts/…  管线脚本",
        "  · references/…  参考材料",
        "  · 示例：workspace/wttr_shenzhen.json（勿写完整 Windows 路径）",
        f"- Python（WokBee 解释器）：{rt.python_exe} ({rt.python_version})",
    ])
    if rt.comspec:
        lines.append(f"- COMSPEC（cmd）：{rt.comspec}")

    if rt.pwsh_exe:
        ver = f" ({rt.pwsh_version})" if rt.pwsh_version else ""
        lines.append(f"- PowerShell 7+（pwsh，**优先**）：{rt.pwsh_exe}{ver}")
    else:
        lines.append("- PowerShell 7+（pwsh）：未在 PATH 中找到")

    if rt.powershell_exe:
        ver = f" ({rt.powershell_version})" if rt.powershell_version else ""
        lines.append(
            f"- Windows PowerShell 5.x（**备选**，无 pwsh 时用）：{rt.powershell_exe}{ver}"
        )

    lines.extend([
        "- 工具选用约定：",
        "  · 有更新/更好用的本机版本时优先用新版，不要无故退回旧版",
        "  · **列目录/读文件优先 ls、read_file、grep 工具**，少用 execute dir/type/cat",
        "  · execute：WokBee 在 Windows 上**自动经 pwsh 执行**（非 cmd），但仍非 Linux/bash",
        "  · **pwsh 调用带空格路径的程序**：用 `&` 调用符 + 加引号完整路径，如 `& \"C:\\Program Files\\Python313\\python.exe\" script.py 参数`；勿把带引号路径裸放句首当作可执行命令",
        "  · 勿在 execute 中用 head/tail/awk/sed（cmd 无；pwsh 也不保证有）；截断用 Select-Object -First N",
        f"  · Python 命令/脚本优先：& \"{rt.python_exe}\" 或 python（与 WokBee 相同）",
        "  · 尽量用下方列出的 CLI 绝对路径，避免假设默认 PATH",
        "  · .ps1 管线脚本：WokBee 自动按 pwsh → powershell 顺序执行",
        f"- 文本编码：UTF-8（locale={rt.encoding}，execute 输出已尽量转 UTF-8）",
    ])

    groups: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("版本控制", ("git", "gh")),
        ("Node / 前端", ("node", "npm", "pnpm", "yarn")),
        ("Python 生态", ("python", "pip", "uv", "conda")),
        ("容器 / 构建", ("docker", "make", "cmake", "mvn", "gradle")),
        ("语言运行时", ("go", "dotnet", "java", "rustc", "cargo")),
        ("Shell / 网络", ("bash", "sh", "curl", "wget", "ffmpeg")),
    )
    for group_name, labels in groups:
        items = [line for label in labels if (line := _format_tool_line(label, rt))]
        if items:
            lines.append(f"- {group_name}：" + "；".join(items))

    if rt.path_preview:
        lines.append(f"- PATH：{rt.path_preview}")

    if model:
        lines.append(f"- 模型：{model}")
    if policy:
        lines.append(f"- 审核策略：{policy}")

    lines.append("- 禁止：访问 archives/ 归档目录")

    if extra.strip():
        lines.append(extra.strip())

    return "\n".join(lines)


def build_runtime_env_block(
    *,
    project_root: str = "",
    model: str = "",
    policy: str = "",
    extra: str = "",
    settings=None,
) -> str:
    """供【会话上下文】注入的环境说明块（读缓存，无缓存时才探测）。"""
    rt = collect_runtime_env(project_root=project_root or None, settings=settings)
    return format_runtime_env_block(
        rt,
        project_root=project_root,
        model=model,
        policy=policy,
        extra=extra,
    )


def build_runtime_env_settings_text(settings=None) -> str:
    """设置页展示用文本。"""
    settings = _settings_or_default(settings)
    rt = get_runtime_env(settings)
    if rt is None:
        return (
            "尚未探测本机环境。\n\n"
            "首次运行 Agent 时将自动探测并保存；也可点击下方「重新探测」。"
            "之后所有 Agent 均加载此缓存，不会重复扫描。"
        )
    header = f"最后探测：{rt.probed_at or '未知'}\n\n"
    return header + format_runtime_env_block(rt)


def build_execute_invocation(command: str, settings=None) -> tuple[list[str] | None, str]:
    """构造 execute 调用参数。Windows 有 pwsh 时用 pwsh -Command，避免默认 cmd 缺 head 等。"""
    rt = ensure_runtime_env(settings)
    if sys.platform == "win32" and rt.pwsh_exe:
        ps_cmd = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
            "$OutputEncoding = [Console]::OutputEncoding; "
            + command
        )
        return (
            [
                rt.pwsh_exe,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_cmd,
            ],
            "pwsh",
        )
    if sys.platform == "win32" and rt.powershell_exe:
        ps_cmd = (
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
            "$OutputEncoding = [Console]::OutputEncoding; "
            + command
        )
        return (
            [
                rt.powershell_exe,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps_cmd,
            ],
            "powershell",
        )
    return (None, "shell")
