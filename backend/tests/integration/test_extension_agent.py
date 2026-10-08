"""P10: a newly registered role is a real integration, not a database row.

The extension (a test-generation role) is registered through the normal
mechanism: capability spec, implementation, tool authorization, declared input
adapters/dependencies and a declared retry budget. This test proves it end to end
with the real graph, real tools and the real fixed test executor:

* the parent dispatches the extension task through the generic ``dispatch_task``;
* its report returns to the parent through the same adapter/detection entry;
* the produced ``test_artifact`` bundle is dispatched as *verifier input* and is
  actually executed (and proven to fail on the pre-fix baseline);
* the extension's failure budget is bounded (no unbounded-retry default);
* a checkpointed run of the extension template can be resumed, and the saved
  config keeps fixing the extension role version.
"""

from __future__ import annotations

import pytest

from app.extensions import build_default_extension_registry
from app.extensions.base import ExtensionError, validate_workflow_config
from app.registry.agents import build_default_agent_registry
from app.schemas.enums import ArtifactType, CheckStatus, RootStatus
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from tests.fixtures.workflow import (
    BUGGY_SOURCE,
    MUTABLE_DEFAULT_FINDING,
    build_harness,
    default_plan,
    extension_workflow,
    make_scripted_client,
    prepare_source,
)
from app.workflow.runner import WorkflowRunner
from app.workflow.state import initial_state

EXTENSION_PLAN = {
    **default_plan(),
    "checks": [
        *default_plan()["checks"],
        {
            "check_id": "CHK-BEHAVIOR",
            "goal_ref": "避免共享可变默认参数",
            "scope": ["helpers.py"],
            "required": True,
            "method": "behavior_test",
            "pass_condition": "生成的最小复现测试在修改后版本通过",
            "applicable_phase": "post_patch",
        },
    ],
}


def extension_decide(context: dict) -> dict:
    """Parent logic for the extension template: generate tests before fixing."""
    tree = {t["node_id"]: t for t in context.get("task_tree", []) if t.get("node_id")}
    reports = context.get("latest_reports", [])
    ran = {r["node_id"] for r in reports if r.get("node_id")}
    detection = context.get("detection") or {}
    category = detection.get("category")
    patched = bool(context.get("patched"))
    source_version = context.get("source_version")

    def dispatch(node_id: str, reason: str) -> dict:
        task = tree[node_id]
        return {
            "action": "dispatch_task",
            "task": {
                "agent_id": task["agent_id"],
                "task_kind": task["task_kind"],
                "task_id": task["task_id"],
                "goal": f"{task['task_kind']}：{context.get('goal') or ''}",
                "input_refs": {"source": "auto", "acceptance_contract": "auto"},
                "acceptance_criteria": ["覆盖全部必需检查项"],
                "reason": reason,
            },
        }

    if "review" not in ran:
        return dispatch("review", "首次审查")
    if "gen" not in ran:
        return dispatch("gen", "为行为检查项生成最小复现测试")
    if context.get("unapplied_fix_attempt_id"):
        return {
            "action": "apply_patch",
            "patch_ref": "auto",
            "base_version": source_version,
            "repair_attempt_id": context["unapplied_fix_attempt_id"],
            "reason": "应用修复 Agent 的候选补丁",
        }
    if patched and context.get("last_verified_source_version") != source_version:
        return dispatch("verify", "验证修改后版本（含生成的测试）")
    if category == "pass":
        return {
            "action": "finish",
            "proposed_passed": True,
            "report_refs": [],
            "reason": "当前版本的必需检查全部通过",
        }
    if category in ("code_defect", "no_progress"):
        if int(context.get("repair_rounds_remaining") or 0) <= 0:
            return {
                "action": "wait_for_recovery",
                "reason": "修复额度耗尽",
                "required_action": "追加 repair_round 额度后恢复任务",
            }
        return dispatch("fix", "根据失败证据生成候选补丁")
    return {
        "action": "wait_for_recovery",
        "reason": "当前无法自动继续",
        "required_action": "人工确认后恢复任务",
    }


@pytest.mark.asyncio
async def test_extension_role_is_dispatched_reported_and_used_as_verifier_input(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    extensions = build_default_extension_registry()
    workflow = extension_workflow(extensions=extensions)
    root_task_id = "T-ext-001"
    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=BUGGY_SOURCE
    )
    repos.workflows.insert(workflow)
    llm = make_scripted_client(
        findings=[MUTABLE_DEFAULT_FINDING], plan=EXTENSION_PLAN, decide=extension_decide
    )
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=llm,
        workflow=workflow,
        extensions=extensions,
    )
    state = initial_state(
        root_task_id=root_task_id,
        goal="检查可变默认参数并修复验证",
        workflow_version=workflow.workflow_version or "wf-ext-1",
        check_mode=workflow.check_mode.value,
        run_segment_id="seg-1",
        source_version=source_version,
        source_artifact=source_artifact,
        agent_versions=dict(workflow.agents),
    )
    final = await harness.graph.ainvoke(
        state, config={"configurable": {"thread_id": f"hw2-task:{root_task_id}"}}
    )
    assert final["status"] == RootStatus.COMPLETED.value, {
        "waiting_reason": final.get("waiting_reason"),
        "rejections": final.get("action_rejections"),
        "errors": final.get("errors"),
        "detection": final.get("detection"),
    }
    assert final["passed"] is True

    # 1. the extension node exists as a real child task, dispatched and reported
    children = {t.task_kind: t for t in repos.tasks.list_children(root_task_id)}
    assert "generate_tests" in children
    gen_task = children["generate_tests"]
    assert gen_task.agent_id == "test_generator"
    assert gen_task.status == "completed"
    # a generator decides no verdict
    assert gen_task.passed is None

    gen_attempts = repos.attempts.list_by_task(gen_task.task_id)
    assert len(gen_attempts) == 1
    gen_result = repos.results.get_by_attempt(gen_attempts[0].attempt_id)
    assert gen_result is not None and gen_result.task_kind == "generate_tests"
    assert gen_result.status.value == "completed"

    # 2. its artifact is a real test bundle
    bundles = [
        a
        for a in artifacts.list_by_root(root_task_id, ArtifactType.TEST_ARTIFACT)
        if a.metadata.get("bundle")
    ]
    assert len(bundles) == 1, [a.artifact_id for a in artifacts.list_by_root(root_task_id)]
    bundle = bundles[0]
    payload = artifacts.read_json(bundle.artifact_id)
    assert {t["check_id"] for t in payload["tests"]} == {"CHK-BEHAVIOR"}

    # 3. the bundle was dispatched as verifier input (the plugin's adapter did it)
    verify_task = children["verify"]
    verify_attempts = repos.attempts.list_by_task(verify_task.task_id)
    assert verify_attempts, "verify was never dispatched"
    assert verify_attempts[-1].input_refs.get("generated_tests") == bundle.artifact_id

    # 4. the generated test really ran: it failed on the baseline, passed now
    checks = repos.check_results.latest_for_check(
        root_task_id, "CHK-BEHAVIOR", final["source_version"]
    )
    assert checks is not None, "CHK-BEHAVIOR was never executed"
    assert checks.status is CheckStatus.PASSED
    assert checks.executed is True
    assert checks.test_count >= 1
    assert "基础版本" in checks.reason
    assert "failed" in checks.reason.split("基础版本", 1)[1]

    # 5. the extension's failure budget is declared and bounded: an unknown task
    #    kind cannot fall into the unbounded-retry default
    from app.services.execution_locks import TASK_KIND_FAULT_BUDGET
    from app.services.task_service import DispatchRejected

    assert extensions.fault_budget("generate_tests") == "generate_retry"
    assert TASK_KIND_FAULT_BUDGET["generate_tests"] == "generate_retry"
    assert workflow.budgets.max_generate_retries == 2
    with pytest.raises(DispatchRejected) as excinfo:
        harness.task_service._budgets_for("ghost_kind", "execution_fault")
    assert excinfo.value.code == "UNKNOWN_RETRY_BUDGET"

    events = repos.events.list_after(root_task_id, 0, limit=500)
    assert any(
        e.event_type.value == "task_dispatched" and e.payload.get("task_kind") == "generate_tests"
        for e in events
    )


@pytest.mark.asyncio
async def test_extension_run_resumes_from_its_own_checkpoint(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    """An extension role is tracked and recoverable like any other child."""
    extensions = build_default_extension_registry()
    workflow = extension_workflow(extensions=extensions)
    root_task_id = "T-ext-resume"
    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=BUGGY_SOURCE
    )
    repos.workflows.insert(workflow)
    llm = make_scripted_client(
        findings=[MUTABLE_DEFAULT_FINDING], plan=EXTENSION_PLAN, decide=extension_decide
    )
    # stop before the re-dispatch decision so the run is left mid-flight
    calls = {"n": 0}

    def hook(request) -> None:  # type: ignore[no-untyped-def]
        if request.script_key == "parent:decide":
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated interruption before re-dispatch")

    llm = make_scripted_client(
        findings=[MUTABLE_DEFAULT_FINDING],
        plan=EXTENSION_PLAN,
        decide=extension_decide,
        handler_hook=hook,
    )

    from app.storage.checkpoints import open_checkpointer, thread_id_for

    async with open_checkpointer(settings.checkpoints_db_path) as checkpointer:
        harness = build_harness(
            settings=settings,
            repos=repos,
            artifacts=artifacts,
            workspace=workspace,
            locks=locks,
            llm=llm,
            workflow=workflow,
            checkpointer=checkpointer,
            extensions=extensions,
        )
        runner = WorkflowRunner(
            runtime=harness.runtime, graph=harness.graph, recovery=harness.recovery
        )
        state = initial_state(
            root_task_id=root_task_id,
            goal="检查可变默认参数并修复验证",
            workflow_version=workflow.workflow_version or "wf-ext-1",
            check_mode=workflow.check_mode.value,
            run_segment_id="seg-1",
            source_version=source_version,
            source_artifact=source_artifact,
            agent_versions=dict(workflow.agents),
        )
        with pytest.raises(RuntimeError):
            await runner.graph.ainvoke(
                state, config=runner.config(root_task_id), durability="sync"
            )

        snapshot = await runner.graph.aget_state(runner.config(root_task_id))
        assert snapshot.values, "no checkpoint was written"

        # the extension child task was created and is still part of the tree
        children = {t.task_kind: t for t in repos.tasks.list_children(root_task_id)}
        assert "generate_tests" in children

        resumed = await runner.graph.ainvoke(None, config=runner.config(root_task_id))
        assert resumed["status"] == RootStatus.COMPLETED.value, {
            "waiting_reason": resumed.get("waiting_reason"),
            "detection": resumed.get("detection"),
        }
        assert resumed["passed"] is True
        assert thread_id_for(root_task_id) == f"hw2-task:{root_task_id}"

        # the extension result survived the interruption
        gen_results = [
            r
            for r in repos.results.list_by_root(root_task_id)
            if r.task_kind == "generate_tests"
        ]
        assert gen_results and gen_results[0].status.value == "completed"


def test_unknown_task_kind_cannot_be_saved_in_a_config() -> None:
    """A config referencing an unregistered role is rejected, naming the field."""
    registry = build_default_agent_registry(extensions=build_default_extension_registry())
    extensions = build_default_extension_registry()
    workflow = extension_workflow(extensions=extensions)
    # a legal extension config validates
    validate_workflow_config(workflow, registry, extensions)

    broken = workflow.model_copy(
        update={
            "nodes": [
                *workflow.nodes,
                workflow.nodes[0].model_copy(
                    update={"id": "ghost", "agent_id": "reviewer", "task_kind": "ghost_kind"}
                ),
            ]
        }
    )
    with pytest.raises(ExtensionError) as excinfo:
        validate_workflow_config(broken, registry, extensions)
    assert excinfo.value.code == "UNKNOWN_TASK_KIND"
    assert excinfo.value.details["node_id"] == "ghost"


def test_extension_registry_refuses_a_kind_without_a_retry_budget() -> None:
    """A new task kind must declare a budget instead of defaulting to unlimited."""
    from app.extensions.base import FlowExtension, TaskKindPlugin
    from app.schemas.agents import AgentSpec

    spec = AgentSpec(
        agent_id="ghost_role",
        version="1.0",
        description="无预算的扩展角色",
        supported_task_kinds=["ghost_kind"],
        retry_budget={"ghost_kind": "review_retry"},
    )

    class _Impl:
        async def execute(self, task, context):  # pragma: no cover - never run
            raise NotImplementedError

    plugin = TaskKindPlugin(
        task_kind="ghost_kind", agent_id="ghost_role", retry_budget_kind=""
    )
    with pytest.raises(ExtensionError) as excinfo:
        build_default_extension_registry().register(
            FlowExtension(
                extension_id="ghost",
                version="1.0",
                spec=spec,
                implementation=_Impl(),  # type: ignore[arg-type]
                plugins=(plugin,),
            )
        )
    assert excinfo.value.code == "EXTENSION_NO_RETRY_BUDGET"
