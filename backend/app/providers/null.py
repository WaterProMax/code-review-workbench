"""A model client that never calls a provider.

Graph inspection and checkpoint reads need an ``LLMClient`` to assemble the
runtime. They must not open a network connection.
"""

from __future__ import annotations

from app.providers.base import LLMClient, LLMError, LLMRequest, LLMResponse


class NullLLM(LLMClient):
    model = "null"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise LLMError("MODEL_NOT_CONFIGURED", "此客户端只用于构图，不会调用模型")

    async def aclose(self) -> None:
        return None
