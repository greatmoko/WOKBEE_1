"""凭据保险箱冒烟：信封加密、CRUD、别名唯一、list 不泄密。

运行：
    PYTHONPATH=src venv/Scripts/python.exe scripts/smoke_credential_vault.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from wokbee.core.credential_crypto import (  # noqa: E402
    CredentialVaultError,
    generate_key,
    open_sealed,
    seal,
)
from wokbee.core.credential_store import (  # noqa: E402
    CredentialStore,
    MemoryKeyBackend,
)
from wokbee.core.timeline_format import format_tool_callback_for_timeline  # noqa: E402
from wokbee.engine.approval_policy import HIGH_RISK_TOOLS, build_interrupt_on  # noqa: E402
from wokbee.core.models import ApprovalFlags  # noqa: E402
from wokbee.engine.credential_tools import build_credential_tools  # noqa: E402
from wokbee.engine.tool_truncate import truncate_tool_result  # noqa: E402

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        _failures.append(msg)
        print(f"  FAIL {msg}")
    else:
        print(f"  OK   {msg}")


def test_seal_roundtrip() -> None:
    key = generate_key()
    blob = seal({"hello": "世界"}, key)
    data = open_sealed(blob, key)
    check(data.get("hello") == "世界", "加密往返保留明文")


def test_tamper_fails() -> None:
    key = generate_key()
    blob = seal({"hello": "x"}, key)
    bad = blob.replace("aes-256-gcm", "aes-256-gcm")  # no-op
    # Flip one character inside ct
    parsed = json.loads(blob)
    ct = parsed["ct"]
    parsed["ct"] = ("A" if ct[0] != "A" else "B") + ct[1:]
    tampered = json.dumps(parsed)
    raised = False
    try:
        open_sealed(tampered, key)
    except CredentialVaultError:
        raised = True
    check(raised, "篡改密文无法解密")
    check(bad.startswith("{"), "信封是 JSON 文本")


def test_wrong_key_fails() -> None:
    blob = seal({"a": 1}, generate_key())
    raised = False
    try:
        open_sealed(blob, generate_key())
    except CredentialVaultError:
        raised = True
    check(raised, "错误主密钥无法解密")


def test_store_crud_and_alias() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "credentials.enc"
        keys = MemoryKeyBackend()
        store = CredentialStore(path, keys=keys)
        rec = store.upsert(
            alias="gitlab",
            title="GitLab",
            url="https://gitlab.example",
            username="alice",
            password="s3cret",
            notes="n",
        )
        check(path.exists(), "写入密文文件")
        raw = path.read_text(encoding="utf-8")
        check("s3cret" not in raw, "磁盘密文不含明文密码")
        check("alice" not in raw, "磁盘密文不含用户名明文")

        listed = store.list_records()
        check(len(listed) == 1, "list 一条")
        check("password" not in listed[0].public_dict(), "public_dict 无 password")
        check(listed[0].password == "s3cret", "内存记录仍有密码")

        dup = False
        try:
            store.upsert(alias="GitLab", title="other", username="b", password="x")
        except CredentialVaultError:
            dup = True
        check(dup, "别名大小写不敏感唯一")

        got = store.get("GITLAB")
        check(got is not None and got.username == "alice", "按别名取值忽略大小写")

        store.upsert(
            rec_id=rec.id,
            alias="gitlab",
            title="GitLab",
            username="alice",
            password="",
            notes="n2",
        )
        again = store.get("gitlab")
        check(again is not None and again.password == "s3cret", "编辑留空密码则保留原值")
        check(again is not None and again.notes == "n2", "备注可更新")

        check(store.delete(rec.id), "删除成功")
        check(store.list_records() == [], "删除后为空")


def test_redact_and_inject() -> None:
    from wokbee.core.credential_store import inject_vault_env, redact_text

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "credentials.enc"
        store = CredentialStore(path, keys=MemoryKeyBackend())
        store.upsert(
            alias="Google",
            title="Gmail",
            username="mm930719@gmail.com",
            password="Moko.7720237",
        )
        secrets = store.redact_strings()
        text = redact_text(
            "账号 mm930719@gmail.com 密码 Moko.7720237",
            secrets,
        )
        check("Moko.7720237" not in text, "脱敏去掉密码")
        check("mm930719@gmail.com" not in text, "脱敏去掉账号")
        env: dict[str, str] = {}
        inject_vault_env(env, store)
        check(env.get("WOKBEE_CRED_GOOGLE_PASSWORD") == "Moko.7720237", "注入密码环境变量")
        check(env.get("WOKBEE_CRED_GOOGLE_USERNAME") == "mm930719@gmail.com", "注入账号环境变量")


def test_tools_do_not_leak_on_list() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "credentials.enc"
        store = CredentialStore(path, keys=MemoryKeyBackend())
        store.upsert(alias="oa", title="OA", username="bob", password="hunter2")
        tools = {t.name: t for t in build_credential_tools(store)}
        listed = tools["list_credentials"].invoke({})
        check("hunter2" not in listed, "list_credentials 不含密码")
        check("bob" not in listed, "list_credentials 不含用户名")
        check("oa" in listed, "list_credentials 含别名")
        got = tools["get_credential"].invoke({"alias": "oa"})
        check("hunter2" not in got, "get_credential 不返回密码明文")
        check("bob" not in got, "get_credential 不返回用户名明文")
        check("WOKBEE_CRED_OA_PASSWORD" in got, "get_credential 返回环境变量名")


def test_timeline_and_dump_redact() -> None:
    body = json.dumps({"password": "hunter2"})
    text = format_tool_callback_for_timeline("get_credential", body)
    check("hunter2" not in text, "时间线回调脱敏")
    check("已隐藏" in text, "时间线提示已隐藏")
    with tempfile.TemporaryDirectory() as td:
        dump = Path(td)
        out = truncate_tool_result(
            "x" * 20_000 + "hunter2",
            dump_dir=dump,
            tool_name="get_credential",
        )
        check(not list(dump.iterdir()), "get_credential 不落盘 dump")
        check("hunter2" not in out, "超长 get_credential 截断后不含密码尾部")


def test_high_risk_interrupt() -> None:
    flags = ApprovalFlags()
    interrupt = build_interrupt_on(flags)
    check(interrupt.get("get_credential") is True, "默认对 get_credential 审批")
    check("get_credential" in HIGH_RISK_TOOLS, "列入高危工具")
    flags.skip_high_risk = True
    interrupt2 = build_interrupt_on(flags)
    check("get_credential" not in interrupt2, "高危免审时不打断 get_credential")


def main() -> int:
    print("credential vault")
    test_seal_roundtrip()
    test_tamper_fails()
    test_wrong_key_fails()
    test_store_crud_and_alias()
    test_redact_and_inject()
    test_tools_do_not_leak_on_list()
    test_timeline_and_dump_redact()
    test_high_risk_interrupt()
    if _failures:
        print(f"\n{len(_failures)} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
