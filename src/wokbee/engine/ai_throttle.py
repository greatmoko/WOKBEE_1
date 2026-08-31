"""每步操作前等待：调用模型 / 调用工具前 sleep（毫秒）。0 = 禁用。进程级单例。"""

from __future__ import annotations

import time


def _shared_settings():
    """进程内缓存 WokBeeSettings 单例，避免每次 wait() 都重跑构造 + 全量 _ensure_defaults。"""
    global _SETTINGS
    if _SETTINGS is None:
        from wokbee.core.settings import WokBeeSettings

        _SETTINGS = WokBeeSettings()
    return _SETTINGS


_SETTINGS = None


class AICallThrottle:
    """每次 wait() 都先睡满当前间隔，再让调用方干活。

    不按「距上一次发起」计时。间隔实时读设置；0 则立即返回。
    测试可设 ``_override_interval``（毫秒）。
    """

    def __init__(self) -> None:
        self._override_interval: int | None = None

    def reset(self) -> None:
        """兼容旧调用；无状态可清。"""

    def _current_interval(self) -> int:
        if self._override_interval is not None:
            return self._override_interval
        try:
            return _shared_settings().ai_interval_ms
        except Exception:
            return 0

    def wait(self) -> None:
        """间隔 > 0 时阻塞这么多秒；否则立即返回。"""
        interval = self._current_interval() / 1000.0
        if interval <= 0:
            return
        time.sleep(interval)


# 进程级单例
ai_throttle = AICallThrottle()
