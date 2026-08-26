"""企业微信群机器人 Webhook 推送。"""

from __future__ import annotations

import httpx

# 每个机器人限 20 条/分钟，超出返回 45009
_RATE_LIMIT_CODE = 45009


def push_wecom(
    webhook_url: str,
    message: str,
    *,
    msgtype: str = "text",
    mention: str = "",
    timeout: int = 10,
) -> tuple[bool, str]:
    """向企业微信群机器人 webhook 推送一条消息。

    msgtype: text | markdown
    mention: text 类型时，可按账号 @（手机号/账号），或 "@all" 全体；
             markdown 类型时追加为正文段落。
    返回 (ok, message)。
    """
    url = (webhook_url or "").strip()
    content = (message or "").strip()
    if not url:
        return False, "未配置企业微信 Webhook 地址"
    if not content:
        return False, "消息内容为空"

    mt = (msgtype or "text").strip().lower()
    if mt not in ("text", "markdown"):
        return False, f"不支持的消息类型：{mt}"

    if mt == "text":
        text_payload: dict = {"content": content}
        mention = (mention or "").strip()
        if mention == "@all":
            text_payload["mentioned_mobile_list"] = ["@all"]
        elif mention:
            text_payload["mentioned_list"] = [mention]
        body = {"msgtype": "text", "text": text_payload}
    else:
        md_content = content
        mention = (mention or "").strip()
        if mention and mention != "@all":
            md_content += f"\n@{mention}"
        body = {"msgtype": "markdown", "markdown": {"content": md_content}}

    try:
        resp = httpx.post(url, json=body, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return False, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
    except httpx.HTTPError as e:
        return False, f"请求失败：{e}"

    try:
        data = resp.json()
    except ValueError:
        return False, f"返回非 JSON：{resp.text[:200]}"

    errcode = data.get("errcode")
    if errcode == 0:
        return True, data.get("errmsg") or "ok"
    if errcode == _RATE_LIMIT_CODE:
        return False, "触发频率限制（机器人限 20 条/分钟）"
    return False, f"错误 {errcode}: {data.get('errmsg')}"[:300]
