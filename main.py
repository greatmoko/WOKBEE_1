"""WokBee 应用启动入口。"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from tokbee.app import Application
from tokbee.utils.logger import setup_logger


def _ensure_console_handles() -> None:
    """pythonw.exe（无控制台启动）下 sys.stdout/stderr 为 None。

    直接使用会令 logging.StreamHandler 拿到 None 流、或让某些库 print() 崩溃。
    为「无控制台后台运行」做保险：把空句柄重定向到系统空设备，保证不炸、也便于脚本
    配合 pythonw 启动。
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


_CHILD_CONSOLE_PATCHED = False


def _suppress_child_consoles() -> None:
    """Win32：本进程创建的控制台子进程默认不弹 cmd 窗口（GUI 后台运行）。

    只在调用方未显式指定 creationflags 时才补 CREATE_NO_WINDOW，绝不覆盖显式选择。
    从 subprocess.Popen 入手，因此 subprocess.run / asyncio.subprocess_exec（anyio/MCP
    的 stdio 服务、deepagents 等）一律生效；避免「任务跑起来时 cmd 窗口一闪一闪」。
    """
    global _CHILD_CONSOLE_PATCHED
    if _CHILD_CONSOLE_PATCHED or sys.platform != "win32":
        return
    import subprocess as _sp

    no_window = int(getattr(_sp, "CREATE_NO_WINDOW", 0) or 0)
    if not no_window:
        return
    _orig = _sp.Popen.__init__

    def _patched(self, *args, **kwargs):
        if kwargs.get("creationflags", 0) == 0:
            kwargs["creationflags"] = no_window
        return _orig(self, *args, **kwargs)

    _sp.Popen.__init__ = _patched
    _CHILD_CONSOLE_PATCHED = True


def main():
    # 仅在「无控制台」启动（pythonw）下才隐藏子进程窗口；控制台（python.exe）开发启动不干预
    no_console = sys.stdout is None or sys.stderr is None
    _ensure_console_handles()
    if no_console:
        _suppress_child_consoles()
    logger = setup_logger()
    logger.info("WokBee 启动中...")

    app = Application()
    exit_code = app.run()

    logger.info("WokBee 已退出")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
