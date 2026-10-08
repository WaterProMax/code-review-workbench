"""Subprocess driver for the A15 parallel-interruption acceptance test.

Runs a *parallel* workflow in a separate process and kills it (SIGKILL) while the
post-patch recheck+verify batch is still in flight: the verify branch has finished
its model work but the recheck branch is still held, so the process dies in the
middle of one dispatch batch.

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

from app.providers.scripted import ScriptedLLMClient
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
    FIX_DIFF,
    MUTABLE_DEFAULT_FINDING,
    build_harness,
    make_scripted_client,
    parallel_workflow,
    prepare_source,
)


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

    workflow = parallel_workflow(attempt_timeout_seconds=180.0)
    if repos.workflows.get(workflow.workflow_version) is None:
        repos.workflows.insert(workflow)

    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=BUGGY_SOURCE
    )
    inner = make_scripted_client(findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[FIX_DIFF])
    sentinel = data_dir / "verify_model_done"

    class StallSecondReviewer(ScriptedLLMClient):
        """Holds the post-patch recheck; kills the process once verify has run.

        Coordination uses a filesystem sentinel so the polling never contends with
        the sibling branch that has to finish.
        """

        def __init__(self) -> None:
            super().__init__(inner._handler)
            self._reviewer_steps = 0

        async def complete(self, request):  # type: ignore[no-untyped-def]
            key = request.script_key or ""
            if key.endswith(":verifier:step"):
                response = await super().complete(request)
                sentinel.write_text("1", encoding="utf-8")
                return response
            if key.endswith(":reviewer:step") and self._reviewer_steps == 0:
                # the initial review is fast; only the post-patch recheck stalls
                self._reviewer_steps = 1
                return await super().complete(request)
            if key.endswith(":reviewer:step"):
                for _ in range(1500):
                    if sentinel.exists():
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(1.0)
                marker.write_text(json.dumps({"parallel": True}), encoding="utf-8")
                sys.stdout.flush()
                sys.stderr.flush()
                os.kill(os.getpid(), signal.SIGKILL)
            return await super().complete(request)

    async with open_checkpointer(settings.checkpoints_db_path) as checkpointer:
        harness = build_harness(
            settings=settings,
            repos=repos,
            artifacts=artifacts,
            workspace=workspace,
            locks=locks,
            llm=StallSecondReviewer(),
            workflow=workflow,
            checkpointer=checkpointer,
        )
        runner = WorkflowRunner(
            runtime=harness.runtime, graph=harness.graph, recovery=harness.recovery, owner="driver"
        )
        final = await runner.start(
            root_task_id=root_task_id,
            goal="修复可变默认参数并验证",
            workflow_version=workflow.workflow_version or "wf-p1",
            check_mode=workflow.check_mode.value,
            source_version=source_version,
            source_artifact=source_artifact,
            ttl_seconds=1.0,
        )
    print(json.dumps({"finished_without_crash": True, "status": final.get("status")}))
    return 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
