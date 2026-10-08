"""Task service: dispatch registration, report receipt and completion detection.

This is the deterministic control layer that owns the parent's state updates. It
registers a batch, its attempts and the budget consumption in *one* business
transaction (so a budget shortfall leaves no attempt behind), accepts exactly one
terminal per attempt, and re-derives the task verdict from persisted evidence
rather than trusting a model's claim (§8.4).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.schemas.actions import DispatchItem
from app.schemas.common import TaskConstraints, TaskDep
from app.schemas.enums import (
    AttemptStatus,
    BudgetKind,
    CheckStatus,
    EventType,
    ResultStatus,
    RetryReason,
    TaskKind,
    TaskStatus,
    TerminalOrigin,
    TerminalOutcome,
)
from app.schemas.results import CheckResult, TaskResult, TerminalRecord
from app.schemas.tasks import Attempt, Task, TaskEnvelope
from app.schemas.workflows import WorkflowConfig
from app.services.events import EventSink
from app.services.execution_locks import (
    TASK_KIND_FAULT_BUDGET,
    BudgetExhausted,
    ExecutionLockService,
)
from app.storage.repositories import Repos


class DispatchRejected(RuntimeError):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class ReceiptRejected(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TaskService:
    def __init__(
        self,
        repos: Repos,
        locks: ExecutionLockService,
        sink_factory=None,  # type: ignore[no-untyped-def]
    ) -> None:
        self.repos = repos
        self.locks = locks
        self.sink_factory = sink_factory

    # ---- helpers -----------------------------------------------------------
    def _sink(self, root_task_id: str, actor: str) -> EventSink:
        from app.services.events import EventContext

        return EventSink(self.repos.events, EventContext(root_task_id=root_task_id, actor_id=actor))

    # ---- dispatch ----------------------------------------------------------
    def register_dispatch(
        self,
        *,
        root_task_id: str,
        items: list[DispatchItem],
        workflow: WorkflowConfig,
        source_version: str,
        contract_version: str,
        retry_reason: str = RetryReason.INITIAL.value,
        retry_reasons: list[str] | None = None,
        budget_kinds: dict[str, list[str]] | None = None,
        operation_key: str | None = None,
        fencing_token: int = 0,
    ) -> tuple[str, list[Attempt]]:
        """Create the batch, its attempts and the budget entries atomically."""
        if not items:
            raise DispatchRejected("EMPTY_DISPATCH", "派发列表为空")
        if retry_reasons is not None and len(retry_reasons) != len(items):
            raise DispatchRejected("BAD_DISPATCH", "retry_reasons 必须与派发项一一对应")
        budgets = workflow.budgets
        batch_id = None
        attempts: list[Attempt] = []

        with self.repos.db.transaction() as conn:
            from app.services.execution_context import check_execution
            check_execution(conn, root_task_id, fencing_token)
            if operation_key:
                recorded = self.repos.idempotency.get(operation_key, conn=conn)
                if recorded:
                    return recorded["response_ref"], self.repos.attempts.list_by_batch(recorded["response_ref"], conn=conn)
            root = self.repos.tasks.get(root_task_id, conn=conn)
            if root is None:
                raise DispatchRejected("UNKNOWN_ROOT", f"未知总任务 {root_task_id}")

            batch_id = self._next_batch_id(root_task_id, conn=conn)
            planned: list[tuple[DispatchItem, Task, str, int, str]] = []
            for index, item in enumerate(items):
                task = self._resolve_task(root, item, conn=conn)
                self._check_dependencies(root, task, item, conn=conn)
                attempt_no = self.repos.attempts.next_attempt_no(task.task_id, conn=conn)
                attempt_id = f"{task.task_id}-A{attempt_no}"
                reason = retry_reasons[index] if retry_reasons else retry_reason
                planned.append((item, task, attempt_id, attempt_no, reason))

            self.repos.batches.create(
                batch_id, root_task_id, [a for _, _, a, _, _ in planned], conn=conn
            )

            for item, task, attempt_id, attempt_no, reason in planned:
                kinds = (budget_kinds or {}).get(item.task_kind) or self._budgets_for(
                    item.task_kind, reason
                )
                if kinds:
                    self.locks.ensure_available(root_task_id, kinds, budgets, conn=conn)
                attempt = Attempt(
                    attempt_id=attempt_id,
                    task_id=task.task_id,
                    root_task_id=root_task_id,
                    dispatch_batch_id=batch_id,
                    attempt_no=attempt_no,
                    source_version=source_version,
                    contract_version=contract_version,
                    input_refs=item.input_refs.as_mapping(),
                    constraints=item.constraints
                    or TaskConstraints(
                        max_tool_steps=budgets.max_tool_steps,
                        timeout_seconds=budgets.attempt_timeout_seconds,
                    ),
                    retry_reason=reason,
                    status=AttemptStatus.QUEUED,
                    fencing_token=fencing_token,
                )
                self.repos.attempts.insert(attempt, conn=conn)
                for kind in kinds:
                    self.locks.consume(
                        root_task_id,
                        kind,
                        operation_key=f"consume:{kind}:{attempt_id}",
                        reason=f"派发 {item.task_kind} attempt（{reason}）",
                        conn=conn,
                    )
                self.repos.tasks.update(
                    task.task_id,
                    status=TaskStatus.QUEUED.value,
                    bump_revision=True,
                    conn=conn,
                )
                attempts.append(attempt)
            if operation_key:
                self.repos.idempotency.put(operation_key=operation_key, operation_kind="dispatch", request_fingerprint=operation_key, response_ref=batch_id, root_task_id=root_task_id, conn=conn)

        for attempt, (item, _task, _aid, _no, reason) in zip(attempts, planned):
            self._sink(root_task_id, "parent").emit(
                EventType.TASK_DISPATCHED,
                task_id=attempt.task_id,
                attempt_id=attempt.attempt_id,
                dispatch_batch_id=batch_id,
                payload={
                    "agent_id": item.agent_id,
                    "task_kind": item.task_kind,
                    "reason": item.reason,
                    "retry_reason": reason,
                },
            )
        return batch_id, attempts

    def _next_batch_id(self, root_task_id: str, conn=None) -> str:  # type: ignore[no-untyped-def]
        existing = self.repos.batches.list_by_root(root_task_id, conn=conn)
        return f"{root_task_id}-B{len(existing) + 1}"

    def _budgets_for(self, task_kind: str, retry_reason: str) -> list[str]:
        """Budget kinds consumed when an attempt is registered (§9.4)."""
        if task_kind == TaskKind.FIX.value:
            # every new fix attempt consumes a repair round
            kinds = [BudgetKind.REPAIR_ROUND.value]
            if retry_reason == RetryReason.EXECUTION_FAULT.value:
                kinds.append(BudgetKind.FIX_RETRY.value)
            return kinds
        if retry_reason == RetryReason.EXECUTION_FAULT.value:
            fault = TASK_KIND_FAULT_BUDGET.get(task_kind)
            if fault is None:
                # A task kind without a declared fault budget must never be
                # re-dispatched on an unlimited budget (§5, §14.2).
                raise DispatchRejected(
                    "UNKNOWN_RETRY_BUDGET",
                    f"任务类型 {task_kind!r} 未声明故障重试预算，拒绝以无限额度重派",
                    {"task_kind": task_kind, "allowed": sorted(TASK_KIND_FAULT_BUDGET)},
                )
            return [fault]
        if retry_reason == RetryReason.INSUFFICIENT_EVIDENCE.value:
            return [BudgetKind.EVIDENCE_RETRY.value]
        return []

    def _resolve_task(self, root: Task, item: DispatchItem, conn=None) -> Task:  # type: ignore[no-untyped-def]
        if item.task_id:
            task = self.repos.tasks.get(item.task_id, conn=conn)
            if task is None or task.root_task_id != root.task_id:
                raise DispatchRejected("UNKNOWN_TASK", f"未知子任务 {item.task_id}")
            if task.agent_id != item.agent_id or task.task_kind != item.task_kind:
                raise DispatchRejected(
                    "ROLE_MISMATCH",
                    f"任务 {task.task_id} 是 {task.agent_id}/{task.task_kind}，"
                    f"不能用 {item.agent_id}/{item.task_kind} 执行",
                )
            return task

        from app.services.task_creation import TaskCreationService

        creator = TaskCreationService(self.repos)
        return creator.create_child(
            root,
            agent_id=item.agent_id,
            agent_version=_version_of(root, item.agent_id),
            task_kind=item.task_kind,
            goal=item.goal,
            depends_on=item.depends_on,
            status=TaskStatus.BLOCKED.value,
            conn=conn,
        )

    def _check_dependencies(self, root: Task, task: Task, item: DispatchItem, conn=None) -> None:  # type: ignore[no-untyped-def]
        problems = self._dependency_problems(
            root.task_id, item.depends_on or task.depends_on, conn=conn
        )
        if problems:
            raise DispatchRejected("DEPENDENCY_UNSATISFIED", "; ".join(problems))

    def _dependency_problems(
        self, root_task_id: str, deps: list[TaskDep], conn=None  # type: ignore[no-untyped-def]
    ) -> list[str]:
        problems: list[str] = []
        for dep in deps:
            dep_task = self.repos.tasks.get(dep.task_id, conn=conn)
            if dep_task is None or dep_task.root_task_id != root_task_id:
                problems.append(f"依赖任务 {dep.task_id} 不存在")
                continue
            if dep.condition == "patch_applied" and dep.attempt_id:
                application = self.find_application(root_task_id, dep.attempt_id, conn=conn)
                if application is None or application.status.value != "committed":
                    problems.append(f"依赖的补丁应用 {dep.attempt_id} 尚未提交")
            elif dep_task.status not in (
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.SKIPPED.value,
            ):
                problems.append(f"依赖任务 {dep.task_id} 尚未结束（当前 {dep_task.status}）")
        return problems

    def precheck_dispatch(
        self,
        *,
        root_task_id: str,
        items: list[DispatchItem],
        workflow: WorkflowConfig,
        retry_reason: str = RetryReason.INITIAL.value,
        budget_kinds: dict[str, list[str]] | None = None,
    ) -> list[str]:
        """Dry-run the dispatch validation without creating anything.

        The same rules are re-checked authoritatively inside the dispatch
        transaction; this lets the parent correct an illegal action instead of
        crashing on it.
        """
        root = self.repos.tasks.get(root_task_id)
        if root is None:
            return [f"未知总任务 {root_task_id}"]
        problems: list[str] = []
        for item in items:
            deps = item.depends_on
            if item.task_id:
                task = self.repos.tasks.get(item.task_id)
                if task is None or task.root_task_id != root_task_id:
                    problems.append(f"未知子任务 {item.task_id}")
                    continue
                if task.agent_id != item.agent_id or task.task_kind != item.task_kind:
                    problems.append(
                        f"任务 {task.task_id} 的角色/类型与派发 {item.agent_id}/{item.task_kind} 不一致"
                    )
                    continue
                if not deps:
                    deps = task.depends_on
            problems.extend(self._dependency_problems(root_task_id, deps))
            try:
                kinds = (budget_kinds or {}).get(item.task_kind) or self._budgets_for(
                    item.task_kind, retry_reason
                )
            except DispatchRejected as exc:
                problems.append(exc.message)
                continue
            if kinds:
                try:
                    self.locks.ensure_available(root_task_id, kinds, workflow.budgets)
                except BudgetExhausted as exc:
                    problems.append(
                        f"{item.task_kind} 额度不足，需要追加 {exc.required_additions}"
                    )
        return problems

    def find_application(self, root_task_id: str, repair_attempt_id: str, conn=None):  # type: ignore[no-untyped-def]
        for application in self.repos.patch_applications.list_by_root(root_task_id, conn=conn):
            if application.repair_attempt_id == repair_attempt_id:
                return application
        return None

    # ---- receipt -----------------------------------------------------------
    def receive_result(self, envelope: TaskEnvelope, result: TaskResult) -> TerminalRecord:
        """Validate identity, persist the single accepted report and its terminal."""
        attempt = self.repos.attempts.get(envelope.attempt_id)
        if attempt is None:
            raise ReceiptRejected("UNKNOWN_ATTEMPT", f"未知执行尝试 {envelope.attempt_id}")

        problems = _identity_problems(envelope, result)
        if problems:
            self.repos.events.append(
                _audit_event(envelope, problems),
            )
            raise ReceiptRejected("INVALID_RESULT", "; ".join(problems))

        if attempt.status is AttemptStatus.INVALIDATED:
            self.repos.events.append(_audit_event(envelope, ["attempt 已失效，迟到回报仅作审计"]))
            raise ReceiptRejected("ATTEMPT_INVALIDATED", "attempt 已失效，回报不参与判定")

        with self.repos.db.transaction() as conn:
            from app.services.execution_context import check_execution, ExecutionFenced
            try:
                check_execution(conn, envelope.root_task_id, envelope.fencing_token)
            except ExecutionFenced as exc:
                raise ReceiptRejected("STALE_FENCING_TOKEN", str(exc)) from exc
            persisted = self.repos.attempts.get(envelope.attempt_id, conn=conn)
            if persisted is None or persisted.fencing_token != envelope.fencing_token:
                raise ReceiptRejected("STALE_FENCING_TOKEN", "派发令牌与登记尝试不一致")
            stored, created = self.repos.results.insert(result, conn=conn)
            if not created:
                terminal = self.repos.terminals.get(envelope.attempt_id, conn=conn)
                if terminal is None:
                    terminal, _ = self.repos.terminals.record(
                        TerminalRecord(
                            attempt_id=envelope.attempt_id,
                            dispatch_batch_id=envelope.dispatch_batch_id,
                            origin=TerminalOrigin.AGENT,
                            outcome=TerminalOutcome.COMPLETED
                            if result.status is ResultStatus.COMPLETED
                            else TerminalOutcome.FAILED,
                            result_id=stored.result_id,
                            operation_key=f"terminal:{envelope.attempt_id}",
                        ),
                        conn=conn,
                    )
                return terminal

            terminal, _ = self.repos.terminals.record(
                TerminalRecord(
                    attempt_id=envelope.attempt_id,
                    dispatch_batch_id=envelope.dispatch_batch_id,
                    origin=TerminalOrigin.AGENT,
                    outcome=TerminalOutcome.COMPLETED
                    if result.status is ResultStatus.COMPLETED
                    else TerminalOutcome.FAILED,
                    result_id=stored.result_id,
                    operation_key=f"terminal:{envelope.attempt_id}",
                ),
                conn=conn,
            )
            self.repos.batches.add_received(
                envelope.dispatch_batch_id, envelope.attempt_id, TerminalOrigin.AGENT, conn=conn
            )
            self.repos.attempts.update_status(
                envelope.attempt_id,
                AttemptStatus.COMPLETED
                if result.status is ResultStatus.COMPLETED
                else AttemptStatus.FAILED,
                error=result.error.message if result.error else None,
                finished_at=result.finished_at,
                conn=conn,
            )
            self.repos.tasks.update(
                envelope.task_id,
                status=TaskStatus.COMPLETED.value
                if result.status is ResultStatus.COMPLETED
                else TaskStatus.FAILED.value,
                passed=result.passed,
                passed_set=True,
                conn=conn,
            )

        self.repos.events.append(
            _result_event(envelope, stored),
        )
        return terminal

    def record_controller_terminal(
        self,
        *,
        envelope: TaskEnvelope,
        code: str,
        message: str,
        fencing_token: int,
        error_ref: str | None = None,
    ) -> TerminalRecord:
        """Controller-side terminal (timeout/interruption): always ``failed``."""
        with self.repos.db.transaction() as conn:
            current = self.repos.controls.get(envelope.root_task_id, conn=conn)
            if current is not None and current.fencing_token != fencing_token:
                raise ReceiptRejected(
                    "STALE_FENCING_TOKEN",
                    f"失效的执行权 {fencing_token}（当前 {current.fencing_token}）",
                )
            terminal, created = self.repos.terminals.record(
                TerminalRecord(
                    attempt_id=envelope.attempt_id,
                    dispatch_batch_id=envelope.dispatch_batch_id,
                    origin=TerminalOrigin.CONTROLLER,
                    outcome=TerminalOutcome.FAILED,
                    error_ref=error_ref,
                    fencing_token=fencing_token,
                    operation_key=f"terminal:{envelope.attempt_id}",
                ),
                conn=conn,
            )
            if created:
                self.repos.attempts.update_status(
                    envelope.attempt_id,
                    AttemptStatus.INVALIDATED,
                    error=f"{code}: {message}",
                    finished_at=datetime.now(timezone.utc),
                    conn=conn,
                )
                self.repos.batches.add_received(
                    envelope.dispatch_batch_id,
                    envelope.attempt_id,
                    TerminalOrigin.CONTROLLER,
                    conn=conn,
                )
                self.repos.tasks.update(
                    envelope.task_id, status=TaskStatus.FAILED.value, conn=conn
                )
        if created:
            self._sink(envelope.root_task_id, "controller").emit(
                EventType.TERMINAL_RECORDED,
                task_id=envelope.task_id,
                attempt_id=envelope.attempt_id,
                dispatch_batch_id=envelope.dispatch_batch_id,
                payload={"origin": "controller", "outcome": "failed", "code": code, "message": message},
            )
        return terminal

    # ---- completion detection ---------------------------------------------
    def detect_completion(
        self,
        *,
        root_task_id: str,
        source_version: str,
        required_check_ids: list[str],
    ) -> tuple[bool, bool | None, list[str], list[str]]:
        """Return ``(can_finish, passed, problems, unresolved_required)``.

        The verdict is recomputed from persisted, current-version evidence; the
        model's ``finish`` proposal is only a suggestion (§8.4).
        """
        problems: list[str] = []
        unresolved: list[str] = []

        active = self.repos.attempts.list_active(root_task_id)
        if active:
            problems.append(f"仍有 {len(active)} 个未结束的执行尝试")

        batches = self.repos.batches.list_by_root(root_task_id)
        for batch in batches:
            if batch and batch["status"] != "complete":
                problems.append(f"批次 {batch['dispatch_batch_id']} 尚未收齐有效终态")

        results = self.repos.check_results.list_for_version(root_task_id, source_version)
        latest: dict[str, CheckResult] = {}
        for result in results:
            latest[result.check_id] = result

        failed: list[str] = []
        missing: list[str] = []
        for check_id in required_check_ids:
            result = latest.get(check_id)
            if result is None:
                missing.append(check_id)
            elif result.status is CheckStatus.FAILED:
                failed.append(check_id)
            elif result.status is not CheckStatus.PASSED:
                missing.append(check_id)

        if failed:
            unresolved = failed
        if missing:
            problems.append(f"缺少当前版本的必需检查证据：{sorted(missing)}")

        for finding in self.repos.findings.list_by_root(root_task_id):
            if finding.required_for_goal and finding.status.value == "open":
                unresolved.append(f"未解决的必需问题 {finding.finding_id}")

        if unresolved:
            problems.append(f"存在未解决的必需项：{unresolved[:5]}")

        # first-pass path must explain why fix/verify were skipped
        skipped = self.repos.tasks.list_children(root_task_id)
        for task in skipped:
            if task.status == TaskStatus.SKIPPED.value and not task.skip_reason:
                problems.append(f"跳过任务 {task.task_id} 缺少原因")

        can_finish = not problems
        if can_finish:
            passed: bool | None = True
        elif unresolved:
            passed = False if failed else None
        else:
            passed = None
        return can_finish, passed, problems, unresolved

    def confirmed_required_failure(
        self, *, root_task_id: str, source_version: str, required_check_ids: list[str]
    ) -> tuple[bool, list[str]]:
        """Whether a *completed/false* verdict is supported by current evidence (§5.4).

        A confirmed failure needs an explicit failing required item: every required
        check must have a definitive result on the current version and at least one
        must be FAILED. Missing or inconclusive evidence is ``null``, never ``false``;
        budget exhaustion is not "unfixable" — the parent must say so explicitly.
        """
        latest: dict[str, CheckResult] = {}
        for result in self.repos.check_results.list_for_version(root_task_id, source_version):
            latest[result.check_id] = result
        failed: list[str] = []
        for check_id in required_check_ids:
            result = latest.get(check_id)
            if result is None or result.status not in (
                CheckStatus.PASSED,
                CheckStatus.FAILED,
            ):
                return False, []
            if result.status is CheckStatus.FAILED:
                failed.append(check_id)
        return bool(failed), failed

    def has_in_flight_work(self, root_task_id: str) -> bool:
        """True while an attempt is still active or a batch is not fully collected."""
        if self.repos.attempts.list_active(root_task_id):
            return True
        return any(
            batch and batch["status"] != "complete"
            for batch in self.repos.batches.list_by_root(root_task_id)
        )


def _version_of(root: Task, agent_id: str) -> str:
    from app.registry.agents import PARENT_VERSION

    return PARENT_VERSION if agent_id == "parent" else "1.0"


def _identity_problems(envelope: TaskEnvelope, result: TaskResult) -> list[str]:
    pairs = [
        ("root_task_id", envelope.root_task_id, result.root_task_id),
        ("task_id", envelope.task_id, result.task_id),
        ("attempt_id", envelope.attempt_id, result.attempt_id),
        ("dispatch_batch_id", envelope.dispatch_batch_id, result.dispatch_batch_id),
        ("agent_id", envelope.agent_id, result.agent_id),
        ("agent_version", envelope.agent_version, result.agent_version),
        ("task_kind", envelope.task_kind, result.task_kind),
        ("source_version", envelope.source_version, result.source_version),
        ("contract_version", envelope.contract_version, result.contract_version),
    ]
    return [
        f"{name} 不匹配：派发 {expected!r}，回报 {actual!r}"
        for name, expected, actual in pairs
        if expected != actual
    ]


def _audit_event(envelope: TaskEnvelope, problems: list[str]):  # type: ignore[no-untyped-def]
    from app.schemas.events import ExecutionEvent

    return ExecutionEvent(
        event_id=f"ev-audit-{uuid.uuid4().hex[:12]}",
        root_task_id=envelope.root_task_id,
        sequence=1,
        task_id=envelope.task_id,
        attempt_id=envelope.attempt_id,
        dispatch_batch_id=envelope.dispatch_batch_id,
        actor_id="controller",
        event_type=EventType.LATE_RESULT_AUDIT,
        payload={"problems": problems},
    )


def _result_event(envelope: TaskEnvelope, result: TaskResult):  # type: ignore[no-untyped-def]
    from app.schemas.events import ExecutionEvent

    return ExecutionEvent(
        event_id=f"ev-res-{uuid.uuid4().hex[:12]}",
        root_task_id=envelope.root_task_id,
        sequence=1,
        task_id=envelope.task_id,
        attempt_id=envelope.attempt_id,
        dispatch_batch_id=envelope.dispatch_batch_id,
        source_version=result.source_version,
        actor_id="controller",
        event_type=EventType.RESULT_RECEIVED,
        payload={
            "result_id": result.result_id,
            "status": result.status.value,
            "passed": result.passed,
            "summary": result.summary[:300],
        },
        artifact_refs=list(result.result_refs.values()) + list(result.evidence_refs),
    )
