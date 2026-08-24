"""对话参数 — 每个对话独立的 AI 参数配置。"""

from dataclasses import dataclass, asdict


@dataclass
class ChatParams:
    system_prompt: str = "You are a helpful assistant."
    temperature: float = 0.7
    top_p: float = 1.0
    max_tokens: int = 8192
    history_rounds: int = 20
    stream: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChatParams":
        fields = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**fields)
