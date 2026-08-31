"""离屏冒烟：execute 墙钟超时 / 取消必须杀进程树并返回，不能卡在 communicate。

覆盖点：
- run_cancellable：短超时、cancel_event、带孙进程的挂起命令（Windows 管道继承场景）。
- ArchiveDeniedBackend.execute：timeout= 与 cancel_event 都能在数秒内返回。

运行：
    PYTHONPATH=src venv/Scripts/python.exe scripts/smoke_execute_timeout.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tokbee.core.subprocess_util import run_cancellable  # noqa: E402

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _failures.append(msg)
        print(f"  FAIL {msg}")
    else:
        print(f"  OK   {msg}")


def _python_sleep_cmd(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _python_orphan_child_cmd() -> list[str]:
    """父进程拉起一个继续 sleep 的孙进程，自身也 sleep —— 旧 subprocess.run(timeout)
    在 Windows 上常因孙进程占管道而永不返回。"""
    inner = "import time; time.sleep(60)"
    body = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{inner!r}]);"
        "time.sleep(60)"
    )
    return [sys.executable, "-c", body]


def test_run_cancellable() -> None:
    print("== run_cancellable ==")
    t0 = time.monotonic()
    r = run_cancellable(_python_sleep_cmd(20), timeout=1.5)
    elapsed = time.monotonic() - t0
    check(r.timed_out, "sleep 20s + timeout=1.5 → timed_out")
    check(elapsed < 6, f"墙钟超时在 6s 内返回（实际 {elapsed:.2f}s）")
    check(r.returncode == 124, f"超时 exit_code=124（实际 {r.returncode}）")

    ev = threading.Event()
    def _cancel_soon():
        time.sleep(0.4)
        ev.set()

    threading.Thread(target=_cancel_soon, daemon=True).start()
    t0 = time.monotonic()
    r2 = run_cancellable(_python_sleep_cmd(20), timeout=30, cancel_event=ev)
    elapsed = time.monotonic() - t0
    check(r2.cancelled, "cancel_event 置位 → cancelled")
    check(elapsed < 5, f"取消在 5s 内返回（实际 {elapsed:.2f}s）")
    check(r2.returncode == 130, f"取消 exit_code=130（实际 {r2.returncode}）")

    t0 = time.monotonic()
    r3 = run_cancellable(_python_orphan_child_cmd(), timeout=1.5)
    elapsed = time.monotonic() - t0
    check(r3.timed_out, "带孙进程的挂起命令 timeout 仍返回 timed_out")
    check(elapsed < 8, f"孙进程场景在 8s 内返回（实际 {elapsed:.2f}s）")


def test_backend_execute() -> None:
    print("== ArchiveDeniedBackend.execute ==")
    from wokbee.engine.archive_guard import ArchiveDeniedBackend

    tmp = Path(__file__).resolve().parent / "_smoke_execute_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    backend = ArchiveDeniedBackend(
        root_dir=str(tmp),
        virtual_mode=True,
        timeout=90,
        inherit_env=True,
    )
    # Windows execute 会包进 pwsh -Command，用 Start-Sleep 才能真正挂住
    cmd = "Start-Sleep -Seconds 20"
    t0 = time.monotonic()
    resp = backend.execute(cmd, timeout=2)
    elapsed = time.monotonic() - t0
    check(resp.exit_code == 124, f"execute timeout=2 → exit 124（实际 {resp.exit_code}）")
    check("timed out" in (resp.output or "").lower() or "超时" in (resp.output or ""),
          "超时文案含 timed out")
    check(elapsed < 8, f"execute 超时在 8s 内返回（实际 {elapsed:.2f}s）")

    ev = threading.Event()
    backend.cancel_event = ev

    def _cancel_soon():
        time.sleep(0.4)
        ev.set()

    threading.Thread(target=_cancel_soon, daemon=True).start()
    t0 = time.monotonic()
    resp2 = backend.execute(cmd, timeout=30)
    elapsed = time.monotonic() - t0
    check(resp2.exit_code == 130, f"execute 暂停 → exit 130（实际 {resp2.exit_code}）")
    check("取消" in (resp2.output or ""), "取消文案含「取消」")
    check(elapsed < 6, f"execute 取消在 6s 内返回（实际 {elapsed:.2f}s）")

    print("== attach_execute_watch ==")
    from wokbee.engine.archive_guard import attach_execute_watch
    from deepagents.backends.protocol import ExecuteResponse

    class _Hang:
        def execute(self, command: str, *, timeout: int | None = None):
            time.sleep(3)
            return ExecuteResponse(output="late", exit_code=0, truncated=False)

    hang = _Hang()
    ev = threading.Event()
    attach_execute_watch(hang, cancel_event=ev, default_timeout=90)
    t0 = time.monotonic()
    resp3 = hang.execute("x", timeout=2)
    elapsed = time.monotonic() - t0
    check(resp3.exit_code == 124, f"watch 超时 → 124（实际 {resp3.exit_code}）")
    check(elapsed < 6, f"watch 超时在 6s 内返回（实际 {elapsed:.2f}s）")

    hang2 = _Hang()
    ev2 = threading.Event()
    attach_execute_watch(hang2, cancel_event=ev2, default_timeout=90)

    def _c():
        time.sleep(0.3)
        ev2.set()

    threading.Thread(target=_c, daemon=True).start()
    t0 = time.monotonic()
    resp4 = hang2.execute("x", timeout=30)
    elapsed = time.monotonic() - t0
    check(resp4.exit_code == 130, f"watch 暂停 → 130（实际 {resp4.exit_code}）")
    check(elapsed < 5, f"watch 暂停在 5s 内返回（实际 {elapsed:.2f}s）")


def main() -> int:
    test_run_cancellable()
    test_backend_execute()
    print("=== smoke_execute_timeout ===")
    if _failures:
        print(f"失败 {len(_failures)} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
