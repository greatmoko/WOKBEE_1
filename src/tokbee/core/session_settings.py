"""对话 SessionSettings — 对齐 Chatbox SessionSettingsSchema。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from tokbee.core.config import default_data_dir
from typing import Any

from tokbee.core.safe_io import safe_write_json


@dataclass
class ProviderOptions:
    """厂商推理相关选项（按当前 provider 选用）。

    统一三套机制，其余一律不传：
    - Chat Completions + OpenAI 风格：顶层 reasoning_effort（none/minimal/low/medium/high/xhigh/max）
    - Chat Completions + DeepSeek 风格：extra_body={"thinking": {"type": "enabled/disabled"}}
      并可带顶层 reasoning_effort（low/high/max）
    - Responses API：嵌套 reasoning={"effort": ...}（none/minimal/low/medium/high/xhigh/max）

    parsing 约定：空 effort / 推理开启但未设置强度时，一律不发送这些参数（默认）。
    """
    reasoning_adapter: str = ""  # "", "openai", "deepseek"；空按 family 推断（openai 默认）
    reasoning_effort: str = ""  # "", none, minimal, low, medium, high, xhigh, max
    reasoning_enabled: bool = True  # 关闭时主动发送禁用信号

    def to_dict(self) -> dict:
        return {
            "reasoning": {
                "adapter": self.reasoning_adapter,
                "effort": self.reasoning_effort,
                "enabled": self.reasoning_enabled,
            },
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ProviderOptions":
        if not d:
            return cls()
        reasoning = d.get("reasoning") or {}
        # 旧格式兼容：openai.reasoning_effort / thinking.enabled / reasoning_control
        effort = str(
            reasoning.get("effort")
            or (d.get("openai") or {}).get("reasoning_effort")
            or d.get("reasoning_effort")
            or ""
        )
        adapter = str(reasoning.get("adapter") or d.get("reasoning_control") or "")
        # 旧 reasoning_control 值映射到新 adapter
        if adapter in ("thinking", "enable_thinking"):
            adapter = "deepseek"
        elif adapter in ("reasoning_effort", "thinking_config"):
            adapter = "openai"
        elif adapter and adapter not in ("openai", "deepseek"):
            adapter = ""
        enabled = reasoning.get("enabled")
        if enabled is None:
            enabled = True
        return cls(
            reasoning_adapter=str(adapter or ""),
            reasoning_effort=effort,
            reasoning_enabled=bool(enabled),
        )


@dataclass
class SessionSettings:
    provider: str = ""
    model_id: str = ""
    system_prompt: str = "You are a helpful assistant."
    # None = 未设置（请求体不发送该字段）
    temperature: float | None = 0.7
    top_p: float | None = 1.0
    max_tokens: int | None = None
    max_context_message_count: int = 40  # 消息条数；很大视为不限
    stream: bool = True
    # 压缩触发比例（相对可用窗口）；对齐 Chatbox compactionThreshold
    compaction_threshold: float = 0.6
    auto_compaction: bool = True
    provider_options: ProviderOptions = field(default_factory=ProviderOptions)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "system_prompt": self.system_prompt,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
            "max_context_message_count": self.max_context_message_count,
            "stream": self.stream,
            "compaction_threshold": self.compaction_threshold,
            "auto_compaction": self.auto_compaction,
            "provider_options": self.provider_options.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "SessionSettings":
        if not d:
            return cls()
        # 兼容旧 ChatParams 字段
        max_ctx = d.get("max_context_message_count")
        if max_ctx is None and "history_rounds" in d:
            try:
                max_ctx = int(d["history_rounds"]) * 2
            except (TypeError, ValueError):
                max_ctx = 40
        if max_ctx is None:
            max_ctx = 40

        temp = d.get("temperature", 0.7)
        top_p = d.get("top_p", d.get("topP", 1.0))
        max_tokens = d.get("max_tokens", d.get("maxTokens"))

        # 旧字段一律读入；显式 null 表示未设置
        def _opt_float(v: Any) -> float | None:
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        def _opt_int(v: Any) -> int | None:
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        thr_raw = d.get("compaction_threshold", d.get("compactionThreshold", 0.6))
        try:
            thr = float(thr_raw)
        except (TypeError, ValueError):
            thr = 0.6
        thr = min(max(thr, 0.1), 0.95)

        return cls(
            provider=str(d.get("provider") or ""),
            model_id=str(d.get("model_id") or d.get("modelId") or ""),
            system_prompt=str(d.get("system_prompt") or "You are a helpful assistant."),
            temperature=_opt_float(temp),
            top_p=_opt_float(top_p),
            max_tokens=_opt_int(max_tokens),
            max_context_message_count=int(max_ctx),
            stream=bool(d.get("stream", True)),
            compaction_threshold=thr,
            auto_compaction=bool(d.get("auto_compaction", d.get("autoCompaction", True))),
            provider_options=ProviderOptions.from_dict(d.get("provider_options") or d.get("providerOptions")),
        )


class GlobalSessionDefaults:
    """新建会话时拷贝的全局默认 SessionSettings。"""

    def __init__(self, config_path: str | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = default_data_dir() / "session_defaults.json"
        self._settings = SessionSettings()
        self._load()

    def _load(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._settings = SessionSettings.from_dict(data)
        except (json.JSONDecodeError, OSError, TypeError):
            self._settings = SessionSettings()

    def get(self) -> SessionSettings:
        return SessionSettings.from_dict(self._settings.to_dict())

    def save(self, settings: SessionSettings):
        self._settings = SessionSettings.from_dict(settings.to_dict())
        safe_write_json(self._path, self._settings.to_dict())
