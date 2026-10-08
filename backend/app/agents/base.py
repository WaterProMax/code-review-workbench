"""Agent protocol, runtime context and the shared model/tool loop (§4.3, §7.3).

Every role implements the same ``execute(TaskEnvelope, AgentContext) -> TaskResult``
signature. The shared loop is the only place a role's model interacts with tools:
the model emits a JSON decision (call a tool, or finish), tools run through the
registry wrapper (permissions, step budget, deadline, events), and the loop ends
with a validated, role-specific final payload.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.providers.base import ChatMessage, LLMClient, LLMError, LLMRequest
from app.registry.tools import ToolContext, ToolRegistry, ToolResult, StepCounter
from app.schemas.agents import AgentSpec
from app.schemas.common import ErrorInfo
from app.schemas.enums import ApplicablePhase, CheckMethod, ErrorCategory, ResultStatus
from app.schemas.results import AcceptanceContract, Finding, CheckResult, CheckSpec, TaskResult
from app.schemas.tasks import TaskEnvelope
from app.services.artifacts import ArtifactService
from app.services.events import EventContext, EventSink
from app.services.workspace import WorkspaceService
from app.storage.repositories import Repos


class AgentExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, category: ErrorCategory = ErrorCategory.UNKNOWN) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.category = category


@dataclass
class AgentContext:
    """Runtime dependencies for one attempt; never serialized into state."""

    llm: LLMClient
    tools: ToolRegistry
    workspace: WorkspaceService
    artifacts: ArtifactService
    repos: Repos
    sink: EventSink
    agent_spec: AgentSpec
    deadline_seconds: float = 180.0
    model_timeout_seconds: float = 60.0
    tool_timeout_seconds: float = 30.0
    max_tool_steps: int = 12
    max_model_retries: int = 2
    cancellation: asyncio.Event = field(default_factory=asyncio.Event)
    scratch_dir: Path | None = None
    allowed_paths: list[str] = field(default_factory=list)
    read_only: bool = True

    def tool_context(self, task: TaskEnvelope) -> ToolContext:
        return ToolContext(
            actor_id=task.agent_id,
            root_task_id=task.root_task_id,
            workspace=self.workspace,
            artifacts=self.artifacts,
            repos=self.repos,
            sink=self.sink,
            source_version=task.source_version,
            contract_version=task.contract_version,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            dispatch_batch_id=task.dispatch_batch_id,
            scratch_dir=self.scratch_dir,
            deadline_seconds=self.deadline_seconds,
            tool_timeout_seconds=self.tool_timeout_seconds,
            steps=StepCounter(limit=self.max_tool_steps),
            cancel_event=self.cancellation,
            read_only=self.read_only,
            allowed_paths=self.allowed_paths,
        )


class AgentProtocol(ABC):
    spec: AgentSpec

    @abstractmethod
    async def execute(self, task: TaskEnvelope, context: AgentContext) -> TaskResult:
        """Run one attempt and return a structured report to the parent."""


class AgentStep(BaseModel):
    """The model's per-turn decision: use a tool, or finish."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    final: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> "AgentStep":
        if (self.tool is None) == (self.final is None):
            raise ValueError("每一步必须二选一：调用一个工具，或给出 final")
        if self.final is not None and self.tool is not None:
            raise ValueError("不能同时调用工具和给出 final")
        return self


OBSERVATION_LIMIT = 4000


class BaseAgent(AgentProtocol):
    """Shared plumbing: prompt + contract loading, tool loop, result envelope."""

    def __init__(self, spec: AgentSpec, prompt: str, final_model: type[BaseModel]) -> None:
        self.spec = spec
        self.prompt = prompt
        self.final_model = final_model

    # ---- contract / inputs -------------------------------------------------
    def load_contract(self, task: TaskEnvelope, context: AgentContext) -> AcceptanceContract:
        contract = AcceptanceContract.model_validate(
            context.artifacts.read_json(task.input_refs.acceptance_contract)
        )
        if contract.contract_version != task.contract_version:
            raise AgentExecutionError(
                "CONTRACT_VERSION_MISMATCH",
                f"合同版本不匹配：派发 {task.contract_version}，产物 {contract.contract_version}",
                ErrorCategory.INVALID_RESULT,
            )
        return contract

    def load_findings(self, task: TaskEnvelope, context: AgentContext) -> list[Finding]:
        ref = task.input_refs.findings
        if not ref:
            return []
        try:
            payload = context.artifacts.read_json(ref)
        except Exception:  # noqa: BLE001 - a missing findings bundle is not fatal
            return []
        return [Finding.model_validate(item) for item in payload.get("findings", [])]

    def checks_for(self, contract: AcceptanceContract, task: TaskEnvelope) -> list[CheckSpec]:
        kind = task.task_kind
        phase = ApplicablePhase.INITIAL_REVIEW if kind == "review" else ApplicablePhase.POST_PATCH
        wanted = {ApplicablePhase.BOTH, phase}
        return [c for c in contract.checks if c.applicable_phase in wanted]

    def validate_final(self, final: BaseModel, task: TaskEnvelope, context: AgentContext) -> None:
        """Role-specific checks beyond the payload's structural schema."""

    # ---- tool loop ---------------------------------------------------------
    async def run_tool_loop(
        self,
        task: TaskEnvelope,
        context: AgentContext,
        tool_ctx: ToolContext,
        *,
        opening: str,
        extra_context: dict[str, Any] | None = None,
        max_steps: int | None = None,
    ) -> tuple[BaseModel, list[dict[str, Any]]]:
        """Drive the model until it produces a valid final payload.

        Returns the validated final payload and the transcript of tool calls, so
        the role can attach evidence references to its report.
        """
        allowed = context.tools.names_for(task.agent_id)
        system = "\n\n".join(
            [
                self.prompt,
                _render_tools(context.tools, allowed),
                _render_decision_protocol(self.final_model),
            ]
        )
        messages = [
            ChatMessage(role="user", content=opening),
            ChatMessage(
                role="user",
                content="任务上下文：\n"
                + json.dumps(
                    {
                        "root_task_id": task.root_task_id,
                        "task_id": task.task_id,
                        "attempt_id": task.attempt_id,
                        "task_kind": task.task_kind,
                        "goal": task.goal,
                        "source_version": task.source_version,
                        "contract_version": task.contract_version,
                        "acceptance_criteria": task.acceptance_criteria,
                        "allowed_paths": task.constraints.allowed_paths,
                        **(extra_context or {}),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            ),
        ]
        transcript: list[dict[str, Any]] = []
        steps = max_steps or context.max_tool_steps
        model: BaseModel | None = None

        for _ in range(steps + 1):
            if context.cancellation.is_set():
                raise AgentExecutionError("CANCELLED", "尝试已被取消", ErrorCategory.CANCELLED)
            request = LLMRequest(
                system=system,
                messages=messages,
                max_output_tokens=8192,
                timeout_seconds=context.model_timeout_seconds,
                operation_key=f"{task.attempt_id}:step",
                script_key=f"{task.task_id}:{task.agent_id}:step",
            )
            try:
                step, _ = await context.llm.complete_json(
                    request, AgentStep, max_corrections=context.max_model_retries
                )
            except LLMError as exc:
                raise AgentExecutionError(exc.code, exc.message, exc.category) from exc

            messages.append(
                ChatMessage(role="assistant", content=json.dumps(step.model_dump(exclude_none=True), ensure_ascii=False))
            )

            if step.final is not None:
                try:
                    model = self.final_model.model_validate(step.final)
                    self.validate_final(model, task, context)
                except Exception as exc:  # noqa: BLE001 - ask the model to correct once
                    model = None
                    messages.append(
                        ChatMessage(
                            role="user",
                            content=f"final 不符合结构：{exc}\n请重新输出 final。",
                        )
                    )
                    continue
                break

            assert step.tool is not None
            result = await context.tools.execute(step.tool, tool_ctx, **step.args)
            transcript.append(
                {
                    "tool": step.tool,
                    "ok": result.ok,
                    "summary": result.summary,
                    "artifact_refs": result.artifact_refs,
                    "faults": result.faults,
                }
            )
            messages.append(
                ChatMessage(
                    role="user",
                    content=_render_observation(step.tool, result),
                )
            )
        if model is None:
            raise AgentExecutionError(
                "TOOL_STEP_LIMIT",
                f"{task.agent_id} 在 {steps} 步内没有给出最终结论",
                ErrorCategory.CYCLE_LIMIT,
            )
        return model, transcript

    # ---- result envelope ---------------------------------------------------
    def build_result(
        self,
        task: TaskEnvelope,
        *,
        status: ResultStatus,
        passed: bool | None,
        summary: str,
        started_at: datetime,
        result_refs: dict[str, str] | None = None,
        evidence_refs: list[str] | None = None,
        error: ErrorInfo | None = None,
    ) -> TaskResult:
        return TaskResult(
            result_id=f"R-{task.attempt_id}-{int(time.time() * 1000) % 100000}",
            root_task_id=task.root_task_id,
            parent_task_id=task.parent_task_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            dispatch_batch_id=task.dispatch_batch_id,
            agent_id=task.agent_id,
            agent_version=task.agent_version,
            task_kind=task.task_kind,
            source_version=task.source_version,
            contract_version=task.contract_version,
            status=status,
            passed=passed,
            summary=summary,
            result_refs=result_refs or {},
            evidence_refs=evidence_refs or [],
            error=error,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )

    # ---- execution helper --------------------------------------------------
    async def _execute(self, task: TaskEnvelope, context: AgentContext):  # type: ignore[no-untyped-def]
        started_at = datetime.now(timezone.utc)
        try:
            return await self.run(task, context, started_at)
        except asyncio.CancelledError:
            # A cancellation is a control-plane signal (deadline/interruption), not
            # a business outcome: the control layer invalidates the attempt and
            # records its own terminal. Swallowing it here would forge a report.
            raise
        except AgentExecutionError as exc:
            return self.build_result(
                task,
                status=ResultStatus.FAILED,
                passed=None,
                summary=f"执行失败：{exc.message}",
                started_at=started_at,
                error=ErrorInfo(
                    code=exc.code,
                    category=exc.category,
                    message=exc.message,
                    recoverable=exc.category.default_recoverable,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - normalize into a failed report
            return self.build_result(
                task,
                status=ResultStatus.FAILED,
                passed=None,
                summary=f"执行异常：{type(exc).__name__}: {exc}",
                started_at=started_at,
                error=ErrorInfo(
                    code="AGENT_INTERNAL_ERROR",
                    category=ErrorCategory.UNKNOWN,
                    message=str(exc)[:400],
                    recoverable=True,
                ),
            )

    @abstractmethod
    async def run(self, task: TaskEnvelope, context: AgentContext, started_at: datetime) -> TaskResult:
        ...

    async def execute(self, task: TaskEnvelope, context: AgentContext) -> TaskResult:
        return await self._execute(task, context)


def _render_tools(registry: ToolRegistry, names: list[str]) -> str:
    lines = ["可用工具（只能使用这些工具，且必须遵守其权限）："]
    for name in names:
        spec = registry.get(name)
        lines.append(
            f"- {name}: {spec.description}\n  参数 JSON Schema: "
            + json.dumps(spec.input_schema, ensure_ascii=False)
        )
    return "\n".join(lines)


def _render_decision_protocol(final_model: type[BaseModel]) -> str:
    example = final_model.model_json_schema()
    return (
        "输出协议（每一步只输出一个 JSON 对象）：\n"
        '1) 调用工具：{"reason": "...", "tool": "<工具名>", "args": {...}}\n'
        '2) 给出最终结论：{"reason": "...", "final": {...}}\n'
        "final 必须符合以下 JSON Schema：\n"
        + json.dumps(example, ensure_ascii=False)
        + "\n不要在 final 与工具调用之间混用；不要输出多余文本。"
    )


def _render_observation(tool: str, result: ToolResult) -> str:
    payload = {
        "tool": tool,
        "ok": result.ok,
        "summary": result.summary,
        "data": result.data,
        "faults": result.faults,
    }
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) > OBSERVATION_LIMIT:
        text = text[:OBSERVATION_LIMIT] + "...(截断)"
    return "工具观察结果：\n" + text


def phase_of(task_kind: str) -> ApplicablePhase:
    return ApplicablePhase.INITIAL_REVIEW if task_kind == "review" else ApplicablePhase.POST_PATCH


def required_checks(contract: AcceptanceContract, task_kind: str) -> list[CheckSpec]:
    phase = phase_of(task_kind)
    wanted = {ApplicablePhase.BOTH, phase}
    return [c for c in contract.checks if c.required and c.applicable_phase in wanted]


def deterministic_checks(checks: list[CheckSpec]) -> list[CheckSpec]:
    return [c for c in checks if c.method is not CheckMethod.MODEL_REVIEW]


def model_review_checks(checks: list[CheckSpec]) -> list[CheckSpec]:
    return [c for c in checks if c.method is CheckMethod.MODEL_REVIEW]


def verdict_from_checks(
    checks: list[CheckSpec], results: list[CheckResult], open_required_findings: int
) -> bool | None:
    """Derive a review verdict from check results (§5.3).

    ``False`` when a required check clearly failed or a required problem is open;
    ``None`` when evidence is incomplete; ``True`` only when every required check
    passed on this version.
    """
    from app.schemas.enums import CheckStatus

    required_ids = [c.check_id for c in checks if c.required]
    by_id = {r.check_id: r for r in results}
    if open_required_findings > 0:
        return False
    statuses = [by_id.get(check_id) for check_id in required_ids]
    if any(s is not None and s.status is CheckStatus.FAILED for s in statuses):
        return False
    if any(s is None or s.status is not CheckStatus.PASSED for s in statuses):
        return None
    return True


def agent_event_sink(context: AgentContext, task: TaskEnvelope) -> EventSink:
    return context.sink.for_attempt(
        attempt_id=task.attempt_id,
        task_id=task.task_id,
        dispatch_batch_id=task.dispatch_batch_id,
        actor_id=task.agent_id,
        source_version=task.source_version,
    )


def bound_event_sink(root_task_id: str, repos: Repos, actor_id: str) -> EventSink:
    return EventSink(repos.events, EventContext(root_task_id=root_task_id, actor_id=actor_id))
