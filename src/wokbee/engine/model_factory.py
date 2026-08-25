"""从应用厂商配置构建 OpenAI 兼容 Chat 模型。"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from tokbee.core.provider_store import ResolvedModel


def normalize_base_url(host: str) -> str:
    url = (host or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return url.rstrip("/")


def build_chat_model(resolved: ResolvedModel, *, temperature: float = 0.2) -> ChatOpenAI:
    """使用 OpenAI 兼容 Chat Completions（非 Responses API）。"""
    base = normalize_base_url(resolved.api_host)
    if not base:
        raise ValueError("厂商 API Host 为空，请先在「厂商设置」中配置。")
    if not resolved.api_key:
        raise ValueError("厂商 API Key 为空，请先在「厂商设置」中配置。")
    if not resolved.model_id:
        raise ValueError("未指定模型。")

    return ChatOpenAI(
        model=resolved.model_id,
        api_key=resolved.api_key,
        base_url=base,
        temperature=temperature,
    )
