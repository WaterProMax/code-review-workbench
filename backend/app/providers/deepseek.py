"""DeepSeek (OpenAI-compatible) client.

Encapsulates authentication, timeouts, retryable errors and a bounded retry so
no vendor SDK leaks into the agents. Keys are never written to logs, events or
reports; only a safe request/response summary is logged.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.providers.base import (
    ChatMessage,
    LLMClient,
    LLMError,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from app.schemas.enums import ErrorCategory

logger = logging.getLogger("hw2.provider.deepseek")

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class DeepSeekClient(LLMClient):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise LLMError(
                "MODEL_NOT_CONFIGURED",
                "模型凭据未配置",
                category=ErrorCategory.MODEL_ERROR,
                recoverable=False,
            )
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature
        self._client = client
        self._owns_client = client is None

    # ---- transport ---------------------------------------------------------
    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ---- completion --------------------------------------------------------
    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system}]
            + [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature
            if request.temperature is not None
            else self.temperature,
            "stream": False,
        }
        if request.max_output_tokens:
            payload["max_tokens"] = request.max_output_tokens
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        timeout = request.timeout_seconds or self.timeout_seconds
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        attempt = 0
        while True:
            started = time.monotonic()
            try:
                client = await self._http()
                response = await client.post(
                    self._endpoint(), json=payload, headers=headers, timeout=timeout
                )
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                raise LLMError(
                    "MODEL_TIMEOUT",
                    f"模型请求超时（{timeout}s，重试 {attempt} 次）",
                    category=ErrorCategory.MODEL_TIMEOUT,
                    recoverable=True,
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < self.max_retries:
                    attempt += 1
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
                    continue
                raise LLMError(
                    "MODEL_TRANSPORT_ERROR",
                    f"模型请求失败：{type(exc).__name__}",
                    category=ErrorCategory.MODEL_ERROR,
                    recoverable=True,
                ) from exc

            latency_ms = int((time.monotonic() - started) * 1000)
            if response.status_code in RETRYABLE_STATUS and attempt < self.max_retries:
                attempt += 1
                await asyncio.sleep(min(2 ** (attempt - 1), 4))
                continue

            if response.status_code >= 400:
                detail = _safe_error_detail(response)
                recoverable = response.status_code in RETRYABLE_STATUS
                raise LLMError(
                    "MODEL_HTTP_ERROR",
                    f"模型服务返回 {response.status_code}: {detail}",
                    category=ErrorCategory.MODEL_ERROR,
                    recoverable=recoverable,
                )

            data = response.json()
            text = _extract_text(data)
            usage = _extract_usage(data)
            logger.info(
                "model call model=%s op=%s latency_ms=%d retries=%d tokens=%d",
                self.model,
                request.operation_key or request.script_key or "-",
                latency_ms,
                attempt,
                usage.total_tokens,
            )
            return LLMResponse(
                text=text,
                model=data.get("model", self.model),
                usage=usage,
                finish_reason=_finish_reason(data),
                latency_ms=latency_ms,
                raw={"id": data.get("id")},
            )


def _extract_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(
            "MODEL_EMPTY_RESPONSE",
            "模型返回中没有 choices",
            category=ErrorCategory.MODEL_ERROR,
            recoverable=True,
        )
    message = choices[0].get("message") or {}
    content = message.get("content")
    if content is None:
        raise LLMError(
            "MODEL_EMPTY_RESPONSE",
            "模型返回中没有文本内容",
            category=ErrorCategory.MODEL_ERROR,
            recoverable=True,
        )
    return content


def _finish_reason(data: dict) -> str | None:
    choices = data.get("choices") or []
    return choices[0].get("finish_reason") if choices else None


def _extract_usage(data: dict) -> LLMUsage:
    usage = data.get("usage") or {}
    return LLMUsage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
    )


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        message = body.get("error", {}).get("message") if isinstance(body, dict) else None
        if message:
            return str(message)[:200]
    except Exception:  # pragma: no cover - best effort
        pass
    return (response.text or "")[:200]
