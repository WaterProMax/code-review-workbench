"""P05: failure classification, the repair loop and budget accounting (§9).

* a failed verification feeds the next repair round with the failure evidence,
  and the logical task keeps its id while a *new* attempt is created;
* when the repair budget is exhausted the run stops in waiting_recovery with the
  known verdict preserved and the missing additions named — never as success.
"""

from __future__ import annotations

import pytest

from app.schemas.enums import BudgetKind, EventType, RetryReason, RootStatus
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from tests.fixtures.workflow import (
    BOGUS_DIFF,
    BUGGY_SOURCE,
    FIX_DIFF,
    MUTABLE_DEFAULT_FINDING,
    run_workflow,
    sequential_workflow,
)


@pytest.mark.asyncio
async def test_failed_verification_triggers_a_second_repair_round(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-loop-001"
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=BUGGY_SOURCE,
        findings=[MUTABLE_DEFAULT_FINDING],
        fix_diffs=[BOGUS_DIFF, FIX_DIFF],
    )

    assert final["status"] == RootStatus.COMPLETED.value
    assert final["passed"] is True

    children = {t.task_kind: t for t in repos.tasks.list_children(root_task_id)}
    fix_task = children["fix"]

    # one logical task, two attempts — the history is preserved
    attempts = repos.attempts.list_by_task(fix_task.task_id)
    assert [a.attempt_no for a in attempts] == [1, 2]
    assert attempts[0].attempt_id != attempts[1].attempt_id
    assert all(a.task_id == fix_task.task_id for a in attempts)
    assert attempts[0].retry_reason == RetryReason.INITIAL.value
    assert attempts[1].retry_reason == RetryReason.BUSINESS_REPAIR.value

    # two applications: the first did not fix the defect, the second did
    applications = repos.patch_applications.list_by_root(root_task_id)
    assert len(applications) == 2
    assert [a.status.value for a in applications] == ["committed", "committed"]
    assert applications[1].base_version == applications[0].result_version

    # two verification attempts, each bound to the version it actually checked
    verify_attempts = repos.attempts.list_by_task(children["verify"].task_id)
    assert [a.source_version for a in verify_attempts] == [
        applications[0].result_version,
        applications[1].result_version,
    ]

    # the second fixer saw the first verification's failure evidence
    assert "verification" in attempts[1].input_refs
    assert "previous_patch" in attempts[1].input_refs

    # budget: two repair rounds consumed, one per new fix attempt
    state = locks.budget_state(root_task_id, sequential_workflow().budgets)
    assert state[BudgetKind.REPAIR_ROUND.value]["consumed"] == 2

    events = repos.events.list_after(root_task_id, 0, limit=2000)
    emitted = [e.payload.get("retry_reason") for e in events if e.event_type is EventType.TASK_DISPATCHED]
    assert RetryReason.BUSINESS_REPAIR.value in emitted
    detections = [e for e in events if e.event_type is EventType.DETECTION_COMPLETED]
    categories = [e.payload["category"] for e in detections]
    assert "code_defect" in categories
    assert "pass" in categories


@pytest.mark.asyncio
async def test_exhausted_repair_budget_waits_and_keeps_the_verdict(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-budget-001"
    workflow = sequential_workflow(max_repair_rounds=1)
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=BUGGY_SOURCE,
        findings=[MUTABLE_DEFAULT_FINDING],
        # the repair never actually fixes the defect, so the loop cannot succeed
        fix_diffs=[BOGUS_DIFF],
        workflow=workflow,
    )

    assert final["status"] == RootStatus.WAITING_RECOVERY.value
    # a known failing verdict is preserved instead of being reported as success
    assert final["passed"] is False

    children = {t.task_kind: t for t in repos.tasks.list_children(root_task_id)}
    assert children["review"].passed is False
    assert children["verify"].passed is False

    # exactly one repair round was granted and consumed; nothing was added
    attempts = repos.attempts.list_by_task(children["fix"].task_id)
    assert len(attempts) == 1
    state = locks.budget_state(root_task_id, workflow.budgets)
    assert state[BudgetKind.REPAIR_ROUND.value] == {
        "consumed": 1,
        "granted_max": 1,
        "remaining": 0,
    }

    assert final["required_action"]
    assert "repair_round" in final["required_action"]
    events = repos.events.list_after(root_task_id, 0, limit=2000)
    assert any(e.event_type is EventType.WAITING_RECOVERY for e in events)
    assert any(e.event_type is EventType.PARENT_DECIDED for e in events)

    # the wait reason records the real blocker, not a generic message
    assert final["waiting_reason"]
    assert "额度" in final["waiting_reason"]


@pytest.mark.asyncio
async def test_repeated_identical_patch_is_detected_as_no_progress(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """A repair that resubmits the same patch is no progress, not a new round."""
    root_task_id = "T-progress-001"
    workflow = sequential_workflow(max_repair_rounds=2)
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=BUGGY_SOURCE,
        findings=[MUTABLE_DEFAULT_FINDING],
        fix_diffs=[BOGUS_DIFF, BOGUS_DIFF],
        workflow=workflow,
    )

    assert final["status"] == RootStatus.WAITING_RECOVERY.value
    assert final["passed"] is False

    events = repos.events.list_after(root_task_id, 0, limit=2000)
    categories = [
        e.payload["category"]
        for e in events
        if e.event_type is EventType.DETECTION_COMPLETED
    ]
    assert "no_progress" in categories
    detection = final["detection"]
    assert detection["category"] in ("no_progress", "code_defect")
