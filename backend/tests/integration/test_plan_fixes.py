"""Fixes from the 2026-10-19 review plan: sandbox, baseline, regression, roles."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.actions import DispatchItem
from app.schemas.artifacts import Patch, StructuredEdit
from app.schemas.enums import ArtifactType, EventType, FindingStatus
from app.schemas.results import CheckSpec, Finding
from app.schemas.tasks import InputRefs
from app.services.tools.checks import RunChecksArgs, run_checks
from app.services.tools.test_executor import cleanup_execution_dir, run_pytest, set_up_execution_dir
from tests.fixtures import builders
from tests.fixtures.agents import make_context, make_envelope, root_sink
from tests.fixtures.workflow import build_harness, make_scripted_client, prepare_source, sequential_workflow


def test_sandbox_hides_secrets_and_relative_database(tmp_path, monkeypatch):
    monkeypatch.setenv("HW2_MODEL_API_KEY", "super-secret")
    (tmp_path / "business.sqlite").write_text("secret-db", encoding="utf-8")
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "def test_env():\n"
        "    key = os.environ.get('HW2_MODEL_API_KEY')\n"
        "    if not key:\n"
        "        raise AssertionError('missing key')\n"
        "    raise AssertionError('has key')\n"
        "def test_db():\n"
        "    path = Path('../../business.sqlite')\n"
        "    if not path.exists():\n"
        "        raise AssertionError('db missing')\n"
        "    raise AssertionError('read db')\n"
    )
    run_dir, _written = set_up_execution_dir(
        base_dir=tmp_path / "evidence" / "runs",
        snapshot_files={"helpers.py": b"x = 1\n"},
        test_files={"test_attack.py": code},
    )
    try:
        assert str(run_dir).startswith(tempfile.gettempdir())
        assert "evidence" not in Path(run_dir).parts
        outcome = run_pytest(run_dir, ["test_attack.py"], 30)
        blob = outcome.stdout + outcome.stderr
        assert "missing key" in blob
        assert "super-secret" not in blob
        assert "db missing" in blob
        assert "secret-db" not in blob
        assert outcome.passed == 0
    finally:
        cleanup_execution_dir(run_dir)


def test_cpu_limit_is_applied(tmp_path):
    code = (
        "import resource\n"
        "def test_cpu():\n"
        "    soft, _hard = resource.getrlimit(resource.RLIMIT_CPU)\n"
        "    assert soft != resource.RLIM_INFINITY\n"
        "    assert soft <= 60\n"
    )
    run_dir, _written = set_up_execution_dir(
        base_dir=tmp_path, snapshot_files={}, test_files={"test_cpu.py": code}
    )
    try:
        outcome = run_pytest(run_dir, ["test_cpu.py"], 30)
        assert outcome.passed == 1, outcome.stdout + outcome.stderr
    finally:
        cleanup_execution_dir(run_dir)


@pytest.mark.asyncio
async def test_rollback_tests_do_not_count_as_a_fix(repos, workspace, artifacts):
    root = builders.new_root(repos)
    task = builders.new_child(repos, root, task_id="T-roll", task_kind="verify")
    original, _ = prepare_source(
        workspace,
        repos,
        root_task_id=root.task_id,
        files={"helpers.py": "def average(values):\n    return sum(values) / len(values)\n"},
    )
    fixed = workspace.publish_snapshot(
        root_task_id=root.task_id,
        files={"helpers.py": b"def average(values):\n    if not values:\n        return 0.0\n    return sum(values) / len(values)\n"},
        parent_version=original,
    )
    reverted = workspace.publish_snapshot(
        root_task_id=root.task_id,
        files={"helpers.py": b"def average(values):\n    return sum(values) / len(values)\n"},
        parent_version=fixed.source_version,
    )
    assert workspace.initial_source_version(root.task_id, reverted.source_version) == original
    bad = (
        "import pytest\n"
        "def test_empty_raises():\n"
        "    from helpers import average\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        average([])\n"
    )
    attempt = builders.new_attempt(
        repos, root, task, attempt_id="T-roll-A1", source_version=reverted.source_version, batch_id="B1"
    )
    ref = artifacts.save_text(
        root_task_id=root.task_id,
        artifact_type=ArtifactType.TEST_ARTIFACT,
        text=bad,
        source_version=reverted.source_version,
        producer_attempt_id=attempt.attempt_id,
    ).artifact_id
    ctx = _tool_ctx(repos, workspace, artifacts, root.task_id, task.task_id, attempt.attempt_id, reverted.source_version)
    spec = CheckSpec(
        check_id="AVG",
        goal_ref="empty average",
        scope=["helpers.py"],
        method="behavior_test",
        pass_condition="empty input returns zero",
    )
    outcome = await run_checks(
        ctx,
        RunChecksArgs(
            checks=[spec],
            contract_version="contract-1",
            generated_tests={"AVG": [ref]},
            include_existing_tests=False,
            baseline_version=original,
            previous_version=fixed.source_version,
        ),
    )
    result = outcome.data["check_results"][0]
    assert result["status"] != "passed"
    assert "基础版本" in result["reason"]


@pytest.mark.asyncio
async def test_regression_suite_keeps_a_proven_reproduction(repos, workspace, artifacts):
    root = builders.new_root(repos)
    task = builders.new_child(repos, root, task_id="T-reg", task_kind="verify")
    original, _ = prepare_source(
        workspace,
        repos,
        root_task_id=root.task_id,
        files={"helpers.py": "def average(values):\n    return sum(values) / len(values)\n"},
    )
    fixed = workspace.publish_snapshot(
        root_task_id=root.task_id,
        files={"helpers.py": b"def average(values):\n    if not values:\n        return 0.0\n    return sum(values) / len(values)\n"},
        parent_version=original,
    )
    good = (
        "def test_empty():\n"
        "    from helpers import average\n"
        "    assert average([]) == 0.0\n"
    )
    attempt = builders.new_attempt(
        repos, root, task, attempt_id="T-reg-A1", source_version=fixed.source_version, batch_id="B-reg"
    )
    ref = artifacts.save_text(
        root_task_id=root.task_id,
        artifact_type=ArtifactType.TEST_ARTIFACT,
        text=good,
        producer_attempt_id=attempt.attempt_id,
    ).artifact_id
    spec = CheckSpec(
        check_id="AVG",
        goal_ref="empty average",
        scope=["helpers.py"],
        method="behavior_test",
        pass_condition="empty input returns zero",
    )
    first_ctx = _tool_ctx(repos, workspace, artifacts, root.task_id, task.task_id, attempt.attempt_id, fixed.source_version)
    first = await run_checks(
        first_ctx,
        RunChecksArgs(
            checks=[spec],
            contract_version="contract-1",
            generated_tests={"AVG": [ref]},
            include_existing_tests=False,
            baseline_version=original,
            repair_check_ids=["AVG"],
        ),
    )
    assert first.data["check_results"][0]["status"] == "passed"
    reverted = workspace.publish_snapshot(
        root_task_id=root.task_id,
        files={"helpers.py": b"def average(values):\n    return sum(values) / len(values)\n"},
        parent_version=fixed.source_version,
    )
    bad = (
        "import pytest\n"
        "def test_empty_raises():\n"
        "    from helpers import average\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        average([])\n"
    )
    bad_ref = artifacts.save_text(
        root_task_id=root.task_id, artifact_type=ArtifactType.TEST_ARTIFACT, text=bad
    ).artifact_id
    second_attempt = builders.new_attempt(
        repos,
        root,
        task,
        attempt_id="T-reg-A2",
        source_version=reverted.source_version,
        batch_id="B-reg-2",
    )
    second_ctx = _tool_ctx(
        repos, workspace, artifacts, root.task_id, task.task_id, second_attempt.attempt_id, reverted.source_version
    )
    second = await run_checks(
        second_ctx,
        RunChecksArgs(
            checks=[spec],
            contract_version="contract-1",
            generated_tests={"AVG": [bad_ref]},
            include_existing_tests=False,
            baseline_version=original,
            repair_check_ids=["AVG"],
        ),
    )
    assert second.data["check_results"][0]["status"] == "failed"


def test_tool_events_use_the_attempt_actor(repos, workspace, artifacts):
    from app.registry.tools import new_tool_registry

    root = builders.new_root(repos)
    child = builders.new_child(repos, root, task_id="T-fix", task_kind="fix")
    ctx = make_context(
        llm=make_scripted_client(),
        tools=new_tool_registry(),
        workspace=workspace,
        artifacts=artifacts,
        repos=repos,
        sink=root_sink(repos, root.task_id, actor="parent"),
        spec=__import__("app.registry.agents", fromlist=["_fixer_spec"])._fixer_spec(),
    )
    envelope = make_envelope(
        root_task_id=root.task_id,
        task=child,
        attempt_id="T-fix-A1",
        batch_id="B1",
        source_version="sv-1",
        contract_version="contract-1",
        contract_artifact_id="art-contract",
        source_artifact_id="art-source",
    )
    tool_ctx = ctx.tool_context(envelope)
    tool_ctx.sink.emit(EventType.TOOL_STARTED, payload={"tool": "read_file"})
    events = repos.events.list_after(root.task_id, 0)
    assert events[-1].event_type is EventType.TOOL_STARTED
    assert events[-1].actor_id == child.agent_id
    assert events[-1].attempt_id == "T-fix-A1"


def test_new_version_reverify_does_not_use_the_evidence_budget(settings, repos, artifacts, workspace, locks):
    harness = build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(),
        workflow=sequential_workflow(),
    )
    root = builders.new_root(repos)
    task = builders.new_child(repos, root, task_id="T-verify", task_kind="verify")
    builders.new_attempt(repos, root, task, attempt_id="T-verify-A1", source_version="sv-old", batch_id="B1")
    item = DispatchItem(
        agent_id="verifier",
        task_kind="verify",
        task_id=task.task_id,
        goal="重新验证",
        input_refs=InputRefs(source="art-source", acceptance_contract="art-contract"),
        acceptance_criteria=["覆盖必需检查"],
        reason="版本已变化",
    )
    assert harness.controller.retry_reason(item, source_version="sv-new") == "new_version_reverify"
    assert harness.task_service._budgets_for("verify", "new_version_reverify") == []


def test_patch_reopens_a_resolved_finding_on_the_same_file(workspace, repos, artifacts):
    root = builders.new_root(repos)
    fix = builders.new_child(repos, root, task_id="T-reopen", task_kind="fix")
    builders.new_attempt(repos, root, fix, attempt_id="T-reopen-A1", batch_id="B1", source_version="sv-x")
    upload = workspace.create_upload([("helpers.py", b"def average(values):\n    return 0\n")])
    manifest = workspace.publish_initial_snapshot(root_task_id=root.task_id, source_id=upload.source_id)
    base = manifest.source_version
    finding = Finding(
        finding_id="F-1",
        root_task_id=root.task_id,
        source_version=base,
        producer_attempt_id="T-reopen-A1",
        required_for_goal=True,
        check_id="AVG",
        file_path="helpers.py",
        rule="empty-average",
        message="空列表应返回 0",
        status=FindingStatus.RESOLVED,
        resolution_evidence_refs=["ev-1"],
    )
    repos.findings.upsert(finding)
    patch = Patch(
        patch_id="P-reopen",
        root_task_id=root.task_id,
        producer_attempt_id="T-reopen-A1",
        base_version=base,
        format="structured_edits",
        edits=[StructuredEdit(file_path="helpers.py", find="return 0\n", replace="return 1\n")],
        rationale="change the same file",
    )
    saved = artifacts.save_json(
        root_task_id=root.task_id,
        artifact_type=ArtifactType.PATCH,
        data=patch.model_dump(mode="json"),
        producer_attempt_id="T-reopen-A1",
        source_version=base,
    )
    workspace.apply_patch(
        root_task_id=root.task_id,
        patch_artifact_id=saved.artifact_id,
        repair_attempt_id="T-reopen-A1",
        base_version=base,
        idempotency_key="apply:reopen",
    )
    assert repos.findings.latest(root.task_id, "F-1").status is FindingStatus.OPEN


def test_oversized_upload_is_rejected(settings):
    settings.max_upload_file_bytes = 8
    app = create_app(settings, llm_factory=make_scripted_client)
    client = TestClient(app)
    response = client.post("/api/sources", files=[("files", ("a.py", b"x" * 32, "text/plain"))])
    assert response.status_code != 201
    assert "上限" in response.text


def _tool_ctx(repos, workspace, artifacts, root_task_id, task_id, attempt_id, source_version):
    from app.registry.tools import ToolContext

    return ToolContext(
        root_task_id=root_task_id,
        task_id=task_id,
        attempt_id=attempt_id,
        actor_id="verifier",
        sink=root_sink(repos, root_task_id),
        source_version=source_version,
        workspace=workspace,
        artifacts=artifacts,
        repos=repos,
        allowed_paths=["helpers.py"],
    )
