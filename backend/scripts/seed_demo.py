"""Seed a demo data directory with three review runs for the workbench.

This is a **development/demo** helper, not a production path. It drives the real
graph, the real tools and the real file workspace; only the language model is the
explicitly injected ``ScriptedLLMClient`` (there is no model credential in this
environment). It exists so the workbench can be browsed against genuine API data.

Usage (from ``backend/``)::

    uv run python -m scripts.seed_demo --data-dir ../data/demo

It prints the created root task ids and the URLs to open.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.artifacts import ArtifactService  # noqa: E402
from app.services.background import TaskRunnerService  # noqa: E402
from app.services.task_creation import TaskCreationService  # noqa: E402
from app.services.workspace import WorkspaceService  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.storage.database import Database  # noqa: E402
from app.storage.repositories import Repos  # noqa: E402
from tests.fixtures.workflow import (  # noqa: E402
    BOGUS_DIFF,
    BUGGY_SOURCE,
    CLEAN_SOURCE,
    FIX_DIFF,
    MUTABLE_DEFAULT_FINDING,
    make_scripted_client,
    prepare_source,
    sequential_workflow,
)


async def _run_one(
    *,
    repos: Repos,
    workspace: WorkspaceService,
    runner: TaskRunnerService,
    root_task_id: str,
    goal: str,
    workflow,
    files: dict[str, str],
) -> str:
    version = workflow.workflow_version or ""
    if repos.workflows.get(version) is None:
        repos.workflows.insert(workflow)
    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=files
    )
    TaskCreationService(repos).create_root(
        goal=goal, workflow_version=version, root_task_id=root_task_id
    )
    await runner.start_task(
        root_task_id=root_task_id,
        goal=goal,
        workflow_version=version,
        check_mode=workflow.check_mode.value,
        source_version=source_version,
        source_artifact=source_artifact,
    )
    await runner.wait(root_task_id, timeout=120)
    return root_task_id


async def main() -> int:
    parser = argparse.ArgumentParser(description="seed demo review runs")
    parser.add_argument("--data-dir", default="data/demo", help="data directory to seed")
    parser.add_argument("--prefix", default="T-demo", help="root task id prefix")
    args = parser.parse_args()

    settings = Settings(data_dir=Path(args.data_dir).expanduser(), model_api_key=None)
    settings.ensure_dirs()
    Database(settings.business_db_path).initialize()
    repos = Repos(Database(settings.business_db_path))
    artifacts = ArtifactService(settings, repos.artifacts)
    workspace = WorkspaceService(settings, artifacts, repos)

    def runner_for(llm) -> TaskRunnerService:
        return TaskRunnerService(settings=settings, repos=repos, llm_factory=lambda: llm)

    clean_llm = make_scripted_client(findings=[])
    # one shared instance per scenario so the second repair round really gets the
    # second diff
    repaired_llm = make_scripted_client(
        findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[BOGUS_DIFF, FIX_DIFF]
    )
    waiting_llm = make_scripted_client(
        findings=[MUTABLE_DEFAULT_FINDING], fix_diffs=[BOGUS_DIFF, FIX_DIFF]
    )

    created: dict[str, str] = {}
    created["first_pass"] = await _run_one(
        repos=repos,
        workspace=workspace,
        runner=runner_for(clean_llm),
        root_task_id=f"{args.prefix}-pass",
        goal="检查可变默认参数与异常处理（预期首次审查通过）",
        workflow=sequential_workflow(workflow_version="wf-demo-2rounds"),
        files=CLEAN_SOURCE,
    )
    created["repaired"] = await _run_one(
        repos=repos,
        workspace=workspace,
        runner=runner_for(repaired_llm),
        root_task_id=f"{args.prefix}-repair",
        goal="检查可变默认参数，发现问题后修复并验证",
        workflow=sequential_workflow(workflow_version="wf-demo-2rounds"),
        files=BUGGY_SOURCE,
    )
    created["waiting"] = await _run_one(
        repos=repos,
        workspace=workspace,
        runner=runner_for(waiting_llm),
        root_task_id=f"{args.prefix}-wait",
        goal="检查可变默认参数（修复额度 1 轮，预期进入待恢复）",
        workflow=sequential_workflow(workflow_version="wf-demo-1round", max_repair_rounds=1),
        files=BUGGY_SOURCE,
    )

    summary = {
        "data_dir": str(settings.data_dir),
        "note": "scripted model at the service layer; no real model credential was used",
        "tasks": {
            key: {
                "root_task_id": root_id,
                "status": repos.tasks.get(root_id).status,
                "passed": repos.tasks.get(root_id).passed,
                "url": f"http://127.0.0.1:5173/tasks/{root_id}",
            }
            for key, root_id in created.items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
