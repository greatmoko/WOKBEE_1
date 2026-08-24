"""AI 模型配置数据管理。"""

import json
import uuid
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from pathlib import Path

from wokbee.core.errors import AIError
from wokbee.core.safe_io import safe_write_json


@dataclass
class ModelEntry:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    provider: str = ""
    model_name: str = ""
    endpoint: str = ""
    api_key: str = ""
    remark: str = ""
    is_primary: bool = False
    disable_thinking: bool = False
    reasoning_effort: str = ""  # "", "low", "high", "max"
    frequency_penalty: float = 0.0
    presence_penalty: float = 0.0
    top_k: int = 0
    stop_sequences: str = ""
    context_window: int = 0


class ModelConfigManager:
    """管理模型配置列表的增删改查和持久化。"""

    def __init__(self, config_path: str | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = Path.home() / ".wokbee" / "models.json"
        self._models: list[ModelEntry] = []
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                valid_fields = {f.name for f in ModelEntry.__dataclass_fields__.values()}
                self._models = [
                    ModelEntry(**{k: v for k, v in item.items() if k in valid_fields})
                    for item in data
                ]
            except (json.JSONDecodeError, OSError, TypeError):
                self._models = []
        else:
            self._models = []

    def save(self):
        data = [asdict(m) for m in self._models]
        safe_write_json(self._path, data)

    def list_all(self) -> list[ModelEntry]:
        return list(self._models)

    def get(self, entry_id: str) -> ModelEntry | None:
        return next((m for m in self._models if m.id == entry_id), None)

    def add(self, entry: ModelEntry):
        self._models.append(entry)
        self.save()

    def update(self, entry_id: str, **kwargs):
        for m in self._models:
            if m.id == entry_id:
                for k, v in kwargs.items():
                    if hasattr(m, k):
                        setattr(m, k, v)
                break
        self.save()

    def delete(self, entry_id: str):
        self._models = [m for m in self._models if m.id != entry_id]
        self.save()

    def set_primary(self, entry_id: str):
        for m in self._models:
            m.is_primary = (m.id == entry_id)
        self.save()

    def get_primary(self) -> ModelEntry | None:
        return next((m for m in self._models if m.is_primary), None)

    def duplicate(self, entry_id: str) -> ModelEntry | None:
        src = self.get(entry_id)
        if not src:
            return None
        d = asdict(src)
        d.pop("id", None)
        d.pop("is_primary", None)
        new = ModelEntry(**d)
        self._models.append(new)
        self.save()
        return new

    def get_unique_providers(self) -> list[tuple[str, str, str]]:
        """返回去重的 (provider, endpoint, api_key) 列表。"""
        seen: set[tuple[str, str]] = set()
        result: list[tuple[str, str, str]] = []
        for m in self._models:
            key = (m.provider, m.endpoint)
            if key not in seen:
                seen.add(key)
                result.append((m.provider, m.endpoint, m.api_key))
        return result

    @staticmethod
    def _build_models_url(endpoint: str) -> str:
        """根据用户输入的 endpoint 构建 /models 查询 URL。

        支持的输入格式：
          https://api.deepseek.com          → .../models
          https://api.deepseek.com/v1       → .../v1/models
          https://api.deepseek.com/v1/      → .../v1/models
          https://openrouter.ai/api/v1      → .../api/v1/models
          https://xxx/v1/chat/completions   → .../v1/models
        """
        url = endpoint.rstrip("/")
        # 去掉尾部的 /chat/completions
        for suffix in ["/chat/completions", "/completions"]:
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        url = url.rstrip("/")
        if not url.endswith("/models"):
            url += "/models"
        return url

    @staticmethod
    def fetch_remote_models(endpoint: str, api_key: str) -> list[str]:
        """通过 OpenAI 兼容的 GET /models 接口查询可用模型列表。"""
        url = ModelConfigManager._build_models_url(endpoint)

        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {api_key}")

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise AIError(f"HTTP {e.code}: {body[:200]}") from e
        except urllib.error.URLError as e:
            raise AIError(f"网络错误: {e.reason}") from e
        except Exception as e:
            raise AIError(f"请求异常: {e}") from e

        models: list[str] = []
        if isinstance(data, dict) and "data" in data:
            for item in data["data"]:
                model_id = item.get("id", "")
                if model_id:
                    models.append(model_id)
        models.sort()
        return models

    @staticmethod
    def mask_key(key: str) -> str:
        if len(key) <= 8:
            return "****"
        return f"{key[:3]}****{key[-4:]}"
