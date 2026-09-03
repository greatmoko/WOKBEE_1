"""厂商配置持久化 — Chatbox 风格 ProviderSettings。

左侧「我的厂商」仅显示用户主动添加的项；内置厂商通过添加弹窗加入。
"""

from __future__ import annotations

import json
import logging
import uuid
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from pathlib import Path
from tokbee.core.config import default_data_dir

from tokbee.core.errors import AIError
from tokbee.core.provider import (
    BUILTIN_PROVIDERS, ProviderModelDef,
    get_builtin, infer_family,
)
from tokbee.core.safe_io import safe_write_json
from wokbee.core.credential_crypto import (
    CredentialVaultError,
    decode_key,
    encode_key,
    generate_key,
    open_sealed,
    seal,
)

_logger = logging.getLogger(__name__)

_master_backend_ref: object | None = None


def _master_backend():
    """复用保险箱的 Keyring 主密钥后端（Windows 凭据管理器 / DPAPI）。"""
    global _master_backend_ref
    if _master_backend_ref is None:
        from wokbee.core.credential_store import KeyringBackend
        _master_backend_ref = KeyringBackend()
    return _master_backend_ref


def _get_master_key() -> bytes | None:
    """取主密钥；缺失则生成并写入系统凭据管理器。不可用时返回 None（降级明文）。"""
    try:
        encoded = _master_backend().get()
        if encoded:
            return decode_key(encoded)
        key = generate_key()
        _master_backend().set(encode_key(key))
        return key
    except Exception as e:  # noqa: BLE001
        _logger.warning("无法访问系统凭据管理器，API Key 将退回明文保存: %s", e)
        return None


def _seal_key(plain: str) -> str:
    """把明文 API Key 封成信封（密文 JSON 文本）；无法加密时降级为明文。"""
    if not plain:
        return ""
    key = _get_master_key()
    if key is None:
        return plain
    try:
        return seal({"v": 1, "s": plain}, key)
    except Exception as e:  # noqa: BLE001
        _logger.warning("API Key 加密失败，退回明文保存: %s", e)
        return plain


def _open_key(blob: str) -> str:
    """解开信封得到明文 API Key；兼容旧版明文；解密失败返回空串（不崩溃）。"""
    if not blob:
        return ""
    if not blob.lstrip().startswith("{"):
        return blob  # 旧版明文
    key = _get_master_key()
    if key is None:
        _logger.warning("凭据管理器不可用，无法解密 API Key，已置空")
        return ""
    try:
        data = open_sealed(blob, key)
        return str(data.get("s") or "")
    except CredentialVaultError as e:
        _logger.warning("解密 API Key 失败，已置空: %s", e)
        return ""



@dataclass
class ProviderModel:
    model_id: str
    nickname: str = ""
    capabilities: list[str] = field(default_factory=list)
    context_window: int = 1_000_000
    max_output: int = 0
    enabled: bool = False  # 默认不勾选，由用户启用
    api_protocol: str = "chat"  # chat | responses
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = True
    reasoning_enabled: bool = True
    openai_reasoning_effort: str = ""
    deepseek_reasoning_effort: str = ""

    @classmethod
    def from_def(cls, d: ProviderModelDef, enabled: bool = False) -> "ProviderModel":
        return cls(
            model_id=d.model_id,
            nickname=d.nickname,
            capabilities=list(d.capabilities),
            context_window=d.context_window,
            max_output=d.max_output,
            enabled=enabled,
            api_protocol="chat",
        )

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderModel":
        return cls(
            model_id=str(d.get("model_id", "")),
            nickname=str(d.get("nickname", "")),
            capabilities=list(d.get("capabilities") or []),
            context_window=int(d.get("context_window") or 1_000_000),
            max_output=int(d.get("max_output") or 0),
            enabled=bool(d.get("enabled", False)),
            api_protocol=(
                str(d.get("api_protocol") or "chat").strip().lower()
                if str(d.get("api_protocol") or "chat").strip().lower() in ("chat", "responses")
                else "chat"
            ),
            temperature=_optional_float(d.get("temperature")),
            top_p=_optional_float(d.get("top_p")),
            max_tokens=_optional_int(d.get("max_tokens")),
            stream=bool(d.get("stream", True)),
            reasoning_enabled=bool(d.get("reasoning_enabled", True)),
            openai_reasoning_effort=str(d.get("openai_reasoning_effort") or ""),
            deepseek_reasoning_effort=str(d.get("deepseek_reasoning_effort") or ""),
        )


def _optional_float(value, default: float | None = None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ProviderSettings:
    api_key: str = ""
    api_host: str = ""
    models: list[ProviderModel] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "api_key": _seal_key(self.api_key),
            "api_host": self.api_host,
            "models": [asdict(m) for m in self.models],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ProviderSettings":
        models = [ProviderModel.from_dict(m) for m in (d.get("models") or [])]
        return cls(
            api_key=_open_key(str(d.get("api_key", ""))),
            api_host=str(d.get("api_host", "")),
            models=models,
        )


@dataclass
class CustomProviderInfo:
    id: str = field(default_factory=lambda: f"custom-{uuid.uuid4().hex[:8]}")
    name: str = "自定义本地 API"
    icon: str = "🖥️"
    family: str = "openai_compat"
    notes: str = "自定义 OpenAI 兼容本地 / 私有 API"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CustomProviderInfo":
        return cls(
            id=str(d.get("id") or f"custom-{uuid.uuid4().hex[:8]}"),
            name=str(d.get("name") or "自定义本地 API"),
            icon=str(d.get("icon") or "🖥️"),
            family=str(d.get("family") or "openai_compat"),
            notes=str(d.get("notes") or "自定义 OpenAI 兼容本地 / 私有 API"),
        )


@dataclass
class ResolvedModel:
    """对话调用时解析出的当前模型连接信息。"""
    provider_id: str
    provider_name: str
    model_id: str
    api_host: str
    api_key: str
    family: str
    context_window: int = 1_000_000
    api_protocol: str = "chat"
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stream: bool = True
    reasoning_enabled: bool = True
    openai_reasoning_effort: str = ""
    deepseek_reasoning_effort: str = ""


class ProviderStore:
    """管理「我的厂商」列表与各厂商配置。"""

    def __init__(self, config_path: str | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = default_data_dir() / "providers.json"
        self._providers: dict[str, ProviderSettings] = {}
        self._custom: list[CustomProviderInfo] = []
        self._enabled_ids: list[str] = []  # 「我的厂商」顺序
        self._default_provider: str = ""
        self._default_model_id: str = ""
        self._cleared_builtin_models: bool = False
        self._load()

    def _load(self):
        if not self._path.exists():
            self._providers = {}
            self._custom = []
            self._enabled_ids = []
            self._default_provider = ""
            self._default_model_id = ""
            self._cleared_builtin_models = True
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            raw = data.get("providers") or {}
            self._providers = {
                pid: ProviderSettings.from_dict(cfg)
                for pid, cfg in raw.items()
                if isinstance(cfg, dict)
            }
            self._custom = [
                CustomProviderInfo.from_dict(item)
                for item in (data.get("custom_providers") or [])
                if isinstance(item, dict)
            ]
            enabled = data.get("enabled_providers")
            if isinstance(enabled, list):
                self._enabled_ids = [str(x) for x in enabled]
            else:
                # 兼容旧数据：若已有 providers 配置，视为已添加
                self._enabled_ids = list(self._providers.keys())
                if not self._enabled_ids:
                    self._enabled_ids = [c.id for c in self._custom]
            self._default_provider = str(data.get("default_provider") or "")
            self._default_model_id = str(
                data.get("default_model_id") or data.get("default_model") or ""
            )
            self._cleared_builtin_models = bool(data.get("cleared_builtin_models_v1"))
            if not self._cleared_builtin_models:
                self._clear_builtin_seed_models()
        except (json.JSONDecodeError, OSError, TypeError):
            self._providers = {}
            self._custom = []
            self._enabled_ids = []
            self._default_provider = ""
            self._default_model_id = ""
            self._cleared_builtin_models = True

    def _clear_builtin_seed_models(self) -> None:
        """一次性清除内置厂商里系统预置的模型列表（自定义厂商不动）。"""
        for pid, settings in list(self._providers.items()):
            if get_builtin(pid) is None:
                continue
            settings.models = []
            if self._default_provider == pid:
                self._default_provider = ""
                self._default_model_id = ""
        self._cleared_builtin_models = True
        self.save()

    def save(self):
        data = {
            "enabled_providers": list(self._enabled_ids),
            "providers": {pid: cfg.to_dict() for pid, cfg in self._providers.items()},
            "custom_providers": [c.to_dict() for c in self._custom],
            "default_provider": self._default_provider,
            "default_model_id": self._default_model_id,
            "cleared_builtin_models_v1": True,
        }
        safe_write_json(self._path, data)

    def list_my_providers(self) -> list[tuple[str, str, str, str]]:
        """我的厂商列表 [(id, name, icon, family), ...]。"""
        result: list[tuple[str, str, str, str]] = []
        seen: set[str] = set()
        for pid in self._enabled_ids:
            if pid in seen:
                continue
            info = self._provider_meta(pid)
            if info:
                result.append(info)
                seen.add(pid)
        return result

    def list_provider_infos(self) -> list[tuple[str, str, str, str]]:
        """兼容旧调用名 → 仅返回我的厂商。"""
        return self.list_my_providers()

    def list_addable_builtins(self) -> list[tuple[str, str, str, str]]:
        """尚未加入「我的厂商」的内置厂商。"""
        enabled = set(self._enabled_ids)
        return [
            (p.id, p.name, p.icon, p.family)
            for p in BUILTIN_PROVIDERS
            if p.id not in enabled
        ]

    def _provider_meta(self, provider_id: str) -> tuple[str, str, str, str] | None:
        b = get_builtin(provider_id)
        if b:
            return (b.id, b.name, b.icon, b.family)
        for c in self._custom:
            if c.id == provider_id:
                return (c.id, c.name, c.icon, c.family)
        return None

    def get_display_name(self, provider_id: str) -> str:
        meta = self._provider_meta(provider_id)
        return meta[1] if meta else provider_id

    def get_family(self, provider_id: str) -> str:
        meta = self._provider_meta(provider_id)
        if meta:
            return meta[3]
        settings = self.get_settings(provider_id)
        return infer_family(provider_id, settings.api_host)

    def is_custom(self, provider_id: str) -> bool:
        return provider_id.startswith("custom-") or any(c.id == provider_id for c in self._custom)

    def get_settings(self, provider_id: str) -> ProviderSettings:
        if provider_id in self._providers:
            return self._providers[provider_id]
        return self._default_settings(provider_id)

    def _default_settings(self, provider_id: str) -> ProviderSettings:
        b = get_builtin(provider_id)
        if b:
            # 内置厂商只带默认 Host，模型需用户拉取或手动添加
            return ProviderSettings(api_host=b.api_host, models=[])
        return ProviderSettings()

    def update_settings(self, provider_id: str, settings: ProviderSettings):
        self._providers[provider_id] = settings
        if provider_id not in self._enabled_ids:
            self._enabled_ids.append(provider_id)
        # 若默认模型被取消勾选，自动清除
        if (
            self._default_provider == provider_id
            and self._default_model_id
            and not any(
                m.enabled and m.model_id == self._default_model_id
                for m in settings.models
            )
        ):
            self._default_provider = ""
            self._default_model_id = ""
        self.save()

    def add_builtin_provider(self, provider_id: str) -> bool:
        """将内置厂商加入我的列表。已存在则返回 False。"""
        if get_builtin(provider_id) is None:
            return False
        if provider_id in self._enabled_ids:
            return False
        if provider_id not in self._providers:
            self._providers[provider_id] = self._default_settings(provider_id)
        self._enabled_ids.append(provider_id)
        self.save()
        return True

    def add_custom_provider(
        self,
        name: str,
        api_host: str = "",
        api_key: str = "",
    ) -> CustomProviderInfo:
        """添加自定义本地 / 私有 OpenAI 兼容厂商（需填写厂商名称）。"""
        clean = name.strip()
        if not clean:
            raise ValueError("请填写厂商名称")
        info = CustomProviderInfo(name=clean)
        self._custom.append(info)
        self._providers[info.id] = ProviderSettings(
            api_key=api_key.strip(),
            api_host=api_host.strip(),
            models=[],
        )
        self._enabled_ids.append(info.id)
        self.save()
        return info

    def remove_from_my_list(self, provider_id: str):
        """从我的厂商中移除；自定义厂商同时删除配置。"""
        self._enabled_ids = [x for x in self._enabled_ids if x != provider_id]
        if self.is_custom(provider_id):
            self._custom = [c for c in self._custom if c.id != provider_id]
            self._providers.pop(provider_id, None)
        if self._default_provider == provider_id:
            self._default_provider = ""
            self._default_model_id = ""
        self.save()

    def remove_custom_provider(self, provider_id: str):
        self.remove_from_my_list(provider_id)

    def reset_provider(self, provider_id: str, *, keep_api_key: bool = True):
        key = ""
        if keep_api_key and provider_id in self._providers:
            key = self._providers[provider_id].api_key
        settings = self._default_settings(provider_id)
        settings.api_key = key
        self._providers[provider_id] = settings
        if self._default_provider == provider_id:
            self._default_provider = ""
            self._default_model_id = ""
        self.save()

    def get_default_ref(self) -> tuple[str, str]:
        """返回 (provider_id, model_id)，未设置则为 ("", "")。"""
        return self._default_provider, self._default_model_id

    def is_default_model(self, provider_id: str, model_id: str) -> bool:
        return (
            bool(provider_id)
            and bool(model_id)
            and self._default_provider == provider_id
            and self._default_model_id == model_id
        )

    def set_default_model(self, provider_id: str, model_id: str):
        """设定新建对话使用的默认模型（需为已启用模型）。"""
        if not provider_id or not model_id:
            raise ValueError("厂商或模型无效")
        settings = self.get_settings(provider_id)
        matched = next(
            (m for m in settings.models if m.model_id == model_id),
            None,
        )
        if not matched:
            raise ValueError("模型不存在")
        if not matched.enabled:
            matched.enabled = True
            self._providers[provider_id] = settings
        self._default_provider = provider_id
        self._default_model_id = model_id
        self.save()

    def clear_default_model(self):
        self._default_provider = ""
        self._default_model_id = ""
        self.save()

    def resolve_default(self) -> ResolvedModel | None:
        """解析当前默认模型；若已失效（未启用/无 Host）则返回 None。"""
        if not self._default_provider or not self._default_model_id:
            return None
        for m in self.list_selectable_models():
            if (
                m.provider_id == self._default_provider
                and m.model_id == self._default_model_id
            ):
                return m
        return None

    def default_display_label(self) -> str:
        """用于 UI 展示的默认模型文案。"""
        resolved = self.resolve_default()
        if resolved:
            return f"{resolved.provider_name} / {resolved.model_id}"
        if self._default_provider and self._default_model_id:
            name = self.get_display_name(self._default_provider)
            return f"{name} / {self._default_model_id}（暂不可用）"
        return "未设置"

    def enabled_models(self, provider_id: str) -> list[ProviderModel]:
        return [m for m in self.get_settings(provider_id).models if m.enabled and m.model_id]

    def list_selectable_models(self) -> list[ResolvedModel]:
        """仅「我的厂商」中已勾选启用的模型。"""
        result: list[ResolvedModel] = []
        for pid, name, _icon, family in self.list_my_providers():
            settings = self.get_settings(pid)
            host = (settings.api_host or "").strip()
            if not host:
                continue
            for m in settings.models:
                if not m.enabled or not m.model_id:
                    continue
                result.append(ResolvedModel(
                    provider_id=pid,
                    provider_name=name,
                    model_id=m.model_id,
                    api_host=host,
                    api_key=settings.api_key,
                    family=family or infer_family(pid, host),
                    context_window=m.context_window,
                    api_protocol=m.api_protocol,
                    temperature=m.temperature,
                    top_p=m.top_p,
                    max_tokens=m.max_tokens,
                    stream=m.stream,
                    reasoning_enabled=m.reasoning_enabled,
                    openai_reasoning_effort=m.openai_reasoning_effort,
                    deepseek_reasoning_effort=m.deepseek_reasoning_effort,
                ))
        return result

    def resolve(self, provider_id: str, model_id: str) -> ResolvedModel | None:
        if not provider_id or not model_id:
            return None
        if provider_id not in self._enabled_ids and not self.is_custom(provider_id):
            # 厂商已从「我的厂商」移除，不再用缓存密钥继续调用
            return None
        settings = self.get_settings(provider_id)
        host = (settings.api_host or "").strip()
        if not host:
            return None
        name = self.get_display_name(provider_id)
        family = self.get_family(provider_id)
        ctx = 0
        api_protocol = "chat"
        model_options = ProviderModel(model_id=model_id)
        for m in settings.models:
            if m.model_id == model_id:
                ctx = m.context_window
                api_protocol = m.api_protocol
                model_options = m
                break
        return ResolvedModel(
            provider_id=provider_id,
            provider_name=name,
            model_id=model_id,
            api_host=host,
            api_key=settings.api_key,
            family=family,
            context_window=ctx,
            api_protocol=api_protocol,
            temperature=model_options.temperature,
            top_p=model_options.top_p,
            max_tokens=model_options.max_tokens,
            stream=model_options.stream,
            reasoning_enabled=model_options.reasoning_enabled,
            openai_reasoning_effort=model_options.openai_reasoning_effort,
            deepseek_reasoning_effort=model_options.deepseek_reasoning_effort,
        )

    def has_any_model(self) -> bool:
        return bool(self.list_selectable_models())

    def first_resolved(self) -> ResolvedModel | None:
        """新建对话优先用默认模型；否则取列表第一个可用模型。"""
        default = self.resolve_default()
        if default:
            return default
        models = self.list_selectable_models()
        return models[0] if models else None

    @staticmethod
    def fetch_remote_models(endpoint: str, api_key: str) -> list[str]:
        url = endpoint.rstrip("/")
        for suffix in ["/chat/completions", "/completions"]:
            if url.endswith(suffix):
                url = url[: -len(suffix)]
                break
        url = url.rstrip("/")
        if not url.endswith("/models"):
            url += "/models"

        req = urllib.request.Request(url, method="GET")
        if api_key:
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
                mid = item.get("id", "")
                if mid:
                    models.append(mid)
        models.sort()
        return models

    @staticmethod
    def mask_key(key: str) -> str:
        if not key:
            return "（未配置）"
        if len(key) <= 8:
            return "****"
        return f"{key[:3]}****{key[-4:]}"
