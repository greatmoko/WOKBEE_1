"""AI 调用节流：两次调用「发起时间」的最小间隔（毫秒），保护本地模型。0 = 禁用。进程级单例。"""

from __future__ import annotations

import threading
import time


class AICallThrottle:
    """限制两次 AI 调用的发起时间至少相隔 `interval` 毫秒。

    状态为进程级（跨所有模型实例 / 调用点共享），用锁保证并发下正确。
    若当前间隔为 0，则不做任何等待。间隔在每次调用时实时读取设置，改完即生效。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_start = 0.0
        # 测试用：非 None 时覆盖真实设置，避免读配置
        self._override_interval: int | None = None

    def reset(self) -> None:
        """清空「上一次发起时间」，下（若启用）一次调用不再等待。"""
        self._last_start = 0.0

    def _current_interval(self) -> int:
        if self._override_interval is not None:
            return self._override_interval
        try:
            from wokbee.core.settings import WokBeeSettings

            return WokBeeSettings().ai_interval_ms
        except Exception:
            return 0

    def wait(self) -> None:
        """阻塞直到「距上一次调用发起 ≥ interval」。interval = 0 则立即返回。"""
        interval = self._current_interval() / 1000.0
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            if self._last_start > 0:
                target = self._last_start + interval
                if target > now:
                    time.sleep(target - now)
                    now = target
            self._last_start = now


# 进程级单例
ai_throttle = AICallThrottle()
