"""根据 SessionSettings + 厂商 family 组装 chat/completions 请求体参数。

推理统一三套机制，其余参数一律不发：
- OpenAI 风格（chat）：顶层 reasoning_effort（none/minimal/low/medium/high/xhigh/max）
- DeepSeek 风格（chat）：extra_body={"thinking":{"type":"enabled/disabled"}} + 顶层 reasoning_effort（low/high/max）
- Responses API：嵌套 reasoning={"effort": ...}（none/minimal/low/medium/high/xhigh/max）
"""

from __future__ import annotations

from tokbee.core.session_settings import SessionSettings, ProviderOptions

# OpenAI 推理模型允许的 reasoning_effort 取值（随模型而异；none 相当于关闭推理）
OPENAI_EFFORT_VALUES = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# DeepSeek 官方：Chat Completions 实际生效为 low / high / max
DEEPSEEK_EFFORT_VALUES = ("low", "medium", "high", "xhigh", "max")
# Responses API 允许的 reasoning.effort 取值
RESPONSES_EFFORT_VALUES = OPENAI_EFFORT_VALUES

_DEEPSEEK_FAMILIES = ("deepseek", "mimo")
DEEPSEEK_FAMILIES = _DEEPSEEK_FAMILIES


def effective_adapter(adapter: str, family: str) -> str:
    """解析当前模型采用的推理适配器：用户指定优先，否则 openai 默认（deepseek/mimo 归 deepseek）。"""
    a = (adapter or "").strip().lower()
    if a in ("openai", "deepseek"):
        return a
    if family in _DEEPSEEK_FAMILIES:
        return "deepseek"
    return "openai"


def _is_openai_reasoning_model(model_id: str) -> bool:
    mid = (model_id or "").lower()
    return mid.startswith("o1") or mid.startswith("o3") or mid.startswith("o4") or "gpt-5" in mid


def _thinking_requested(opts: ProviderOptions, family: str) -> bool:
    """该模型是否通过 thinking 开关（deepseek/mimo 风格）控制推理。"""
    return effective_adapter(opts.reasoning_adapter, family) == "deepseek"


def build_completion_params(
    settings: SessionSettings,
    *,
    model_id: str,
    family: str,
    stream: bool | None = None,
    api_protocol: str = "chat",
) -> dict:
    """返回应并入请求体的参数（不含 model/messages）。未设置字段不包含。"""
    body: dict = {}
    use_stream = settings.stream if stream is None else stream
    if use_stream:
        body["stream"] = True

    opts = settings.provider_options
    # 深度思考/思考型模型不接受 temperature / top_p；OpenAI 推理模型同样跳过采样
    deepseek_thinking = family in _DEEPSEEK_FAMILIES and opts.reasoning_enabled
    openai_reasoning = family == "openai" and _is_openai_reasoning_model(model_id)
    skip_sampling = deepseek_thinking or openai_reasoning

    if settings.temperature is not None and not skip_sampling:
        body["temperature"] = settings.temperature

    if settings.top_p is not None and not skip_sampling:
        body["top_p"] = settings.top_p

    if settings.max_tokens is not None and settings.max_tokens > 0:
        if openai_reasoning or family == "mimo":
            body["max_completion_tokens"] = settings.max_tokens
        else:
            body["max_tokens"] = settings.max_tokens

    _apply_reasoning(body, opts, family=family, api_protocol=api_protocol)
    return body


def _apply_reasoning(
    body: dict,
    opts: ProviderOptions,
    *,
    family: str,
    api_protocol: str,
) -> None:
    """根据适配器把推理参数写进 body；默认（开启但未设强度）一律不发。"""
    effort = (opts.reasoning_effort or "").strip().lower()

    if api_protocol == "responses":
        if not opts.reasoning_enabled:
            body["reasoning"] = {"effort": "none"}
        elif effort in RESPONSES_EFFORT_VALUES:
            body["reasoning"] = {"effort": effort}
        return

    if effective_adapter(opts.reasoning_adapter, family) == "deepseek":
        if not opts.reasoning_enabled:
            body["extra_body"] = {"thinking": {"type": "disabled"}}
        elif effort in DEEPSEEK_EFFORT_VALUES:
            body["extra_body"] = {"thinking": {"type": "enabled"}}
            body["reasoning_effort"] = effort
        return

    # OpenAI 风格
    if not opts.reasoning_enabled:
        body["reasoning_effort"] = "none"
    elif effort in OPENAI_EFFORT_VALUES:
        body["reasoning_effort"] = effort
