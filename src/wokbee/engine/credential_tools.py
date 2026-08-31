"""Agent 凭据工具：不把账号密码交给模型；execute 环境会注入对应变量。"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool

from wokbee.core.credential_store import CredentialStore, CredentialVaultError

LIST_CREDENTIALS_DESCRIPTION = """列出本机凭据库条目（不含账号、密码）。

返回 alias、title、url。登录前对需要的那一条调用 get_credential。
不要猜测或要求用户在对话里再发一遍密码。"""

GET_CREDENTIAL_DESCRIPTION = """按别名取出一条登录凭据的**环境变量名**（不含账号、密码明文）。高危操作，默认需用户审批。

execute / 脚本运行时，WokBee 已把对应账号密码注入进程环境。
PowerShell 示例：$env:WOKBEE_CRED_<ALIAS>_USERNAME 与 $env:WOKBEE_CRED_<ALIAS>_PASSWORD
（ALIAS 中的非字母数字会变成下划线并大写，如 Google → WOKBEE_CRED_GOOGLE_PASSWORD）。

严禁：在回复、execute 命令字符串、文件、经验里写出真实账号或密码。只引用环境变量名。
只取当前任务需要的那一条。"""


def build_credential_tools(store: CredentialStore | None = None) -> list[Any]:
    vault = store or CredentialStore()

    @tool(description=LIST_CREDENTIALS_DESCRIPTION)
    def list_credentials() -> str:
        try:
            rows = [r.agent_list_dict() for r in vault.list_records()]
        except CredentialVaultError as e:
            return f"错误：{e}"
        if not rows:
            return "凭据库为空。请用户在「AIConfig → 凭据库」录入账号。"
        return json.dumps(rows, ensure_ascii=False, indent=2)

    @tool(description=GET_CREDENTIAL_DESCRIPTION)
    def get_credential(alias: str) -> str:
        name = (alias or "").strip()
        if not name:
            return "错误：alias 不能为空"
        try:
            rec = vault.get(name)
        except CredentialVaultError as e:
            return f"错误：{e}"
        if rec is None:
            return f"错误：找不到别名「{name}」。请先 list_credentials。"
        prefix = rec.env_prefix()
        return json.dumps(
            {
                "alias": rec.alias,
                "title": rec.title,
                "url": rec.url,
                "username_env": f"{prefix}_USERNAME",
                "password_env": f"{prefix}_PASSWORD",
                "usage": (
                    "明文不会返回。execute 时环境已注入上述变量。"
                    "PowerShell 用 $env:变量名；Python 用 os.environ[变量名]。"
                    "禁止把账号密码写进回复或命令。"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )

    return [list_credentials, get_credential]


CREDENTIAL_TOOL_NAMES = ("list_credentials", "get_credential")
