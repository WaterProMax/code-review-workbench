"""P06: post-patch parallel recheck + verify, and the controller timeout terminal.

* the two branches read the same frozen snapshot, run concurrently and the parent
  decides only after both valid terminals have arrived;
* an attempt that exceeds its deadline is invalidated by the control layer and
  recorded as a *controller* terminal — never as a child report.
"""

from __future__ import annotations

import asyncio

import pytest

from app.providers.scripted import ScriptedLLMClient
from app.schemas.enums import EventType, RootStatus, TerminalOrigin, TerminalOutcome
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from tests.fixtures.workflow import (
    BUGGY_SOURCE,
    CLEAN_SOURCE,
    FIX_DIFF,
    MUTABLE_DEFAULT_FINDING,
    parallel_workflow,
    run_workflow,
)


@pytest.mark.asyncio
async def test_parallel_recheck_and_verify_on_one_frozen_snapshot(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-parallel-001"
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=BUGGY_SOURCE,
        findings=[MUTABLE_DEFAULT_FINDING],
        fix_diffs=[FIX_DIFF],
        latency_seconds=0.02,
        workflow=parallel_workflow(),
    )

    assert final["status"] == RootStatus.COMPLETED.value, {
        "waiting_reason": final.get("waiting_reason"),
        "rejections": final.get("action_rejections")[-2:],
        "detection": final.get("detection"),
    }
    assert final["passed"] is True

    # the parallel batch dispatched two attempts of the same snapshot, together
    post_batches = [
        b
        for b in repos.batches.list_by_root(root_task_id)
        if len(b["expected_attempt_ids"]) == 2
    ]
    assert len(post_batches) == 1
    batch = post_batches[0]
    assert batch["status"] == "complete"
    assert set(batch["received_attempt_ids"]) == set(batch["expected_attempt_ids"])
    assert all(
        origin == TerminalOrigin.AGENT.value
        for origin in batch["outcome_origins"].values()
    )

    batch_attempts = [repos.attempts.get(a) for a in batch["expected_attempt_ids"]]
    assert {a.source_version for a in batch_attempts} == {final["source_version"]}
    assert {repos.tasks.get(a.task_id).agent_id for a in batch_attempts} == {
        "reviewer",
        "verifier",
    }

    # the two branches really overlapped in time
    results = [repos.results.get_by_attempt(a.attempt_id) for a in batch_attempts]
    assert all(r is not None for r in results)
    first, second = sorted(results, key=lambda r: r.started_at)
    assert second.started_at < first.finished_at

    # both reports returned to the parent before it decided
    events = repos.events.list_after(root_task_id, 0, limit=2000)
    batch_attempt_ids = set(batch["expected_attempt_ids"])
    received = [
        e
        for e in events
        if e.event_type is EventType.RESULT_RECEIVED and e.attempt_id in batch_attempt_ids
    ]
    assert len(received) == 2
    last_received_seq = max(e.sequence for e in received)
    final_decision = max(
        e.sequence for e in events if e.event_type is EventType.PARENT_DECIDED
    )
    assert final_decision > last_received_seq

    # both post-patch required checks are proven on the new version
    latest = repos.check_results.list_for_version(root_task_id, final["source_version"])
    statuses = {r.check_id: r.status.value for r in latest}
    assert statuses["CHK-SYNTAX"] == "passed"
    assert statuses["CHK-MUTABLE"] == "passed"


@pytest.mark.asyncio
async def test_timed_out_attempt_gets_a_controller_terminal(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-timeout-001"
    workflow = parallel_workflow(attempt_timeout_seconds=0.15)
    calls = {"review": 0}

    class TimeoutOnceClient(ScriptedLLMClient):
        """Stalls the first child attempt past the deadline; later ones are fast."""

        def __init__(self):
            super().__init__(handler)
            self._stalled = False

        async def complete(self, request):
            key = request.script_key or ""
            if key.endswith(":reviewer:step") and not self._stalled:
                self._stalled = True
                await asyncio.sleep(0.5)
            return await super().complete(request)

    def handler(request):
        from tests.fixtures.workflow import _context_of, default_plan, run_parent_decide

        key = request.script_key or ""
        if key == "parent:plan":
            return default_plan()
        if key == "parent:decide":
            return {
                "reasoning": "依据检测事实选择下一步",
                "action": run_parent_decide(_context_of(request)),
            }
        if key.endswith(":reviewer:step"):
            calls["review"] += 1
            return {
                "reason": "审查完成",
                "final": {
                    "summary": "审查完成",
                    "model_review_results": [],
                    "findings": [],
                    "coverage": ["CHK-SYNTAX", "CHK-MUTABLE"],
                    "not_checked": [],
                },
            }
        raise AssertionError(f"unexpected script_key {key!r}")

    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=CLEAN_SOURCE,
        workflow=workflow,
        llm=TimeoutOnceClient(),
    )

    assert final["status"] == RootStatus.COMPLETED.value, {
        "waiting_reason": final.get("waiting_reason"),
        "required_action": final.get("required_action"),
        "rejections": final.get("action_rejections"),
        "detection": final.get("detection"),
        "fatal": final.get("fatal_error"),
    }
    assert final["passed"] is True

    attempts = repos.attempts.list_by_root(root_task_id)
    invalidated = [a for a in attempts if a.status.value == "invalidated"]
    assert len(invalidated) == 1
    terminal = repos.terminals.get(invalidated[0].attempt_id)
    assert terminal is not None
    assert terminal.origin is TerminalOrigin.CONTROLLER
    assert terminal.outcome is TerminalOutcome.FAILED
    assert terminal.error_ref
    # a controller terminal is not a child report
    assert repos.results.get_by_attempt(invalidated[0].attempt_id) is None

    # the batch counted the controller terminal as that attempt's legal terminal
    owning = [
        b
        for b in repos.batches.list_by_root(root_task_id)
        if invalidated[0].attempt_id in b["expected_attempt_ids"]
    ]
    assert owning
    assert (
        owning[0]["outcome_origins"][invalidated[0].attempt_id]
        == TerminalOrigin.CONTROLLER.value
    )

    events = repos.events.list_after(root_task_id, 0, limit=2000)
    assert any(e.event_type is EventType.TERMINAL_RECORDED for e in events)
    categories = [
        e.payload["category"]
        for e in events
        if e.event_type is EventType.DETECTION_COMPLETED
    ]
    assert "execution_fault" in categories

    # the retry after invalidation really ran, and it consumed the review fault
    # budget rather than a repair round
    review_task = next(
        t for t in repos.tasks.list_children(root_task_id) if t.task_kind == "review"
    )
    review_attempts = repos.attempts.list_by_task(review_task.task_id)
    assert [a.attempt_no for a in review_attempts] == [1, 2]
    assert review_attempts[1].retry_reason == "execution_fault"
    assert calls["review"] >= 1
    state = locks.budget_state(root_task_id, workflow.budgets)
    assert state["review_retry"]["consumed"] >= 1
    assert state["repair_round"]["consumed"] == 0
