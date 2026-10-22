"""AgentNodeAdapter: the single bridge between the graph and a role implementation.

It validates the dispatch, builds the runtime context, marks the attempt running,
emits the ``attempt_started`` event, calls the registered implementation, and
checks that the returned report's identity matches the envelope before the
receiving control layer may accept it. A mismatch is *not* converted into a
verdict — it is rejected so the parent can recover (A10).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.agents.base import AgentContext
from app.registry.agents import AgentRegistry, AgentRegistrationError
from app.registry.tools import ToolRegistry
from app.schemas.agents import AgentSpec
from app.schemas.enums import AttemptStatus, EventType
from app.schemas.results import TaskResult
from app.schemas.tasks import Attempt, TaskEnvelope
from app.services.artifacts import ArtifactService
from app.services.events import EventSink
from app.services.workspace import WorkspaceService
from app.storage.repositories import Repos


class ResultIdentityError(RuntimeError):
    """The report does not belong to the dispatched attempt."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


ContextFactory = Callable[[TaskEnvelope, AgentSpec], AgentContext]


class AgentNodeAdapter:
    def __init__(
        self,
        *,
        registry: AgentRegistry,
        repos: Repos,
        sink: EventSink,
        context_factory: ContextFactory,
    ) -> None:
        self.registry = registry
        self.repos = repos
        self.sink = sink
        self.context_factory = context_factory

    # ---- input validation --------------------------------------------------
    def validate_dispatch(self, task: TaskEnvelope, attempt: Attempt | None) -> list[str]:
        problems: list[str] = []
        if attempt is None:
            return [f"没有登记的执行尝试 {task.attempt_id}"]
        if attempt.task_id != task.task_id:
            problems.append(f"attempt 属于任务 {attempt.task_id}，派发声明为 {task.task_id}")
        if attempt.root_task_id != task.root_task_id:
            problems.append("attempt 的总任务与派发不一致")
        if attempt.dispatch_batch_id != task.dispatch_batch_id:
            problems.append("attempt 的批次与派发不一致")
        if attempt.fencing_token != task.fencing_token:
            problems.append("执行令牌与登记尝试不一致")
        if attempt.source_version != task.source_version:
            problems.append("attempt 记录的源码版本与派发不一致")
        if not self.registry.has(task.agent_id, task.agent_version):
            problems.append(f"角色 {task.agent_id}@{task.agent_version} 未注册")
        else:
            spec = self.registry.get_spec(task.agent_id, task.agent_version)
            if task.task_kind not in spec.supported_task_kinds:
                problems.append(f"角色 {task.agent_id} 不支持任务类型 {task.task_kind}")
        if attempt.status in (AttemptStatus.INVALIDATED.value, AttemptStatus.COMPLETED.value, AttemptStatus.FAILED.value):
            problems.append(f"attempt 已是终态 {attempt.status}，不能再次执行")
        return problems

    # ---- result validation -------------------------------------------------
    def validate_result(self, task: TaskEnvelope, result: TaskResult) -> list[str]:
        problems: list[str] = []
        pairs = [
            ("root_task_id", task.root_task_id, result.root_task_id),
            ("task_id", task.task_id, result.task_id),
            ("attempt_id", task.attempt_id, result.attempt_id),
            ("dispatch_batch_id", task.dispatch_batch_id, result.dispatch_batch_id),
            ("agent_id", task.agent_id, result.agent_id),
            ("agent_version", task.agent_version, result.agent_version),
            ("task_kind", task.task_kind, result.task_kind),
            ("source_version", task.source_version, result.source_version),
            ("contract_version", task.contract_version, result.contract_version),
        ]
        for name, expected, actual in pairs:
            if expected != actual:
                problems.append(f"{name} 不匹配：派发 {expected!r}，回报 {actual!r}")
        return problems

    # ---- execution ---------------------------------------------------------
    async def execute(self, task: TaskEnvelope) -> TaskResult:
        from app.services.execution_context import check_execution
        with self.repos.db.transaction() as conn:
            check_execution(conn, task.root_task_id, task.fencing_token)
        attempt = self.repos.attempts.get(task.attempt_id)
        problems = self.validate_dispatch(task, attempt)
        if problems:
            raise ResultIdentityError(problems)

        spec = self.registry.get_spec(task.agent_id, task.agent_version)
        implementation = self.registry.resolve(task.agent_id, task.agent_version)
        context = self.context_factory(task, spec)

        bound = context.sink.for_attempt(
            attempt_id=task.attempt_id,
            task_id=task.task_id,
            dispatch_batch_id=task.dispatch_batch_id,
            actor_id=task.agent_id,
            source_version=task.source_version,
        )
        bound.emit(
            EventType.ATTEMPT_STARTED,
            payload={
                "agent_id": task.agent_id,
                "agent_version": task.agent_version,
                "task_kind": task.task_kind,
                "contract_version": task.contract_version,
            },
        )
        self.repos.attempts.update_status(
            task.attempt_id, AttemptStatus.RUNNING, started_at=datetime.now(timezone.utc)
        )

        result = await implementation.execute(task, context)

        mismatches = self.validate_result(task, result)
        if mismatches:
            raise ResultIdentityError(mismatches)
        return result
