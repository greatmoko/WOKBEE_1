"""跨平台 subprocess 调用辅助：隐藏窗口、杀进程树、可取消超时等待。"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

# 当前仍在跑的 execute 子进程（含 Job Object 句柄），暂停时从任意线程强杀。
_LIVE_LOCK = threading.Lock()
_LIVE_RUNS: list[tuple[subprocess.Popen, Any]] = []


def nowin() -> int:
    """返回应传入 subprocess 的 creationflags，隐藏子进程 cmd 窗口。

    GUI 应用（pythonw 启动）调用 subprocess 时，若不设 CREATE_NO_WINDOW，
    Windows 会给每个控制台子进程弹一个闪一下就消失的 cmd 窗口。此函数在
    win32 上返回 CREATE_NO_WINDOW（0x08000000），其它平台返回 0（无副作用）。

    用法：`subprocess.run(..., creationflags=nowin())`。
    """
    if sys.platform == "win32":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return 0


def _win_job_create() -> Any:
    """创建一个 Windows Job Object，便于 TerminateJobObject 一次收掉子进程树。"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    handle = kernel32.CreateJobObjectW(None, None)
    return handle or None


def _win_job_assign(job: Any, proc: subprocess.Popen) -> bool:
    if not job or sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    ph = int(getattr(proc, "_handle", 0) or 0)
    if not ph:
        return False
    return bool(kernel32.AssignProcessToJobObject(job, ph))


def _win_job_close(job: Any) -> None:
    if not job or sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    try:
        kernel32.CloseHandle(job)
    except OSError:
        pass


def _win_job_kill(job: Any) -> None:
    if not job or sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    try:
        kernel32.TerminateJobObject(job, 1)
    except OSError:
        pass
    _win_job_close(job)


def _register_live(proc: subprocess.Popen, job: Any) -> None:
    with _LIVE_LOCK:
        _LIVE_RUNS.append((proc, job))


def _unregister_live(proc: subprocess.Popen) -> Any:
    job = None
    with _LIVE_LOCK:
        remain: list[tuple[subprocess.Popen, Any]] = []
        for p, j in _LIVE_RUNS:
            if p is proc:
                job = j
            else:
                remain.append((p, j))
        _LIVE_RUNS[:] = remain
    return job


def kill_all_cancellable_runs() -> None:
    """暂停/超时兜底：杀掉当前所有 execute 子进程树（任意线程可调）。"""
    with _LIVE_LOCK:
        snapshot = list(_LIVE_RUNS)
        _LIVE_RUNS.clear()
    for proc, job in snapshot:
        _win_job_kill(job)
        try:
            if proc.poll() is None and proc.pid:
                kill_process_tree(int(proc.pid))
                proc.kill()
        except OSError:
            pass


def kill_process_tree(pid: int) -> None:
    """尽量杀掉 pid 及其子孙进程。

    Windows 上 `Popen.kill()` / `subprocess.run(timeout=)` 只杀直接子进程。
    若命令经 pwsh/cmd 再拉起 node/浏览器，孙进程会继承 stdout 管道，导致
    `communicate()` 在父进程已死后仍永久阻塞——超时看起来「从未生效」。
    """
    if pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(  # noqa: S603
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=8,
                creationflags=nowin(),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        return
    try:
        time.sleep(0.15)
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


@dataclass
class CancellableRunResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 1
    timed_out: bool = False
    cancelled: bool = False


def run_cancellable(
    args: Sequence[str] | str,
    *,
    timeout: float,
    cancel_event: threading.Event | None = None,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    shell: bool = False,
    encoding: str = "utf-8",
    errors: str = "replace",
    max_output_bytes: int = 100_000,
    poll_interval: float = 0.2,
) -> CancellableRunResult:
    """跑子进程：墙钟超时或 cancel_event 都会杀进程树并返回，不依赖 communicate() 超时。

    边跑边读管道，避免 PIPE 填满死锁；超时/取消时先杀整棵树再收尾，避免 Windows
    上孙进程占着管道导致永远「等待返回」。
    """
    if timeout is None or float(timeout) <= 0:
        raise ValueError(f"timeout must be positive, got {timeout}")

    popen_kw: dict[str, Any] = {
        "args": args,
        "shell": bool(shell),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "text": True,
        "encoding": encoding,
        "errors": errors,
        "cwd": cwd,
        "env": dict(env) if env is not None else None,
    }
    flags = nowin()
    if sys.platform == "win32":
        popen_kw["creationflags"] = flags
    else:
        popen_kw["start_new_session"] = True

    proc = subprocess.Popen(**popen_kw)  # noqa: S603
    job = _win_job_create()
    if job and not _win_job_assign(job, proc):
        _win_job_kill(job)
        job = None
    _register_live(proc, job)
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    stdout_size = 0
    stderr_size = 0
    lock = threading.Lock()
    limit = max(1024, int(max_output_bytes or 100_000))

    def _reader(stream, bucket: list[str], size_name: str) -> None:
        nonlocal stdout_size, stderr_size
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                with lock:
                    cur = stdout_size if size_name == "out" else stderr_size
                    if cur < limit * 2:
                        bucket.append(chunk)
                        if size_name == "out":
                            stdout_size += len(chunk.encode("utf-8", errors="replace"))
                        else:
                            stderr_size += len(chunk.encode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass

    t_out = threading.Thread(
        target=_reader, args=(proc.stdout, stdout_parts, "out"), daemon=True
    )
    t_err = threading.Thread(
        target=_reader, args=(proc.stderr, stderr_parts, "err"), daemon=True
    )
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + float(timeout)
    timed_out = False
    cancelled = False
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                kill_process_tree(proc.pid)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                kill_process_tree(proc.pid)
                break
            remaining = deadline - time.monotonic()
            time.sleep(min(poll_interval, max(0.05, remaining)))
        # 给被杀进程一点时间退出；仍活着再补一刀
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
    finally:
        leftover = _unregister_live(proc)
        if leftover is not None:
            if timed_out or cancelled:
                _win_job_kill(leftover)
            else:
                _win_job_close(leftover)
        t_out.join(timeout=1.5)
        t_err.join(timeout=2)
        for s in (proc.stdout, proc.stderr):
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass

    with lock:
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
    code = proc.returncode if proc.returncode is not None else (130 if cancelled else 124)
    if cancelled:
        code = 130
    elif timed_out:
        code = 124
    return CancellableRunResult(
        stdout=stdout,
        stderr=stderr,
        returncode=int(code),
        timed_out=timed_out,
        cancelled=cancelled,
    )
