"""LLM client abstraction and the shared structured-output loop.

One request/response shape is used by every provider so agents never depend on a
vendor SDK. Structured output is requested as JSON, parsed and validated, with a
bounded number of explicit corrections — a failure is reported as a real error,
never turned into a default success (§7.1).
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.schemas.enums import ErrorCategory

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    """A model request could not be completed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.MODEL_ERROR,
        recoverable: bool = True,
        detail_ref: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category
        self.recoverable = recoverable
        self.detail_ref = detail_ref


class LLMParseError(LLMError):
    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__("MODEL_OUTPUT_UNPARSEABLE", message, category=ErrorCategory.MODEL_ERROR)
        self.raw = raw


@dataclass
class ChatMessage:
    role: str
    content: str


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class LLMRequest:
    system: str
    messages: list[ChatMessage]
    json_mode: bool = False
    temperature: float | None = None
    max_output_tokens: int | None = 2048
    timeout_seconds: float | None = None
    operation_key: str | None = None
    script_key: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    text: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    finish_reason: str | None = None
    latency_ms: int = 0
    corrections: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient(ABC):
    """Unified async model interface."""

    model: str = "unknown"

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion; raises :class:`LLMError` on failure."""

    async def complete_json(
        self,
        request: LLMRequest,
        model_cls: type[T],
        *,
        max_corrections: int = 2,
    ) -> tuple[T, LLMResponse]:
        """Request JSON, validate it, and self-correct a bounded number of times."""
        request = LLMRequest(
            system=request.system,
            messages=list(request.messages),
            json_mode=True,
            temperature=request.temperature,
            max_output_tokens=request.max_output_tokens,
            timeout_seconds=request.timeout_seconds,
            operation_key=request.operation_key,
            script_key=request.script_key,
            metadata=dict(request.metadata),
        )
        request.system = (
            request.system
            + "\n\n完整 JSON Schema（所有 required 字段必须提供）：\n"
            + json.dumps(model_cls.model_json_schema(), ensure_ascii=False)
            + "\n\n只输出一个 JSON 对象，符合给定结构；不要输出解释、Markdown 代码块或多余文本。"
        )
        corrections = 0
        last_error = ""
        last_raw = ""
        while True:
            response = await self.complete(request)
            last_raw = response.text
            try:
                payload = extract_json(response.text)
                parsed = model_cls.model_validate(payload)
                response.corrections = corrections
                return parsed, response
            except (LLMParseError, ValidationError, ValueError) as exc:
                last_error = _describe(exc)
                if corrections >= max_corrections:
                    raise LLMParseError(
                        f"模型输出在 {max_corrections} 次纠正后仍不符合 {model_cls.__name__}: {last_error}",
                        raw=last_raw,
                    ) from exc
                corrections += 1
                request.messages = list(request.messages) + [
                    ChatMessage(role="assistant", content=last_raw[:2000]),
                    ChatMessage(
                        role="user",
                        content=(
                            f"上一条输出无法通过校验：{last_error}\n"
                            f"请重新只输出一个符合 {model_cls.__name__} 结构的 JSON 对象。"
                        ),
                    ),
                ]


def extract_json(text: str) -> Any:
    """Extract a JSON value from model output (tolerating code fences)."""
    if text is None:
        raise LLMParseError("empty model output")
    stripped = text.strip()
    if not stripped:
        raise LLMParseError("empty model output", raw=text or "")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    fence = _FENCE_RE.search(stripped)
    if fence:
        try:
            return json.loads(fence.group(1).strip())
        except json.JSONDecodeError:
            pass
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"model output is not valid JSON: {exc}", raw=text) from exc
    raise LLMParseError("model output contains no JSON object", raw=text)


def _describe(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]
        )
    return str(exc)[:500]
