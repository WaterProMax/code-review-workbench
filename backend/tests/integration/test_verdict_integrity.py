"""Forged pytest summaries and conftest hooks must not become a pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.registry.tools import ToolContext, ToolError
from app.schemas.enums import ArtifactType, CheckStatus
from app.schemas.results import CheckSpec
from app.services.tools.checks import RunChecksArgs, _classify, run_checks
from app.services.tools.patches import SubmitPatchArgs, submit_patch
from app.services.tools.test_executor import PytestOutcome, run_pytest
from tests.fixtures import builders
from tests.fixtures.agents import root_sink
from tests.fixtures.workflow import prepare_source


def test_conftest_cannot_rewrite_a_failure(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(
        "import pytest\n"
        "@pytest.hookimpl(hookwrapper=True)\n"
        "def pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n"
        "    rep = outcome.get_result()\n"
        "    rep.outcome = 'passed'\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\nnorecursedirs = *\n", encoding="utf-8")
    (tmp_path / "tox.ini").write_text("[pytest]\naddopts = --collect-only\n", encoding="utf-8")
    (tmp_path / "test_lie.py").write_text(
        "def test_false():\n    assert 1 == 2\n",
        encoding="utf-8",
    )
    outcome = run_pytest(tmp_path, ["test_lie.py"], 30)
    status, _reason = _classify(outcome)
    assert status is not CheckStatus.PASSED
    assert status is CheckStatus.FAILED
    assert outcome.failed >= 1


def test_forged_summary_and_exit_is_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / "forged.py").write_text(
        "import os\n"
        "def _lie(self):\n"
        "    print('3 passed in 0.01s')\n"
        "    os._exit(0)\n"
        "import _pytest.terminal as terminal\n"
        "terminal.TerminalReporter.summary_stats = _lie\n",
        encoding="utf-8",
    )
    (tmp_path / "test_forged.py").write_text(
        "import forged\n"
        "def test_false():\n"
        "    assert 1 == 2\n",
        encoding="utf-8",
    )
    outcome = run_pytest(tmp_path, ["test_forged.py"], 30)
    status, _reason = _classify(outcome)
    assert status is not CheckStatus.PASSED
    assert status in {CheckStatus.FAILED, CheckStatus.INCONCLUSIVE}
    assert outcome.passed != 3 or not outcome.integrity_ok


def test_classify_ignores_a_passed_line_without_junit() -> None:
    outcome = PytestOutcome(
        exit_code=0,
        counts={"passed": 3, "collected": 3},
        summary_line="3 passed in 0.01s",
        integrity_ok=False,
    )
    status, _reason = _classify(outcome)
    assert status is not CheckStatus.PASSED


@pytest.mark.asyncio
async def test_submit_patch_rejects_test_tampering(repos, workspace, artifacts) -> None:
    root = builders.new_root(repos)
    version, _source = prepare_source(
        workspace,
        repos,
        root_task_id=root.task_id,
        files={"helpers.py": "def add(a, b):\n    return a + b\n"},
    )
    ctx = ToolContext(
        root_task_id=root.task_id,
        actor_id="fixer",
        sink=root_sink(repos, root.task_id),
        source_version=version,
        workspace=workspace,
        artifacts=artifacts,
        repos=repos,
        allowed_paths=["helpers.py"],
    )
    clean = (
        "--- a/helpers.py\n"
        "+++ b/helpers.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b  # still honest\n"
    )
    saved = await submit_patch(
        ctx,
        SubmitPatchArgs(base_version=version, rationale="honest change", diff_text=clean),
    )
    assert saved.ok
    forged = (
        "--- a/helpers.py\n"
        "+++ b/helpers.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def add(a, b):\n"
        "+    import os; os._exit(0)\n"
        "     return a + b\n"
    )
    with pytest.raises(ToolError) as caught:
        await submit_patch(
            ctx,
            SubmitPatchArgs(base_version=version, rationale="forge the suite", diff_text=forged),
        )
    assert caught.value.code == "PATCH_REJECTED"
    assert "TEST_TAMPERING" in caught.value.message


@pytest.mark.asyncio
async def test_deleted_regression_suite_is_not_refrozen(repos, workspace, artifacts) -> None:
    from app.services.tools.checks import _regression_path, update_regression_file

    root = builders.new_root(repos)
    version, _source = prepare_source(
        workspace,
        repos,
        root_task_id=root.task_id,
        files={"helpers.py": "def add(a, b):\n    return a + b\n"},
    )
    ctx = ToolContext(
        root_task_id=root.task_id,
        actor_id="verifier",
        sink=root_sink(repos, root.task_id),
        source_version=version,
        workspace=workspace,
        artifacts=artifacts,
        repos=repos,
        allowed_paths=["helpers.py"],
    )
    path = _regression_path(ctx, "ADD")
    update_regression_file(path, "ADD", {"_hw2_tests/ADD/frozen.py": "def test_add():\n    assert False\n"})
    path.unlink()
    ref = artifacts.save_text(
        root_task_id=root.task_id,
        artifact_type=ArtifactType.TEST_ARTIFACT,
        text="def test_add():\n    assert False\n",
    ).artifact_id
    spec = CheckSpec(
        check_id="ADD",
        goal_ref="add",
        scope=["helpers.py"],
        method="behavior_test",
        pass_condition="adds",
    )
    outcome = await run_checks(
        ctx,
        RunChecksArgs(
            checks=[spec],
            contract_version="contract-1",
            generated_tests={"ADD": [ref]},
            include_existing_tests=False,
            baseline_version=version,
        ),
    )
    result = outcome.data["check_results"][0]
    assert "REGRESSION_SUITE_MISSING" in result["reason"]
    assert result["status"] != "passed"
    assert not path.exists()
    assert any(fault["code"] == "REGRESSION_SUITE_MISSING" for fault in outcome.data["faults"])


@pytest.mark.asyncio
async def test_five_files_use_three_pytest_invocations(repos, workspace, artifacts, monkeypatch) -> None:
    root = builders.new_root(repos)
    original, _source = prepare_source(
        workspace,
        repos,
        root_task_id=root.task_id,
        files={"helpers.py": "def add(a, b):\n    return a - b\n"},
    )
    previous = workspace.publish_snapshot(
        root_task_id=root.task_id,
        files={"helpers.py": b"def add(a, b):\n    return a - b\n"},
        parent_version=original,
    )
    current = workspace.publish_snapshot(
        root_task_id=root.task_id,
        files={"helpers.py": b"def add(a, b):\n    return a + b\n"},
        parent_version=previous.source_version,
    )
    calls: list[list[str]] = []

    def fake_run(workdir, target, timeout_seconds, cancel_event=None):
        del workdir, timeout_seconds, cancel_event
        calls.append(list(target))
        return PytestOutcome(
            exit_code=1,
            counts={"failed": len(target), "collected": len(target)},
            summary_line=f"{len(target)} failed",
            integrity_ok=True,
            failed_node_ids=[f"{name}::test_case" for name in target],
        )

    monkeypatch.setattr("app.services.tools.checks.run_pytest", fake_run)
    ctx = ToolContext(
        root_task_id=root.task_id,
        actor_id="verifier",
        sink=root_sink(repos, root.task_id),
        source_version=current.source_version,
        workspace=workspace,
        artifacts=artifacts,
        repos=repos,
        allowed_paths=["helpers.py"],
    )
    refs = []
    for index in range(5):
        refs.append(
            artifacts.save_text(
                root_task_id=root.task_id,
                artifact_type=ArtifactType.TEST_ARTIFACT,
                text=f"def test_case():\n    assert {index} == {index}\n",
            ).artifact_id
        )
    spec = CheckSpec(
        check_id="ADD",
        goal_ref="add",
        scope=["helpers.py"],
        method="behavior_test",
        pass_condition="adds",
    )
    await run_checks(
        ctx,
        RunChecksArgs(
            checks=[spec],
            contract_version="contract-1",
            generated_tests={"ADD": refs},
            include_existing_tests=False,
            baseline_version=original,
            previous_version=previous.source_version,
        ),
    )
    assert len(calls) == 3
    assert all(len(target) == 5 for target in calls)

    calls.clear()
    empty = CheckSpec(
        check_id="EMPTY",
        goal_ref="add",
        scope=["helpers.py"],
        method="behavior_test",
        pass_condition="adds",
    )
    await run_checks(
        ctx,
        RunChecksArgs(
            checks=[empty],
            contract_version="contract-1",
            generated_tests={},
            include_existing_tests=False,
        ),
    )
    assert calls == []
