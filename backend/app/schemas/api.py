"""API envelopes: unified errors, pagination and the HTTP request/response models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.artifacts import ArtifactType
from app.schemas.common import ErrorInfo
from app.schemas.enums import CheckMode
from app.schemas.events import ExecutionEvent
from app.schemas.results import CheckSummary, DetectionResult, TaskResult
from app.schemas.tasks import Task
from app.schemas.workflows import WorkflowConfig

T = TypeVar("T")


class ErrorCode(str, Enum):
    """Stable machine-readable error codes shared by every endpoint."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    CONFIG_MODE_CONFLICT = "CONFIG_MODE_CONFLICT"
    CONFIG_DEFAULT_FROZEN = "CONFIG_DEFAULT_FROZEN"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CONFLICT = "CONFLICT"
    NOT_RECOVERABLE = "NOT_RECOVERABLE"
    NOT_RESUMABLE = "NOT_RESUMABLE"
    NOT_TERMINABLE = "NOT_TERMINABLE"
    MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"
    MODEL_REQUEST_FAILED = "MODEL_REQUEST_FAILED"
    TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorResponse(BaseModel):
    code: ErrorCode
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class Page(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #


class SourceFileView(BaseModel):
    path: str
    size: int
    sha256: str


class SourceUploadResponse(BaseModel):
    source_id: str
    files: list[SourceFileView]
    total_bytes: int
    content_digest: str
    created_at: datetime
    artifact_id: str | None = None


# --------------------------------------------------------------------------- #
# workflows
# --------------------------------------------------------------------------- #


class WorkflowSaveResponse(BaseModel):
    workflow_version: str
    semantic_hash: str
    check_mode: CheckMode
    config: WorkflowConfig


class WorkflowListItem(BaseModel):
    workflow_version: str
    semantic_hash: str
    check_mode: CheckMode
    created_at: datetime


class TemplateListing(BaseModel):
    """A shipped template the canvas can start from (§14.1)."""

    name: str
    description: str
    check_mode: CheckMode
    config: WorkflowConfig


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #


class SubmitTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    workflow_version: str = Field(min_length=1)
    check_mode: CheckMode | None = None


class SubmitTaskResponse(BaseModel):
    root_task_id: str
    status: str
    revision: int
    workflow_version: str
    contract_version: str | None = None
    created_at: datetime


class BudgetItem(BaseModel):
    budget_kind: str
    consumed: int
    granted_max: int
    remaining: int


class BudgetStatus(BaseModel):
    items: list[BudgetItem] = Field(default_factory=list)


class TaskListItem(BaseModel):
    root_task_id: str
    goal: str
    status: str
    passed: bool | None
    workflow_version: str
    check_mode: CheckMode
    revision: int
    created_at: datetime
    updated_at: datetime


class AttemptView(BaseModel):
    attempt_id: str
    task_id: str
    root_task_id: str
    task_kind: str | None
    agent_id: str | None
    agent_version: str | None
    attempt_no: int
    dispatch_batch_id: str | None
    status: str
    retry_reason: str | None
    source_version: str
    contract_version: str | None
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    input_refs: dict[str, str] = Field(default_factory=dict)
    result: TaskResult | None = None


class TaskDetailResponse(BaseModel):
    root_task_id: str
    goal: str
    status: str
    passed: bool | None
    revision: int
    workflow_version: str
    check_mode: CheckMode
    contract_version: str | None
    source_version: str | None
    tasks: list[Task]
    detection: DetectionResult | None = None
    next_action: str | None = None
    required_action: str | None = None
    error: ErrorInfo | None = None
    report_ref: str | None = None
    report_artifact_id: str | None = None
    checks: list[CheckSummary] = Field(default_factory=list)
    budgets: BudgetStatus = Field(default_factory=BudgetStatus)
    artifact_refs: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class EventsResponse(BaseModel):
    root_task_id: str
    events: list[ExecutionEvent]
    next_seq: int
    status: str
    passed: bool | None = None
    revision: int


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    additional_repair_rounds: int = Field(default=0, ge=0)
    additional_execution_retries: dict[str, int] = Field(default_factory=dict)
    additional_evidence_retries: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1)


class ResumeResponse(BaseModel):
    root_task_id: str
    status: str
    revision: int
    run_segment_id: str
    granted: list[BudgetItem]
    message: str


class TerminateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    reason: str = Field(min_length=1)


class TerminateResponse(BaseModel):
    root_task_id: str
    status: str
    passed: bool | None
    revision: int
    reason: str
    skipped_tasks: list[str] = Field(default_factory=list)


class ArtifactView(BaseModel):
    artifact_id: str
    root_task_id: str
    producer_attempt_id: str | None
    artifact_type: ArtifactType
    source_version: str | None
    hash: str
    size: int
    metadata: dict = Field(default_factory=dict)
    created_at: datetime
    download_url: str


class ReportResponse(BaseModel):
    root_task_id: str
    final_status: str
    passed: bool | None
    conclusion_scope: str
    report: dict[str, Any] = Field(default_factory=dict)
    markdown: str | None = None
    report_artifact_id: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
