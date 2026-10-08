"""Subprocess driver for the P07 process-interruption acceptance test.

Runs a real workflow in a *separate process* and kills it (SIGKILL, no cleanup,
no chance to flush anything) at a deterministic point: the parent decision that
immediately follows a committed patch. The test then restarts in a fresh process
and proves the original task can be resumed with the same IDs, the same history
and exactly one patch application.

Environment:
* ``HW2_DATA_DIR``      data directory shared with the resuming process
* ``HW2_ROOT_TASK_ID``  the root task id to drive
* ``HW2_CRASH_MARKER``  file written just before the process is killed
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from app.providers.base import LLMRequest
from app.settings import Settings
from app.storage.checkpoints import open_checkpointer
from app.storage.database import Database
from app.storage.repositories import Repos
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.workflow.runner import WorkflowRunner
from tests.fixtures.workflow import (
    BUGGY_SOURCE,
    MUTABLE_DEFAULT_FINDING,
    _context_of,
    build_harness,
    make_scripted_client,
    prepare_source,
    sequential_workflow,
)


def _crash_hook(marker: Path):
    state = {"done": False}

    def hook(request: LLMRequest) -> None:
        if state["done"] or request.script_key != "parent:decide":
            return
        context = _context_of(request)
        if not context.get("patched"):
            return
        if context.get("unapplied_fix_attempt_id") is not None:
            return
        state["done"] = True
        marker.write_text(
            json.dumps(
                {
                    "patched": True,
                    "source_version": context.get("source_version"),
                    "contract_version": context.get("contract", {}).get("contract_version"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sys.stdout.flush()
        sys.stderr.flush()
        os.kill(os.getpid(), signal.SIGKILL)

    return hook


async def main() -> int:
    data_dir = Path(os.environ["HW2_DATA_DIR"])
    root_task_id = os.environ["HW2_ROOT_TASK_ID"]
    marker = Path(os.environ["HW2_CRASH_MARKER"])

    settings = Settings(data_dir=data_dir, model_api_key=None)
    settings.ensure_dirs()
    database = Database(settings.business_db_path)
    database.initialize()
    repos = Repos(database)
    artifacts = ArtifactService(settings, repos.artifacts)
    workspace = WorkspaceService(settings, artifacts, repos)
    locks = ExecutionLockService(repos.controls, repos.budget)

    workflow = sequential_workflow()
    if repos.workflows.get(workflow.workflow_version) is None:
        repos.workflows.insert(workflow)

    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=BUGGY_SOURCE
    )
    llm = make_scripted_client(
        findings=[MUTABLE_DEFAULT_FINDING], handler_hook=_crash_hook(marker)
    )

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
        )
        runner = WorkflowRunner(
            runtime=harness.runtime, graph=harness.graph, recovery=harness.recovery, owner="driver"
        )
        final = await runner.start(
            root_task_id=root_task_id,
            goal="修复可变默认参数并验证",
            workflow_version=workflow.workflow_version or "wf-1",
            check_mode=workflow.check_mode.value,
            source_version=source_version,
            source_artifact=source_artifact,
            ttl_seconds=1.0,
        )
    # Reaching here means the crash hook never fired: a false run would be a bug.
    print(json.dumps({"finished_without_crash": True, "status": final.get("status")}))
    return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
