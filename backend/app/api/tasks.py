"""Task submission, query, resume, terminate and report endpoints (§12.1).

The submit endpoint mints the root task id through the controlled creation
service (never the client), freezes the uploaded input as the first immutable
snapshot, and returns 202 while a background run drives the graph.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Header, Query, Request

from app.api.deps import AppServices, services
from app.api.errors import AppError
from app.schemas.api import (
    ArtifactView,
    AttemptView,
    BudgetItem,
    BudgetStatus,
    ErrorCode,
    EventsResponse,
    ReportResponse,
    ResumeRequest,
    ResumeResponse,
    SubmitTaskRequest,
    SubmitTaskResponse,
    TaskDetailResponse,
    TaskListItem,
    TerminateRequest,
    TerminateResponse,
)
from app.schemas.enums import (
    ArtifactType,
    BudgetKind,
    CheckStatus,
    EventType,
    RootStatus,
    TaskLevel,
)
from app.schemas.results import AcceptanceContract, CheckSummary, DetectionResult, Finding
from app.services.execution_locks import request_fingerprint
from app.services.task_creation import TaskCreationService
from app.storage.repositories import ConflictError

router = APIRouter(tags=["tasks"])

REQUIRED_KINDS = ("review", "fix", "verify")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _require_key(key: str | None, op: str) -> str:
    if not key or not key.strip():
        raise AppError(ErrorCode.VALIDATION_ERROR, f"{op} 需要 Idempotency-Key 请求头")
    return key.strip()


def _replay_or_conflict(
    svc: AppServices, operation_key: str, fingerprint: str, root_task_id: str | None
) -> bool:
    """Return True when this is a replay of the same request."""
    record = svc.repos.idempotency.get(operation_key)
    if record is None:
        return False
    if record["request_fingerprint"] != fingerprint:
        raise AppError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            f"幂等键 {operation_key} 已用于不同内容的请求",
        )
    if root_task_id is not None and record["root_task_id"] not in (None, root_task_id):
        raise AppError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            f"幂等键 {operation_key} 已用于其他任务 {record['root_task_id']}",
        )
    return True


def _root_or_404(svc: AppServices, root_task_id: str):
    root = svc.repos.tasks.get(root_task_id)
    if root is None or root.task_level is not TaskLevel.ROOT:
        raise AppError(ErrorCode.NOT_FOUND, f"未知总任务 {root_task_id}")
    return root


def _budget_status(svc: AppServices, root_task_id: str, budgets) -> BudgetStatus:
    state = svc.locks.budget_state(root_task_id, budgets)
    items = [
        BudgetItem(
            budget_kind=kind,
            consumed=state[kind]["consumed"],
            granted_max=state[kind]["granted_max"],
            remaining=state[kind]["remaining"],
        )
        for kind in [k.value for k in BudgetKind]
        if kind in state
    ]
    return BudgetStatus(items=items)


def _check_summaries(
    svc: AppServices, root_task_id: str, contract: AcceptanceContract, source_version: str | None
) -> list[CheckSummary]:
    latest = (
        svc.governance.detector.current_checks(root_task_id, source_version)
        if source_version
        else {}
    )
    out: list[CheckSummary] = []
    for spec in contract.checks:
        result = latest.get(spec.check_id)
        out.append(
            CheckSummary(
                check_id=spec.check_id,
                goal_ref=spec.goal_ref,
                method=spec.method,
                required=spec.required,
                status=result.status if result else CheckStatus.NOT_RUN,
                latest_evidence_ref=(
                    result.evidence_refs[0] if result and result.evidence_refs else None
                ),
                source_version=source_version if result else None,
                note=result.reason if result else "未执行",
            )
        )
    return out


def _contract_of(svc: AppServices, root_task_id: str):
    from app.schemas.results import AcceptanceContract

    row = svc.repos.contracts.latest(root_task_id)
    if row is None:
        return None
    return AcceptanceContract.model_validate(svc.artifacts.read_json(row["artifact_id"]))


def _latest_detection(svc: AppServices, root_task_id: str) -> DetectionResult | None:
    candidates = svc.artifacts.list_by_root(root_task_id, ArtifactType.DETECTION)
    if not candidates:
        return None
    try:
        return DetectionResult.model_validate(svc.artifacts.read_json(candidates[-1].artifact_id))
    except Exception:  # noqa: BLE001 - a corrupt projection must not break the API
        return None


# --------------------------------------------------------------------------- #
# submit / list / detail
# --------------------------------------------------------------------------- #
@router.post("/tasks", response_model=SubmitTaskResponse, status_code=202)
async def submit_task(
    request: Request,
    payload: SubmitTaskRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SubmitTaskResponse:
    svc = services(request)
    key = _require_key(idempotency_key, "提交任务")
    fingerprint = request_fingerprint(payload.model_dump(mode="json"))

    existing = svc.repos.idempotency.get(key)
    if existing is not None:
        if existing["request_fingerprint"] != fingerprint:
            raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, f"幂等键 {key} 已用于不同的提交请求")
        root = _root_or_404(svc, existing["response_ref"] or "")
        return _submit_response(svc, root.task_id)

    if svc.repos.uploads.get(payload.source_id) is None:
        raise AppError(ErrorCode.NOT_FOUND, f"未知上传 source_id {payload.source_id}")
    workflow = svc.workflow(payload.workflow_version)
    if payload.check_mode is not None and payload.check_mode is not workflow.check_mode:
        raise AppError(
            ErrorCode.CONFIG_MODE_CONFLICT,
            f"工作流 {payload.workflow_version} 的模式是 {workflow.check_mode.value}",
        )
    svc.runner.ensure_model_ready()

    creator = TaskCreationService(svc.repos)
    root_task_id = creator.new_root_task_id()
    root = creator.create_root(
        goal=payload.goal,
        workflow_version=payload.workflow_version,
        root_task_id=root_task_id,
    )
    manifest = svc.workspace.publish_initial_snapshot(
        root_task_id=root_task_id, source_id=payload.source_id
    )
    source_artifact = svc.workspace.register_source_artifact(
        root_task_id=root_task_id, source_version=manifest.source_version
    )
    svc.repos.uploads.attach_root(payload.source_id, root_task_id)
    svc.repos.idempotency.put(
        operation_key=key,
        operation_kind="submit_task",
        request_fingerprint=fingerprint,
        response_ref=root_task_id,
        root_task_id=root_task_id,
    )
    svc.governance.recovery.sink(root_task_id, "task_service").emit(
        EventType.TASK_CREATED,
        payload={
            "goal": payload.goal,
            "workflow_version": payload.workflow_version,
            "source_id": payload.source_id,
            "source_version": manifest.source_version,
        },
    )
    await svc.runner.start_task(
        root_task_id=root_task_id,
        goal=payload.goal,
        workflow_version=payload.workflow_version,
        check_mode=workflow.check_mode.value,
        source_version=manifest.source_version,
        source_artifact=source_artifact,
    )
    return _submit_response(svc, root_task_id)


def _submit_response(svc: AppServices, root_task_id: str) -> SubmitTaskResponse:
    root = _root_or_404(svc, root_task_id)
    contract = _contract_of(svc, root_task_id)
    return SubmitTaskResponse(
        root_task_id=root.task_id,
        status=root.status,
        revision=root.revision,
        workflow_version=root.workflow_version,
        contract_version=contract.contract_version if contract else None,
        created_at=root.created_at or datetime.now(timezone.utc),
    )


@router.get("/tasks", response_model=list[TaskListItem])
async def list_tasks(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[TaskListItem]:
    svc = services(request)
    items: list[TaskListItem] = []
    for root in svc.repos.tasks.list_roots(limit=limit, offset=offset):
        workflow = svc.repos.workflows.get(root.workflow_version)
        if workflow is None:
            continue
        items.append(
            TaskListItem(
                root_task_id=root.task_id,
                goal=root.goal,
                status=root.status,
                passed=root.passed,
                workflow_version=root.workflow_version,
                check_mode=workflow.check_mode,
                revision=root.revision,
                created_at=root.created_at or datetime.now(timezone.utc),
                updated_at=root.updated_at or datetime.now(timezone.utc),
            )
        )
    return items


@router.get("/tasks/{root_task_id}", response_model=TaskDetailResponse)
async def task_detail(request: Request, root_task_id: str) -> TaskDetailResponse:
    svc = services(request)
    root = _root_or_404(svc, root_task_id)
    workflow = svc.workflow(root.workflow_version)
    contract = _contract_of(svc, root_task_id)
    source_version = svc.governance.recovery.current_source_version(root_task_id)
    control = svc.repos.controls.get(root_task_id)
    report_row = svc.repos.reports.latest(root_task_id, "final")
    artifacts = svc.artifacts.list_by_root(root_task_id)
    detection = _latest_detection(svc, root_task_id)
    if svc.runner.is_running(root_task_id):
        # a live background run supersedes a stale projected status
        status = RootStatus.RUNNING.value
    else:
        status = root.status
    return TaskDetailResponse(
        root_task_id=root.task_id,
        goal=root.goal,
        status=status,
        passed=root.passed,
        revision=root.revision,
        workflow_version=root.workflow_version,
        check_mode=workflow.check_mode,
        contract_version=contract.contract_version if contract else None,
        source_version=source_version,
        tasks=svc.repos.tasks.list_children(root_task_id),
        detection=detection,
        next_action=None,
        required_action=(control.required_action if control and control.required_action else None),
        error=None,
        report_ref=report_row["artifact_id"] if report_row else None,
        report_artifact_id=report_row["artifact_id"] if report_row else None,
        checks=_check_summaries(svc, root_task_id, contract, source_version) if contract else [],
        budgets=_budget_status(svc, root_task_id, workflow.budgets),
        artifact_refs=[a.artifact_id for a in artifacts],
        created_at=root.created_at or datetime.now(timezone.utc),
        updated_at=root.updated_at or datetime.now(timezone.utc),
    )


@router.get("/tasks/{root_task_id}/attempts", response_model=list[AttemptView])
async def task_attempts(
    request: Request,
    root_task_id: str,
    task_id: str | None = Query(default=None),
) -> list[AttemptView]:
    svc = services(request)
    _root_or_404(svc, root_task_id)
    views: list[AttemptView] = []
    for attempt in svc.repos.attempts.list_by_root(root_task_id):
        if task_id and attempt.task_id != task_id:
            continue
        task = svc.repos.tasks.get(attempt.task_id)
        result = svc.repos.results.get_by_attempt(attempt.attempt_id)
        duration = None
        if attempt.started_at and attempt.finished_at:
            duration = int((attempt.finished_at - attempt.started_at).total_seconds() * 1000)
        views.append(
            AttemptView(
                attempt_id=attempt.attempt_id,
                task_id=attempt.task_id,
                root_task_id=attempt.root_task_id,
                task_kind=task.task_kind if task else None,
                agent_id=task.agent_id if task else None,
                agent_version=task.agent_version if task else None,
                attempt_no=attempt.attempt_no,
                dispatch_batch_id=attempt.dispatch_batch_id,
                status=attempt.status.value,
                retry_reason=attempt.retry_reason,
                source_version=attempt.source_version,
                contract_version=attempt.contract_version,
                started_at=attempt.started_at,
                finished_at=attempt.finished_at,
                duration_ms=duration,
                input_refs=attempt.input_refs,
                result=result,
            )
        )
    return views


@router.get("/tasks/{root_task_id}/events", response_model=EventsResponse)
async def task_events(
    request: Request,
    root_task_id: str,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> EventsResponse:
    svc = services(request)
    root = _root_or_404(svc, root_task_id)
    events = svc.repos.events.list_after(root_task_id, after_seq, limit=limit)
    last = svc.repos.events.max_sequence(root_task_id)
    status = RootStatus.RUNNING.value if svc.runner.is_running(root_task_id) else root.status
    return EventsResponse(
        root_task_id=root_task_id,
        events=events,
        next_seq=events[-1].sequence if events else after_seq,
        status=status,
        passed=root.passed,
        revision=root.revision,
    )


@router.get("/tasks/{root_task_id}/artifacts", response_model=list[ArtifactView])
async def task_artifacts(
    request: Request,
    root_task_id: str,
    artifact_type: str | None = Query(default=None),
) -> list[ArtifactView]:
    svc = services(request)
    _root_or_404(svc, root_task_id)
    kind = ArtifactType(artifact_type) if artifact_type else None
    views: list[ArtifactView] = []
    for artifact in svc.artifacts.list_by_root(root_task_id, kind):
        views.append(
            ArtifactView(
                artifact_id=artifact.artifact_id,
                root_task_id=artifact.root_task_id,
                producer_attempt_id=artifact.producer_attempt_id,
                artifact_type=artifact.artifact_type,
                source_version=artifact.source_version,
                hash=artifact.hash,
                size=artifact.size,
                metadata=artifact.metadata,
                created_at=artifact.created_at,
                download_url=f"/api/artifacts/{artifact.artifact_id}",
            )
        )
    return views


@router.get("/tasks/{root_task_id}/findings", response_model=list[Finding])
async def task_findings(
    request: Request,
    root_task_id: str,
    status: str | None = Query(default=None),
) -> list[Finding]:
    """Current persisted findings: each id resolves to its newest version, not the review snapshot."""
    svc = services(request)
    _root_or_404(svc, root_task_id)
    findings = svc.repos.findings.list_current_by_root(root_task_id)
    if status:
        findings = [item for item in findings if item.status.value == status]
    return findings


# --------------------------------------------------------------------------- #
# resume / terminate / report
# --------------------------------------------------------------------------- #
@router.post("/tasks/{root_task_id}/resume", response_model=ResumeResponse)
async def resume_task(
    request: Request,
    root_task_id: str,
    payload: ResumeRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ResumeResponse:
    svc = services(request)
    key = _require_key(idempotency_key, "恢复任务")
    root = _root_or_404(svc, root_task_id)
    workflow = svc.workflow(root.workflow_version)
    svc.runner.ensure_model_ready()
    try:
        outcome = svc.governance.recovery.resume(
            root_task_id,
            payload,
            budgets=workflow.budgets,
            owner=svc.runner.owner,
            operation_key=key,
        )
    except ConflictError as exc:
        raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - map control-layer refusals to codes
        _raise_recovery_error(exc)
        raise
    if not outcome.reused:
        await svc.runner.resume_task(root_task_id)
    return ResumeResponse(
        root_task_id=root_task_id,
        status=outcome.status,
        revision=outcome.revision,
        run_segment_id=outcome.run_segment_id,
        granted=[
            BudgetItem(
                budget_kind=g["budget_kind"],
                consumed=g["consumed"],
                granted_max=g["granted_max"],
                remaining=g["remaining"],
            )
            for g in outcome.granted
        ],
        message="已受理恢复请求" + ("（重复请求，返回同一结果）" if outcome.reused else ""),
    )


@router.post("/tasks/{root_task_id}/terminate", response_model=TerminateResponse)
async def terminate_task(
    request: Request,
    root_task_id: str,
    payload: TerminateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> TerminateResponse:
    svc = services(request)
    key = _require_key(idempotency_key, "终止任务")
    _root_or_404(svc, root_task_id)
    try:
        outcome = svc.governance.recovery.terminate(
            root_task_id, payload, owner=svc.runner.owner, operation_key=key
        )
    except ConflictError as exc:
        raise AppError(ErrorCode.IDEMPOTENCY_CONFLICT, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _raise_recovery_error(exc)
        raise
    return TerminateResponse(
        root_task_id=root_task_id,
        status=outcome.status,
        passed=outcome.passed,
        revision=outcome.revision,
        reason=outcome.reason,
        skipped_tasks=outcome.skipped_tasks,
    )


@router.get("/tasks/{root_task_id}/report", response_model=ReportResponse)
async def task_report(request: Request, root_task_id: str) -> ReportResponse:
    svc = services(request)
    root = _root_or_404(svc, root_task_id)
    row = svc.repos.reports.latest(root_task_id, "final")
    if row is None or not row["artifact_id"]:
        raise AppError(ErrorCode.NOT_FOUND, "任务尚未生成最终报告")
    report = svc.artifacts.read_json(row["artifact_id"])
    return ReportResponse(
        root_task_id=root_task_id,
        final_status=row["final_status"],
        passed=row["passed"],
        conclusion_scope=report.get("conclusion_scope", ""),
        report=report,
        markdown=None,
        report_artifact_id=row["artifact_id"],
        artifact_refs=list(report.get("artifact_refs", [])),
    )


# --------------------------------------------------------------------------- #
# error mapping
# --------------------------------------------------------------------------- #
def _raise_recovery_error(exc: Exception) -> None:
    code = getattr(exc, "code", None)
    if code in ("NOT_RESUMABLE",):
        raise AppError(ErrorCode.NOT_RESUMABLE, str(exc))
    if code in ("VERSION_CONFLICT",):
        raise AppError(ErrorCode.VERSION_CONFLICT, str(exc))
    if code in ("LEASE_HELD", "ACTIVE_ATTEMPTS"):
        raise AppError(ErrorCode.CONFLICT, str(exc))
    if code in ("GRANT_OVER_CAP", "UNKNOWN_RETRY_KIND"):
        raise AppError(ErrorCode.BUDGET_EXHAUSTED, str(exc), getattr(exc, "details", {}))
    if code == "UNKNOWN_TASK":
        raise AppError(ErrorCode.NOT_FOUND, str(exc))
    raise exc
