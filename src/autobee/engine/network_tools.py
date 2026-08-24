"""联网工具：供 AutoBee Agent 访问真实网络。"""

from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import quote_plus

import httpx
from langchain_core.tools import tool


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _decode_http_bytes(raw: bytes, content_type: str = "") -> str:
    """按声明编码优先，失败再试 utf-8/gbk，避免中文页乱码。"""
    if not raw:
        return ""
    declared = ""
    m = re.search(r"charset\s*=\s*['\"]?([\w\-]+)", content_type or "", re.I)
    if m:
        declared = m.group(1).strip().lower().replace("gb2312", "gbk")
    if not declared:
        m2 = re.search(br"charset\s*=\s*['\"]?([\w\-]+)", raw[:4096], re.I)
        if m2:
            declared = m2.group(1).decode("ascii", "ignore").lower().replace("gb2312", "gbk")

    def _try(enc: str) -> str | None:
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            return None

    # 声明编码能严格解出则直接用（避免 utf-8 被 gbk「误读」抢分）
    if declared:
        hit = _try(declared)
        if hit is not None and hit.count("\ufffd") == 0:
            return hit

    for enc in ("utf-8", "utf-8-sig", "gbk", "cp936", "big5"):
        if enc == declared:
            continue
        hit = _try(enc)
        if hit is not None:
            return hit
    return raw.decode("utf-8", errors="replace")


def _resp_text(resp: httpx.Response) -> str:
    return _decode_http_bytes(resp.content or b"", resp.headers.get("content-type") or "")


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """在互联网上搜索信息，返回标题、链接与摘要。

    用于查天气、新闻、资料、官方文档等需要最新或外部信息的场景。
    """
    q = (query or "").strip()
    if not q:
        return "错误：query 不能为空"
    max_results = max(1, min(int(max_results or 5), 10))

    # DuckDuckGo HTML（无需 API Key）
    url = f"https://html.duckduckgo.com/html/?q={quote_plus(q)}"
    try:
        with httpx.Client(
            timeout=30.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            html = _resp_text(resp)
    except Exception as e:
        return f"搜索失败：{e}"

    # 解析结果块
    results: list[str] = []
    # 常见结构：result__a + result__snippet
    blocks = re.findall(
        r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
        r'.*?(?:class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|td|div))?',
        html,
    )
    if not blocks:
        # 兜底：任意带 uddg 的链接
        links = re.findall(r'uddg=([^&"]+)', html)
        from urllib.parse import unquote

        for link in links[:max_results]:
            results.append(f"- {unquote(link)}")
    else:
        for href, title, snippet in blocks[:max_results]:
            title_t = _strip_html(title)[:120]
            snip_t = _strip_html(snippet or "")[:240]
            # DuckDuckGo 可能包一层 redirect
            if "uddg=" in href:
                m = re.search(r"uddg=([^&]+)", href)
                if m:
                    from urllib.parse import unquote

                    href = unquote(m.group(1))
            results.append(f"- {title_t}\n  URL: {href}\n  {snip_t}".rstrip())

    if not results:
        # Instant Answer API 兜底
        try:
            api = f"https://api.duckduckgo.com/?q={quote_plus(q)}&format=json&no_html=1&skip_disambig=1"
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                data = client.get(api).json()
            abstract = (data.get("AbstractText") or "").strip()
            abs_url = (data.get("AbstractURL") or "").strip()
            related = data.get("RelatedTopics") or []
            lines = []
            if abstract:
                lines.append(f"摘要：{abstract}")
                if abs_url:
                    lines.append(f"来源：{abs_url}")
            for item in related[:max_results]:
                if isinstance(item, dict) and item.get("Text"):
                    lines.append(f"- {item.get('Text')} ({item.get('FirstURL', '')})")
                elif isinstance(item, dict) and item.get("Topics"):
                    for sub in item["Topics"][:3]:
                        if isinstance(sub, dict) and sub.get("Text"):
                            lines.append(f"- {sub.get('Text')} ({sub.get('FirstURL', '')})")
            if lines:
                return "\n".join(lines)
        except Exception as e:
            return f"未解析到搜索结果，且 Instant Answer 失败：{e}"
        return "未找到搜索结果，请换关键词或改用 http_get 访问具体网址。"

    return f"搜索「{q}」结果：\n" + "\n".join(results)


@tool
def http_get(url: str, max_chars: int = 12000) -> str:
    """用 GET 请求访问任意公网 URL，返回响应正文（自动去掉部分 HTML 标签）。

    适用于天气 API、新闻页、官方公告、JSON 接口等。需要完整网络权限的场景请优先用此工具或 web_search。
    """
    u = (url or "").strip()
    if not u:
        return "错误：url 不能为空"
    if not u.startswith(("http://", "https://")):
        return "错误：url 必须以 http:// 或 https:// 开头"
    max_chars = max(500, min(int(max_chars or 12000), 50000))
    try:
        with httpx.Client(
            timeout=45.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; AutoBee/0.1; +local-work-assistant)"
                ),
                "Accept": "text/html,application/json,text/plain,*/*",
            },
        ) as client:
            resp = client.get(u)
            ctype = (resp.headers.get("content-type") or "").lower()
            text = _resp_text(resp)
            if "application/json" in ctype:
                try:
                    text = json.dumps(resp.json(), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            elif "html" in ctype:
                text = _strip_html(text)
            truncated = len(text) > max_chars
            body = text[:max_chars]
            header = f"HTTP {resp.status_code} | {ctype or 'unknown'} | len={len(text)}"
            if truncated:
                header += f" | truncated_to={max_chars}"
            return f"{header}\nURL: {str(resp.url)}\n\n{body}"
    except Exception as e:
        return f"请求失败：{e}"


@tool
def http_request(
    url: str,
    method: str = "GET",
    headers_json: str = "",
    body: str = "",
    max_chars: int = 12000,
) -> str:
    """发送自定义 HTTP 请求（GET/POST/PUT/DELETE 等），可带 JSON headers 与 body。

    headers_json 示例：'{"Authorization":"Bearer xxx","Content-Type":"application/json"}'
    """
    u = (url or "").strip()
    if not u.startswith(("http://", "https://")):
        return "错误：url 必须以 http:// 或 https:// 开头"
    method = (method or "GET").upper().strip()
    max_chars = max(500, min(int(max_chars or 12000), 50000))
    headers: dict[str, str] = {
        "User-Agent": "Mozilla/5.0 (compatible; AutoBee/0.1; +local-work-assistant)",
    }
    if headers_json.strip():
        try:
            extra = json.loads(headers_json)
            if isinstance(extra, dict):
                headers.update({str(k): str(v) for k, v in extra.items()})
        except json.JSONDecodeError as e:
            return f"headers_json 不是合法 JSON：{e}"
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.request(method, u, headers=headers, content=body.encode("utf-8") if body else None)
            ctype = (resp.headers.get("content-type") or "").lower()
            text = _resp_text(resp)
            if "json" in ctype:
                try:
                    text = json.dumps(resp.json(), ensure_ascii=False, indent=2)
                except Exception:
                    pass
            elif "html" in ctype:
                text = _strip_html(text)
            body_out = text[:max_chars]
            return (
                f"HTTP {resp.status_code} {method} | {ctype or 'unknown'}\n"
                f"URL: {str(resp.url)}\n\n{body_out}"
            )
    except Exception as e:
        return f"请求失败：{e}"


NETWORK_TOOLS = [web_search, http_get, http_request]
NETWORK_TOOL_NAMES = tuple(t.name for t in NETWORK_TOOLS)
