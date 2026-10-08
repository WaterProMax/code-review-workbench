"""P02 acceptance: persistence, idempotent receipts, sequence allocation and
database-backed execution rights (ImplementationPlan §6, A09/A10/A13/A14/A17)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.common import ErrorInfo
from app.schemas.enums import (
    AttemptStatus,
    BudgetKind,
    ErrorCategory,
    EventType,
    ResultStatus,
    TaskKind,
    TerminalOrigin,
    TerminalOutcome,
)
from app.schemas.events import ExecutionEvent
from app.schemas.results import TaskResult, TerminalRecord
from app.schemas.workflows import BudgetConfig
from app.services.events import EventContext, EventSink
from app.services.execution_locks import (
    BudgetExhausted,
    BudgetNotAllowed,
    ExecutionRightError,
)
from app.storage.database import Database
from app.storage.repositories import ConflictError, Repos
from tests.fixtures import builders


def test_migrations_are_idempotent(db: Database) -> None:
    assert db.schema_versions() == ["0001_init", "0002_execution_tokens"]
    assert db.initialize() == []  # second run applies nothing


def test_reopening_the_database_still_shows_tasks_and_events(
    settings, db: Database, repos: Repos
) -> None:
    root = builders.new_root(repos)
    sink = EventSink(repos.events, EventContext(root_task_id=root.task_id, actor_id="parent"))
    sink.emit(EventType.TASK_CREATED, payload={"goal": root.goal})

    reopened = Repos(Database(settings.business_db_path))
    assert reopened.tasks.get(root.task_id) is not None
    events = reopened.events.list_after(root.task_id)
    assert len(events) == 1
    assert events[0].event_type is EventType.TASK_CREATED


def test_event_sequence_is_unique_under_concurrency(repos: Repos) -> None:
    root = builders.new_root(repos)
    sink = EventSink(repos.events, EventContext(root_task_id=root.task_id, actor_id="tool"))
    errors: list[Exception] = []

    def worker(index: int) -> None:
        try:
            sink.emit(EventType.TOOL_STARTED, payload={"index": index})
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    sequences = [e.sequence for e in repos.events.list_after(root.task_id, limit=100)]
    assert len(sequences) == 12
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == 12


def test_batch_receipts_require_expected_attempts_and_are_idempotent(repos: Repos) -> None:
    root = builders.new_root(repos)
    review = builders.new_child(repos, root, task_id="T101", task_kind="review")
    verify = builders.new_child(repos, root, task_id="T103", task_kind="verify")
    a1 = builders.new_attempt(repos, root, review, attempt_id="T101-A1", batch_id="B1")
    a2 = builders.new_attempt(repos, root, verify, attempt_id="T103-A1", batch_id="B1")
    builders.new_batch(repos, root, "B1", [a1.attempt_id, a2.attempt_id])

    batch = repos.batches.add_received("B1", a1.attempt_id, TerminalOrigin.AGENT)
    assert batch["status"] == "open"
    batch = repos.batches.add_received("B1", a1.attempt_id, TerminalOrigin.AGENT)
    assert batch["received_attempt_ids"] == [a1.attempt_id]

    with pytest.raises(ValueError):
        repos.batches.add_received("B1", "T999-A1", TerminalOrigin.CONTROLLER)

    batch = repos.batches.add_received("B1", a2.attempt_id, TerminalOrigin.CONTROLLER)
    assert batch["status"] == "complete"
    assert batch["outcome_origins"][a2.attempt_id] == "controller"

    assert repos.batches.mark_consumed("B1") is True
    assert repos.batches.mark_consumed("B1") is False


def test_terminal_record_is_single_and_controller_cannot_pass(repos: Repos) -> None:
    root = builders.new_root(repos)
    review = builders.new_child(repos, root, task_id="T101", task_kind="review")
    builders.new_attempt(repos, root, review, attempt_id="T101-A1", batch_id="B1")

    agent_terminal, created = repos.terminals.record(
        TerminalRecord(
            attempt_id="T101-A1",
            dispatch_batch_id="B1",
            origin=TerminalOrigin.AGENT,
            outcome=TerminalOutcome.COMPLETED,
            result_id="R-T101-A1",
            operation_key="terminal:T100:T101-A1",
        )
    )
    assert created is True

    # a later controller timeout must not overwrite the agent terminal
    winner, created_again = repos.terminals.record(
        TerminalRecord(
            attempt_id="T101-A1",
            origin=TerminalOrigin.CONTROLLER,
            outcome=TerminalOutcome.FAILED,
            error_ref="artifact-timeout",
            operation_key="terminal:T100:T101-A1",
        )
    )
    assert created_again is False
    assert winner.origin is TerminalOrigin.AGENT
    assert winner.result_id == "R-T101-A1"
    assert agent_terminal == winner


def test_result_receipt_is_idempotent_and_rejects_conflicting_content(repos: Repos) -> None:
    root = builders.new_root(repos)
    verify = builders.new_child(repos, root, task_id="T103", task_kind="verify")
    builders.new_attempt(repos, root, verify, attempt_id="T103-A1", batch_id="B1")
    started = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)
    finished = datetime(2026, 10, 8, 2, 0, 3, tzinfo=timezone.utc)

    def make(passed: bool, summary: str) -> TaskResult:
        return TaskResult(
            result_id="R-T103-A1",
            root_task_id=root.task_id,
            parent_task_id=root.task_id,
            task_id="T103",
            attempt_id="T103-A1",
            dispatch_batch_id="B1",
            agent_id="verifier",
            agent_version="1.0",
            task_kind=TaskKind.VERIFY.value,
            source_version="sv-1",
            contract_version="contract-1",
            status=ResultStatus.COMPLETED,
            passed=passed,
            summary=summary,
            evidence_refs=["art-1"],
            started_at=started,
            finished_at=finished,
        )

    first, created = repos.results.insert(make(False, "一项失败"))
    assert created is True
    again, created_again = repos.results.insert(make(False, "一项失败"))
    assert created_again is False
    assert again.result_id == first.result_id

    with pytest.raises(ConflictError):
        repos.results.insert(make(True, "改口通过"))


def test_budget_ledger_counts_once_per_operation(repos: Repos) -> None:
    root = builders.new_root(repos)
    budgets = BudgetConfig(max_repair_rounds=2)
    locks_kind = BudgetKind.REPAIR_ROUND.value

    assert locks_count_once(repos, root.task_id, locks_kind) is True
    assert locks_count_once(repos, root.task_id, locks_kind) is False

    totals = repos.budget.totals(root.task_id)[locks_kind]
    assert totals == {"consumed": 1, "granted": 0}
    assert locks_state(repos, root.task_id, budgets)[locks_kind]["remaining"] == 1


def locks_count_once(repos: Repos, root_task_id: str, kind: str) -> bool:
    return repos.budget.append(
        root_task_id=root_task_id,
        budget_kind=kind,
        scope_key="root",
        operation_key="consume:dispatch:T102:T102-A1",
        delta=-1,
        reason="派发新的 fix attempt",
    )


def locks_state(repos: Repos, root_task_id: str, budgets: BudgetConfig) -> dict:
    from app.services.execution_locks import ExecutionLockService

    return ExecutionLockService(repos.controls, repos.budget).budget_state(root_task_id, budgets)


def test_budget_exhaustion_blocks_dispatch_without_partial_writes(repos: Repos) -> None:
    from app.services.execution_locks import ExecutionLockService

    root = builders.new_root(repos)
    locks = ExecutionLockService(repos.controls, repos.budget)
    budgets = BudgetConfig(max_repair_rounds=1, max_fix_retries=0)

    # consume the only repair round
    locks.consume(root.task_id, BudgetKind.REPAIR_ROUND.value, "consume:1", "first fix")
    with pytest.raises(BudgetExhausted) as exc:
        locks.ensure_available(
            root.task_id,
            [BudgetKind.REPAIR_ROUND.value, BudgetKind.FIX_RETRY.value],
            budgets,
        )
    assert BudgetKind.REPAIR_ROUND.value in exc.value.required_additions
    # nothing was consumed by the failed check
    assert repos.budget.totals(root.task_id)[BudgetKind.REPAIR_ROUND.value]["consumed"] == 1


def test_grant_cannot_exceed_server_cap(repos: Repos) -> None:
    from app.services.execution_locks import ExecutionLockService

    root = builders.new_root(repos)
    locks = ExecutionLockService(repos.controls, repos.budget)
    budgets = BudgetConfig(max_repair_rounds=10)
    with pytest.raises(BudgetNotAllowed):
        locks.grant(root.task_id, BudgetKind.REPAIR_ROUND.value, 1, "grant:over", "too much", budgets)


def test_lease_acquire_fencing_and_release(repos: Repos) -> None:
    from app.services.execution_locks import ExecutionLockService

    root = builders.new_root(repos)
    locks = ExecutionLockService(repos.controls, repos.budget)
    repos.controls.ensure(root.task_id, "seg-1", 256)
    repos.tasks.update(root.task_id, status="running")  # revision 1

    ok, control = locks.acquire(root.task_id, "worker-a", ttl_seconds=30, expected_revision=1)
    assert ok is True
    assert control.lease_owner == "worker-a"
    first_token = control.fencing_token

    # a competing worker cannot take a live lease
    ok_other, _ = locks.acquire(root.task_id, "worker-b", ttl_seconds=30)
    assert ok_other is False

    # revision mismatch is refused
    ok_rev, _ = locks.acquire(root.task_id, "worker-b", ttl_seconds=30, expected_revision=99)
    assert ok_rev is False

    assert locks.has_right(root.task_id, first_token, owner="worker-a") is True
    assert locks.has_right(root.task_id, first_token - 1, owner="worker-a") is False

    # taking over after expiry bumps the fencing token, invalidating the old one
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    repos.controls.update(root.task_id, lease_expires_at=expired)
    ok_takeover, control2 = locks.acquire(root.task_id, "worker-b", ttl_seconds=30)
    assert ok_takeover is True
    assert control2.fencing_token == first_token + 1
    with pytest.raises(ExecutionRightError):
        locks.require_right(root.task_id, first_token, owner="worker-a")

    assert locks.release(root.task_id, "worker-b") is True
    assert locks.heartbeat(root.task_id, "worker-b") is False


def test_idempotency_record_rejects_reuse_with_different_content(repos: Repos) -> None:
    repos.idempotency.put(
        operation_key="submit:abc",
        operation_kind="submit_task",
        request_fingerprint="sha256:one",
        response_ref="T100",
        root_task_id="T100",
    )
    assert (
        repos.idempotency.put(
            operation_key="submit:abc",
            operation_kind="submit_task",
            request_fingerprint="sha256:one",
            response_ref="T100",
        )
        is False
    )
    with pytest.raises(ConflictError):
        repos.idempotency.put(
            operation_key="submit:abc",
            operation_kind="submit_task",
            request_fingerprint="sha256:two",
            response_ref="T200",
        )


def test_failed_attempt_projects_into_result_semantics(repos: Repos) -> None:
    """A tool that cannot run yields failed/null with an execution error (§5.2.6)."""
    root = builders.new_root(repos)
    verify = builders.new_child(repos, root, task_id="T103", task_kind="verify")
    builders.new_attempt(repos, root, verify, attempt_id="T103-A1", batch_id="B1")
    started = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)
    result = TaskResult(
        result_id="R-T103-A1",
        root_task_id=root.task_id,
        parent_task_id=root.task_id,
        task_id="T103",
        attempt_id="T103-A1",
        dispatch_batch_id="B1",
        agent_id="verifier",
        agent_version="1.0",
        task_kind=TaskKind.VERIFY.value,
        source_version="sv-1",
        contract_version="contract-1",
        status=ResultStatus.FAILED,
        passed=None,
        summary="测试执行器不可用",
        error=ErrorInfo(
            code="MISSING_DEPENDENCY",
            category=ErrorCategory.MISSING_DEPENDENCY,
            message="pytest 未安装",
        ),
        started_at=started,
        finished_at=started,
    )
    repos.results.insert(result)
    stored = repos.results.get_by_attempt("T103-A1")
    assert stored is not None
    assert stored.status is ResultStatus.FAILED
    assert stored.passed is None
    assert stored.error is not None and stored.error.recoverable is True

    repos.attempts.update_status("T103-A1", AttemptStatus.FAILED, error="missing dependency")
    assert repos.attempts.get("T103-A1").status is AttemptStatus.FAILED
    assert sorted(a.attempt_id for a in repos.attempts.list_active(root.task_id)) == []
