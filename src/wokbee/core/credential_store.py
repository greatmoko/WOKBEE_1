"""本机凭据保险箱：密文文件 + 主密钥进 OS Keyring（Windows 为凭据管理器 / DPAPI）。"""

from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from tokbee.core.config import default_data_dir
from tokbee.core.safe_io import safe_write_text
from wokbee.core.credential_crypto import (
    CredentialVaultError,
    decode_key,
    encode_key,
    generate_key,
    open_sealed,
    seal,
)

KEYRING_SERVICE = "WokBee.Vault"
KEYRING_USER = "master-key"
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class KeyBackend(Protocol):
    def get(self) -> str | None: ...
    def set(self, encoded_key: str) -> None: ...


class KeyringBackend:
    """jaraco/keyring，Windows 上落到凭据管理器。"""

    def get(self) -> str | None:
        import keyring

        try:
            val = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        except Exception as e:  # noqa: BLE001
            raise CredentialVaultError(f"读取系统凭据失败：{e}") from e
        return val or None

    def set(self, encoded_key: str) -> None:
        import keyring

        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, encoded_key)
        except Exception as e:  # noqa: BLE001
            raise CredentialVaultError(f"写入系统凭据失败：{e}") from e


class MemoryKeyBackend:
    """测试用内存主密钥，不碰系统凭据管理器。"""

    def __init__(self, encoded_key: str | None = None):
        self._key = encoded_key

    def get(self) -> str | None:
        return self._key

    def set(self, encoded_key: str) -> None:
        self._key = encoded_key


@dataclass
class CredentialRecord:
    id: str
    alias: str
    title: str
    url: str
    username: str
    password: str
    notes: str
    updated_at: str

    def public_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "alias": self.alias,
            "title": self.title,
            "url": self.url,
            "username": self.username,
            "updated_at": self.updated_at,
        }

    def agent_list_dict(self) -> dict[str, str]:
        """给 Agent 的列表项：不含账号密码。"""
        return {
            "alias": self.alias,
            "title": self.title,
            "url": self.url,
        }

    def env_prefix(self) -> str:
        return cred_env_prefix(self.alias)

    def to_dict(self) -> dict[str, str]:
        d = self.public_dict()
        d["password"] = self.password
        d["notes"] = self.notes
        return d

    @classmethod
    def from_dict(cls, d: dict) -> CredentialRecord:
        return cls(
            id=str(d.get("id") or ""),
            alias=str(d.get("alias") or "").strip(),
            title=str(d.get("title") or "").strip(),
            url=str(d.get("url") or "").strip(),
            username=str(d.get("username") or ""),
            password=str(d.get("password") or ""),
            notes=str(d.get("notes") or ""),
            updated_at=str(d.get("updated_at") or ""),
        )


_REDACT_MIN_LEN = 4
_REDACT_PLACEHOLDER = "***凭据已隐藏***"
_redact_cache_at = 0.0
_redact_cache: tuple[str, ...] = ()


def cred_env_prefix(alias: str) -> str:
    raw = re.sub(r"[^A-Za-z0-9]+", "_", (alias or "").strip()).strip("_").upper()
    if not raw:
        raw = "ITEM"
    if raw[0].isdigit():
        raw = "C_" + raw
    return f"WOKBEE_CRED_{raw}"


def redact_text(text: str, secrets: list[str] | tuple[str, ...] | None = None) -> str:
    """把保险箱里的账号/密码从展示文本中抹掉。"""
    if not text:
        return text
    needles = list(secrets) if secrets is not None else list(cached_redact_secrets())
    needles.sort(key=len, reverse=True)
    out = text
    for s in needles:
        if s and len(s) >= _REDACT_MIN_LEN and s in out:
            out = out.replace(s, _REDACT_PLACEHOLDER)
    return out


def redact_obj(value, secrets: list[str] | tuple[str, ...] | None = None):
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {k: redact_obj(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v, secrets) for v in value]
    return value


def cached_redact_secrets() -> tuple[str, ...]:
    global _redact_cache_at, _redact_cache
    now = time.monotonic()
    if now - _redact_cache_at < 2.0:
        return _redact_cache
    try:
        _redact_cache = tuple(CredentialStore().redact_strings())
    except Exception:  # noqa: BLE001
        _redact_cache = ()
    _redact_cache_at = now
    return _redact_cache


def inject_vault_env(env: dict[str, str], store: CredentialStore | None = None) -> dict[str, str]:
    """把保险箱账号密码注入子进程环境，模型不必也不应见到明文。"""
    try:
        vault = store or CredentialStore()
        for rec in vault.list_records():
            prefix = rec.env_prefix()
            env[f"{prefix}_USERNAME"] = rec.username or ""
            env[f"{prefix}_PASSWORD"] = rec.password or ""
    except Exception:  # noqa: BLE001
        pass
    return env


def normalize_alias(raw: str) -> str:
    alias = (raw or "").strip()
    if not _ALIAS_RE.match(alias):
        raise CredentialVaultError(
            "别名须为 1–64 位字母数字，可含 . _ -，且以字母或数字开头（Agent 靠它检索）"
        )
    return alias


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_vault_path() -> Path:
    return default_data_dir() / "vault" / "credentials.enc"


class CredentialStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        keys: KeyBackend | None = None,
    ):
        self._path = Path(path) if path else default_vault_path()
        self._keys = keys or KeyringBackend()
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._path

    def list_records(self) -> list[CredentialRecord]:
        with self._lock:
            return list(self._load().values())

    def get(self, alias: str) -> CredentialRecord | None:
        alias_key = (alias or "").strip().lower()
        if not alias_key:
            return None
        with self._lock:
            for rec in self._load().values():
                if rec.alias.lower() == alias_key:
                    return rec
        return None

    def redact_strings(self) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for rec in self.list_records():
            for val in (rec.password, rec.username):
                s = (val or "").strip()
                if len(s) >= _REDACT_MIN_LEN and s not in seen:
                    seen.add(s)
                    found.append(s)
        return found

    def upsert(
        self,
        *,
        rec_id: str = "",
        alias: str,
        title: str = "",
        url: str = "",
        username: str = "",
        password: str = "",
        notes: str = "",
    ) -> CredentialRecord:
        alias = normalize_alias(alias)
        title = (title or "").strip() or alias
        rec_id = (rec_id or "").strip()
        with self._lock:
            items = self._load()
            if rec_id and rec_id in items:
                existing = items[rec_id]
            else:
                existing = None
                rec_id = rec_id or uuid.uuid4().hex
            for other in items.values():
                if other.alias.lower() == alias.lower() and other.id != rec_id:
                    raise CredentialVaultError(f"别名「{alias}」已存在")
            rec = CredentialRecord(
                id=rec_id,
                alias=alias,
                title=title,
                url=(url or "").strip(),
                username=username or "",
                password=password if password is not None else "",
                notes=notes or "",
                updated_at=_now_iso(),
            )
            if existing is not None and not password:
                rec.password = existing.password
            items[rec.id] = rec
            self._save(items)
            return rec

    def delete(self, rec_id: str) -> bool:
        rec_id = (rec_id or "").strip()
        if not rec_id:
            return False
        with self._lock:
            items = self._load()
            if rec_id not in items:
                return False
            del items[rec_id]
            self._save(items)
            return True

    def _load(self) -> dict[str, CredentialRecord]:
        if not self._path.exists():
            return {}
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as e:
            raise CredentialVaultError(f"读取保险箱失败：{e}") from e
        if not text.strip():
            return {}
        encoded = self._keys.get()
        if not encoded:
            raise CredentialVaultError(
                "找不到主密钥，保险箱无法打开。若更换过 Windows 账户，"
                "请备份后删除旧文件再重建。"
            )
        key = decode_key(encoded)
        data = open_sealed(text, key)
        raw_items = data.get("items") or []
        out: dict[str, CredentialRecord] = {}
        if not isinstance(raw_items, list):
            raise CredentialVaultError("保险箱条目格式无效")
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            rec = CredentialRecord.from_dict(item)
            if rec.id and rec.alias:
                out[rec.id] = rec
        return out

    def _save(self, items: dict[str, CredentialRecord]) -> None:
        encoded = self._keys.get()
        if encoded:
            key = decode_key(encoded)
        else:
            if self._path.exists() and self._path.stat().st_size > 0:
                raise CredentialVaultError(
                    "保险箱文件已存在但系统里没有主密钥，拒绝覆盖以免毁掉旧数据。"
                )
            key = generate_key()
            self._keys.set(encode_key(key))
        payload = {
            "items": [rec.to_dict() for rec in sorted(items.values(), key=lambda r: r.alias.lower())]
        }
        text = seal(payload, key)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        safe_write_text(self._path, text)
        try:
            self._path.chmod(0o600)
        except OSError:
            pass
        global _redact_cache_at
        _redact_cache_at = 0.0
