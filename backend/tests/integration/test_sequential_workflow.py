"""P04: the real LangGraph + SQLite + workspace sequential closed loop.

Two required acceptance paths (ImplementationPlan §8 验收):
* 首次审查通过  → completed/true, fix and verify skipped with a reason;
* 审查失败 → 修复 → 应用 → 验证通过 → completed/true with current-version evidence.

Events must show every child report returning to the parent.
"""

from __future__ import annotations

import pytest

from app.providers.base import LLMRequest
from app.providers.scripted import ScriptedLLMClient
from app.schemas.enums import ActionType, EventType, RootStatus
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from tests.fixtures.workflow import (
    BUGGY_SOURCE,
    CLEAN_SOURCE,
    MUTABLE_DEFAULT_FINDING,
    _context_of,
    default_plan,
    parent_decide,
    run_workflow,
    sequential_workflow,
)


@pytest.mark.asyncio
async def test_first_review_pass_skips_fix_and_verify(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-clean-001"
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=CLEAN_SOURCE,
    )

    assert final["status"] == RootStatus.COMPLETED.value, {
        "waiting_reason": final.get("waiting_reason"),
        "rejections": final.get("action_rejections"),
        "errors": final.get("errors"),
        "detection": final.get("detection"),
    }
    assert final["passed"] is True

    children = {t.task_kind: t for t in repos.tasks.list_children(root_task_id)}
    assert children["review"].status == "completed"
    assert children["review"].passed is True
    assert children["fix"].status == "skipped"
    assert children["fix"].skip_reason
    assert children["verify"].status == "skipped"
    assert children["verify"].skip_reason

    # only the review batch ran; no patch was applied
    assert len(repos.batches.list_by_root(root_task_id)) == 1
    assert repos.patch_applications.list_by_root(root_task_id) == []
    assert final["report_ref"]

    # every child report came back through the parent's receiving layer
    events = repos.events.list_after(root_task_id, 0, limit=500)
    received = [e for e in events if e.event_type is EventType.RESULT_RECEIVED]
    assert len(received) == 1
    assert received[0].actor_id == "controller"
    assert any(e.event_type is EventType.TASK_SKIPPED for e in events)
    assert any(e.event_type is EventType.TASK_COMPLETED for e in events)


@pytest.mark.asyncio
async def test_review_fail_fix_apply_verify_pass(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-repair-001"
    final, _ = await run_workflow(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        root_task_id=root_task_id,
        files=BUGGY_SOURCE,
        findings=[MUTABLE_DEFAULT_FINDING],
    )

    assert final["status"] == RootStatus.COMPLETED.value
    assert final["passed"] is True

    children = {t.task_kind: t for t in repos.tasks.list_children(root_task_id)}
    assert children["review"].passed is False
    assert children["fix"].status == "completed"
    assert children["fix"].passed is None  # a patch is not proof
    assert children["verify"].passed is True

    # the patch was really applied and verification bound to the new version
    applications = repos.patch_applications.list_by_root(root_task_id)
    assert len(applications) == 1
    application = applications[0]
    assert application.status.value == "committed"
    assert application.result_version is not None
    assert final["source_version"] == application.result_version

    verify_attempts = repos.attempts.list_by_task(children["verify"].task_id)
    assert len(verify_attempts) == 1
    assert verify_attempts[0].source_version == application.result_version

    # the required finding was closed by current-version evidence, not by the fixer
    findings = repos.findings.list_by_root(root_task_id)
    assert findings
    assert all(f.status.value == "resolved" for f in findings)
    assert all(f.resolution_evidence_refs for f in findings)

    # every child attempt produced exactly one accepted report back to the parent
    events = repos.events.list_after(root_task_id, 0, limit=1000)
    received = [e for e in events if e.event_type is EventType.RESULT_RECEIVED]
    assert len(received) == 3
    assert {e.attempt_id for e in received} == {
        a.attempt_id for a in repos.attempts.list_by_root(root_task_id)
    }
    assert any(e.event_type is EventType.PATCH_APPLIED for e in events)
    assert any(e.event_type is EventType.PARENT_DECIDED for e in events)

    # the fixer's input carried the review findings and the current version
    fix_attempts = repos.attempts.list_by_task(children["fix"].task_id)
    assert fix_attempts
    assert "findings" in fix_attempts[0].input_refs
    assert fix_attempts[0].source_version == application.base_version


@pytest.mark.asyncio
async def test_graph_runs_on_the_real_sqlite_checkpointer(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """The closed loop must run on the real LangGraph + SQLite checkpointer."""
    from app.storage.checkpoints import open_checkpointer, thread_id_for

    root_task_id = "T-ckpt-001"
    thread_id = thread_id_for(root_task_id)
    async with open_checkpointer(settings.checkpoints_db_path) as checkpointer:
        final, _ = await run_workflow(
            settings=settings,
            repos=repos,
            artifacts=artifacts,
            workspace=workspace,
            locks=locks,
            root_task_id=root_task_id,
            files=CLEAN_SOURCE,
            checkpointer=checkpointer,
        )
        assert final["status"] == RootStatus.COMPLETED.value
        assert final["contract_version"] == "contract-1"
        assert final["task_tree"]
    assert settings.checkpoints_db_path.exists()


@pytest.mark.asyncio
async def test_illegal_parent_action_is_corrected(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """An illegal action is rejected with a reason and the run continues (§9.3)."""
    root_task_id = "T-illegal-001"
    seen: list[int] = []

    def handler(request: LLMRequest):
        key = request.script_key or ""
        if key == "parent:plan":
            return default_plan()
        if key == "parent:decide":
            context = _context_of(request)
            seen.append(len(seen))
            if len(seen) == 1:
                tree = {t["task_kind"]: t for t in context["task_tree"]}
                return {
                    "reasoning": "先尝试直接验证",
                    "action": {
                        "action": "dispatch_task",
                        "task": {
                            "agent_id": "verifier",
                            "task_kind": "verify",
                            "task_id": tree["verify"]["task_id"],
                            "goal": "直接验证",
                            "input_refs": {"source": "auto", "acceptance_contract": "auto"},
                            "acceptance_criteria": ["验证"],
                            "reason": "非法动作：尚无补丁",
                        },
                    },
                }
            return {"reasoning": "改为派发审查", "action": parent_decide(context, [])}
        if key.endswith(":reviewer:step"):
            return {
                "reason": "审查完成",
                "final": {
                    "summary": "无问题",
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
        workflow=sequential_workflow(),
        llm=ScriptedLLMClient(handler),
    )

    assert final["status"] == RootStatus.COMPLETED.value
    rejections = final["action_rejections"]
    assert rejections and rejections[0]["code"] == "ILLEGAL_ACTION"
    assert "补丁应用" in rejections[0]["message"]
    events = repos.events.list_after(root_task_id, 0, limit=500)
    assert any(e.event_type is EventType.ACTION_REJECTED for e in events)
    assert any(e.payload.get("action") == ActionType.DISPATCH_TASK.value for e in events)
