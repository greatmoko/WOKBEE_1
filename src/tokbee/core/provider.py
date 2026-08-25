"""内置 AI 厂商定义 — 仅提供 Host / 元信息；模型列表由用户拉取或手动添加。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProviderModelDef:
    model_id: str
    nickname: str = ""
    capabilities: list[str] = field(default_factory=list)
    context_window: int = 0
    max_output: int = 0


@dataclass
class BuiltinProvider:
    id: str
    name: str
    icon: str
    api_host: str
    models: list[ProviderModelDef] = field(default_factory=list)
    notes: str = ""
    # openai | gemini | deepseek | qwen | glm | kimi | openai_compat
    family: str = "openai_compat"
    supports_model_query: bool = True


BUILTIN_PROVIDERS: list[BuiltinProvider] = [
    BuiltinProvider(
        id="openai",
        name="OpenAI",
        icon="🟢",
        api_host="https://api.openai.com/v1",
        family="openai",
        notes="官方 OpenAI API",
    ),
    BuiltinProvider(
        id="gemini",
        name="Google Gemini",
        icon="💎",
        api_host="https://generativelanguage.googleapis.com/v1beta/openai/",
        family="gemini",
        notes="Gemini OpenAI 兼容接口",
    ),
    BuiltinProvider(
        id="deepseek",
        name="DeepSeek",
        icon="🔵",
        api_host="https://api.deepseek.com/v1",
        family="deepseek",
        notes="DeepSeek 官方 API",
    ),
    BuiltinProvider(
        id="glm",
        name="智谱 GLM",
        icon="🔷",
        api_host="https://open.bigmodel.cn/api/paas/v4",
        family="glm",
        notes="智谱 AI OpenAI 兼容接口",
    ),
    BuiltinProvider(
        id="qwen",
        name="通义千问",
        icon="🟣",
        api_host="https://dashscope.aliyuncs.com/compatible-mode/v1",
        family="qwen",
        notes="阿里云 DashScope 兼容模式",
    ),
    BuiltinProvider(
        id="kimi",
        name="Kimi (Moonshot)",
        icon="🌙",
        api_host="https://api.moonshot.cn/v1",
        family="kimi",
        notes="月之暗面 Kimi",
    ),
    BuiltinProvider(
        id="openrouter",
        name="OpenRouter",
        icon="🌐",
        api_host="https://openrouter.ai/api/v1",
        family="openai_compat",
        notes="聚合多家模型",
    ),
    BuiltinProvider(
        id="siliconflow",
        name="硅基流动",
        icon="⚡",
        api_host="https://api.siliconflow.cn/v1",
        family="openai_compat",
        notes="国产模型聚合",
    ),
    BuiltinProvider(
        id="ollama",
        name="Ollama",
        icon="🦙",
        api_host="http://127.0.0.1:11434/v1",
        family="openai_compat",
        notes="本地 Ollama 服务（默认 11434）",
    ),
]


def get_builtin(provider_id: str) -> BuiltinProvider | None:
    return next((p for p in BUILTIN_PROVIDERS if p.id == provider_id), None)


def infer_family(provider_id: str, api_host: str = "") -> str:
    builtin = get_builtin(provider_id)
    if builtin:
        return builtin.family
    host = (api_host or "").lower()
    if "googleapis.com" in host or "gemini" in host:
        return "gemini"
    if "deepseek" in host:
        return "deepseek"
    if "dashscope" in host or "qwen" in host:
        return "qwen"
    if "bigmodel.cn" in host:
        return "glm"
    if "moonshot" in host:
        return "kimi"
    if "openai.com" in host:
        return "openai"
    return "openai_compat"
