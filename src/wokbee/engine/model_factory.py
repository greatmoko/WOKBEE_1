"""从应用厂商配置构建 OpenAI 兼容 Chat 模型。

对 langchain_openai 的模块级「delta/消息 → LangChain 消息」转换函数打一次幂等补丁，
保留 DeepSeek 等厂商在 choice/message 里附带的 `reasoning_content`（官方默认丢弃），
塞进 additional_kwargs，供引擎层 _reasoning_text 读取，可在时间线渲染为「思考块」。
"""

from __future__ import annotations

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


def _apply_throttle_patch() -> None:
    """对 ChatOpenAI 类做**一次**进程级节流补丁：真正发起请求的核心方法前先 wait()。

    覆盖 `_generate` / `_agenerate` / `_stream` / `_astream`——公开的 invoke/stream/ainvoke
    以及 bind_tools/with_structured_output 派生的 runnable 最终都落到这些私有方法上，
    所以单点补齐即覆盖全部调用，且不会因公开/私有双重包装而等待两次。
    节流器 `ai_throttle` 为进程级单例，间隔实时读设置。
    """
    global _THROTTLE_PATCHED
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

    # 同步流：返回生成器，首次迭代前 wait
    orig_stream = getattr(ChatOpenAI, "_stream", None)
    if orig_stream is not None:

        def _s_wrapped(self, *args, **kwargs):
            gen = orig_stream(self, *args, **kwargs)

            def _it():
                ai_throttle.wait()
                yield from gen

            return _it()

        setattr(ChatOpenAI, "_stream", _s_wrapped)

    # 异步流：async generator，async for 前 wait
    orig_astream = getattr(ChatOpenAI, "_astream", None)
    if orig_astream is not None:

        async def _as_wrapped(self, *args, **kwargs):
            agen = orig_astream(self, *args, **kwargs)
            ai_throttle.wait()
            async for chunk in agen:
                yield chunk

        setattr(ChatOpenAI, "_astream", _as_wrapped)


def build_chat_model(resolved: ResolvedModel, *, temperature: float = 0.2) -> ChatOpenAI:
    """使用 OpenAI 兼容 Chat Completions（非 Responses API）。"""
    _apply_throttle_patch()
    base = normalize_base_url(resolved.api_host)
    if not base:
        raise ValueError("厂商 API Host 为空，请先在「厂商设置」中配置。")
    if not resolved.api_key:
        raise ValueError("厂商 API Key 为空，请先在「厂商设置」中配置。")
    if not resolved.model_id:
        raise ValueError("未指定模型。")

    _inject_reasoning()
    return ChatOpenAI(
        model=resolved.model_id,
        api_key=resolved.api_key,
        base_url=base,
        temperature=temperature,
    )
