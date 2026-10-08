"""Task tree, execution attempt and dispatch envelope contracts (§5.1–5.2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import SCHEMA_VERSION, TaskConstraints, TaskDep, utcnow
from app.schemas.enums import (
    AttemptStatus,
    RootStatus,
    TaskLevel,
    TaskStatus,
)


class Task(BaseModel):
    """A logical task: the root task or one of its children.

    ``status`` is validated against the *level-specific* set: root tasks use the
    system lifecycle, children use the work-unit lifecycle (plan §5.2.1).
    """

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    parent_task_id: str | None = None
    task_level: TaskLevel
    agent_id: str | None = None
    agent_version: str | None = None
    task_kind: str | None = None
    goal: str = Field(min_length=1)
    depends_on: list[TaskDep] = Field(default_factory=list)
    status: str
    passed: bool | None = None
    skip_reason: str | None = None
    workflow_version: str = Field(min_length=1)
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check_level_consistency(self) -> "Task":
        if self.task_level is TaskLevel.ROOT:
            if self.task_id != self.root_task_id:
                raise ValueError("root task_id must equal root_task_id")
            if self.parent_task_id is not None:
                raise ValueError("root task parent_task_id must be null")
            if self.agent_id is not None or self.task_kind is not None:
                raise ValueError("root task must not carry agent_id/task_kind")
            if self.status not in {s.value for s in RootStatus}:
                raise ValueError(
                    f"invalid root status {self.status!r}; "
                    f"expected one of {sorted(s.value for s in RootStatus)}"
                )
            if self.skip_reason is not None:
                raise ValueError("root task cannot be skipped")
        else:
            if self.parent_task_id is None:
                raise ValueError("child task requires parent_task_id")
            if self.task_id == self.root_task_id:
                raise ValueError("child task_id must differ from root_task_id")
            if not self.agent_id:
                raise ValueError("child task requires agent_id")
            if not self.task_kind:
                raise ValueError("child task requires task_kind")
            if self.status not in {s.value for s in TaskStatus}:
                raise ValueError(
                    f"invalid child status {self.status!r}; "
                    f"expected one of {sorted(s.value for s in TaskStatus)}"
                )
        return self

    @model_validator(mode="after")
    def _check_skip_reason(self) -> "Task":
        if self.status == TaskStatus.SKIPPED.value and not self.skip_reason:
            raise ValueError("skipped task requires skip_reason")
        if self.status == TaskStatus.FAILED.value and self.passed is True:
            raise ValueError("failed task cannot be passed=True")
        return self

    def is_active(self) -> bool:
        return self.status in {
            TaskStatus.BLOCKED.value,
            TaskStatus.QUEUED.value,
            TaskStatus.RUNNING.value,
        }


class TaskTree(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: Task
    children: list[Task] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_tree(self) -> "TaskTree":
        if self.root.task_level is not TaskLevel.ROOT:
            raise ValueError("task tree root must be a root-level task")
        seen: set[str] = set()
        for child in self.children:
            if child.root_task_id != self.root.task_id:
                raise ValueError(f"child {child.task_id} belongs to another root task")
            if child.task_id in seen:
                raise ValueError(f"duplicate child task_id {child.task_id}")
            seen.add(child.task_id)
        return self

    def find(self, task_id: str) -> Task | None:
        if task_id == self.root.task_id:
            return self.root
        return next((c for c in self.children if c.task_id == task_id), None)

    def by_kind(self, task_kind: str) -> list[Task]:
        return [c for c in self.children if c.task_kind == task_kind]


class Attempt(BaseModel):
    """One execution attempt of a logical task (attempt_no stays unique)."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    dispatch_batch_id: str | None = None
    attempt_no: int = Field(ge=1)
    source_version: str = Field(min_length=1)
    contract_version: str | None = None
    input_refs: dict[str, str] = Field(default_factory=dict)
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)
    retry_reason: str | None = None
    fencing_token: int = Field(default=0, ge=0)
    status: AttemptStatus = AttemptStatus.QUEUED
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check_times(self) -> "Attempt":
        if self.started_at and self.finished_at and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        if self.status is AttemptStatus.RUNNING and self.started_at is None:
            raise ValueError("running attempt requires started_at")
        return self


class InputRefs(BaseModel):
    """Input artifact references for a dispatch (Desgin §4.1).

    ``source`` and ``acceptance_contract`` are always required; task-specific
    references (findings, previous patch, verification report, generated tests)
    are optional and carry the fixed versions of what the child must read.
    """

    model_config = ConfigDict(extra="allow")

    source: str = Field(min_length=1)
    acceptance_contract: str = Field(min_length=1)
    findings: str | None = None
    previous_patch: str | None = None
    verification: str | None = None
    patch_application: str | None = None
    generated_tests: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)

    def as_mapping(self) -> dict[str, str]:
        data = self.model_dump(exclude={"extra"}, exclude_none=True)
        data.update(self.model_extra or {})
        data.update(self.extra)
        return {k: str(v) for k, v in data.items()}


class TaskEnvelope(BaseModel):
    """Dispatch payload handed to a child agent (Desgin §4.1)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    fencing_token: int = Field(default=0, ge=0)

    # identity
    root_task_id: str = Field(min_length=1)
    parent_task_id: str | None = None
    task_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    dispatch_batch_id: str = Field(min_length=1)

    # executor + goal
    agent_id: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    task_kind: str = Field(min_length=1)
    goal: str = Field(min_length=1)

    # inputs
    input_refs: InputRefs
    source_version: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(default_factory=list)

    # constraints
    constraints: TaskConstraints = Field(default_factory=TaskConstraints)

    @model_validator(mode="after")
    def _check_identity(self) -> "TaskEnvelope":
        if self.parent_task_id is None:
            raise ValueError(
                "dispatch envelope requires parent_task_id (children report to the parent task)"
            )
        if self.attempt_id == self.task_id:
            raise ValueError("attempt_id must differ from task_id")
        if not self.acceptance_criteria:
            raise ValueError("envelope requires at least one acceptance criterion")
        return self


class TaskControl(BaseModel):
    """Mutable per-root runtime control state.

    Stored in its own ``task_controls`` record rather than on the task row, so
    recovery mutexes are DB-backed (never Python in-process locks) and fencing
    tokens survive restarts (plan §6.1).
    """

    model_config = ConfigDict(extra="forbid")

    root_task_id: str = Field(min_length=1)
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    fencing_token: int = Field(default=0, ge=0)
    run_segment_id: str = Field(min_length=1)
    graph_steps_granted: int = Field(default=0, ge=0)
    graph_steps_used: int = Field(default=0, ge=0)
    current_decision_id: str | None = None
    parent_corrections: int = Field(default=0, ge=0)
    required_action: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "TaskControl":
        if self.graph_steps_used > self.graph_steps_granted:
            raise ValueError("graph_steps_used cannot exceed graph_steps_granted")
        if self.lease_owner and self.lease_expires_at is None:
            raise ValueError("a lease owner requires an expiry")
        return self

    def lease_valid(self, now: datetime) -> bool:
        return (
            self.lease_owner is not None
            and self.lease_expires_at is not None
            and self.lease_expires_at > now
        )


class IdempotencyRecord(BaseModel):
    """Maps a stable operation key to the outcome of one logical operation.

    Used so that request retries, recovery replays and duplicate dispatches do
    not create tasks, consume budget or apply patches twice (plan §6.1, §11).
    """

    model_config = ConfigDict(extra="forbid")

    operation_key: str = Field(min_length=1)
    operation_kind: str = Field(min_length=1)
    root_task_id: str | None = None
    request_fingerprint: str = Field(min_length=1)
    response_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
