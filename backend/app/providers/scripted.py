"""Scripted model used by deterministic tests.

It is only ever constructed explicitly (tests inject it); the application never
falls back to it when the real model fails, so a scripted run can never be
mistaken for a real integration result (§7.1).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from app.providers.base import LLMClient, LLMError, LLMRequest, LLMResponse, LLMUsage
from app.schemas.enums import ErrorCategory

Handler = Callable[[LLMRequest], Any]


class ScriptedLLMClient(LLMClient):
    """Returns canned or handler-produced responses in a deterministic order."""

    def __init__(
        self,
        handler: Handler | None = None,
        *,
        responses: list[Any] | None = None,
        model: str = "scripted",
        latency_seconds: float = 0.0,
    ) -> None:
        if handler is None and not responses:
            raise ValueError("ScriptedLLMClient needs a handler or a list of responses")
        self.model = model
        self.latency_seconds = latency_seconds
        self._handler = handler
        self._responses = list(responses or [])
        self.calls: list[LLMRequest] = []

    def add(self, response: Any) -> None:
        self._responses.append(response)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.latency_seconds:
            # A yield point so concurrency in tests is observable, not an artifact
            # of every scripted call completing synchronously.
            await asyncio.sleep(self.latency_seconds)
        if self._handler is not None:
            value = self._handler(request)
        elif self._responses:
            value = self._responses.pop(0)
        else:
            raise LLMError(
                "SCRIPTED_EXHAUSTED",
                "脚本模型没有更多预设响应",
                category=ErrorCategory.MODEL_ERROR,
                recoverable=False,
            )
        if isinstance(value, Exception):
            raise value
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return LLMResponse(
            text=text,
            model=self.model,
            usage=LLMUsage(),
            finish_reason="stop",
            latency_ms=0,
        )

    async def aclose(self) -> None:  # pragma: no cover - symmetry with real client
        return None
