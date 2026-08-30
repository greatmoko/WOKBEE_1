"""WokBee 执行引擎：Deep Agents + LangGraph。

注意：本包的 `__init__` 不再直接导入 runner / lessons 等重型模块。deepagents /
langchain / langgraph 的导入会被挪到「真正运行 Agent 时」或由
`start_engine_warmup()` 在后台线程预加载——从而避免把整个引擎栈拖进应用启动关键路径。
"""

from __future__ import annotations

import importlib
import threading
from typing import Any

__all__ = [
    "AgentRunner",
    "RunRequest",
    "RunResult",
    "LessonStore",
    "ensure_engine_warm",
    "start_engine_warmup",
]

# 重度栈：touch 到 runner 就会连带 deepagents/langchain/langgraph。
# worker / executor 也被一并预载，避免首个 Agent 运行时同步导入。
_ENGINE_STACK = (
    "wokbee.engine.runner",
    "wokbee.engine.worker",
    "autobee.engine.executor",
)

_warmup_thread: threading.Thread | None = None
_warmup_lock = threading.Lock()


def _import_engine_stack() -> None:
    for name in _ENGINE_STACK:
        importlib.import_module(name)


def _do_warmup() -> None:
    import logging
    import time

    logger = logging.getLogger("wokbee")
    for attempt in (1, 2):
        try:
            _import_engine_stack()
            return
        except Exception as exc:  # noqa: BLE001
            # PySide6 6.11.x(shiboken) 与 Python 3.14 存在一个**偶发、非致命**的导入竞态：
            # shiboken 注入的 meta_path 钩子 inspect 新导入模块（feature 检测）时，在**非主线程**
            # 导入 pydantic 会递归进 inspect.getsource → unwrap。只发生在预加载后台线程，
            # 且属临时性——重试一次通常即通过；失败也不致命（引擎随后按需加载）。
            if attempt == 1:
                logger.warning(
                    "引擎后台预加载遇临时竞态（attempt 1），重试：%s: %s",
                    type(exc).__name__, exc,
                )
                time.sleep(0.5)
                continue
            logger.warning(
                "引擎后台预加载未完成（首次运行时将按需加载）：%s: %s",
                type(exc).__name__, exc,
            )


def start_engine_warmup() -> threading.Thread | None:
    """启动后台预加载线程（幂等）。窗口显示后调用，不阻塞 UI。"""
    global _warmup_thread
    with _warmup_lock:
        if _warmup_thread is not None:
            return _warmup_thread
        _warmup_thread = threading.Thread(
            target=_do_warmup, name="wokbee-engine-warmup", daemon=True
        )
        _warmup_thread.start()
    return _warmup_thread


def ensure_engine_warm(timeout: float = 30.0) -> None:
    """确保引擎已加载；若预热线程仍在跑则等待至多 ``timeout`` 秒。

    仅供**工作线程**调用（APScheduler 线程池、AgentWorker/LessonWorker 等）。
    不要在 UI 主线程调用——那会阻塞界面；主线程只需靠
    ``start_engine_warmup()`` 预热。
    """
    with _warmup_lock:
        thread = _warmup_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    # 兜底：预热失败/未启动时立即导入（幂等，不会重复加载）
    try:
        _import_engine_stack()
    except Exception:
        pass  # 让调用方报真正的业务错误


def __getattr__(name: str) -> Any:
    """PEP 562：保留 `from wokbee.engine import AgentRunner/…` 兼容，但延迟加载。"""
    if name in ("AgentRunner", "RunRequest", "RunResult"):
        from . import runner

        return getattr(runner, name)
    if name == "LessonStore":
        from . import lessons

        return getattr(lessons, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
