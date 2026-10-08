"""Recovery coordination across the three persistence layers (§11.2).

The business database (dispatch/receipt/patch ledgers), the framework checkpoint
(execution position) and the file system (immutable evidence) are not one atomic
transaction. This module reconciles them:

* a run holds a DB-backed lease with a fencing token, so two processes can never
  drive the same root task and a stale executor cannot publish results;
* a patch whose files were published but whose application row was not committed
  is confirmed from the content-addressed snapshot instead of being applied twice;
* on startup, runs that lost their lease are marked ``interrupted`` with their
  orphan attempts closed, so an interrupted task can be resumed by id;
* an explicit resume checks revision, recovers the current version, and grants
  additional budget atomically (never partially).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.schemas.api import ResumeRequest, TerminateRequest
from app.schemas.artifacts import PatchApplication
from app.schemas.enums import (
    ACTIVE_ROOT_STATUSES,
    RESUMABLE_ROOT_STATUSES,
    ArtifactType,
    AttemptStatus,
    BudgetKind,
    EventType,
    PatchApplicationStatus,
    RootStatus,
    TaskStatus,
    TerminalOrigin,
    TerminalOutcome,
)
from app.schemas.results import AcceptanceContract, TerminalRecord
from app.schemas.workflows import BudgetConfig
from app.services.artifacts import ArtifactService
from app.services.events import EventContext, EventSink
from app.services.execution_locks import BUDGET_CAPS, ExecutionLockService
from app.services.task_service import TaskService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import ConflictError, Repos
from app.workflow.detection import Detector

# task_kind -> the fault-retry budget a resume may top up
EXECUTION_RETRY_BUDGET: dict[str, str] = {
    "review": BudgetKind.REVIEW_RETRY.value,
    "fix": BudgetKind.FIX_RETRY.value,
    "verify": BudgetKind.VERIFICATION_RETRY.value,
    "generate_tests": BudgetKind.GENERATE_RETRY.value,
}


class RunRightDenied(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RecoveryRejected(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class RunRight:
    """Proof that this process currently owns the root task's execution right."""

    root_task_id: str
    owner: str
    fencing_token: int
    run_segment_id: str


@dataclass
class Reconciliation:
    """The corrected current-version projection after reading the business ledger."""

    source_version: str | None
    patched: bool
    application: PatchApplication | None = None
    corrected: bool = False
    recovered_application_ids: list[str] = field(default_factory=list)


@dataclass
class ResumeOutcome:
    root_task_id: str
    status: str
    revision: int
    run_segment_id: str
    granted: list[dict[str, Any]]
    reused: bool = False


@dataclass
class TerminateOutcome:
    root_task_id: str
    status: str
    passed: bool | None
    revision: int
    reason: str
    skipped_tasks: list[str] = field(default_factory=list)
    reused: bool = False


class RecoveryCoordinator:
    def __init__(
        self,
        *,
        repos: Repos,
        workspace: WorkspaceService,
        artifacts: ArtifactService,
        locks: ExecutionLockService,
        task_service: TaskService,
        detector: Detector,
        controller: Any,
        settings: Settings,
    ) -> None:
        self.repos = repos
        self.workspace = workspace
        self.artifacts = artifacts
        self.locks = locks
        self.task_service = task_service
        self.detector = detector
        self.controller = controller
        self.settings = settings

    # ---- events ------------------------------------------------------------
    def sink(self, root_task_id: str, actor: str = "task_service") -> EventSink:
        return EventSink(
            self.repos.events, EventContext(root_task_id=root_task_id, actor_id=actor)
        )

    # ---- execution right ---------------------------------------------------
    def acquire_run_right(
        self,
        root_task_id: str,
        owner: str,
        *,
        ttl_seconds: float = 60.0,
        expected_revision: int | None = None,
        run_segment_id: str | None = None,
    ) -> RunRight:
        control = self.repos.controls.get(root_task_id)
        if control is None:
            if run_segment_id is None:
                raise RunRightDenied("NO_CONTROL_RECORD", f"缺少 task_controls 记录 {root_task_id}")
            control = self.repos.controls.ensure(
                root_task_id, run_segment_id, self.controller.settings.max_graph_steps
            )
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ok, updated = self.repos.controls.acquire_lease(
            root_task_id, owner, expires, expected_revision=expected_revision
        )
        if not ok or updated is None:
            raise RunRightDenied(
                "LEASE_HELD",
                f"任务 {root_task_id} 已有有效执行租约（owner={updated.lease_owner}）",
            )
        return RunRight(
            root_task_id=root_task_id,
            owner=owner,
            fencing_token=updated.fencing_token,
            run_segment_id=updated.run_segment_id,
        )

    def heartbeat(self, right: RunRight, *, ttl_seconds: float = 60.0) -> bool:
        return self.locks.heartbeat(right.root_task_id, right.owner, ttl_seconds)

    def release(self, right: RunRight) -> bool:
        return self.locks.release(right.root_task_id, right.owner)

    def holds_right(self, right: RunRight) -> bool:
        return self.locks.has_right(right.root_task_id, right.fencing_token, owner=right.owner)

    # ---- patch application recovery ---------------------------------------
    def reconcile_patch_applications(self, root_task_id: str) -> list[PatchApplication]:
        """Commit prepared applications whose snapshot is already published."""
        recovered: list[PatchApplication] = []
        for application in self.repos.patch_applications.list_by_root(root_task_id):
            if application.status is not PatchApplicationStatus.PREPARED:
                continue
            if not self.workspace.verify_published_application(application):
                continue
            committed = self.workspace.commit_application(application)
            self.sink(root_task_id, "controller").emit(
                EventType.PATCH_APPLIED,
                payload={
                    "application_id": committed.application_id,
                    "base_version": committed.base_version,
                    "result_version": committed.result_version,
                    "recovered": True,
                },
            )
            recovered.append(committed)
        return recovered

    def reconcile(self, root_task_id: str, current_version: str | None) -> Reconciliation:
        """Read the patch ledger and correct the current-version projection."""
        recovered = self.reconcile_patch_applications(root_task_id)
        committed = [
            a
            for a in self.repos.patch_applications.list_by_root(root_task_id)
            if a.status is PatchApplicationStatus.COMMITTED
        ]
        result = Reconciliation(
            source_version=current_version,
            patched=False,
            recovered_application_ids=[a.application_id for a in recovered],
        )
        if not committed:
            return result
        latest = committed[-1]
        result.application = latest
        result.patched = True
        result.corrected = current_version != latest.result_version
        result.source_version = latest.result_version
        return result

    # ---- startup interruption scan ----------------------------------------
    def recover_incomplete_runs(self, *, now: datetime | None = None) -> list[str]:
        """Mark runs without a valid lease as ``interrupted`` and close orphans."""
        now = now or datetime.now(timezone.utc)
        interrupted: list[str] = []
        for root in self.repos.tasks.list_roots(limit=1000):
            if root.status not in {s.value for s in ACTIVE_ROOT_STATUSES}:
                continue
            control = self.repos.controls.get(root.task_id)
            if control is not None and control.lease_valid(now):
                continue
            self._close_orphan_attempts(root.task_id, control)
            self.repos.tasks.update(
                root.task_id,
                status=RootStatus.INTERRUPTED.value,
                passed=None,
                passed_set=False,
                bump_revision=True,
            )
            self.sink(root.task_id).emit(
                EventType.TASK_INTERRUPTED,
                payload={
                    "reason": "进程中断：没有有效执行租约",
                    "recoverable": True,
                    "required_action": "确认后请求恢复原任务",
                },
            )
            interrupted.append(root.task_id)
        return interrupted

    def _close_orphan_attempts(self, root_task_id: str, control: Any) -> None:
        token = control.fencing_token if control is not None else 0
        for attempt in self.repos.attempts.list_active(root_task_id):
            # a controller terminal must carry evidence, so an interruption records
            # why the attempt was closed instead of leaving an unexplained failure
            error_ref = self.artifacts.save_json(
                root_task_id=root_task_id,
                artifact_type=ArtifactType.EVIDENCE,
                data={
                    "code": "RUN_INTERRUPTED",
                    "category": "interruption",
                    "message": "进程中断，尝试未结束",
                    "attempt_id": attempt.attempt_id,
                },
                source_version=attempt.source_version,
                name=f"interrupted-{attempt.attempt_id}",
            ).artifact_id
            with self.repos.db.transaction() as conn:
                terminal, created = self.repos.terminals.record(
                    TerminalRecord(
                        attempt_id=attempt.attempt_id,
                        dispatch_batch_id=attempt.dispatch_batch_id,
                        origin=TerminalOrigin.CONTROLLER,
                        outcome=TerminalOutcome.FAILED,
                        error_ref=error_ref,
                        fencing_token=token,
                        operation_key=f"terminal:{attempt.attempt_id}",
                    ),
                    conn=conn,
                )
                if not created:
                    continue
                self.repos.attempts.update_status(
                    attempt.attempt_id,
                    AttemptStatus.INTERRUPTED,
                    error="进程中断，尝试未结束",
                    finished_at=datetime.now(timezone.utc),
                    conn=conn,
                )
                if attempt.dispatch_batch_id:
                    self.repos.batches.add_received(
                        attempt.dispatch_batch_id,
                        attempt.attempt_id,
                        TerminalOrigin.CONTROLLER,
                        conn=conn,
                    )
                self.repos.tasks.update(
                    attempt.task_id, status=TaskStatus.FAILED.value, conn=conn
                )
            self.sink(root_task_id, "controller").emit(
                EventType.TERMINAL_RECORDED,
                task_id=attempt.task_id,
                attempt_id=attempt.attempt_id,
                dispatch_batch_id=attempt.dispatch_batch_id,
                payload={
                    "origin": "controller",
                    "outcome": "failed",
                    "code": "PROCESS_INTERRUPTED",
                    "message": "进程中断，尝试被控制层失效",
                },
            )

    # ---- resume ------------------------------------------------------------
    def can_resume(self, root_task_id: str) -> list[str]:
        root = self.repos.tasks.get(root_task_id)
        if root is None:
            return [f"未知总任务 {root_task_id}"]
        problems: list[str] = []
        if root.status not in {s.value for s in RESUMABLE_ROOT_STATUSES}:
            problems.append(
                f"任务状态 {root.status} 不可恢复（仅 waiting_recovery/interrupted）"
            )
        control = self.repos.controls.get(root_task_id)
        if control is not None and control.lease_valid(datetime.now(timezone.utc)):
            problems.append(f"任务已有有效执行租约（owner={control.lease_owner}）")
        return problems

    def resume(
        self,
        root_task_id: str,
        request: ResumeRequest,
        *,
        budgets: BudgetConfig,
        owner: str,
        operation_key: str,
        fingerprint: str | None = None,
        ttl_seconds: float = 60.0,
    ) -> ResumeOutcome:
        grants = self._planned_grants(request)
        fingerprint = fingerprint or _resume_fingerprint(request)

        with self.repos.db.transaction() as conn:
            root = self.repos.tasks.get(root_task_id, conn=conn)
            if root is None:
                raise RecoveryRejected("UNKNOWN_TASK", f"未知总任务 {root_task_id}")

            record = self.repos.idempotency.get(operation_key, conn=conn)
            if record is not None:
                if record["request_fingerprint"] != fingerprint:
                    raise ConflictError(
                        f"幂等键 {operation_key} 已用于不同的恢复请求"
                    )
                control = self.repos.controls.get(root_task_id, conn=conn)
                return ResumeOutcome(
                    root_task_id=root_task_id,
                    status=root.status,
                    revision=root.revision,
                    run_segment_id=control.run_segment_id if control else "",
                    granted=self._granted_items(root_task_id, budgets, list(grants)),
                    reused=True,
                )

            if root.status not in {s.value for s in RESUMABLE_ROOT_STATUSES}:
                raise RecoveryRejected(
                    "NOT_RESUMABLE",
                    f"任务状态 {root.status} 不可恢复（仅 waiting_recovery/interrupted）",
                )
            if root.revision != request.expected_revision:
                raise RecoveryRejected(
                    "VERSION_CONFLICT",
                    f"任务已更新：期望 revision {request.expected_revision}，当前 {root.revision}",
                )

            control = self.repos.controls.get(root_task_id, conn=conn)
            if control is None:
                control = self.repos.controls.ensure(
                    root_task_id, f"seg-{root_task_id}", self.settings.max_graph_steps
                )
            now = datetime.now(timezone.utc)
            if control.lease_valid(now) and control.lease_owner != owner:
                raise RecoveryRejected(
                    "LEASE_HELD", f"任务已有有效执行租约（owner={control.lease_owner}）"
                )

            expires = now + timedelta(seconds=ttl_seconds)
            ok, updated = self.repos.controls.acquire_lease(
                root_task_id, owner, expires, conn=conn
            )
            if not ok or updated is None:
                raise RecoveryRejected("LEASE_HELD", "取得执行权失败：已有有效执行租约")

            # every grant is checked against the server cap before any is written
            for kind, delta in grants:
                self._assert_grant_allowed(root_task_id, kind, delta, budgets, conn=conn)

            new_segment = f"seg-{updated.fencing_token}"
            self.repos.controls.update(
                root_task_id,
                run_segment_id=new_segment,
                add_graph_steps=budgets.max_graph_steps,
                required_action="",
                conn=conn,
            )
            for kind, delta in grants:
                self.locks.grant(
                    root_task_id,
                    kind,
                    delta,
                    operation_key=f"resume-grant:{root_task_id}:{new_segment}:{kind}",
                    reason=request.reason,
                    budgets=budgets,
                    conn=conn,
                )
            revision = self.repos.tasks.update(
                root_task_id, status=RootStatus.RUNNING.value, conn=conn
            )
            self.repos.idempotency.put(
                operation_key=operation_key,
                operation_kind="resume_task",
                request_fingerprint=fingerprint,
                response_ref=new_segment,
                root_task_id=root_task_id,
                conn=conn,
            )

        self.sink(root_task_id).emit(
            EventType.TASK_RESUMED,
            payload={
                "run_segment_id": new_segment,
                "reason": request.reason,
                "granted": {kind: delta for kind, delta in grants},
            },
        )
        return ResumeOutcome(
            root_task_id=root_task_id,
            status=RootStatus.RUNNING.value,
            revision=revision,
            run_segment_id=new_segment,
            granted=self._granted_items(root_task_id, budgets, list(grants)),
        )

    # ---- terminate ---------------------------------------------------------
    def terminate(
        self,
        root_task_id: str,
        request: TerminateRequest,
        *,
        owner: str,
        operation_key: str,
        fingerprint: str | None = None,
    ) -> TerminateOutcome:
        fingerprint = fingerprint or _terminate_fingerprint(request)
        with self.repos.db.transaction() as conn:
            root = self.repos.tasks.get(root_task_id, conn=conn)
            if root is None:
                raise RecoveryRejected("UNKNOWN_TASK", f"未知总任务 {root_task_id}")

            record = self.repos.idempotency.get(operation_key, conn=conn)
            if record is not None:
                if record["request_fingerprint"] != fingerprint:
                    raise ConflictError(f"幂等键 {operation_key} 已用于不同的终止请求")
                return TerminateOutcome(
                    root_task_id=root_task_id,
                    status=root.status,
                    passed=root.passed,
                    revision=root.revision,
                    reason=request.reason,
                    reused=True,
                )

            if root.status not in {s.value for s in RESUMABLE_ROOT_STATUSES}:
                raise RecoveryRejected(
                    "NOT_RESUMABLE",
                    f"仅 waiting_recovery/interrupted 可终止，当前 {root.status}",
                )
            if root.revision != request.expected_revision:
                raise RecoveryRejected(
                    "VERSION_CONFLICT",
                    f"任务已更新：期望 revision {request.expected_revision}，当前 {root.revision}",
                )
            control = self.repos.controls.get(root_task_id, conn=conn)
            if control is not None and control.lease_valid(datetime.now(timezone.utc)):
                raise RecoveryRejected(
                    "LEASE_HELD", f"任务仍在执行中（owner={control.lease_owner}），不能终止"
                )
            active = self.repos.attempts.list_active(root_task_id, conn=conn)
            if active:
                raise RecoveryRejected(
                    "ACTIVE_ATTEMPTS", f"仍有 {len(active)} 个未结束的执行尝试"
                )

            passed = self._verdict_for_termination(root_task_id, conn=conn)
            # insufficient evidence stays null and is projected as partial, never
            # as a false "passed"; a confirmed failing required item stays false.
            final_status = (
                RootStatus.PARTIAL.value if passed is None else RootStatus.COMPLETED.value
            )

            revision = self.repos.tasks.update(
                root_task_id,
                status=final_status,
                passed=passed,
                passed_set=True,
                bump_revision=True,
                conn=conn,
            )
            self.repos.idempotency.put(
                operation_key=operation_key,
                operation_kind="terminate_task",
                request_fingerprint=fingerprint,
                response_ref=final_status,
                root_task_id=root_task_id,
                conn=conn,
            )
            self.repos.controls.update(root_task_id, required_action="", conn=conn)

        skipped = self.controller.finalize_skips(root_task_id=root_task_id, passed=passed)
        self.sink(root_task_id).emit(
            EventType.TASK_INTERRUPTED,
            payload={
                "terminated": True,
                "reason": request.reason,
                "final_status": final_status,
                "passed": passed,
            },
        )
        return TerminateOutcome(
            root_task_id=root_task_id,
            status=final_status,
            passed=passed,
            revision=revision,
            reason=request.reason,
            skipped_tasks=[s["task_id"] for s in skipped],
        )

    # ---- helpers -----------------------------------------------------------
    def _planned_grants(self, request: ResumeRequest) -> list[tuple[str, int]]:
        grants: list[tuple[str, int]] = []
        if request.additional_repair_rounds:
            grants.append((BudgetKind.REPAIR_ROUND.value, request.additional_repair_rounds))
        for task_kind, count in request.additional_execution_retries.items():
            if count <= 0:
                continue
            kind = EXECUTION_RETRY_BUDGET.get(task_kind)
            if kind is None:
                raise RecoveryRejected(
                    "UNKNOWN_RETRY_KIND",
                    f"未注册重试预算策略的 task_kind：{task_kind!r}",
                    details={"allowed": sorted(EXECUTION_RETRY_BUDGET)},
                )
            grants.append((kind, count))
        if request.additional_evidence_retries:
            grants.append((BudgetKind.EVIDENCE_RETRY.value, request.additional_evidence_retries))
        return grants

    def _assert_grant_allowed(
        self, root_task_id: str, kind: str, delta: int, budgets: BudgetConfig, *, conn=None
    ) -> None:  # type: ignore[no-untyped-def]
        current = self.locks.budget_state(root_task_id, budgets)[kind]["granted_max"]
        if current + delta > BUDGET_CAPS[kind]:
            raise RecoveryRejected(
                "GRANT_OVER_CAP",
                f"{kind}: 当前上限 {current} + {delta} 超过服务端上限 {BUDGET_CAPS[kind]}",
                details={"budget_kind": kind},
            )

    def _granted_items(
        self, root_task_id: str, budgets: BudgetConfig, kinds: list[tuple[str, int]]
    ) -> list[dict[str, Any]]:
        state = self.locks.budget_state(root_task_id, budgets)
        items: list[dict[str, Any]] = []
        for kind, delta in kinds:
            entry = dict(state[kind])
            entry["budget_kind"] = kind
            entry["delta"] = delta
            items.append(entry)
        return items

    def current_source_version(self, root_task_id: str) -> str | None:
        """The version the ledger says is current: latest committed patch, else last attempt."""
        committed = [
            a
            for a in self.repos.patch_applications.list_by_root(root_task_id)
            if a.status is PatchApplicationStatus.COMMITTED
        ]
        if committed:
            return committed[-1].result_version
        attempts = self.repos.attempts.list_by_root(root_task_id)
        if attempts:
            return attempts[-1].source_version
        return None

    def _verdict_for_termination(self, root_task_id: str, *, conn=None) -> bool | None:  # type: ignore[no-untyped-def]
        row = self.repos.contracts.latest(root_task_id, conn=conn)
        if row is None:
            return None
        contract = AcceptanceContract.model_validate(self.artifacts.read_json(row["artifact_id"]))
        source_version = self.current_source_version(root_task_id)
        if source_version is None:
            return None
        patched = any(
            a.status is PatchApplicationStatus.COMMITTED
            for a in self.repos.patch_applications.list_by_root(root_task_id)
        )
        required_ids = self.detector.required_check_ids(contract, patched=patched)
        _can_finish, passed, _problems, _unresolved = self.task_service.detect_completion(
            root_task_id=root_task_id,
            source_version=source_version,
            required_check_ids=required_ids,
        )
        return passed


def _resume_fingerprint(request: ResumeRequest) -> str:
    from app.services.execution_locks import request_fingerprint

    return request_fingerprint(request.model_dump(mode="json"))


def _terminate_fingerprint(request: TerminateRequest) -> str:
    from app.services.execution_locks import request_fingerprint

    return request_fingerprint(request.model_dump(mode="json"))
