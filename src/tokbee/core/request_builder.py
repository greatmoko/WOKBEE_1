"""根据 SessionSettings + 厂商 family 组装 chat/completions 请求体参数。"""

from __future__ import annotations

from tokbee.core.session_settings import SessionSettings, ProviderOptions

# DeepSeek 官方：Chat Completions 实际生效为 low / high / max
_DEEPSEEK_EFFORT_MAP = {
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
}


def _is_openai_reasoning_model(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return mid.startswith("o1") or mid.startswith("o3") or mid.startswith("o4") or "gpt-5" in mid


def _deepseek_thinking_active(opts: ProviderOptions) -> bool:
    """DeepSeek 默认开启思考；仅显式 off 时关闭。"""
    return opts.thinking_enabled != "off"


def build_completion_params(
    settings: SessionSettings,
    *,
    model_id: str,
    family: str,
    stream: bool | None = None,
) -> dict:
    """返回应并入请求体的参数（不含 model/messages）。未设置字段不包含。"""
    body: dict = {}
    use_stream = settings.stream if stream is None else stream
    if use_stream:
        body["stream"] = True

    opts = settings.provider_options
    # DeepSeek 官方：思考模式下 temperature / top_p 等无效
    skip_sampling = family == "deepseek" and _deepseek_thinking_active(opts)
    # 小米 MiMo：思考型模型不接受 temperature / top_p（传了会 400），统一跳过采样
    mimo_thinking = family == "mimo"

    if settings.temperature is not None and not skip_sampling and not mimo_thinking and not (
        family == "openai" and _is_openai_reasoning_model(model_id)
    ):
        body["temperature"] = settings.temperature

    if settings.top_p is not None and not skip_sampling and not mimo_thinking and not (
        family == "openai" and _is_openai_reasoning_model(model_id)
    ):
        body["top_p"] = settings.top_p

    if settings.max_tokens is not None and settings.max_tokens > 0:
        # MiMo 用 max_completion_tokens（而非旧的 max_tokens）
        if family == "mimo" or (family == "openai" and _is_openai_reasoning_model(model_id)):
            body["max_completion_tokens"] = settings.max_tokens
        else:
            body["max_tokens"] = settings.max_tokens

    _apply_provider_options(body, opts, family=family, model_id=model_id)
    return body


def _apply_provider_options(
    body: dict,
    opts: ProviderOptions,
    *,
    family: str,
    model_id: str,
):
    if family == "openai":
        if opts.openai_reasoning_effort in ("low", "medium", "high"):
            body["reasoning_effort"] = opts.openai_reasoning_effort
        return

    if family == "gemini":
        thinking: dict = {}
        if opts.google_include_thoughts:
            thinking["include_thoughts"] = True
        if opts.google_thinking_level:
            thinking["thinking_level"] = opts.google_thinking_level
        if opts.google_thinking_budget is not None:
            thinking["thinking_budget"] = opts.google_thinking_budget
        if thinking:
            body["extra_body"] = {"google": {"thinking_config": thinking}}
            body["thinking_config"] = thinking
        return

    if family == "deepseek":
        if opts.thinking_enabled == "on":
            body["thinking"] = {"type": "enabled"}
        elif opts.thinking_enabled == "off":
            body["thinking"] = {"type": "disabled"}
        # 默认不传 thinking，由服务端默认开启

        if _deepseek_thinking_active(opts):
            raw = (opts.openai_reasoning_effort or "").strip().lower()
            if raw in _DEEPSEEK_EFFORT_MAP:
                body["reasoning_effort"] = _DEEPSEEK_EFFORT_MAP[raw]
        return

    if family == "mimo":
        return

    if family in ("qwen", "glm", "kimi", "openai_compat"):
        if opts.thinking_enabled == "on":
            body["thinking"] = {"type": "enabled"}
            if family == "qwen":
                body["enable_thinking"] = True
                body["chat_template_kwargs"] = {"enable_thinking": True}
            # 兼容代理：透传 reasoning_effort
            raw = (opts.openai_reasoning_effort or "").strip().lower()
            if raw in _DEEPSEEK_EFFORT_MAP:
                body["reasoning_effort"] = _DEEPSEEK_EFFORT_MAP[raw]
        elif opts.thinking_enabled == "off":
            body["thinking"] = {"type": "disabled"}
            body["chat_template_kwargs"] = {"enable_thinking": False}
            if family == "qwen":
                body["enable_thinking"] = False
        return
