"""OpenAI 兼容 API 客户端 — 封装 chat/completions 调用（支持流式）。"""

import json
import logging
import threading
import time
import urllib.request
import urllib.error
from collections.abc import Callable, Generator
from dataclasses import dataclass

from wokbee.core.errors import AIError

logger = logging.getLogger("wokbee")


@dataclass
class ChatResponse:
    content: str
    reasoning_content: str = ""
    model: str = ""
    usage: dict | None = None
    tool_calls: list[dict] | None = None


@dataclass
class StreamChunk:
    """流式响应的单个增量片段。"""
    content_delta: str = ""
    reasoning_delta: str = ""
    is_finished: bool = False
    raw_delta: dict | None = None


class AIClient:
    """调用 OpenAI 兼容的 /chat/completions 接口。"""

    def __init__(self, endpoint: str, api_key: str, model: str, *,
                 retry_interval: int = 0, call_delay: float = 0,
                 disable_thinking: bool = False,
                 reasoning_effort: str = ""):
        self._url = self._build_chat_url(endpoint)
        self._api_key = api_key
        self._model = model
        self._retry_interval = max(0, int(retry_interval))
        self._call_delay = max(0.0, float(call_delay))
        self._disable_thinking = disable_thinking
        self._reasoning_effort = reasoning_effort  # "", "low", "high", "max"
        self.on_retry_log: Callable[[str], None] | None = None
        self.cancel_check: Callable[[], bool] | None = None

    @staticmethod
    def _build_chat_url(endpoint: str) -> str:
        url = endpoint.rstrip("/")
        for suffix in ["/chat/completions", "/completions"]:
            if url.endswith(suffix):
                return url if suffix == "/chat/completions" else url.replace("/completions", "/chat/completions")
        if url.endswith("/models"):
            url = url[: -len("/models")]
        url = url.rstrip("/")
        return url + "/chat/completions"

    def _make_request(self, body: dict) -> urllib.request.Request:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return urllib.request.Request(
            self._url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Connection": "close",
            },
        )

    @staticmethod
    def _handle_http_error(e: urllib.error.HTTPError):
        error_body = e.read().decode("utf-8", errors="replace")
        try:
            error_data = json.loads(error_body)
            msg = error_data.get("error", {}).get("message", error_body[:300])
        except (json.JSONDecodeError, AttributeError):
            msg = error_body[:300]
        raise AIError(f"API 错误 (HTTP {e.code}): {msg}") from e

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """判断异常是否值得重试（瞬态网络错误）。"""
        if isinstance(exc, ConnectionResetError):
            return True
        if isinstance(exc, urllib.error.URLError):
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError)):
                return True
        if isinstance(exc, (TimeoutError, ConnectionAbortedError)):
            return True
        if isinstance(exc, OSError) and getattr(exc, "winerror", 0) in (10054, 10053):
            return True
        return False

    def _cancellable_sleep(self, seconds: float) -> bool:
        """分段 sleep，每 0.5 秒检查一次 cancel_check。返回 True 表示被取消。"""
        elapsed = 0.0
        step = 0.5
        while elapsed < seconds:
            if self.cancel_check and self.cancel_check():
                return True
            chunk = min(step, seconds - elapsed)
            time.sleep(chunk)
            elapsed += chunk
        return bool(self.cancel_check and self.cancel_check())

    def _emit_retry(self, wait: int, count: int):
        msg = f"触发限速 (HTTP 429)，{wait}秒后重试（第{count}次）…"
        logger.warning(msg)
        if self.on_retry_log:
            try:
                self.on_retry_log(msg)
            except Exception:
                pass

    def _apply_thinking_params(self, body: dict):
        """根据配置向请求体注入思考模式参数。

        DeepSeek 格式: thinking.type + reasoning_effort
        本地模型格式: chat_template_kwargs.enable_thinking
        """
        if self._reasoning_effort in ("low", "high", "max"):
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = self._reasoning_effort
        elif self._disable_thinking:
            body["thinking"] = {"type": "disabled"}
            body["chat_template_kwargs"] = {"enable_thinking": False}

    def _apply_call_delay(self):
        """在发送请求前主动等待，避免 API 频率限制。"""
        if self._call_delay > 0:
            if self._cancellable_sleep(self._call_delay):
                raise AIError("用户取消")

    def _do_single_request(self, body: dict, timeout: int = 180) -> dict:
        """执行一次 HTTP 请求，返回解析后的 JSON。"""
        req = self._make_request(body)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _request_with_cancel(self, body: dict, timeout: int = 180) -> dict:
        """在后台线程中发请求，主线程可通过 cancel_check 取消等待。"""
        result_holder: list = []
        error_holder: list = []

        def _worker():
            try:
                result_holder.append(self._do_single_request(body, timeout))
            except Exception as exc:
                error_holder.append(exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

        while t.is_alive():
            if self.cancel_check and self.cancel_check():
                raise AIError("用户取消")
            t.join(timeout=0.5)

        if error_holder:
            raise error_holder[0]
        if result_holder:
            return result_holder[0]
        raise AIError("请求未返回结果")

    def _request_with_retry(self, body: dict, timeout: int = 180, max_retries: int = 3) -> dict:
        """发送请求并在瞬态网络错误或 429 限速时自动重试。"""
        self._apply_call_delay()
        last_exc: Exception | None = None
        attempt = 0
        rate_limit_count = 0
        while attempt < max_retries:
            try:
                return self._request_with_cancel(body, timeout)
            except urllib.error.HTTPError as e:
                if e.code == 429 and self._retry_interval > 0 and rate_limit_count < 100:
                    try:
                        e.read()
                    except Exception:
                        pass
                    rate_limit_count += 1
                    self._emit_retry(self._retry_interval, rate_limit_count)
                    if self._cancellable_sleep(self._retry_interval):
                        raise AIError("用户取消") from e
                    continue
                self._handle_http_error(e)
            except RuntimeError:
                raise
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1 and self._is_retryable(e):
                    wait = 2 ** attempt
                    logger.warning("请求失败 (第%d次), %ds后重试: %s", attempt + 1, wait, e)
                    time.sleep(wait)
                    attempt += 1
                    continue
                if isinstance(e, urllib.error.URLError):
                    raise AIError(f"网络连接失败: {e.reason}") from e
                if isinstance(e, TimeoutError):
                    raise AIError("请求超时，请检查网络连接或 API 地址") from None
                raise AIError(f"请求异常: {e}") from e
        raise AIError(f"网络连接失败 (重试{max_retries}次后仍失败): {last_exc}") from last_exc

    def chat(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        top_p: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = "auto",
        timeout: int = 180,
        frequency_penalty: float = 0.0,
        presence_penalty: float = 0.0,
        top_k: int = 0,
        stop: list[str] | None = None,
    ) -> ChatResponse:
        """同步非流式调用，返回完整 ChatResponse。"""
        body: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if frequency_penalty != 0.0:
            body["frequency_penalty"] = frequency_penalty
        if presence_penalty != 0.0:
            body["presence_penalty"] = presence_penalty
        if top_k > 0:
            body["top_k"] = top_k
        if stop:
            body["stop"] = stop
        if tools is not None:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        self._apply_thinking_params(body)

        msg_chars = sum(len(str(m.get("content", ""))) for m in messages)
        logger.debug("chat() 开始: %d条消息, 约%d字符", len(messages), msg_chars)
        t0 = time.monotonic()
        data = self._request_with_retry(body, timeout=timeout)
        elapsed = time.monotonic() - t0
        logger.debug("chat() 完成: 耗时 %.1fs", elapsed)

        try:
            choice = data["choices"][0]
            message = choice["message"]
            raw_content = message.get("content")
            if raw_content is None:
                content = ""
            elif isinstance(raw_content, str):
                content = raw_content
            else:
                content = str(raw_content)
        except (KeyError, IndexError, TypeError) as e:
            raise AIError(f"API 返回格式异常: {data}") from e

        reasoning = message.get("reasoning_content") or ""
        tool_calls = message.get("tool_calls")
        if tool_calls is not None and not isinstance(tool_calls, list):
            tool_calls = None

        return ChatResponse(
            content=content,
            reasoning_content=reasoning,
            model=data.get("model", self._model),
            usage=data.get("usage"),
            tool_calls=tool_calls,
        )

    def chat_stream(
        self,
        messages: list[dict],
        *,
        temperature: float = 0.7,
        top_p: float = 1.0,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = "auto",
        frequency_penalty: float = 0.0,
        presence_penalty: float = 0.0,
        top_k: int = 0,
        stop: list[str] | None = None,
    ) -> Generator[StreamChunk, None, None]:
        """流式调用 chat/completions，逐块 yield StreamChunk。"""
        body: dict = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if frequency_penalty != 0.0:
            body["frequency_penalty"] = frequency_penalty
        if presence_penalty != 0.0:
            body["presence_penalty"] = presence_penalty
        if top_k > 0:
            body["top_k"] = top_k
        if stop:
            body["stop"] = stop
        if tools is not None:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        self._apply_thinking_params(body)

        self._apply_call_delay()
        last_exc: Exception | None = None
        resp = None
        max_retries = 3
        attempt = 0
        rate_limit_count = 0
        while attempt < max_retries:
            try:
                req = self._make_request(body)
                resp = urllib.request.urlopen(req, timeout=180)
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and self._retry_interval > 0 and rate_limit_count < 100:
                    try:
                        e.read()
                    except Exception:
                        pass
                    rate_limit_count += 1
                    self._emit_retry(self._retry_interval, rate_limit_count)
                    if self._cancellable_sleep(self._retry_interval):
                        raise AIError("用户取消") from e
                    continue
                self._handle_http_error(e)
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1 and self._is_retryable(e):
                    wait = 2 ** attempt
                    logger.warning("流式请求失败 (第%d次), %ds后重试: %s", attempt + 1, wait, e)
                    time.sleep(wait)
                    attempt += 1
                    continue
                if isinstance(e, urllib.error.URLError):
                    raise AIError(f"网络连接失败: {e.reason}") from e
                if isinstance(e, TimeoutError):
                    raise AIError("请求超时，请检查网络连接或 API 地址") from None
                raise AIError(f"请求异常: {e}") from e
        if resp is None:
            raise AIError(f"网络连接失败 (重试{max_retries}次后仍失败): {last_exc}") from last_exc

        try:
            buffer = ""
            for raw_bytes in resp:
                buffer += raw_bytes.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data_str = line[len("data:"):].strip()
                        if data_str == "[DONE]":
                            yield StreamChunk(is_finished=True)
                            return
                        try:
                            data = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        choices = data.get("choices")
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content_delta = delta.get("content") or ""
                        reasoning_delta = delta.get("reasoning_content") or ""
                        raw_delta = delta if delta else None
                        if content_delta or reasoning_delta or delta.get("tool_calls"):
                            yield StreamChunk(
                                content_delta=content_delta,
                                reasoning_delta=reasoning_delta,
                                raw_delta=raw_delta,
                            )
            yield StreamChunk(is_finished=True)
        finally:
            resp.close()
