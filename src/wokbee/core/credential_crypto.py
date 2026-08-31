"""凭据保险箱信封加密：AES-256-GCM，主密钥由调用方保管。"""

from __future__ import annotations

import json
import os
from base64 import b64decode, b64encode
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VAULT_VERSION = 1
VAULT_ALG = "aes-256-gcm"
AAD = b"wokbee-vault-v1"
KEY_BYTES = 32
NONCE_BYTES = 12


class CredentialVaultError(Exception):
    """保险箱无法打开或写入失败。"""


def generate_key() -> bytes:
    return os.urandom(KEY_BYTES)


def encode_key(key: bytes) -> str:
    if len(key) != KEY_BYTES:
        raise CredentialVaultError("主密钥长度无效")
    return b64encode(key).decode("ascii")


def decode_key(text: str) -> bytes:
    try:
        key = b64decode(text.encode("ascii"), validate=True)
    except (ValueError, TypeError) as e:
        raise CredentialVaultError("主密钥格式无效") from e
    if len(key) != KEY_BYTES:
        raise CredentialVaultError("主密钥长度无效")
    return key


def seal(payload: dict[str, Any], key: bytes) -> str:
    """把明文 JSON 封成可落盘的信封（本身是 JSON 文本）。"""
    if len(key) != KEY_BYTES:
        raise CredentialVaultError("主密钥长度无效")
    nonce = os.urandom(NONCE_BYTES)
    aes = AESGCM(key)
    plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = aes.encrypt(nonce, plain, AAD)
    blob = {
        "v": VAULT_VERSION,
        "alg": VAULT_ALG,
        "nonce": b64encode(nonce).decode("ascii"),
        "ct": b64encode(ct).decode("ascii"),
    }
    return json.dumps(blob, indent=2, ensure_ascii=False) + "\n"


def open_sealed(text: str, key: bytes) -> dict[str, Any]:
    """解开信封。密文损坏或密钥不对时抛 CredentialVaultError。"""
    if len(key) != KEY_BYTES:
        raise CredentialVaultError("主密钥长度无效")
    try:
        blob = json.loads(text)
    except json.JSONDecodeError as e:
        raise CredentialVaultError("保险箱文件已损坏（不是合法 JSON）") from e
    if not isinstance(blob, dict):
        raise CredentialVaultError("保险箱文件格式无效")
    if int(blob.get("v") or 0) != VAULT_VERSION:
        raise CredentialVaultError("不支持的保险箱版本")
    if str(blob.get("alg") or "") != VAULT_ALG:
        raise CredentialVaultError("不支持的加密算法")
    try:
        nonce = b64decode(str(blob.get("nonce") or ""), validate=True)
        ct = b64decode(str(blob.get("ct") or ""), validate=True)
    except (ValueError, TypeError) as e:
        raise CredentialVaultError("保险箱密文格式无效") from e
    if len(nonce) != NONCE_BYTES or not ct:
        raise CredentialVaultError("保险箱密文格式无效")
    aes = AESGCM(key)
    try:
        plain = aes.decrypt(nonce, ct, AAD)
    except InvalidTag as e:
        raise CredentialVaultError("无法解密：主密钥不匹配或文件被篡改") from e
    try:
        data = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise CredentialVaultError("保险箱明文已损坏") from e
    if not isinstance(data, dict):
        raise CredentialVaultError("保险箱明文格式无效")
    return data
