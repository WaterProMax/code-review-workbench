"""P11 acceptance scenarios that the earlier phase suites did not yet prove (§15.2).

Each test is written against real components (database, workspace, graph and
recovery) — no mocked model, no fake progress. The scenarios covered here are the
ones the phase suites left open:

* A12 one parallel branch faults: terminals are collected, evidence is preserved
  and the fault returns to the parent instead of deadlocking;
* A25 a required check that never runs keeps the verdict ``null``, never ``true``;
* A26 a contract cannot be downgraded and a non-executable method cannot be the
  post-patch proof;
* A29 an explicitly unfixable run completes ``false`` with the capability limit;
* A30 mismatched/late reports are audited only and must not change the verdict;
* A31 a final state refuses resume, so terminate/resume have a single winner;
* A32 a verify top-up grants only verify and an over-cap request writes nothing;
* A33 a fault re-dispatch of a fix attempt consumes both declared budgets;
* A36 old-version evidence cannot close a new-version finding;
* A37 graph-step and parent-correction limits end in ``waiting_recovery``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.providers.scripted import ScriptedLLMClient
from app.schemas.api import ResumeRequest
from app.schemas.common import TaskConstraints
from app.schemas.enums import (
    ApplicablePhase,
    AttemptStatus,
    BudgetKind,
    CheckMethod,
    CheckStatus,
    EventType,
    FindingSeverity,
    FindingStatus,
    ResultStatus,
    RetryReason,
    RootStatus,
    TerminalOrigin,
)
from app.schemas.results import (
    CheckResult,
    CheckSpec,
    Finding,
    TaskResult,
    VerificationReport,
)
from app.schemas.tasks import InputRefs, TaskEnvelope
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.task_service import ReceiptRejected, TaskService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from tests.fixtures import builders
from tests.fixtures.workflow import (
    BOGUS_DIFF,
    BUGGY_SOURCE,
    CLEAN_SOURCE,
    FIX_DIFF,
    MUTABLE_DEFAULT_FINDING,
    build_harness,
    default_plan,
    make_scripted_client,
    parallel_workflow,
    run_workflow,
    sequential_workflow,
)


def _with_budget(config, **overrides):  # type: ignore[no-untyped-def]
    return config.model_copy(
        update={"budgets": config.budgets.model_copy(update=overrides)}
    )


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


def _result(
    envelope: TaskEnvelope, *, batch_id: str | None = None, summary: str = "审查完成"
) -> TaskResult:
    now = datetime.now(timezone.utc)
    return TaskResult(
        result_id=f"res-{envelope.attempt_id}",
        root_task_id=envelope.root_task_id,
        parent_task_id=envelope.parent_task_id,
        task_id=envelope.task_id,
        attempt_id=envelope.attempt_id,
        dispatch_batch_id=batch_id or envelope.dispatch_batch_id,
        agent_id=envelope.agent_id,
        agent_version=envelope.agent_version,
        task_kind=envelope.task_kind,
        source_version=envelope.source_version,
        contract_version=envelope.contract_version,
        status=ResultStatus.COMPLETED,
        passed=None,
        summary=summary,
        started_at=now,
        finished_at=now,
    )


# --------------------------------------------------------------------------- #
# A12: one branch of a parallel batch faults
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a12_parallel_branch_fault_is_collected_and_returns_to_the_parent(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """A timing-out recheck must not deadlock: both terminals arrive, the verify
    evidence survives, and the fault goes back to the parent for a decision."""
    root_task_id = "T-a12-001"
    workflow = parallel_workflow(attempt_timeout_seconds=0.2)
    inner = make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[FIX_DIFF])

    class StallSecondReviewer(ScriptedLLMClient):
        """Stalls the post-patch *recheck* (the 2nd reviewer step) past its deadline."""

        def __init__(self) -> None:
            super().__init__(inner._handler)  # reuse the deterministic handler
            self._reviewer_steps = 0

        async def complete(self, request):  # type: ignore[no-untyped-def]
            key = request.script_key or ""
            if key.endswith(":reviewer:step"):
                self._reviewer_steps += 1
                if self._reviewer_steps == 2:
                    await asyncio.sleep(1.0)
            return await super().complete(request)

    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=BUGGY_SOURCE,
        workflow=workflow,
        llm=StallSecondReviewer(),
    )

    # no deadlock: the run reached a parent decision and stopped in waiting_recovery
    assert final["status"] == RootStatus.WAITING_RECOVERY.value, {
        "waiting_reason": final.get("waiting_reason"),
        "rejections": final.get("action_rejections"),
    }
    assert final["waiting_reason"]

    # the parallel batch collected *both* terminals, one of them a controller fault
    post_batches = [
        b for b in repos.batches.list_by_root(root_task_id) if len(b["expected_attempt_ids"]) == 2
    ]
    assert len(post_batches) == 1
    batch = post_batches[0]
    assert batch["status"] == "complete"
    assert set(batch["received_attempt_ids"]) == set(batch["expected_attempt_ids"])
    origins = sorted(batch["outcome_origins"].values())
    assert origins == [TerminalOrigin.AGENT.value, TerminalOrigin.CONTROLLER.value]

    # the healthy branch's evidence is preserved
    verify_attempt = next(
        a
        for a in (repos.attempts.get(x) for x in batch["expected_attempt_ids"])
        if repos.tasks.get(a.task_id).task_kind == "verify"
    )
    assert repos.results.get_by_attempt(verify_attempt.attempt_id) is not None
    assert repos.verifications.latest(root_task_id) is not None

    # the fault is reported to the parent *after* the batch was fully collected
    events = repos.events.list_after(root_task_id, 0, limit=2000)
    terminal_seq = max(
        e.sequence
        for e in events
        if e.event_type in (EventType.RESULT_RECEIVED, EventType.TERMINAL_RECORDED)
        and e.attempt_id in set(batch["expected_attempt_ids"])
    )
    last_decision = max(
        e.sequence for e in events if e.event_type is EventType.PARENT_DECIDED
    )
    assert last_decision > terminal_seq


@pytest.mark.asyncio
async def test_a15_parallel_mid_batch_exit_keeps_the_finished_branch_evidence(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """A real SIGKILL lands while the post-patch batch has one finished branch.

    The finished (verify) branch must keep its single attempt *and* its report; the
    unfinished branch may be re-dispatched, and the patch must not be applied twice.

    Regression: persist each finished report before the batch barrier and replay
    only the interrupted branch after process exit.
    """
    from app.schemas.api import ResumeRequest
    from app.storage.checkpoints import open_checkpointer
    from app.storage.database import Database
    from app.workflow.runner import WorkflowRunner

    backend_dir = Path(__file__).resolve().parents[2]
    root_task_id = "T-a15-001"
    marker = settings.data_dir / "a15.marker"
    env = dict(os.environ)
    env.update(
        {
            "HW2_DATA_DIR": str(settings.data_dir),
            "HW2_ROOT_TASK_ID": root_task_id,
            "HW2_CRASH_MARKER": str(marker),
            "PYTHONPATH": str(backend_dir),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e.interruption_driver_parallel"],
        cwd=str(backend_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 90
    while time.time() < deadline and not marker.exists() and proc.poll() is None:
        time.sleep(0.05)
    if not marker.exists():
        out, err = proc.communicate(timeout=10)
        pytest.fail(
            f"driver never reached the crash point (rc={proc.returncode})\nstdout={out}\nstderr={err}"
        )
    assert proc.wait(timeout=15) == -signal.SIGKILL

    # Recovery waits for the crashed process's live lease to expire.
    await asyncio.sleep(1.1)
    resumed_repos = Repos(Database(settings.business_db_path))
    verify_task = next(
        t for t in resumed_repos.tasks.list_children(root_task_id) if t.task_kind == "verify"
    )
    verify_attempts_at_crash = [
        a.attempt_id for a in resumed_repos.attempts.list_by_task(verify_task.task_id)
    ]
    assert len(verify_attempts_at_crash) == 1

    resumed_artifacts = ArtifactService(settings, resumed_repos.artifacts)
    resumed_workspace = WorkspaceService(settings, resumed_artifacts, resumed_repos)
    resumed_locks = ExecutionLockService(resumed_repos.controls, resumed_repos.budget)
    workflow = parallel_workflow()
    interrupted = build_harness(
        settings=settings,
        repos=resumed_repos,
        artifacts=resumed_artifacts,
        workspace=resumed_workspace,
        locks=resumed_locks,
        llm=make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[FIX_DIFF]),
        workflow=workflow,
    ).recovery.recover_incomplete_runs()
    assert root_task_id in interrupted

    async with open_checkpointer(settings.checkpoints_db_path) as checkpointer:
        harness = build_harness(
            settings=settings,
            repos=resumed_repos,
            artifacts=resumed_artifacts,
            workspace=resumed_workspace,
            locks=resumed_locks,
            llm=make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[FIX_DIFF]),
            workflow=workflow,
            checkpointer=checkpointer,
        )
        revision = resumed_repos.tasks.get(root_task_id).revision
        harness.recovery.resume(
            root_task_id,
            ResumeRequest(expected_revision=revision, reason="并行中断后恢复"),
            budgets=workflow.budgets,
            owner="resumer",
            operation_key="resume:T-a15-001",
        )
        runner = WorkflowRunner(
            runtime=harness.runtime, graph=harness.graph, recovery=harness.recovery, owner="resumer"
        )
        final = await runner.resume(root_task_id=root_task_id)
        assert final["status"] == RootStatus.COMPLETED.value, final
        assert final["passed"] is True, final

    # the finished branch was neither re-run nor lost: one attempt, with its report
    assert [
        a.attempt_id for a in resumed_repos.attempts.list_by_task(verify_task.task_id)
    ] == verify_attempts_at_crash
    assert resumed_repos.results.get_by_attempt(verify_attempts_at_crash[0]) is not None
    # the patch was applied exactly once
    assert len(resumed_repos.patch_applications.list_by_root(root_task_id)) == 1


# --------------------------------------------------------------------------- #
# A15 (recovery side): an unfinished attempt is closed with evidence, no crash
# --------------------------------------------------------------------------- #
def test_a15_unfinished_attempt_is_closed_with_a_controller_terminal(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """Recovering a run with an in-flight attempt must close it, not crash.

    A controller terminal requires evidence, so the orphan attempt is closed with
    a recorded interruption reason and counted as the batch's legal terminal.
    """
    from datetime import datetime, timedelta, timezone

    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=parallel_workflow(),
    )
    root = builders.new_root(repos, root_task_id="T-a15-002", status=RootStatus.RUNNING.value)
    recheck = builders.new_child(repos, root, task_id="T-a15-002-C001", task_kind="review")
    verify = builders.new_child(repos, root, task_id="T-a15-002-C002", task_kind="verify")
    builders.new_batch(
        repos, root, "B1", ["T-a15-002-C001-A1", "T-a15-002-C002-A1"]
    )
    builders.new_attempt(repos, root, recheck, attempt_id="T-a15-002-C001-A1", batch_id="B1")
    builders.new_attempt(repos, root, verify, attempt_id="T-a15-002-C002-A1", batch_id="B1")
    repos.controls.ensure(root.task_id, "seg-1", 256)
    ok, _control = locks.acquire(root.task_id, "dead-owner", ttl_seconds=30)
    assert ok
    repos.controls.update(
        root.task_id,
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )

    interrupted = harness.recovery.recover_incomplete_runs()
    assert root.task_id in interrupted
    for attempt_id in ("T-a15-002-C001-A1", "T-a15-002-C002-A1"):
        assert repos.attempts.get(attempt_id).status is AttemptStatus.INTERRUPTED
        terminal = repos.terminals.get(attempt_id)
        assert terminal is not None
        assert terminal.origin is TerminalOrigin.CONTROLLER
        assert terminal.error_ref  # a controller terminal must carry evidence
    batch = repos.batches.get("B1")
    assert batch["status"] == "complete"
    assert all(
        origin == TerminalOrigin.CONTROLLER.value
        for origin in batch["outcome_origins"].values()
    )


# --------------------------------------------------------------------------- #
# A25: a required check with no executable input is never a pass
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a25_required_check_that_never_runs_keeps_the_verdict_null(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-a25-001"
    plan = default_plan()
    plan["checks"].append(
        {
            "check_id": "CHK-BEHAVIOR-X",
            "goal_ref": "add_item 不共享默认列表",
            "scope": ["helpers.py"],
            "required": True,
            "method": "behavior_test",
            "pass_condition": "行为测试通过",
            "applicable_phase": "both",
        }
    )
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=CLEAN_SOURCE,
        llm=make_scripted_client(plan=plan),
    )

    # a required item with no executable input is missing evidence -> null, not true
    assert final["status"] == RootStatus.WAITING_RECOVERY.value, final.get("waiting_reason")
    assert final["passed"] is None
    assert final["status"] != RootStatus.COMPLETED.value
    assert "CHK-BEHAVIOR-X" in (final["waiting_reason"] or "")

    row = repos.check_results.latest_for_check(
        root_task_id, "CHK-BEHAVIOR-X", final["source_version"]
    )
    assert row is not None
    assert row.status is CheckStatus.NOT_RUN
    assert row.executed is False
    assert "可执行的测试输入" in (row.reason or "")


# --------------------------------------------------------------------------- #
# A26: no downgrade, no non-executable post-patch proof
# --------------------------------------------------------------------------- #
def test_a26_contract_cannot_be_downgraded_or_proved_by_a_non_executable_method(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=sequential_workflow(),
    )
    root = builders.new_root(repos, root_task_id="T-a26-001")
    checks = [
        CheckSpec(
            check_id="CHK-A",
            goal_ref="目标A",
            scope=["helpers.py"],
            required=True,
            method=CheckMethod.SYNTAX,
            pass_condition="可解析",
            applicable_phase=ApplicablePhase.BOTH,
        ),
        CheckSpec(
            check_id="CHK-B",
            goal_ref="目标B",
            scope=["helpers.py"],
            required=True,
            method=CheckMethod.STATIC_RULE,
            pass_condition="无命中",
            applicable_phase=ApplicablePhase.BOTH,
            rule_ids=["B006-mutable-default"],
        ),
    ]
    harness.controller.persist_contract(root_task_id=root.task_id, goal="目标", checks=checks)

    # a superseding contract that drops a required check is refused and not written
    from app.workflow.controller import ActionRejected

    with pytest.raises(ActionRejected) as exc:
        harness.controller.persist_contract(
            root_task_id=root.task_id,
            goal="目标",
            checks=[checks[0]],
            supersedes="contract-1",
            append_reason="偷懒删掉一个必需项",
        )
    assert exc.value.code == "CONTRACT_DOWNGRADE"
    assert len(repos.contracts.list_by_root(root.task_id)) == 1

    # model_review is not executable evidence, so it cannot be the post-patch proof
    problems = harness.controller.validate_plan(
        [
            CheckSpec(
                check_id="CHK-MR",
                goal_ref="异常处理",
                scope=["helpers.py"],
                required=True,
                method=CheckMethod.MODEL_REVIEW,
                pass_condition="模型认为通过",
                applicable_phase=ApplicablePhase.BOTH,
            )
        ],
        goal="目标",
        files=["helpers.py"],
    )
    assert any("model_review" in p for p in problems)


# --------------------------------------------------------------------------- #
# A29: explicitly unfixable -> completed/false with the capability limit
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a29_explicitly_unfixable_run_completes_false(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-a29-001"

    def unfixable_decide(context):  # type: ignore[no-untyped-def]
        from tests.fixtures.workflow import parent_decide

        action = parent_decide(context, [])
        detection = context.get("detection") or {}
        if context.get("patched") and detection.get("category") in ("code_defect", "no_progress"):
            return {
                "action": "finish",
                "proposed_passed": False,
                "report_refs": [],
                "reason": "该缺陷需要改写模块契约，超出当前自动修复能力，不再盲目派发修复",
            }
        return action

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
        # the repair never fixes the defect; the parent recognises it cannot
        fix_diffs=[BOGUS_DIFF],
        workflow=workflow,
        llm=make_scripted_client(
            findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[BOGUS_DIFF], decide=unfixable_decide
        ),
    )

    assert final["status"] == RootStatus.COMPLETED.value, {
        "waiting_reason": final.get("waiting_reason"),
        "rejections": final.get("action_rejections"),
    }
    assert final["passed"] is False

    # the parent did not blindly dispatch a second repair though the budget allowed it
    fix_task = next(t for t in repos.tasks.list_children(root_task_id) if t.task_kind == "fix")
    assert len(repos.attempts.list_by_task(fix_task.task_id)) == 1
    state = locks.budget_state(root_task_id, workflow.budgets)
    assert state[BudgetKind.REPAIR_ROUND.value]["consumed"] == 1
    assert state[BudgetKind.REPAIR_ROUND.value]["remaining"] == 1

    # the report carries the confirmed defect and the stated capability limit
    row = repos.reports.latest(root_task_id, "final")
    assert row is not None
    report = artifacts.read_json(row["artifact_id"])
    assert report["passed"] is False
    assert "CHK-MUTABLE" in report["unresolved_finding_ids"] or "CHK-MUTABLE" in str(
        report["conclusion_scope"]
    )
    assert "无法自动修复" in report["conclusion_scope"]
    assert "能力" in report["execution_summary"]["unfixable_reason"]


# --------------------------------------------------------------------------- #
# A30: mismatched / late reports are audited only
# --------------------------------------------------------------------------- #
def test_a30_mismatched_and_late_reports_are_audited_only(
    repos: Repos, locks: ExecutionLockService
) -> None:
    root = builders.new_root(repos, root_task_id="T-a30-001")
    review = builders.new_child(repos, root, task_id="T-a30-001-C001", task_kind="review")
    attempt = builders.new_attempt(
        repos, root, review, attempt_id="T-a30-001-C001-A1", batch_id="B1"
    )
    repos.controls.ensure(root.task_id, "seg-1", 256)
    repos.tasks.update(root.task_id, status=RootStatus.RUNNING.value)
    ok, control = locks.acquire(root.task_id, "owner-a", ttl_seconds=30)
    assert ok
    service = TaskService(repos, locks)

    # 1) a report whose batch id does not match the dispatch is rejected and audited
    envelope = _envelope(root.task_id, review.task_id, attempt.attempt_id, "B1")
    mismatched = _result(envelope, batch_id="B2")
    with pytest.raises(ReceiptRejected):
        service.receive_result(envelope, mismatched)
    assert repos.results.get_by_attempt(attempt.attempt_id) is None
    events = repos.events.list_after(root.task_id, 0, limit=200)
    assert any(e.event_type is EventType.LATE_RESULT_AUDIT for e in events)

    # 2) a report that arrives after the controller invalidated the attempt is audited
    service.record_controller_terminal(
        envelope=envelope,
        code="ATTEMPT_TIMEOUT",
        message="超时",
        fencing_token=control.fencing_token,
        error_ref="art-timeout",
    )
    assert repos.attempts.get(attempt.attempt_id).status is AttemptStatus.INVALIDATED
    audits_before = sum(
        1 for e in repos.events.list_after(root.task_id, 0, limit=500)
        if e.event_type is EventType.LATE_RESULT_AUDIT
    )
    with pytest.raises(ReceiptRejected) as exc:
        service.receive_result(envelope, _result(envelope))
    assert exc.value.code == "ATTEMPT_INVALIDATED"
    assert repos.results.get_by_attempt(attempt.attempt_id) is None
    audits_after = sum(
        1 for e in repos.events.list_after(root.task_id, 0, limit=500)
        if e.event_type is EventType.LATE_RESULT_AUDIT
    )
    assert audits_after > audits_before


# --------------------------------------------------------------------------- #
# A31: a final state refuses resume (terminate/resume have one winner)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a31_final_state_refuses_resume_so_terminate_wins(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    from app.schemas.api import TerminateRequest
    from app.workflow.recovery import RecoveryRejected

    workflow = sequential_workflow()
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id="T-a31-001",
        files=CLEAN_SOURCE,
        workflow=workflow,
    )
    assert final["status"] == RootStatus.COMPLETED.value

    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=workflow,
    )
    revision = repos.tasks.get("T-a31-001").revision
    with pytest.raises(RecoveryRejected) as exc:
        harness.recovery.resume(
            "T-a31-001",
            ResumeRequest(expected_revision=revision, reason="再跑一次"),
            budgets=workflow.budgets,
            owner="owner-a",
            operation_key="resume:T-a31-001",
        )
    assert exc.value.code == "NOT_RESUMABLE"

    # a waiting task that is terminated can then never be resumed (single winner)
    wait_root = builders.new_root(
        repos, root_task_id="T-a31-002", status=RootStatus.WAITING_RECOVERY.value
    )
    repos.controls.ensure(wait_root.task_id, "seg-1", 256)
    terminated = harness.recovery.terminate(
        wait_root.task_id,
        TerminateRequest(expected_revision=wait_root.revision, reason="用户终止"),
        owner="owner-a",
        operation_key="terminate:T-a31-002",
    )
    assert terminated.status in (RootStatus.PARTIAL.value, RootStatus.COMPLETED.value)
    with pytest.raises(RecoveryRejected) as exc2:
        harness.recovery.resume(
            wait_root.task_id,
            ResumeRequest(
                expected_revision=repos.tasks.get(wait_root.task_id).revision, reason="恢复"
            ),
            budgets=workflow.budgets,
            owner="owner-a",
            operation_key="resume:T-a31-002",
        )
    assert exc2.value.code == "NOT_RESUMABLE"


# --------------------------------------------------------------------------- #
# A32: a verify top-up touches only verify; an over-cap request writes nothing
# --------------------------------------------------------------------------- #
def test_a32_verify_top_up_grants_only_verify_and_over_cap_writes_nothing(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    from app.schemas.workflows import BudgetConfig
    from app.workflow.recovery import RecoveryRejected

    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=sequential_workflow(),
    )
    budgets = BudgetConfig(
        max_repair_rounds=1, max_fix_retries=1, max_verification_retries=1
    )
    root = builders.new_root(
        repos, root_task_id="T-a32-001", status=RootStatus.WAITING_RECOVERY.value
    )
    repos.controls.ensure(root.task_id, "seg-1", 256)

    outcome = harness.recovery.resume(
        root.task_id,
        ResumeRequest(
            expected_revision=root.revision,
            additional_execution_retries={"verify": 1},
            reason="追加一次验证许可",
        ),
        budgets=budgets,
        owner="owner-a",
        operation_key="resume:T-a32-001",
    )
    assert {g["budget_kind"] for g in outcome.granted} == {
        BudgetKind.VERIFICATION_RETRY.value
    }
    state = locks.budget_state(root.task_id, budgets)
    assert state[BudgetKind.VERIFICATION_RETRY.value]["granted_max"] == 2
    # the repair ledger is untouched
    assert state[BudgetKind.REPAIR_ROUND.value] == {
        "consumed": 0,
        "granted_max": 1,
        "remaining": 1,
    }

    # an over-cap request is refused without any partial write
    root2 = builders.new_root(
        repos, root_task_id="T-a32-002", status=RootStatus.WAITING_RECOVERY.value
    )
    repos.controls.ensure(root2.task_id, "seg-1", 256)
    with pytest.raises(RecoveryRejected) as exc:
        harness.recovery.resume(
            root2.task_id,
            ResumeRequest(
                expected_revision=root2.revision,
                additional_execution_retries={"verify": 99},
                reason="狮子大开口",
            ),
            budgets=budgets,
            owner="owner-a",
            operation_key="resume:T-a32-002",
        )
    assert exc.value.code == "GRANT_OVER_CAP"
    assert locks.budget_state(root2.task_id, budgets)[
        BudgetKind.VERIFICATION_RETRY.value
    ]["granted_max"] == 1
    assert repos.tasks.get(root2.task_id).status == RootStatus.WAITING_RECOVERY.value


# --------------------------------------------------------------------------- #
# A33: a fault re-dispatch of a fix attempt consumes both declared budgets
# --------------------------------------------------------------------------- #
def test_a33_fix_fault_retry_consumes_both_declared_budgets(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=sequential_workflow(),
    )
    assert harness.task_service._budgets_for("fix", RetryReason.EXECUTION_FAULT.value) == [
        BudgetKind.REPAIR_ROUND.value,
        BudgetKind.FIX_RETRY.value,
    ]
    # a plain business repair consumes only a repair round
    assert harness.task_service._budgets_for("fix", RetryReason.BUSINESS_REPAIR.value) == [
        BudgetKind.REPAIR_ROUND.value
    ]


# --------------------------------------------------------------------------- #
# A36: old-version evidence cannot close a new-version finding
# --------------------------------------------------------------------------- #
def test_a36_old_version_evidence_cannot_close_a_new_version_finding(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=sequential_workflow(),
    )
    root_id = "T-a36-001"
    builders.new_root(repos, root_task_id=root_id)
    finding = Finding(
        finding_id="f-a36",
        root_task_id=root_id,
        source_version="sv-A",
        producer_attempt_id="att-A",
        goal_ref="避免共享可变默认参数",
        required_for_goal=True,
        check_id="CHK-MUTABLE",
        file_path="helpers.py",
        line=1,
        rule="B006-mutable-default",
        severity=FindingSeverity.ERROR,
        message="共享可变默认参数",
        status=FindingStatus.OPEN,
    )
    repos.findings.upsert(finding)

    # a passing verification that only ever ran on the *old* version
    old_passing = VerificationReport(
        verification_id="v-old",
        root_task_id=root_id,
        producer_attempt_id="att-old",
        source_version="sv-A",
        contract_version="contract-1",
        target_finding_ids=["f-a36"],
        check_results=[
            CheckResult(
                check_id="CHK-MUTABLE",
                contract_version="contract-1",
                source_version="sv-A",
                producer_attempt_id="att-old",
                status=CheckStatus.PASSED,
                evidence_refs=["ev-old"],
                reason="旧版本通过",
            )
        ],
        passed=True,
        evidence_refs=["art-old"],
    )
    repos.verifications.insert(old_passing, artifact_id="art-old")

    # the current version has a passed check for the finding's own check ...
    repos.check_results.insert(
        CheckResult(
            check_id="CHK-MUTABLE",
            contract_version="contract-1",
            source_version="sv-B",
            producer_attempt_id="att-B",
            status=CheckStatus.PASSED,
            evidence_refs=["ev-new"],
            reason="新版本通过",
        ),
        root_id,
    )
    # ... but the only verification evidence is bound to sv-A, so nothing closes
    assert harness.detector.resolve_required_findings(root_task_id=root_id, source_version="sv-B") == []
    assert repos.findings.latest(root_id, "f-a36").status is FindingStatus.OPEN

    # evidence on the current version does close it
    new_passing = old_passing.model_copy(
        update={
            "verification_id": "v-new",
            "source_version": "sv-B",
            "check_results": [
                old_passing.check_results[0].model_copy(update={"source_version": "sv-B"})
            ],
            "evidence_refs": ["art-new"],
        }
    )
    repos.verifications.insert(new_passing, artifact_id="art-new")
    assert harness.detector.resolve_required_findings(
        root_task_id=root_id, source_version="sv-B"
    ) == ["f-a36"]
    assert repos.findings.latest(root_id, "f-a36").status is FindingStatus.RESOLVED


# --------------------------------------------------------------------------- #
# A37: graph-step and parent-correction limits end in waiting_recovery
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a37_graph_step_limit_waits_with_a_reason(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    workflow = _with_budget(sequential_workflow(), max_graph_steps=3)
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id="T-a37-001",
        files=BUGGY_SOURCE,
        findings=[MUTABLE_DEFAULT_FINDING],
        fix_diffs=[FIX_DIFF],
        workflow=workflow,
    )
    assert final["status"] == RootStatus.WAITING_RECOVERY.value, final.get("waiting_reason")
    assert "图执行步数达到上限 3" in (final["waiting_reason"] or "")
    assert "图执行步数" in (final["required_action"] or "")


@pytest.mark.asyncio
async def test_a37_parent_correction_limit_waits_with_a_reason(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    def always_illegal(context):  # type: ignore[no-untyped-def]
        tree = {t["task_kind"]: t for t in context.get("task_tree", []) if t.get("task_kind")}
        return {
            "action": "dispatch_task",
            "task": {
                "agent_id": "verifier",
                "task_kind": "verify",
                "task_id": tree["verify"]["task_id"],
                "goal": "尚无补丁就尝试验证",
                "input_refs": {"source": "auto", "acceptance_contract": "auto"},
                "acceptance_criteria": ["验证"],
                "reason": "非法动作：尚无补丁",
            },
        }

    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id="T-a37-002",
        files=CLEAN_SOURCE,
        llm=make_scripted_client(decide=always_illegal),
    )
    assert final["status"] == RootStatus.WAITING_RECOVERY.value, final.get("waiting_reason")
    assert "非法动作" in (final["waiting_reason"] or "")
    rejections = final.get("action_rejections") or []
    assert len(rejections) >= settings.max_parent_corrections
