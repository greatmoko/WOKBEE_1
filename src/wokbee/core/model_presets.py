"""预设提供商模板 — 快速添加主流 LLM 服务商配置。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProviderPreset:
    id: str
    name: str
    icon: str
    endpoint: str
    default_models: list[str] = field(default_factory=list)
    notes: str = ""
    supports_model_query: bool = True


PROVIDER_PRESETS: list[ProviderPreset] = [
    ProviderPreset(
        id="openai",
        name="OpenAI",
        icon="🟢",
        endpoint="https://api.openai.com/v1",
        default_models=["gpt-4o", "gpt-4o-mini", "o3-mini"],
        notes="官方 OpenAI API，需海外网络",
        supports_model_query=True,
    ),
    ProviderPreset(
        id="deepseek",
        name="DeepSeek",
        icon="🔵",
        endpoint="https://api.deepseek.com/v1",
        default_models=["deepseek-chat", "deepseek-reasoner"],
        notes="国产高性能模型，支持推理模式",
        supports_model_query=True,
    ),
    ProviderPreset(
        id="openrouter",
        name="OpenRouter",
        icon="🌐",
        endpoint="https://openrouter.ai/api/v1",
        default_models=[],
        notes="聚合多家模型，单 Key 访问所有主流模型",
        supports_model_query=True,
    ),
    ProviderPreset(
        id="qwen",
        name="通义千问",
        icon="🟣",
        endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        default_models=["qwen-max", "qwen-plus", "qwen-turbo"],
        notes="阿里云通义千问，OpenAI 兼容接口",
        supports_model_query=True,
    ),
    ProviderPreset(
        id="zhipu",
        name="智谱GLM",
        icon="🔷",
        endpoint="https://open.bigmodel.cn/api/paas/v4",
        default_models=["glm-4-plus", "glm-4-flash", "glm-4-long"],
        notes="智谱 AI 大模型，支持长上下文",
        supports_model_query=True,
    ),
    ProviderPreset(
        id="moonshot",
        name="Moonshot",
        icon="🌙",
        endpoint="https://api.moonshot.cn/v1",
        default_models=["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        notes="Kimi 大模型，支持超长上下文",
        supports_model_query=True,
    ),
    ProviderPreset(
        id="siliconflow",
        name="硅基流动",
        icon="⚡",
        endpoint="https://api.siliconflow.cn/v1",
        default_models=[],
        notes="国产模型聚合平台，性价比高",
        supports_model_query=True,
    ),
    ProviderPreset(
        id="gemini",
        name="Google Gemini",
        icon="💎",
        endpoint="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_models=["gemini-2.5-pro", "gemini-2.5-flash"],
        notes="Google Gemini OpenAI 兼容接口",
        supports_model_query=True,
    ),
    ProviderPreset(
        id="local",
        name="本地部署",
        icon="🖥️",
        endpoint="http://localhost:11434/v1",
        default_models=[],
        notes="Ollama / vLLM / LM Studio 等本地服务",
        supports_model_query=True,
    ),
]


def get_preset(preset_id: str) -> ProviderPreset | None:
    return next((p for p in PROVIDER_PRESETS if p.id == preset_id), None)


# 默认模型参数值（用于一键重置）
DEFAULT_PARAMS = {
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "top_k": 0,
    "stop_sequences": "",
    "context_window": 0,
    "disable_thinking": False,
    "reasoning_effort": "",
}
