"""跨平台 subprocess 调用辅助：Windows 下隐藏子进程的控制台窗口。"""

from __future__ import annotations

import subprocess
import sys


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
