"""Tool protocol, registry and the unified execution wrapper (§7.2).

Every tool declares its schemas, timeout and *role permissions*. The wrapper is
the only place tools are invoked: it enforces permissions, the per-attempt tool
step budget, the deadline and cancellation, normalizes errors, counts slow calls
and emits ``tool_started`` / ``tool_finished`` events. A tool therefore cannot
bypass logging or run outside its authorization.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from app.schemas.common import ErrorInfo
from app.schemas.enums import ErrorCategory, EventType
from app.schemas.agents import AgentSpec
from app.services.artifacts import ArtifactService
from app.services.events import EventSink
from app.services.workspace import WorkspaceService
from app.storage.repositories import Repos

ToolImpl = Callable[["ToolContext", BaseModel], Awaitable["ToolResult"]]


class ToolError(RuntimeError):
    def __init__(self, code: str, message: str, category: ErrorCategory = ErrorCategory.TOOL_ERROR) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category


class StepBudgetExceeded(ToolError):
    def __init__(self, used: int, limit: int) -> None:
        super().__init__(
            "TOOL_STEP_LIMIT",
            f"工具步数已达上限（{used}/{limit}）",
            ErrorCategory.CYCLE_LIMIT,
        )


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    input_schema: dict
    output_schema: dict
    timeout_seconds: float = 30.0
    allowed_roles: tuple[str, ...] = ()
    read_only: bool = True

    def allows(self, role: str) -> bool:
        return "any" in self.allowed_roles or role in self.allowed_roles


@dataclass
class ToolResult:
    ok: bool
    summary: str
    data: Any = None
    artifact_refs: list[str] = field(default_factory=list)
    error: ErrorInfo | None = None
    # Execution problems (timeout, missing dependency, crash) that the agent
    # must surface as an execution fault rather than a check verdict (§7.5.5).
    faults: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: int = 0

    @staticmethod
    def success(summary: str, data: Any = None, artifact_refs: list[str] | None = None) -> "ToolResult":
        return ToolResult(ok=True, summary=summary, data=data, artifact_refs=artifact_refs or [])


@dataclass
class StepCounter:
    limit: int
    used: int = 0

    def consume(self) -> None:
        self.used += 1
        if self.used > self.limit:
            raise StepBudgetExceeded(self.used, self.limit)

    def remaining(self) -> int:
        return max(self.limit - self.used, 0)


@dataclass
class ToolContext:
    """Runtime dependencies and constraints for one attempt's tool calls."""

    actor_id: str
    root_task_id: str
    workspace: WorkspaceService
    artifacts: ArtifactService
    repos: Repos
    sink: EventSink
    source_version: str
    contract_version: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    dispatch_batch_id: str | None = None
    scratch_dir: Path | None = None
    deadline_seconds: float = 180.0
    tool_timeout_seconds: float = 30.0
    steps: StepCounter = field(default_factory=lambda: StepCounter(limit=12))
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    read_only: bool = True
    allowed_paths: list[str] = field(default_factory=list)
    attempt_started: float = field(default_factory=time.monotonic)

    def remaining_seconds(self) -> float:
        """Seconds left in the attempt's overall deadline."""
        return max(self.deadline_seconds - (time.monotonic() - self.attempt_started), 0.0)


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._impls: dict[str, ToolImpl] = {}

    # ---- registration ------------------------------------------------------
    def register(self, spec: ToolSpec, impl: ToolImpl) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        # Advertise exactly the validation schema, including nested required fields.
        spec.input_schema = spec.args_model.model_json_schema()
        self._specs[spec.name] = spec
        self._impls[spec.name] = impl

    def get(self, name: str) -> ToolSpec:
        if name not in self._specs:
            raise ToolError("TOOL_UNKNOWN", f"未知工具 {name!r}")
        return self._specs[name]

    def specs_for(self, role: str) -> list[ToolSpec]:
        return [s for s in self._specs.values() if s.allows(role)]

    def names_for(self, role: str) -> list[str]:
        return sorted(s.name for s in self.specs_for(role))

    def names(self) -> list[str]:
        return sorted(self._specs)

    # ---- execution ---------------------------------------------------------
    async def execute(self, tool_name: str, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        spec = self.get(tool_name)
        if not spec.allows(ctx.actor_id):
            return _failure(
                ctx, spec, "TOOL_FORBIDDEN", f"{ctx.actor_id} 无权调用工具 {tool_name}", ErrorCategory.TOOL_ERROR, 0
            )

        try:
            ctx.steps.consume()
        except StepBudgetExceeded as exc:
            return _failure(ctx, spec, exc.code, exc.message, exc.category, 0)

        started = time.monotonic()
        ctx.sink.emit(EventType.TOOL_STARTED, payload={"tool": tool_name, "args": _safe_args(kwargs)})
        try:
            try:
                args = spec.args_model.model_validate(kwargs)
            except ValidationError as exc:
                raise ToolError("TOOL_BAD_INPUT", f"工具参数不合法: {exc.errors()[:3]}") from exc

            timeout = min(spec.timeout_seconds, ctx.tool_timeout_seconds, ctx.remaining_seconds() or 0.001)
            if timeout <= 0:
                raise ToolError("TOOL_TIMEOUT", "尝试期限已到，不能再调用工具", ErrorCategory.TOOL_TIMEOUT)
            impl = self._impls[tool_name]
            work = asyncio.ensure_future(impl(ctx, args))
            cancel_wait = asyncio.ensure_future(ctx.cancel_event.wait())
            done, pending = await asyncio.wait(
                {work, cancel_wait}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if cancel_wait in done and work not in done:
                work.cancel()
                raise ToolError("TOOL_CANCELLED", "尝试已被取消", ErrorCategory.CANCELLED)
            if work not in done:
                work.cancel()
                raise ToolError(
                    "TOOL_TIMEOUT",
                    f"工具 {tool_name} 执行超时（{timeout:.1f}s）",
                    ErrorCategory.TOOL_TIMEOUT,
                )
            result = await work
        except ToolError as exc:
            result = _failure(ctx, spec, exc.code, exc.message, exc.category, int((time.monotonic() - started) * 1000))
        except asyncio.CancelledError:
            result = _failure(ctx, spec, "TOOL_CANCELLED", "工具调用被取消", ErrorCategory.CANCELLED, 0)
        except Exception as exc:  # noqa: BLE001 - normalized per plan §7.2
            result = _failure(
                ctx,
                spec,
                "TOOL_ERROR",
                f"{type(exc).__name__}: {exc}",
                ErrorCategory.TOOL_ERROR,
                int((time.monotonic() - started) * 1000),
            )

        result.duration_ms = result.duration_ms or int((time.monotonic() - started) * 1000)
        ctx.sink.emit(
            EventType.TOOL_FINISHED,
            payload={
                "tool": tool_name,
                "ok": result.ok,
                "summary": result.summary[:300],
                "error": result.error.model_dump(mode="json") if result.error else None,
            },
            artifact_refs=result.artifact_refs,
            duration_ms=result.duration_ms,
        )
        return result


def _failure(
    ctx: ToolContext,
    spec: ToolSpec,
    code: str,
    message: str,
    category: ErrorCategory,
    duration_ms: int,
) -> ToolResult:
    return ToolResult(
        ok=False,
        summary=message,
        error=ErrorInfo(code=code, category=category, message=message, recoverable=category.default_recoverable),
        duration_ms=duration_ms,
    )


def _safe_args(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Truncate argument values for the event payload (no secrets, bounded size)."""
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if isinstance(value, str):
            out[key] = value[:200]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list):
            out[key] = f"list[{len(value)}]"
        elif isinstance(value, dict):
            out[key] = f"dict[{len(value)}]"
        else:
            out[key] = type(value).__name__
    return out


def new_tool_registry() -> ToolRegistry:
    """Build the registry with the built-in tools registered."""
    from app.services.tools import build_default_registry

    return build_default_registry()


def agent_spec_tool_names(registry: ToolRegistry, spec: AgentSpec) -> list[str]:
    """Intersection of declared tools and the tools the role may actually call."""
    allowed = set(registry.names_for(spec.agent_id))
    return sorted(allowed & set(spec.tool_names))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"
