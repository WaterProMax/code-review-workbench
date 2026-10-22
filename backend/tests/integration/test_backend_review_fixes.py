"""Regressions from the 2026-10-09 backend review; real DB/files/graph/pytest."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.api.tasks import submit_task
from app.main import create_app
from app.registry.tools import ToolContext
from app.schemas.api import ResumeRequest, SubmitTaskRequest, TerminateRequest
from app.schemas.results import CheckSpec
from app.services.tools.checks import RunChecksArgs, run_checks
from app.services.tools.test_executor import MAX_OUTPUT_CHARS, run_pytest
from app.storage.repositories import ConflictError
from tests.fixtures import builders
from tests.fixtures.agents import root_sink
from tests.fixtures.workflow import (
    BUGGY_SOURCE, MUTABLE_DEFAULT_FINDING, build_harness,
    make_scripted_client, prepare_source, sequential_workflow,
)
from tests.e2e.test_api import _client, _save_workflow, _upload


@pytest.fixture
def harness(settings, repos, artifacts, workspace, locks):
    return build_harness(
        settings=settings, repos=repos, artifacts=artifacts,
        workspace=workspace, locks=locks, llm=make_scripted_client(),
        workflow=sequential_workflow(),
    )


@pytest.mark.parametrize("method,scope,rules,expected", [
    ("static_rule", ["pkg/bad.py"], ["B006"], "not_run"),
    ("static_rule", ["good.py", "pkg/*.py"], ["B006-mutable-default"], "failed"),
    ("static_rule", ["pkg/*.py"], ["B006-mutable-default"], "failed"),
    ("static_rule", ["good.py", "absent/*.py"], ["B006-mutable-default"], "not_run"),
    ("syntax", ["broken/*.py"], [], "failed"),
    ("syntax", ["missing.py"], [], "not_run"),
    ("syntax", ["notes.md"], [], "not_run"),
])
async def test_contract_rules_and_scopes_cannot_false_pass(
    repos, workspace, artifacts, harness, method, scope, rules, expected,
):
    root = builders.new_root(repos)
    files = {"good.py": "x = 1\n", "pkg/bad.py": "def f(items=[]):\n    return items\n",
             "broken/bad.py": "def broken(\n", "notes.md": "notes"}
    version, _ = prepare_source(workspace, repos, root_task_id=root.task_id, files=files)
    check = CheckSpec(check_id="CHECK", goal_ref="源码检查", scope=scope,
                      method=method, pass_condition="符合约定", rule_ids=rules)
    problems = harness.controller.validate_plan([check], goal="检查源码", files=files)
    if rules == ["B006"] or "absent/*.py" in scope or scope == ["missing.py"]:
        assert problems
    else:
        assert not problems
    ctx = ToolContext(root_task_id=root.task_id, source_version=version,
                      actor_id="reviewer", sink=root_sink(repos, root.task_id),
                      workspace=workspace, artifacts=artifacts, repos=repos)
    result = await run_checks(ctx, RunChecksArgs(checks=[check], contract_version="contract-1"))
    item = result.data["check_results"][0]
    assert item["status"] == expected
    if expected == "not_run":
        assert item["executed"] is False
    if method == "static_rule" and expected == "failed":
        assert result.data["findings"][0]["file_path"] == "pkg/bad.py"


async def test_quick_restart_eventually_surfaces_expired_lease(settings, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "RECOVERY_SCAN_INTERVAL_SECONDS", .01)
    app = create_app(settings, llm_factory=make_scripted_client)
    svc = app.state.services
    root = builders.new_root(svc.repos, root_task_id="T-crashed", status="running")
    svc.repos.controls.ensure(root.task_id, "seg-1", 256)
    svc.locks.acquire(root.task_id, "dead-process", ttl_seconds=60)
    async with app.router.lifespan_context(app):
        assert svc.repos.tasks.get(root.task_id).status == "running"
        svc.repos.controls.update(root.task_id, lease_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1))
        for _ in range(100):
            if svc.repos.tasks.get(root.task_id).status == "interrupted":
                break
            await asyncio.sleep(.01)
        assert svc.repos.tasks.get(root.task_id).status == "interrupted"
        assert svc.repos.controls.get(root.task_id).lease_owner is None
        assert not svc.locks.heartbeat(root.task_id, "dead-process")
        assert svc.governance.recovery.can_resume(root.task_id) == []
        events = svc.repos.events.list_after(root.task_id, 0)
        assert sum(e.event_type.value == "task_interrupted" for e in events) == 1


def test_periodic_scan_preserves_healthy_and_newly_queued_tasks(repos, harness, locks):
    healthy = builders.new_root(repos, root_task_id="T-healthy", status="running")
    queued = builders.new_root(repos, root_task_id="T-new", status="queued")
    repos.controls.ensure(healthy.task_id, "seg-1", 256)
    locks.acquire(healthy.task_id, "other-worker", ttl_seconds=60)
    assert harness.recovery.recover_incomplete_runs(include_unleased_queued=False) == []
    assert repos.tasks.get(healthy.task_id).status == "running"
    assert repos.tasks.get(queued.task_id).status == "queued"


@pytest.mark.parametrize("operation", ["resume", "terminate"])
def test_cross_task_key_reuse_is_rejected_without_side_effects(repos, harness, locks, operation):
    a = builders.new_root(repos, root_task_id="T-a", status="waiting_recovery")
    b = builders.new_root(repos, root_task_id="T-b", status="waiting_recovery")
    for root in (a, b):
        repos.controls.ensure(root.task_id, "seg-1", 256)
    if operation == "resume":
        payload = ResumeRequest(expected_revision=0, additional_repair_rounds=1, reason="追加额度")
        call = lambda root_id: harness.recovery.resume(
            root_id, payload, owner="owner", operation_key="shared-key", budgets=harness.runtime.workflow.budgets,
        )
    else:
        payload = TerminateRequest(expected_revision=0, reason="结束")
        call = lambda root_id: harness.recovery.terminate(root_id, payload, owner="owner", operation_key="shared-key")
    call(a.task_id)
    before = locks.budget_state(b.task_id, harness.runtime.workflow.budgets)
    with pytest.raises(ConflictError):
        call(b.task_id)
    assert repos.tasks.get(b.task_id).status == "waiting_recovery"
    assert repos.controls.get(b.task_id).lease_owner is None
    assert locks.budget_state(b.task_id, harness.runtime.workflow.budgets) == before


def test_cross_operation_key_reuse_is_rejected(repos, harness):
    root = builders.new_root(repos, status="waiting_recovery")
    repos.controls.ensure(root.task_id, "seg-1", 256)
    repos.idempotency.put(operation_key="other-op", operation_kind="submit_task",
                         request_fingerprint="same", response_ref=root.task_id, root_task_id=root.task_id)
    with pytest.raises(ConflictError):
        harness.recovery.terminate(root.task_id, TerminateRequest(expected_revision=0, reason="结束"),
                                   owner="owner", operation_key="other-op", fingerprint="same")
    assert repos.tasks.get(root.task_id).status == "waiting_recovery"


def test_parallel_submission_creates_and_schedules_only_one_root(settings, monkeypatch):
    app = create_app(settings, llm_factory=make_scripted_client)
    svc = app.state.services
    wf = sequential_workflow()
    svc.repos.workflows.insert(wf)
    upload = svc.workspace.create_upload([("good.py", b"x = 1\n")])
    request = SimpleNamespace(app=app)
    payload = SubmitTaskRequest(source_id=upload.source_id, goal="检查源码", workflow_version=wf.workflow_version)
    barrier = threading.Barrier(2)
    original_get = svc.repos.idempotency.get
    def get(key, conn=None):
        record = original_get(key, conn=conn)
        if key == "concurrent-submit" and conn is None:
            barrier.wait(timeout=5)
        return record
    monkeypatch.setattr(svc.repos.idempotency, "get", get)
    scheduled = []
    async def schedule(**kwargs):
        scheduled.append(kwargs["root_task_id"])
    monkeypatch.setattr(svc.runner, "start_task", schedule)
    def submit():
        return asyncio.run(submit_task(request, payload, "concurrent-submit"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(), range(2)))
    assert responses[0].root_task_id == responses[1].root_task_id
    assert svc.repos.tasks.count_roots() == 1
    assert scheduled == [responses[0].root_task_id]
    events = svc.repos.events.list_after(responses[0].root_task_id, 0)
    created = [e for e in events if e.event_type.value == "task_created"]
    assert len(created) == 1 and created[0].actor_id == "parent"


def test_noisy_pytest_is_streamed_and_keeps_the_final_verdict(tmp_path):
    (tmp_path / "test_noisy.py").write_text(
        "import os\ndef test_noisy():\n"
        "    for _ in range(64):\n"
        "        os.write(1, b'x' * 65536)\n"
        "        os.write(2, b'y' * 65536)\n"
        "    assert False, 'expected failure'\n"
    )
    result = run_pytest(tmp_path, ["test_noisy.py"], 10)
    assert result.exit_code == 1 and result.failed == 1
    assert len(result.stdout) <= MAX_OUTPUT_CHARS
    assert len(result.stderr) <= MAX_OUTPUT_CHARS
    assert "1 failed" in result.summary_line


async def test_terminated_failed_task_has_downloadable_idempotent_report(settings):
    app = create_app(settings, llm_factory=lambda: make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING]))
    svc = app.state.services
    async with _client(app) as client:
        workflow_version = await _save_workflow(client, max_repair_rounds=0)
        source_id = await _upload(client, BUGGY_SOURCE)
        response = await client.post("/api/tasks", json={"source_id": source_id, "goal": "修复可变默认参数",
                                     "workflow_version": workflow_version}, headers={"Idempotency-Key": "submit"})
        assert response.status_code == 202
        root_id = response.json()["root_task_id"]
        await svc.runner.wait(root_id, timeout=20)
        root = svc.repos.tasks.get(root_id)
        assert root.status == "waiting_recovery"
        payload = {"expected_revision": root.revision, "reason": "保留证据后终止"}
        for _ in range(2):
            stop = await client.post(f"/api/tasks/{root_id}/terminate", json=payload,
                                     headers={"Idempotency-Key": "stop"})
            assert stop.status_code == 200, stop.text
            assert stop.json()["passed"] is False
        report = await client.get(f"/api/tasks/{root_id}/report")
        assert report.status_code == 200, report.text
        data = report.json()
        assert data["final_status"] == "completed" and data["passed"] is False
        assert data["report"]["checks"] and data["report"]["unresolved_finding_ids"]
        assert data["report"]["execution_summary"]["termination_reason"] == payload["reason"]
        artifact = await client.get(f"/api/artifacts/{data['report_artifact_id']}")
        assert artifact.status_code == 200
        reports = svc.artifacts.list_by_root(root_id)
        assert sum(a.artifact_type.value == "final_report" for a in reports) == 1


def test_terminate_without_contract_still_reports_missing_evidence(repos, harness, artifacts):
    root = builders.new_root(repos, status="interrupted")
    repos.controls.ensure(root.task_id, "seg-1", 256)
    outcome = harness.recovery.terminate(root.task_id, TerminateRequest(expected_revision=0, reason="终止"),
                                          owner="owner", operation_key="stop")
    report = artifacts.read_json(repos.reports.latest(root.task_id, "final")["artifact_id"])
    assert outcome.status == report["final_status"] == "partial"
    assert report["passed"] is None and report["contract_version"] is None
    assert report["not_run_items"]


def test_termination_report_failure_rolls_back_final_state(repos, harness, monkeypatch):
    root = builders.new_root(repos, status="waiting_recovery")
    repos.controls.ensure(root.task_id, "seg-1", 256)
    child = builders.new_child(repos, root, task_id="T100-C001", task_kind="fix", status="blocked")
    def fail_report(**kwargs):
        raise OSError("report storage unavailable")
    monkeypatch.setattr(harness.controller, "build_report", fail_report)
    with pytest.raises(OSError):
        harness.recovery.terminate(root.task_id, TerminateRequest(expected_revision=0, reason="终止"),
                                   owner="owner", operation_key="stop")
    assert repos.tasks.get(root.task_id).status == "waiting_recovery"
    assert repos.tasks.get(child.task_id).status == "blocked"
    assert repos.idempotency.get("stop") is None
