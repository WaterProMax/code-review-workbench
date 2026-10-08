"""P07 acceptance: a real process is killed mid-run and the task is resumed.

The plan (§11 验收) requires an independent process to actually start, be
terminated at a key breakpoint, and then be resumed by a fresh process while
keeping the same root/task IDs and history and applying the patch only once. An
in-process exception is explicitly not sufficient evidence.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.schemas.api import ResumeRequest
from app.schemas.enums import EventType, PatchApplicationStatus, RootStatus
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.checkpoints import open_checkpointer
from app.storage.database import Database
from app.storage.repositories import Repos
from app.workflow.recovery import RecoveryCoordinator
from app.workflow.runner import WorkflowRunner
from tests.fixtures.workflow import (
    MUTABLE_DEFAULT_FINDING,
    build_harness,
    make_scripted_client,
    sequential_workflow,
)

BACKEND_DIR = Path(__file__).resolve().parents[2]
ROOT_TASK_ID = "T-proc-001"


def _recovery(settings, repos, artifacts, workspace, locks, workflow) -> RecoveryCoordinator:
    return build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING]),
        workflow=workflow,
    ).recovery


@pytest.mark.asyncio
async def test_killed_process_is_resumed_without_duplicating_work(
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
) -> None:
    marker = settings.data_dir / "crash.marker"
    env = dict(os.environ)
    env.update(
        {
            "HW2_DATA_DIR": str(settings.data_dir),
            "HW2_ROOT_TASK_ID": ROOT_TASK_ID,
            "HW2_CRASH_MARKER": str(marker),
            "PYTHONPATH": str(BACKEND_DIR),
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.e2e.interruption_driver"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 60
    while time.time() < deadline and not marker.exists() and proc.poll() is None:
        time.sleep(0.05)
    if not marker.exists():
        out, err = proc.communicate(timeout=10)
        pytest.fail(
            f"driver never reached the crash point (rc={proc.returncode})\n"
            f"stdout={out}\nstderr={err}"
        )
    returncode = proc.wait(timeout=15)
    assert returncode == -signal.SIGKILL, f"expected an external kill, got rc={returncode}"

    crash = json.loads(marker.read_text(encoding="utf-8"))
    assert crash["patched"] is True
    crashed_version = crash["source_version"]
    crashed_contract = crash["contract_version"]

    # the killed process left a held lease that a supervisor must consider dead
    control = repos.controls.get(ROOT_TASK_ID)
    assert control is not None and control.lease_owner == "driver"
    lease_deadline = time.time() + 10
    while time.time() < lease_deadline:
        if not repos.controls.get(ROOT_TASK_ID).lease_valid(_utcnow()):
            break
        time.sleep(0.1)
    assert not repos.controls.get(ROOT_TASK_ID).lease_valid(_utcnow())

    # a fresh process scans the business ledger and marks the run interrupted
    workflow = sequential_workflow()
    resumed_repos = Repos(Database(settings.business_db_path))
    resumed_artifacts = ArtifactService(settings, resumed_repos.artifacts)
    resumed_workspace = WorkspaceService(settings, resumed_artifacts, resumed_repos)
    resumed_locks = ExecutionLockService(resumed_repos.controls, resumed_repos.budget)
    interrupted = _recovery(
        settings, resumed_repos, resumed_artifacts, resumed_workspace, resumed_locks, workflow
    ).recover_incomplete_runs()
    assert ROOT_TASK_ID in interrupted
    assert resumed_repos.tasks.get(ROOT_TASK_ID).status == RootStatus.INTERRUPTED.value

    attempts_before = [a.attempt_id for a in resumed_repos.attempts.list_by_root(ROOT_TASK_ID)]

    async with open_checkpointer(settings.checkpoints_db_path) as checkpointer:
        harness = build_harness(
            settings=settings,
            repos=resumed_repos,
            artifacts=resumed_artifacts,
            workspace=resumed_workspace,
            locks=resumed_locks,
            llm=make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING]),
            workflow=workflow,
            checkpointer=checkpointer,
        )
        # the API-level resume validates the revision and takes the execution right
        revision = resumed_repos.tasks.get(ROOT_TASK_ID).revision
        request = ResumeRequest(
            expected_revision=revision, reason="进程中断后恢复原任务"
        )
        outcome = harness.recovery.resume(
            ROOT_TASK_ID,
            request,
            budgets=workflow.budgets,
            owner="resumer",
            operation_key="resume:T-proc-001",
        )
        assert outcome.status == RootStatus.RUNNING.value
        assert outcome.granted == []  # a plain restart never invents extra budget

        runner = WorkflowRunner(
            runtime=harness.runtime,
            graph=harness.graph,
            recovery=harness.recovery,
            owner="resumer",
        )
        final = await runner.resume(root_task_id=ROOT_TASK_ID)

    # same task, same contract, same patched version — nothing was redone
    assert final["root_task_id"] == ROOT_TASK_ID
    assert final["contract_version"] == crashed_contract
    assert final["status"] == RootStatus.COMPLETED.value, {
        "waiting_reason": final.get("waiting_reason"),
        "rejections": final.get("action_rejections"),
    }
    assert final["passed"] is True
    assert final["source_version"] == crashed_version

    applications = resumed_repos.patch_applications.list_by_root(ROOT_TASK_ID)
    assert len(applications) == 1
    assert applications[0].status is PatchApplicationStatus.COMMITTED
    assert applications[0].result_version == crashed_version

    # the pre-crash attempt history is preserved and no branch was re-run blindly
    assert [a.attempt_id for a in resumed_repos.attempts.list_by_root(ROOT_TASK_ID)][
        : len(attempts_before)
    ] == attempts_before

    events = resumed_repos.events.list_after(ROOT_TASK_ID, 0, limit=1000)
    assert any(e.event_type is EventType.TASK_INTERRUPTED for e in events)
    assert any(e.event_type is EventType.TASK_RESUMED for e in events)
    assert any(e.event_type is EventType.TASK_COMPLETED for e in events)


def _utcnow():  # type: ignore[no-untyped-def]
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
