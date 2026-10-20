"""LangGraph Send fan-out, human interrupt, and lease heartbeat."""

from __future__ import annotations

import asyncio
import time

import pytest

from app.providers.base import LLMError
from app.schemas.api import ResumeRequest
from app.schemas.enums import ErrorCategory, EventType, RootStatus
from app.services.artifacts import ArtifactService
from app.services.execution_context import ExecutionFenced
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.checkpoints import open_checkpointer, thread_id_for
from app.storage.repositories import Repos
from app.workflow.builder import GRAPH_RECURSION_LIMIT
from app.workflow.recovery import Reconciliation, RunRight
from app.workflow.runner import RunCancelled, WorkflowRunner
from app.workflow.state import initial_state
from tests.fixtures.workflow import (
    BUGGY_SOURCE,
    FIX_DIFF,
    MUTABLE_DEFAULT_FINDING,
    build_harness,
    make_scripted_client,
    parallel_workflow,
    prepare_source,
    sequential_workflow,
)


class SuperstepCrash(BaseException):
    """Uncaught branch failure. ``except Exception`` in the node must not swallow it."""


def _config(root_task_id: str) -> dict:
    return {
        "recursion_limit": GRAPH_RECURSION_LIMIT,
        "configurable": {"thread_id": thread_id_for(root_task_id)},
    }


def test_mermaid_shows_dispatch_fanout_and_wait_interrupt(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
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
    drawable = harness.graph.get_graph()
    mermaid = drawable.draw_mermaid()
    assert "dispatch -.-> run_attempt" in mermaid
    assert "run_attempt --> collect_results" in mermaid
    pairs = {(edge.source, edge.target) for edge in drawable.edges}
    assert ("dispatch", "run_attempt") in pairs
    assert ("run_attempt", "collect_results") in pairs
    assert ("enter_wait", "await_human") in pairs


@pytest.mark.asyncio
async def test_recoverable_error_retries_only_that_branch(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-send-retry"
    workflow = parallel_workflow()
    repos.workflows.insert(workflow)
    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=BUGGY_SOURCE
    )
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[FIX_DIFF]),
        workflow=workflow,
    )
    counts: dict[str, int] = {}
    failed: set[str] = set()
    original = harness.runtime.adapter.execute

    async def execute(envelope):  # type: ignore[no-untyped-def]
        counts[envelope.attempt_id] = counts.get(envelope.attempt_id, 0) + 1
        task = repos.tasks.get(envelope.task_id)
        goal = task.goal if task is not None else ""
        if "节点 recheck" in goal and envelope.attempt_id not in failed:
            failed.add(envelope.attempt_id)
            raise LLMError(
                "TRANSIENT",
                "transient model error",
                category=ErrorCategory.MODEL_ERROR,
                recoverable=True,
            )
        return await original(envelope)

    harness.runtime.adapter.execute = execute
    state = initial_state(
        root_task_id=root_task_id,
        goal="检查可变默认参数并修复验证",
        workflow_version=workflow.workflow_version or "wf-1",
        check_mode=workflow.check_mode.value,
        run_segment_id="seg-1",
        source_version=source_version,
        source_artifact=source_artifact,
        agent_versions=dict(workflow.agents),
    )
    final = await harness.graph.ainvoke(state, config=_config(root_task_id))
    assert final["status"] == RootStatus.COMPLETED.value, {
        "waiting_reason": final.get("waiting_reason"),
        "rejections": final.get("action_rejections"),
        "detection": final.get("detection"),
    }

    recheck = next(task for task in repos.tasks.list_children(root_task_id) if "节点 recheck" in task.goal)
    verify = next(task for task in repos.tasks.list_children(root_task_id) if task.agent_id == "verifier")
    recheck_attempts = repos.attempts.list_by_task(recheck.task_id)
    verify_attempts = repos.attempts.list_by_task(verify.task_id)
    assert len(recheck_attempts) == 1
    assert len(verify_attempts) == 1
    assert counts[recheck_attempts[0].attempt_id] == 2
    assert counts[verify_attempts[0].attempt_id] == 1


@pytest.mark.asyncio
async def test_failed_superstep_does_not_rerun_a_finished_branch(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-send-crash"
    workflow = parallel_workflow()
    repos.workflows.insert(workflow)
    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=BUGGY_SOURCE
    )
    async with open_checkpointer(settings.checkpoints_db_path) as checkpointer:
        harness = build_harness(
            settings=settings,
            repos=repos,
            artifacts=artifacts,
            workspace=workspace,
            locks=locks,
            llm=make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[FIX_DIFF]),
            workflow=workflow,
            checkpointer=checkpointer,
        )
        counts: dict[str, int] = {}
        sibling_done = asyncio.Event()
        original = harness.runtime.adapter.execute

        async def execute(envelope):  # type: ignore[no-untyped-def]
            counts[envelope.attempt_id] = counts.get(envelope.attempt_id, 0) + 1
            task = repos.tasks.get(envelope.task_id)
            goal = task.goal if task is not None else ""
            if "节点 recheck" in goal and counts[envelope.attempt_id] == 1:
                await sibling_done.wait()
                await asyncio.sleep(0.2)
                raise SuperstepCrash("sibling already returned")
            result = await original(envelope)
            if envelope.agent_id == "verifier":
                sibling_done.set()
            return result

        harness.runtime.adapter.execute = execute
        state = initial_state(
            root_task_id=root_task_id,
            goal="检查可变默认参数并修复验证",
            workflow_version=workflow.workflow_version or "wf-1",
            check_mode=workflow.check_mode.value,
            run_segment_id="seg-1",
            source_version=source_version,
            source_artifact=source_artifact,
            agent_versions=dict(workflow.agents),
        )
        config = _config(root_task_id)
        with pytest.raises(SuperstepCrash):
            await harness.graph.ainvoke(state, config, durability="sync")

        verify = next(
            task for task in repos.tasks.list_children(root_task_id) if task.agent_id == "verifier"
        )
        verify_attempt = repos.attempts.list_by_task(verify.task_id)[0]
        assert counts[verify_attempt.attempt_id] == 1

        final = await harness.graph.ainvoke(None, config, durability="sync")

    assert counts[verify_attempt.attempt_id] == 1
    assert final["status"] == RootStatus.COMPLETED.value, {
        "waiting_reason": final.get("waiting_reason"),
        "rejections": final.get("action_rejections"),
        "detection": final.get("detection"),
    }


@pytest.mark.asyncio
async def test_interrupt_resume_keeps_one_waiting_event_and_no_forged_detect(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    root_task_id = "T-send-wait"
    workflow = sequential_workflow(max_repair_rounds=0)
    repos.workflows.insert(workflow)
    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=BUGGY_SOURCE
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
            runtime=harness.runtime,
            graph=harness.graph,
            recovery=harness.recovery,
            owner="interrupt-owner",
        )
        final = await runner.start(
            root_task_id=root_task_id,
            goal="修复可变默认参数",
            workflow_version=workflow.workflow_version or "wf-1",
            check_mode=workflow.check_mode.value,
            source_version=source_version,
            source_artifact=source_artifact,
        )
        assert final["status"] == RootStatus.WAITING_RECOVERY.value
        snapshot = await harness.graph.aget_state(runner.config(root_task_id))
        interrupts = [item for task in snapshot.tasks for item in task.interrupts]
        assert interrupts
        assert interrupts[0].value["reason"]
        assert final["waiting_reason"] in str(interrupts[0].value["reason"])

        waiting = [
            event
            for event in repos.events.list_after(root_task_id, 0, limit=2000)
            if event.event_type is EventType.WAITING_RECOVERY
        ]
        assert len(waiting) == 1

        harness.recovery.resume(
            root_task_id,
            ResumeRequest(
                expected_revision=repos.tasks.get(root_task_id).revision,
                additional_repair_rounds=1,
                reason="追加一轮修复",
            ),
            budgets=workflow.budgets,
            owner="interrupt-owner",
            operation_key="resume-send-wait",
        )
        second = await runner.resume(root_task_id=root_task_id)
        assert second["status"] == RootStatus.COMPLETED.value, {
            "waiting_reason": second.get("waiting_reason"),
            "rejections": second.get("action_rejections"),
        }

        waiting_after = [
            event
            for event in repos.events.list_after(root_task_id, 0, limit=2000)
            if event.event_type is EventType.WAITING_RECOVERY
        ]
        assert len(waiting_after) == 1

        history = [item async for item in harness.graph.aget_state_history(runner.config(root_task_id))]
        assert all(item.metadata.get("source") != "update" for item in history)
        chronological = list(reversed(history))
        next_nodes = [tuple(item.next) for item in chronological]
        wait_at = next(index for index, nodes in enumerate(next_nodes) if "await_human" in nodes)
        assert any("budget" in nodes for nodes in next_nodes[wait_at + 1 :])


@pytest.mark.asyncio
async def test_resume_acquires_the_lease_before_reconcile() -> None:
    order: list[str] = []

    class Recovery:
        def acquire_run_right(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            order.append("acquire")
            return RunRight("T-lease", "owner", 2, "seg-2")

        def reconcile(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            order.append("reconcile")
            return Reconciliation(source_version="sv", patched=False)

        def heartbeat(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return True

        def holds_right(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return True

        def release(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return True

    class Snapshot:
        values = {
            "contract": {"contract_version": "c1"},
            "source_version": "sv",
            "action_rejections": [{"code": "OLD"}],
            "initial_source_version": "sv",
            "source_artifact": "art",
        }
        next = ()
        tasks = ()

    class Graph:
        checkpointer = None

        async def aget_state(self, _config):  # type: ignore[no-untyped-def]
            return Snapshot()

        async def aupdate_state(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        async def ainvoke(self, _state, _config, **_kwargs):  # type: ignore[no-untyped-def]
            return {"status": "completed"}

    class Runtime:
        fencing_token = 0
        cancellation = asyncio.Event()

    runner = WorkflowRunner(
        runtime=Runtime(), graph=Graph(), recovery=Recovery(), owner="owner"  # type: ignore[arg-type]
    )
    await runner.resume(root_task_id="T-lease", ttl_seconds=30)
    assert order.index("acquire") < order.index("reconcile")


@pytest.mark.asyncio
async def test_lost_heartbeat_cancels_the_drive_and_stops_the_model() -> None:
    class Recovery:
        def heartbeat(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return False

        def holds_right(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return False

        def release(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return False

    class Graph:
        def __init__(self) -> None:
            self.calls = 0
            self.checkpointer = None

        async def ainvoke(self, _state, _config, **_kwargs):  # type: ignore[no-untyped-def]
            while True:
                self.calls += 1
                await asyncio.sleep(0.05)

    class Runtime:
        def __init__(self) -> None:
            self.fencing_token = 0
            self.cancellation = asyncio.Event()

    graph = Graph()
    runtime = Runtime()
    runner = WorkflowRunner(
        runtime=runtime, graph=graph, recovery=Recovery(), owner="owner"  # type: ignore[arg-type]
    )
    runner.heartbeat_interval = 0.05
    right = RunRight("T-beat", "owner", 1, "seg-1")
    started = time.monotonic()
    drive = asyncio.create_task(runner._drive(right, {}, ttl_seconds=0.15))
    with pytest.raises((ExecutionFenced, RunCancelled)):
        await drive
    elapsed = time.monotonic() - started
    assert elapsed < 2
    stopped_at = graph.calls
    await asyncio.sleep(0.3)
    assert graph.calls == stopped_at
    assert runner.cancel_event.is_set()
    assert runtime.cancellation.is_set()
