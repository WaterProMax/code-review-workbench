"""P07 acceptance: leases, idempotent resume, patch recovery and termination.

Covers ImplementationPlan §11: a stale executor cannot publish, a published but
uncommitted patch is confirmed from its snapshot instead of reapplied, resume
grants budget atomically and is idempotent, and a waiting task can be terminated
while preserving its verdict.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.api import ResumeRequest, TerminateRequest
from app.schemas.common import TaskConstraints
from app.schemas.enums import (
    EventType,
    PatchApplicationStatus,
    RootStatus,
    TaskStatus,
)
from app.schemas.tasks import InputRefs, TaskEnvelope
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.task_service import ReceiptRejected
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.checkpoints import open_checkpointer
from app.storage.repositories import Repos
from app.workflow.recovery import RecoveryRejected, RunRightDenied
from app.workflow.runner import WorkflowRunner
from tests.fixtures import builders
from tests.fixtures.workflow import (
    BUGGY_SOURCE,
    CLEAN_SOURCE,
    MUTABLE_DEFAULT_FINDING,
    build_harness,
    make_scripted_client,
    prepare_source,
    sequential_workflow,
)
from app.schemas.workflows import BudgetConfig


def _envelope(root_id: str, task_id: str, attempt_id: str, batch_id: str) -> TaskEnvelope:
    return TaskEnvelope(
        root_task_id=root_id,
        parent_task_id=root_id,
        task_id=task_id,
        attempt_id=attempt_id,
        dispatch_batch_id=batch_id,
        agent_id="reviewer",
        agent_version="1.0",
        task_kind="review",
        goal="审查",
        input_refs=InputRefs(source="art-source", acceptance_contract="art-contract"),
        source_version="sv-1",
        contract_version="contract-1",
        acceptance_criteria=["覆盖全部必需检查项"],
        constraints=TaskConstraints(),
    )


def test_stale_executor_cannot_publish_results(repos: Repos, locks: ExecutionLockService) -> None:
    from app.services.task_service import TaskService

    root = builders.new_root(repos)
    review = builders.new_child(repos, root, task_id="T100-C001", task_kind="review")
    attempt = builders.new_attempt(repos, root, review, attempt_id="T100-C001-A1", batch_id="B1")
    repos.controls.ensure(root.task_id, "seg-1", 256)
    repos.tasks.update(root.task_id, status=RootStatus.RUNNING.value)  # revision 1

    ok, control = locks.acquire(root.task_id, "owner-a", ttl_seconds=30)
    assert ok
    stale_token = control.fencing_token

    # a second executor cannot take a live lease
    ok_b, _ = locks.acquire(root.task_id, "owner-b", ttl_seconds=30)
    assert ok_b is False

    # after expiry a takeover bumps the fencing token, invalidating owner-a
    repos.controls.update(
        root.task_id, lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    ok_b2, control_b = locks.acquire(root.task_id, "owner-b", ttl_seconds=30)
    assert ok_b2 and control_b.fencing_token == stale_token + 1

    # the stale owner can no longer publish a terminal outcome
    service = TaskService(repos, locks)
    with pytest.raises(ReceiptRejected):
        service.record_controller_terminal(
            envelope=_envelope(root.task_id, review.task_id, attempt.attempt_id, "B1"),
            code="ATTEMPT_TIMEOUT",
            message="stale",
            fencing_token=stale_token,
        )

    # the current owner still can
    terminal = service.record_controller_terminal(
        envelope=_envelope(root.task_id, review.task_id, attempt.attempt_id, "B1"),
        code="ATTEMPT_TIMEOUT",
        message="ok",
        fencing_token=control_b.fencing_token,
        error_ref="art-timeout",
    )
    assert terminal.origin.value == "controller"


@pytest.mark.asyncio
async def test_published_but_uncommitted_patch_is_recovered_without_reapplying(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """A crash after publish but before commit is confirmed, not replayed (§11.2)."""
    workflow = sequential_workflow()
    repos.workflows.insert(workflow)
    root_id = "T-recover-001"
    source_version, _ = prepare_source(
        workspace, repos, root_task_id=root_id, files=BUGGY_SOURCE
    )
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING]),
        workflow=workflow,
    )
    runner = WorkflowRunner(
        runtime=harness.runtime, graph=harness.graph, recovery=harness.recovery, owner="r1"
    )
    await runner.start(
        root_task_id=root_id,
        goal="修复可变默认参数并验证",
        workflow_version=workflow.workflow_version or "wf-1",
        check_mode=workflow.check_mode.value,
        source_version=source_version,
        source_artifact=harness.controller.source_artifact(root_id, source_version),
    )

    applications = repos.patch_applications.list_by_root(root_id)
    assert len(applications) == 1
    application = applications[0]
    assert application.status is PatchApplicationStatus.COMMITTED
    snapshot_meta = workspace.snapshot_dir(root_id, application.result_version) / ".snapshot.json"
    published_at = snapshot_meta.stat().st_mtime_ns

    # simulate the crash: the row lost its commit flag even though the snapshot is live
    repos.patch_applications.update(
        application.application_id,
        status=PatchApplicationStatus.PREPARED,
        result_version=application.result_version,
    )
    assert (
        repos.patch_applications.get(application.application_id).status
        is PatchApplicationStatus.PREPARED
    )

    recovered = harness.recovery.reconcile_patch_applications(root_id)
    assert [a.application_id for a in recovered] == [application.application_id]
    assert (
        repos.patch_applications.get(application.application_id).status
        is PatchApplicationStatus.COMMITTED
    )
    # the snapshot was confirmed in place, never rewritten
    assert snapshot_meta.stat().st_mtime_ns == published_at

    events = repos.events.list_after(root_id, 0, limit=500)
    assert any(
        e.event_type is EventType.PATCH_APPLIED and e.payload.get("recovered") is True
        for e in events
    )


@pytest.mark.asyncio
async def test_resume_after_interruption_does_not_duplicate_or_reapply(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """Resuming a finished-by-wait graph reuses the tree and applies the patch once."""
    workflow = sequential_workflow()
    repos.workflows.insert(workflow)
    root_id = "T-resume-001"
    source_version, _ = prepare_source(
        workspace, repos, root_task_id=root_id, files=BUGGY_SOURCE
    )
    async with open_checkpointer(settings.checkpoints_db_path) as checkpointer:
        harness = build_harness(
            settings=settings,
            repos=repos,
            artifacts=artifacts,
            workspace=workspace,
            locks=locks,
            llm=make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING]),
            workflow=workflow,
            checkpointer=checkpointer,
        )
        runner = WorkflowRunner(
            runtime=harness.runtime, graph=harness.graph, recovery=harness.recovery, owner="r1"
        )
        final = await runner.start(
            root_task_id=root_id,
            goal="修复可变默认参数并验证",
            workflow_version=workflow.workflow_version or "wf-1",
            check_mode=workflow.check_mode.value,
            source_version=source_version,
            source_artifact=harness.controller.source_artifact(root_id, source_version),
        )
        assert final["status"] == RootStatus.COMPLETED.value
        assert final["passed"] is True

        children_before = len(repos.tasks.list_children(root_id))
        contract_before = final["contract_version"]
        applications_before = len(repos.patch_applications.list_by_root(root_id))
        source_before = final["source_version"]

        # an explicit resume of a finished graph must not create or redo anything
        again = await runner.resume(root_task_id=root_id)
        assert again["status"] == RootStatus.COMPLETED.value
        assert again["passed"] is True
        assert len(repos.tasks.list_children(root_id)) == children_before
        assert again["contract_version"] == contract_before
        assert len(repos.patch_applications.list_by_root(root_id)) == applications_before
        assert again["source_version"] == source_before


def test_resume_grants_budget_atomically_and_is_idempotent(
    settings: Settings, repos: Repos, workspace: WorkspaceService, locks: ExecutionLockService, artifacts: ArtifactService
) -> None:
    workflow = sequential_workflow()
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=workflow,
    )
    root = builders.new_root(repos, root_task_id="T-wait-001", status=RootStatus.WAITING_RECOVERY.value)
    repos.controls.ensure(root.task_id, "seg-1", 256)
    budgets = BudgetConfig(max_repair_rounds=1, max_fix_retries=1, max_review_retries=1)
    revision = repos.tasks.get(root.task_id).revision

    request = ResumeRequest(
        expected_revision=revision,
        additional_repair_rounds=1,
        additional_execution_retries={"fix": 1},
        reason="修复网络后追加一轮修复许可",
    )
    outcome = harness.recovery.resume(
        root.task_id,
        request,
        budgets=budgets,
        owner="owner-a",
        operation_key="resume:T-wait-001",
    )
    assert outcome.status == RootStatus.RUNNING.value
    assert not outcome.reused
    state = locks.budget_state(root.task_id, budgets)
    assert state["repair_round"]["granted_max"] == 2
    assert state["fix_retry"]["granted_max"] == 2
    assert outcome.granted and {g["budget_kind"] for g in outcome.granted} == {
        "repair_round",
        "fix_retry",
    }

    # repeating the same request returns the same outcome without granting twice
    repeat = harness.recovery.resume(
        root.task_id,
        request,
        budgets=budgets,
        owner="owner-a",
        operation_key="resume:T-wait-001",
    )
    assert repeat.reused is True
    assert locks.budget_state(root.task_id, budgets)["repair_round"]["granted_max"] == 2

    events = repos.events.list_after(root.task_id, 0, limit=200)
    assert sum(1 for e in events if e.event_type is EventType.TASK_RESUMED) == 1


def test_resume_rejects_unknown_or_over_cap_grants_without_partial_writes(
    settings: Settings, repos: Repos, workspace: WorkspaceService, locks: ExecutionLockService, artifacts: ArtifactService
) -> None:
    workflow = sequential_workflow()
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=workflow,
    )
    root = builders.new_root(repos, root_task_id="T-wait-002", status=RootStatus.WAITING_RECOVERY.value)
    repos.controls.ensure(root.task_id, "seg-1", 256)
    budgets = BudgetConfig(max_repair_rounds=1)
    revision = repos.tasks.get(root.task_id).revision

    with pytest.raises(RecoveryRejected) as exc:
        harness.recovery.resume(
            root.task_id,
            ResumeRequest(expected_revision=revision, additional_execution_retries={"deploy": 1}, reason="x"),
            budgets=budgets,
            owner="owner-a",
            operation_key="resume:T-wait-002:bad-kind",
        )
    assert exc.value.code == "UNKNOWN_RETRY_KIND"

    with pytest.raises(RecoveryRejected) as exc2:
        harness.recovery.resume(
            root.task_id,
            ResumeRequest(expected_revision=revision, additional_repair_rounds=100, reason="x"),
            budgets=budgets,
            owner="owner-a",
            operation_key="resume:T-wait-002:over-cap",
        )
    assert exc2.value.code == "GRANT_OVER_CAP"

    # nothing was granted and the task stays waiting; its status was never flipped
    assert locks.budget_state(root.task_id, budgets)["repair_round"]["granted_max"] == 1
    assert repos.tasks.get(root.task_id).status == RootStatus.WAITING_RECOVERY.value


def test_terminate_preserves_null_verdict_and_skips_children(
    settings: Settings, repos: Repos, workspace: WorkspaceService, locks: ExecutionLockService, artifacts: ArtifactService
) -> None:
    workflow = sequential_workflow()
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=workflow,
    )
    root = builders.new_root(repos, root_task_id="T-term-001", status=RootStatus.INTERRUPTED.value)
    repos.controls.ensure(root.task_id, "seg-1", 256)
    for kind, tid in (("review", "T-term-001-C001"), ("fix", "T-term-001-C002")):
        builders.new_child(repos, root, task_id=tid, task_kind=kind, status=TaskStatus.BLOCKED.value)
    revision = repos.tasks.get(root.task_id).revision

    outcome = harness.recovery.terminate(
        root.task_id,
        TerminateRequest(expected_revision=revision, reason="用户确认终止"),
        owner="owner-a",
        operation_key="terminate:T-term-001",
    )
    # no contract/evidence → null verdict, never a false "passed"
    assert outcome.passed is None
    assert outcome.status == RootStatus.PARTIAL.value
    assert set(outcome.skipped_tasks) == {"T-term-001-C001", "T-term-001-C002"}
    root_after = repos.tasks.get(root.task_id)
    assert root_after.passed is None
    assert root_after.status == RootStatus.PARTIAL.value
    for task in repos.tasks.list_children(root.task_id):
        assert task.status == TaskStatus.SKIPPED.value
        assert task.skip_reason
