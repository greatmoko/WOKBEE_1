"""DeepSeek 服务端搜索 → 包成一个普通函数工具给 Agent 用。

主模型可以是本地大模型 / DeepSeek / 任何 OpenAI 兼容厂商——搜索动作由本工具
自己发 HTTP 到 DeepSeek 的 Anthropic 兼容 Messages 接口完成，与主模型调用链解耦，
所以「平时用本地大模型」的 Agent 也能调用它。

原理（对齐 deepseek-ai/deepseek-harness 的 web-search-deepseek provider）：
- 端点：https://api.deepseek.com/anthropic/v1/messages（仅 Anthropic 兼容接口支持，
  OpenAI chat completions 接口没有服务端搜索）。
- 请求：tools:[{"type":"web_search_20250305","name":"web_search","max_uses":5}]
- 响应：解析 web_search_tool_result 块 → title / url / snippet / page_age，
  并用 text 块的 citations 补一份「引用原文」摘录。
- 搜索在服务端完成，结果以 token 计费（非本地检索倒排），但换来多轮检索与引用质量。

注意：这里是「搜索工具」，不是把 Agent 整体切成 Anthropic 接口。关键在解耦。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("wokbee")

# 官方 Anthropic 兼容 Messages 端点（服务端搜索只在这里，不在 /v1）
DEEPSEEK_ANTHROPIC_BASE = "https://api.deepseek.com/anthropic/v1"
# 搜索请求本身用的模型（服务端检索模型，与主 Agent 模型无关）
SEARCH_MODEL = "deepseek-v4-flash"
# 单次请求最多触发几次搜索（max_uses）
SEARCH_MAX_USES = 5
MAX_TOKENS = 4096
TIMEOUT_SECONDS = 60.0
# 返回结果上限：条数与摘要长度（服务端结果可能很大）
MAX_RESULTS = 8
SNIPPET_MAX_CHARS = 400
# 单个结果去重时的键：url 更稳定；无 url 时退化为 title
DEEPSEEK_PROVIDER_ID = "deepseek"


def build_deepseek_search_tool(provider_store: Any):
    """构造一个 langchain `@tool`：deepseek_web_search(query)。

    每次调用懒读取 provider_store 中 DeepSeek 的 API Key，Key 变更即时生效。
    provider_store.get_settings("deepseek").api_key 为空时返回明确错误，不会崩。
    """
    from langchain_core.tools import tool

    @tool
    def deepseek_web_search(query: str, max_uses: int = SEARCH_MAX_USES) -> str:
        """用 DeepSeek 官方服务端搜索联网：返回 DeepSeek 生成的结果摘要 + 来源标题/链接。

        需要最新或外部资料（天气、新闻、论文、官方文档等）且更看重检索质量时优先用这个；
        普通快速联网可用 web_search（本地 DuckDuckGo）。
        正文内容在服务端加密（encrypted_content）不可读，只有标题/链接；需正文请再 http_get 对应 URL。
        需在应用「厂商设置」里配置官方 DeepSeek 的 API Key（服务端搜索仅官方 /anthropic 端点支持）。
        """
        q = (query or "").strip()
        if not q:
            return "错误：query 不能为空"
        max_uses = max(1, min(int(max_uses or SEARCH_MAX_USES), 10))

        api_key = _resolve_api_key(provider_store)
        if not api_key:
            return (
                "无法使用 DeepSeek 服务端搜索：未配置官方 DeepSeek 的 API Key。"
                "请在「厂商设置」里为 DeepSeek 填写 Key（或改用本地 web_search 工具）。"
            )

        try:
            body = {
                "model": SEARCH_MODEL,
                "max_tokens": MAX_TOKENS,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"Perform a web search for the query: {q}"}
                        ],
                    }
                ],
                "tools": [
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": max_uses,
                    }
                ],
            }
            with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
                resp = client.post(
                    f"{DEEPSEEK_ANTHROPIC_BASE}/messages",
                    headers={
                        "content-type": "application/json",
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                    },
                    json=body,
                )
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as e:
            detail = _safe_detail(e.response)
            return f"[deepseek_web_search] 搜索失败（HTTP {e.response.status_code}）：{detail}"
        except httpx.HTTPError as e:
            return f"[deepseek_web_search] 网络请求失败：{e}"
        except Exception as e:  # noqa: BLE001 —— 工具应自吞异常，让 Agent 能继续
            logger.exception("deepseek_web_search 处理异常")
            return f"[deepseek_web_search] 异常：{e}"

        return _format_results(payload, q)

    return deepseek_web_search


def _resolve_api_key(provider_store: Any) -> str:
    """取官方 DeepSeek 的 API Key（懒读取，每次调用实时拿）。"""
    if provider_store is None:
        return ""
    try:
        settings = provider_store.get_settings(DEEPSEEK_PROVIDER_ID)
        return (getattr(settings, "api_key", "") or "").strip()
    except Exception:
        logger.exception("读取 DeepSeek 厂商配置失败")
        return ""


def _safe_detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") or data
        if isinstance(err, dict):
            return str(err.get("message") or err)
        return str(err)[:300]
    except Exception:
        return (resp.text or "")[:300]


def _format_results(payload: dict, query: str) -> str:
    """解析 Anthropic 兼容响应 → 可读文本。

    真实结构（实测）：
    - web_search_tool_result.content[] 中 type=="web_search_result" 的项含
      title / url / page_age（正文为 encrypted_content，不可读）。
    - 最后的 text 块是 DeepSeek 服务端生成的「结果摘要」，与 sources 一起返回。
    """
    content = payload.get("content") or []
    if not isinstance(content, list):
        return f"[deepseek_web_search] 查询「{query}」未返回结果（内容格式异常）"

    items: dict[str, dict] = {}
    answers: list[str] = []
    fallback_citations: dict[str, list[str]] = {}

    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "web_search_tool_result":
            for r in block.get("content") or []:
                if not isinstance(r, dict) or (r.get("type") or "") != "web_search_result":
                    continue
                url = str(r.get("url") or "").strip()
                title = str(r.get("title") or "").strip()
                if not (url or title):
                    continue
                if url and url in items:
                    continue
                items[url or title] = {
                    "url": url,
                    "title": title,
                    "published": str(r.get("page_age") or "").strip(),
                }
        elif btype == "text":
            text = str(block.get("text") or "").strip()
            if len(text) > 40:  # 过滤「I'll search...」这类占位
                answers.append(text)
            for cite in _iter_citations(block):
                u = str(cite.get("url") or "").strip()
                c = str(cite.get("cited_text") or "").strip()
                if u and c:
                    fallback_citations.setdefault(u, []).append(c)

    if not items and not answers:
        return (
            f"[deepseek_web_search] 查询「{query}」未触发服务端搜索，未拿到结果，"
            "可换一个更明确的 query。"
        )

    blocks: list[str] = []
    if answers:
        best = max(answers, key=len)
        if len(best) > 1500:
            best = best[:1500] + "…"
        blocks.append("【DeepSeek 生成的结果摘要】\n" + best)
    if items:
        blocks.append(f"【来源】共 {len(items)} 条（显示前 {min(len(items), MAX_RESULTS)} 条）：")
        for i, key in enumerate(list(items)[:MAX_RESULTS], 1):
            it = items[key]
            lines = [f"{i}. {it['title'] or '(无标题)'}"]
            if it["url"]:
                lines.append(f"   URL: {it['url']}")
            if it["published"]:
                lines.append(f"   时间: {it['published']}")
            cited = fallback_citations.get(it["url"], [])
            if cited:
                c = cited[0]
                c = c if len(c) <= SNIPPET_MAX_CHARS else c[:SNIPPET_MAX_CHARS] + "…"
                lines.append(f"   摘要: {c}")
            blocks.append("\n".join(lines))

    return f"DeepSeek 服务端搜索「{query}」：\n\n" + "\n\n".join(blocks)


def _iter_citations(block: dict) -> list[dict]:
    citations = block.get("citations")
    if isinstance(citations, list):
        return [c for c in citations if isinstance(c, dict)]
    return []
