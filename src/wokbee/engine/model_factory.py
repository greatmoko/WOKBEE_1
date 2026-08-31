"""从应用厂商配置构建 OpenAI 兼容 Chat 模型。

对 langchain_openai 的模块级「delta/消息 → LangChain 消息」转换函数打一次幂等补丁，
保留 DeepSeek 等厂商在 choice/message 里附带的 `reasoning_content`（官方默认丢弃），
塞进 additional_kwargs，供引擎层 _reasoning_text 读取，可在时间线渲染为「思考块」。
"""

from __future__ import annotations

import threading

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from tokbee.core.provider_store import ResolvedModel


def normalize_base_url(host: str) -> str:
    url = (host or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def _reasoning_of(raw: object) -> str:
    if isinstance(raw, list):
        return "".join(str(x) for x in raw)
    return str(raw or "").strip()


def _inject_reasoning() -> None:
    """幂等地给 langchain_openai 的转换函数打补丁，保留 reasoning_content。"""
    import langchain_openai.chat_models.base as _lb

    if getattr(_lb, "_wokbee_reasoning_patched", False):
        return

    orig_dict = _lb._convert_dict_to_message

    def _patched_dict(_dict):
        msg = orig_dict(_dict)
        rc = _reasoning_of(_dict.get("reasoning_content")) if isinstance(_dict, dict) else ""
        if rc and isinstance(msg, AIMessage) and not msg.additional_kwargs.get("reasoning_content"):
            msg.additional_kwargs["reasoning_content"] = rc
        return msg

    orig_delta = _lb._convert_delta_to_message_chunk

    def _patched_delta(_dict, default_class):
        chunk = orig_delta(_dict, default_class)
        rc = _reasoning_of(_dict.get("reasoning_content")) if isinstance(_dict, dict) else ""
        if rc and hasattr(chunk, "additional_kwargs") and not chunk.additional_kwargs.get(
            "reasoning_content"
        ):
            chunk.additional_kwargs["reasoning_content"] = rc
        return chunk

    _lb._convert_dict_to_message = _patched_dict
    _lb._convert_delta_to_message_chunk = _patched_delta
    _lb._wokbee_reasoning_patched = True


_THROTTLE_PATCHED = False
_THROTTLE_LOCK = threading.Lock()


def _apply_throttle_patch() -> None:
    """对 ChatOpenAI 类做**一次**进程级节流补丁：真正发起请求的核心方法前先 wait()。

    覆盖 `_generate` / `_agenerate` / `_stream` / `_astream`。每次调用前 sleep 当前间隔。
    流式路径必须先 wait 再调原方法，避免 HTTP 在等待前发出。
    """
    global _THROTTLE_PATCHED
    # 标志位检查-设置必须加锁：否则并发线程会在检查时都看到 False，
    # 各自包装 _generate/_stream，导致每次请求等两次节流（C1）。
    with _THROTTLE_LOCK:
        if _THROTTLE_PATCHED:
            return
        _THROTTLE_PATCHED = True

    from wokbee.engine.ai_throttle import ai_throttle

    def _gather(name: str):
        orig = getattr(ChatOpenAI, name, None)
        if orig is None:
            return None

        def wrapped(self, *args, **kwargs):
            ai_throttle.wait()
            return orig(self, *args, **kwargs)

        return orig, wrapped

    # 同步生成（返回 ChatResult）
    pair = _gather("_generate")
    if pair:
        setattr(ChatOpenAI, "_generate", pair[1])

    # 异步生成：async def，wait 后 await 原方法
    orig_agen = getattr(ChatOpenAI, "_agenerate", None)
    if orig_agen is not None:

        async def _a_wrapped(self, *args, **kwargs):
            ai_throttle.wait()
            return await orig_agen(self, *args, **kwargs)

        setattr(ChatOpenAI, "_agenerate", _a_wrapped)

    # 同步流：先 wait 再创建底层流，避免 HTTP 在等待前就发出
    orig_stream = getattr(ChatOpenAI, "_stream", None)
    if orig_stream is not None:

        def _s_wrapped(self, *args, **kwargs):
            def _it():
                ai_throttle.wait()
                yield from orig_stream(self, *args, **kwargs)

            return _it()

        setattr(ChatOpenAI, "_stream", _s_wrapped)

    # 异步流：先 wait 再迭代原异步生成器
    orig_astream = getattr(ChatOpenAI, "_astream", None)
    if orig_astream is not None:

        async def _as_wrapped(self, *args, **kwargs):
            ai_throttle.wait()
            async for chunk in orig_astream(self, *args, **kwargs):
                yield chunk

        setattr(ChatOpenAI, "_astream", _as_wrapped)


def build_chat_model(
    resolved: ResolvedModel,
    *,
    temperature: float = 0.2,
    timeout: float | None = None,
) -> ChatOpenAI:
    """使用 OpenAI 兼容 Chat Completions（非 Responses API）。

    timeout>0 时给单次模型请求挂硬超时：真正卡死的连接会在此范围内抛错，避免
    agent.stream() 永久阻塞在模型调用上——那样「终止」只能翻到下一个 chunk 边界才生效，
    用户点停止后任务会一直卡在运行中/待审核。流式响应的读超时按「相邻字节间隔」计，
    持续出 token 的正常流式不受影响，只拦真正无响应的挂死连接。
    max_retries=1：不重试一次就卡死的请求（默认 2 会把一次挂起放大成三次）。
    """
    _apply_throttle_patch()
    base = normalize_base_url(resolved.api_host)
    if not base:
        raise ValueError("厂商 API Host 为空，请先在「厂商设置」中配置。")
    if not resolved.api_key:
        raise ValueError("厂商 API Key 为空，请先在「厂商设置」中配置。")
    if not resolved.model_id:
        raise ValueError("未指定模型。")

    _inject_reasoning()
    kwargs: dict = {
        "model": resolved.model_id,
        "api_key": resolved.api_key,
        "base_url": base,
        "temperature": temperature,
        "max_retries": 1,
    }
    if timeout and timeout > 0:
        kwargs["timeout"] = float(timeout)
    return ChatOpenAI(**kwargs)
