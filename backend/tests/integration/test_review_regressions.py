import asyncio
import time
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from tests.fixtures.agents import root_sink

import pytest


from app.schemas.api import ResumeRequest
from app.storage.checkpoints import open_checkpointer
from app.workflow.runner import WorkflowRunner
from tests.fixtures.workflow import sequential_workflow, prepare_source, build_harness, make_scripted_client, BUGGY_SOURCE, MUTABLE_DEFAULT_FINDING

@pytest.mark.asyncio
async def test_resume_grants_a_new_graph_segment(settings, repos, artifacts, workspace, locks):
    workflow = sequential_workflow()
    workflow = workflow.model_copy(update={
        "budgets": workflow.budgets.model_copy(update={"max_graph_steps": 3})
    })
    llm = make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING])
    root_id = "T-graph-segment"
    repos.workflows.insert(workflow)
    version, source = prepare_source(workspace, repos, root_task_id=root_id, files=BUGGY_SOURCE)
    async with open_checkpointer(settings.checkpoints_db_path) as cp:
        h = build_harness(settings=settings, repos=repos, artifacts=artifacts, workspace=workspace, locks=locks, llm=llm, workflow=workflow, checkpointer=cp)
        runner = WorkflowRunner(runtime=h.runtime, graph=h.graph, recovery=h.recovery, owner='probe')
        first = await runner.start(root_task_id=root_id, goal='检查可变默认参数并修复', workflow_version=workflow.workflow_version, check_mode=workflow.check_mode.value, source_version=version, source_artifact=source)
        assert first['status'] == 'waiting_recovery'
        before = len(llm.calls)
        h.recovery.resume(root_id, ResumeRequest(expected_revision=repos.tasks.get(root_id).revision, reason='explicit permission for next segment'), budgets=workflow.budgets, owner='probe', operation_key='resume-'+root_id)
        second = await runner.resume(root_task_id=root_id)
        assert len(llm.calls) > before
        assert second['graph_steps'] <= workflow.budgets.max_graph_steps * 2



@pytest.mark.asyncio
async def test_resume_keeps_denial_audit_without_reusing_old_corrections(settings, repos, artifacts, workspace, locks):
    from tests.fixtures.workflow import run_parent_decide

    contexts = []
    def decide(context):
        contexts.append(context)
        # Deliberately propose repair without allowance until the controller's
        # correction limit is reached, as a real model can do before recovery.
        if context.get("latest_reports") and not context.get("patched") and not context.get("unapplied_fix_attempt_id") and context["repair_rounds_remaining"] == 0:
            child = next(t for t in context["task_tree"] if t["task_kind"] == "fix")
            return {"action": "dispatch_task", "task": {
                "agent_id": "fixer", "task_kind": "fix", "task_id": child["task_id"],
                "goal": "修复可变默认参数", "reason": "已有缺陷证据",
                "input_refs": {"source": "auto", "acceptance_contract": "auto"},
                "acceptance_criteria": ["修复后验证必需检查"],
            }}
        return run_parent_decide(context)

    workflow = sequential_workflow(max_repair_rounds=0)
    repos.workflows.insert(workflow)
    root_id = "T-resume-rejection-history"
    version, source = prepare_source(workspace, repos, root_task_id=root_id, files=BUGGY_SOURCE)
    llm = make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], decide=decide)
    async with open_checkpointer(settings.checkpoints_db_path) as cp:
        h = build_harness(settings=settings, repos=repos, artifacts=artifacts, workspace=workspace, locks=locks, llm=llm, workflow=workflow, checkpointer=cp)
        runner = WorkflowRunner(runtime=h.runtime, graph=h.graph, recovery=h.recovery, owner="recovery-test")
        first = await runner.start(root_task_id=root_id, goal="修复可变默认参数", workflow_version=workflow.workflow_version, check_mode="sequential", source_version=version, source_artifact=source)
        assert first["status"] == "waiting_recovery"
        old_denials = list(first["action_rejections"])
        assert len(old_denials) == settings.max_parent_corrections
        before = len(contexts)
        call_count = len(llm.calls)
        h.recovery.resume(root_id, ResumeRequest(expected_revision=repos.tasks.get(root_id).revision, additional_repair_rounds=1, reason="追加一轮修复"), budgets=workflow.budgets, owner="recovery-test", operation_key="resume-denials")
        second = await runner.resume(root_task_id=root_id)
        assert second["status"] == "completed" and second["passed"] is True
        assert contexts[before]["repair_rounds_remaining"] == 1
        assert contexts[before]["action_rejections"] == []
        first_decision = next(r for r in llm.calls[call_count:] if r.script_key == "parent:decide")
        assert len(first_decision.messages) == 1
        assert second["action_rejections"] == old_denials


def test_stale_agent_result_rejected(repos, locks):
    from tests.fixtures import builders
    from tests.integration.test_recovery import _envelope
    from app.schemas.results import TaskResult
    from app.services.task_service import TaskService
    root = builders.new_root(repos)
    child = builders.new_child(repos, root, task_id='T100-C001', task_kind='review')
    attempt = builders.new_attempt(repos, root, child, attempt_id='T100-C001-A1', batch_id='B1')
    repos.controls.ensure(root.task_id, 'seg-1', 256)
    _, a = locks.acquire(root.task_id, 'owner-a')
    repos.controls.update(root.task_id, lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    _, b = locks.acquire(root.task_id, 'owner-b')
    envelope = _envelope(root.task_id, child.task_id, attempt.attempt_id, 'B1')
    data = envelope.model_dump(exclude={'goal','input_refs','acceptance_criteria','constraints','fencing_token'})
    result = TaskResult(**data, result_id='r-stale', status='completed', passed=True, summary='stale executor result', started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc))
    from app.services.task_service import ReceiptRejected
    with pytest.raises(ReceiptRejected, match="执行权"):
        TaskService(repos, locks).receive_result(envelope, result)
    assert repos.results.get_by_attempt(attempt.attempt_id) is None

@pytest.mark.asyncio
@pytest.mark.parametrize("prior_kind", ["none", "failed", "passed_other", "regression", "targeted", "non_target"])
async def test_baseline_proof_and_unchanged_regression(settings, repos, artifacts, workspace, locks, prior_kind):
    from app.registry.tools import ToolContext
    from app.schemas.results import CheckSpec
    from app.services.tools.checks import run_checks, RunChecksArgs
    from app.schemas.enums import ArtifactType
    from tests.fixtures import builders
    root = builders.new_root(repos)
    task = builders.new_child(repos, root, task_id='T100-C001', task_kind='verify')
    baseline, _ = prepare_source(workspace, repos, root_task_id=root.task_id, files={'helpers.py':'def average(values):\n    return sum(values) / len(values)\n'})
    current = workspace.publish_snapshot(root_task_id=root.task_id, files={'helpers.py':b'# unrelated change\ndef average(values):\n    return sum(values) / len(values)\n'}, parent_version=baseline)
    attempt = builders.new_attempt(repos, root, task, attempt_id='T100-C001-A1', source_version=current.source_version, batch_id='B1')
    current_test = 'def test_trivial():\n    assert True\n'
    if prior_kind in {'regression', 'targeted', 'non_target'}:
        current_test = 'def test_nonempty():\n    from helpers import average\n    assert average([1, 3]) == 2\n'
    ref = artifacts.save_text(root_task_id=root.task_id, artifact_type=ArtifactType.TEST_ARTIFACT, text=current_test, source_version=current.source_version, producer_attempt_id=attempt.attempt_id).artifact_id
    ctx = ToolContext(root_task_id=root.task_id, task_id=task.task_id, attempt_id=attempt.attempt_id, actor_id='verifier', sink=root_sink(repos, root.task_id), source_version=current.source_version, workspace=workspace, artifacts=artifacts, repos=repos, allowed_paths=['helpers.py'])
    spec = CheckSpec(check_id='AVG', goal_ref='average empty input', scope=['helpers.py'], method='behavior_test', pass_condition='empty input returns zero')
    if prior_kind != "none":
        original_source = "def test_nonempty():\n    from helpers import average\n    assert average([1, 3]) == 2\n"
        if prior_kind == "failed":
            original_source = "def test_defect():\n    assert False\n"
        if prior_kind in {"regression", "targeted", "non_target"}:
            original_source = artifacts.read_text(ref)
        prior_ref = artifacts.save_text(root_task_id=root.task_id, artifact_type=ArtifactType.TEST_ARTIFACT, text=original_source).artifact_id
        prior_ctx = replace(ctx, source_version=baseline)
        await run_checks(prior_ctx, RunChecksArgs(checks=[spec], contract_version='contract-1', generated_tests={'AVG': [prior_ref]}))
    outcome = await run_checks(ctx, RunChecksArgs(checks=[spec], contract_version='contract-1', generated_tests={'AVG':[ref]}, baseline_version=baseline, repair_check_ids=['AVG'] if prior_kind == 'targeted' else ['OTHER'] if prior_kind == 'non_target' else None))
    result = outcome.data['check_results'][0]
    assert result['status'] == ('passed' if prior_kind in {'regression', 'non_target'} else 'inconclusive')


def test_timeout_reaps_descendants(tmp_path):
    from app.services.tools.test_executor import run_pytest
    marker = tmp_path/'escaped.txt'
    child = 'import time; from pathlib import Path; time.sleep(2); Path('+repr(str(marker))+').write_text("alive")'
    code = 'import subprocess,sys,time\ndef test_spawn():\n    subprocess.Popen([sys.executable, "-c", '+repr(child)+'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n    time.sleep(10)\n'
    (tmp_path/'test_spawn.py').write_text(code)
    outcome = run_pytest(tmp_path, ['test_spawn.py'], 1)
    time.sleep(2)
    assert outcome.timed_out and not marker.exists()

@pytest.mark.asyncio
async def test_incremental_events_keeps_boundary(repos):
    from types import SimpleNamespace
    from app.api.tasks import task_events
    from app.services.events import EventSink, EventContext
    from app.schemas.enums import EventType
    from tests.fixtures import builders
    root = builders.new_root(repos)
    sink = EventSink(repos.events, EventContext(root_task_id=root.task_id, actor_id='probe'))
    svc = SimpleNamespace(repos=repos, runner=SimpleNamespace(is_running=lambda _:False))
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=svc)))
    sink.emit(EventType.TASK_CREATED, payload={})
    first = await task_events(req, root.task_id, after_seq=0, limit=200)
    sink.emit(EventType.TASK_DISPATCHED, payload={})
    second = await task_events(req, root.task_id, after_seq=first.next_seq, limit=200)
    assert first.next_seq == 1
    assert [e.sequence for e in second.events] == [2]


def test_stale_execution_cannot_publish_snapshot(repos, locks, workspace):
    from app.services.execution_context import active_execution, ExecutionFenced
    from tests.fixtures import builders
    root = builders.new_root(repos)
    repos.controls.ensure(root.task_id, "seg-1", 256)
    _, old = locks.acquire(root.task_id, "old")
    repos.controls.update(root.task_id, lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    locks.acquire(root.task_id, "new")
    binding = active_execution.set((root.task_id, old.fencing_token))
    try:
        with pytest.raises(ExecutionFenced):
            workspace.publish_snapshot(root_task_id=root.task_id, files={"x.py": b"x = 1"})
    finally:
        active_execution.reset(binding)
    assert not list((workspace.settings.workspaces_dir / root.task_id).rglob(".snapshot.json"))


def test_tool_description_matches_argument_validator():
    from app.services.tools import build_default_registry
    tools = build_default_registry()
    for role in ("reviewer", "fixer", "verifier"):
        for spec in tools.specs_for(role):
            assert spec.input_schema == spec.args_model.model_json_schema()
    patch = tools.get("submit_patch").input_schema["properties"]
    assert "edits" in patch and "diff_text" in patch


def test_named_evidence_is_immutable(artifacts, repos):
    from tests.fixtures import builders
    from app.schemas.enums import ArtifactType
    root = builders.new_root(repos)
    first = artifacts.save_text(root_task_id=root.task_id, artifact_type=ArtifactType.EVIDENCE, name="same", text="old")
    second = artifacts.save_text(root_task_id=root.task_id, artifact_type=ArtifactType.EVIDENCE, name="same", text="new")
    assert first.storage_ref != second.storage_ref
    assert artifacts.read_text(first.artifact_id) == "old"
    assert artifacts.read_text(second.artifact_id) == "new"


@pytest.mark.asyncio
async def test_cancelled_check_reaps_descendants(settings, repos, artifacts, workspace, tmp_path):
    from app.registry.tools import ToolContext
    from app.schemas.results import CheckSpec
    from app.services.tools.checks import run_checks, RunChecksArgs
    from app.schemas.enums import ArtifactType
    from tests.fixtures import builders
    from tests.fixtures.agents import root_sink
    root = builders.new_root(repos)
    version, _ = prepare_source(workspace, repos, root_task_id=root.task_id, files={"helpers.py": "x = 1\n"})
    started, escaped = tmp_path / "started", tmp_path / "escaped"
    child = f"import time; from pathlib import Path; time.sleep(1); Path({str(escaped)!r}).write_text('alive')"
    source = f"import subprocess, sys, time\nfrom pathlib import Path\ndef test_wait():\n    subprocess.Popen([sys.executable, '-c', {child!r}])\n    Path({str(started)!r}).write_text('yes')\n    time.sleep(10)\n"
    ref = artifacts.save_text(root_task_id=root.task_id, artifact_type=ArtifactType.TEST_ARTIFACT, text=source).artifact_id
    ctx = ToolContext(root_task_id=root.task_id, actor_id="verifier", sink=root_sink(repos, root.task_id), source_version=version, workspace=workspace, artifacts=artifacts, repos=repos)
    check = CheckSpec(check_id="cancel", goal_ref="cancel", scope=["helpers.py"], method="behavior_test", pass_condition="executes")
    worker = asyncio.create_task(run_checks(ctx, RunChecksArgs(checks=[check], contract_version="contract-1", generated_tests={"cancel": [ref]})))
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(.02)
    assert started.exists()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    await asyncio.sleep(1.1)
    assert not escaped.exists()
    assert not list((settings.evidence_dir / root.task_id / "runs").glob("run-*"))


@pytest.mark.asyncio
async def test_fenced_background_error_does_not_overwrite_new_run(settings, repos):
    from app.services.background import TaskRunnerService, RunHandle
    from app.services.execution_context import ExecutionFenced
    from tests.fixtures import builders
    root = builders.new_root(repos, status="running")
    before = repos.tasks.get(root.task_id)
    async def stale_run():
        raise ExecutionFenced("new executor owns this task")
    service = TaskRunnerService(settings=settings, repos=repos)
    handle = RunHandle(root_task_id=root.task_id, kind="resume")
    await service._guard(handle, stale_run())
    after = repos.tasks.get(root.task_id)
    assert after.status == before.status == "running"
    assert after.revision == before.revision
    assert handle.done.is_set()


def test_workflow_requirements_are_not_source_checks(settings, repos, artifacts, workspace, locks):
    from app.schemas.results import CheckSpec
    harness = build_harness(settings=settings, repos=repos, artifacts=artifacts,
        workspace=workspace, locks=locks, llm=make_scripted_client(), workflow=sequential_workflow())
    checks = [CheckSpec(check_id="syntax", goal_ref="可解析", scope=["helpers.py"], method="syntax", pass_condition="可解析"),
        CheckSpec(check_id="parallel", goal_ref="修改后在同一版本并行复审和验证并收齐两个分支", scope=["helpers.py"], method="behavior_test", pass_condition="分支均返回")]
    problems = harness.runtime.controller.validate_plan(checks, goal="检查源码并行验证", files=["helpers.py"])
    assert any("execution_requirements" in p for p in problems)
