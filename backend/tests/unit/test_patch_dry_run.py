"""A broken diff is reported to the model and does not burn a repair round."""

from __future__ import annotations

import pytest

from app.registry.tools import ToolContext, new_tool_registry
from app.schemas.enums import ArtifactType, BudgetKind, RetryReason
from app.services.artifacts import ArtifactService
from app.services.events import EventContext, EventSink
from app.services.execution_locks import BudgetExhausted, ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from tests.fixtures import builders
from tests.fixtures.workflow import sequential_workflow

SOURCE = "def add(a, b):\n    return a - b\n"
GOOD_DIFF = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""
BAD_DIFF = """--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a * b
"""


def _ctx(repos: Repos, artifacts: ArtifactService, workspace: WorkspaceService, root_id: str, version: str) -> ToolContext:
    return ToolContext(
        actor_id="fixer",
        root_task_id=root_id,
        workspace=workspace,
        artifacts=artifacts,
        repos=repos,
        sink=EventSink(repos.events, EventContext(root_task_id=root_id, actor_id="fixer")),
        source_version=version,
        attempt_id="fix-A1",
    )


@pytest.mark.asyncio
async def test_bad_diff_does_not_burn_repair_round_and_a_valid_diff_can_follow(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    workflow = sequential_workflow()
    root = builders.new_root(repos, root_task_id="T-patch-dry", workflow_version=workflow.workflow_version or "wf-1")
    manifest = workspace.publish_snapshot(root_task_id=root.task_id, files={"calc.py": SOURCE.encode()})
    locks.consume(root.task_id, BudgetKind.REPAIR_ROUND.value, "consume:seed", "已有一轮修复")
    before = locks.budget_state(root.task_id, workflow.budgets)[BudgetKind.REPAIR_ROUND.value]["consumed"]
    tools = new_tool_registry()
    ctx = _ctx(repos, artifacts, workspace, root.task_id, manifest.source_version)

    rejected = await tools.execute(
        "submit_patch",
        ctx,
        base_version=manifest.source_version,
        rationale="错误的上下文",
        format="unified_diff",
        diff_text=BAD_DIFF,
    )
    assert rejected.ok is False
    assert rejected.error is not None
    assert rejected.error.code == "PATCH_INVALID"
    assert rejected.summary
    assert artifacts.list_by_root(root.task_id, ArtifactType.PATCH) == []
    assert locks.budget_state(root.task_id, workflow.budgets)[BudgetKind.REPAIR_ROUND.value]["consumed"] == before

    accepted = await tools.execute(
        "submit_patch",
        ctx,
        base_version=manifest.source_version,
        rationale="改成加法",
        format="unified_diff",
        diff_text=GOOD_DIFF,
    )
    assert accepted.ok is True
    assert accepted.data["patch_id"]
    assert len(artifacts.list_by_root(root.task_id, ArtifactType.PATCH)) == 1
    assert locks.budget_state(root.task_id, workflow.budgets)[BudgetKind.REPAIR_ROUND.value]["consumed"] == before


def test_patch_invalid_budget_is_separate_and_caps_at_two(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    from app.schemas.actions import DispatchItem
    from app.schemas.tasks import InputRefs
    from app.services.task_service import TaskService

    workflow = sequential_workflow()
    root = builders.new_root(repos, root_task_id="T-patch-cap", workflow_version=workflow.workflow_version or "wf-1")
    service = TaskService(repos, locks)
    assert service._budgets_for("fix", RetryReason.INITIAL.value) == [BudgetKind.REPAIR_ROUND.value]
    assert service._budgets_for("fix", RetryReason.BUSINESS_REPAIR.value) == [BudgetKind.REPAIR_ROUND.value]
    assert service._budgets_for("fix", RetryReason.EXECUTION_FAULT.value) == [BudgetKind.FIX_RETRY.value]
    assert service._budgets_for("fix", RetryReason.PATCH_INVALID.value) == [BudgetKind.PATCH_INVALID.value]

    def item(task_id: str | None) -> DispatchItem:
        return DispatchItem(
            agent_id="fixer",
            task_kind="fix",
            goal="提交可应用的补丁",
            input_refs=InputRefs(source="art-source", acceptance_contract="art-contract"),
            acceptance_criteria=["补丁能应用"],
            task_id=task_id,
            reason="补丁无效后重试",
        )

    task_id = None
    for _ in range(2):
        _batch, attempts = service.register_dispatch(
            root_task_id=root.task_id,
            items=[item(task_id)],
            workflow=workflow,
            source_version="sv-1",
            contract_version="contract-1",
            retry_reason=RetryReason.PATCH_INVALID.value,
        )
        task_id = attempts[0].task_id
    state = locks.budget_state(root.task_id, workflow.budgets)
    assert state[BudgetKind.PATCH_INVALID.value]["consumed"] == 2
    assert state[BudgetKind.PATCH_INVALID.value]["remaining"] == 0
    assert state[BudgetKind.REPAIR_ROUND.value]["consumed"] == 0

    with pytest.raises(BudgetExhausted):
        service.register_dispatch(
            root_task_id=root.task_id,
            items=[item(task_id)],
            workflow=workflow,
            source_version="sv-1",
            contract_version="contract-1",
            retry_reason=RetryReason.PATCH_INVALID.value,
        )
    assert locks.budget_state(root.task_id, workflow.budgets)[BudgetKind.REPAIR_ROUND.value]["consumed"] == 0
    assert len(repos.attempts.list_by_task(task_id or "")) == 2
