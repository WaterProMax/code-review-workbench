"""Helpers that build persisted task trees, batches and attempts for tests."""

from __future__ import annotations

from datetime import datetime, timezone

from app.schemas.common import TaskConstraints
from app.schemas.enums import AttemptStatus, TaskLevel
from app.schemas.tasks import Attempt, Task
from app.storage.repositories import Repos

ROLE_FOR_KIND = {"review": "reviewer", "fix": "fixer", "verify": "verifier"}


def new_root(
    repos: Repos,
    *,
    root_task_id: str = "T100",
    goal: str = "检查上传项目并按需修复",
    workflow_version: str = "wf-1",
    status: str = "running",
) -> Task:
    task = Task(
        task_id=root_task_id,
        root_task_id=root_task_id,
        parent_task_id=None,
        task_level=TaskLevel.ROOT,
        goal=goal,
        status=status,
        workflow_version=workflow_version,
    )
    repos.tasks.insert(task)
    return task


def new_child(
    repos: Repos,
    root: Task,
    *,
    task_id: str,
    task_kind: str,
    agent_id: str | None = None,
    status: str = "queued",
    goal: str | None = None,
) -> Task:
    task = Task(
        task_id=task_id,
        root_task_id=root.task_id,
        parent_task_id=root.task_id,
        task_level=TaskLevel.CHILD,
        agent_id=agent_id or ROLE_FOR_KIND[task_kind],
        agent_version="1.0",
        task_kind=task_kind,
        goal=goal or f"{task_kind} task",
        status=status,
        workflow_version=root.workflow_version,
    )
    repos.tasks.insert(task)
    return task


def new_attempt(
    repos: Repos,
    root: Task,
    task: Task,
    *,
    attempt_id: str,
    source_version: str = "sv-1",
    batch_id: str | None = None,
    attempt_no: int | None = None,
    retry_reason: str | None = None,
    constraints: TaskConstraints | None = None,
) -> Attempt:
    # a batch must exist before its attempts (FK); the real dispatcher creates
    # the batch and the attempts inside one business transaction.
    if batch_id and repos.batches.get(batch_id) is None:
        repos.batches.create(batch_id, root.task_id, [attempt_id])
    attempt = Attempt(
        attempt_id=attempt_id,
        task_id=task.task_id,
        root_task_id=root.task_id,
        dispatch_batch_id=batch_id,
        attempt_no=attempt_no or repos.attempts.next_attempt_no(task.task_id),
        source_version=source_version,
        contract_version="contract-1",
        input_refs={"source": "art-source", "acceptance_contract": "art-contract"},
        constraints=constraints or TaskConstraints(),
        retry_reason=retry_reason,
        status=AttemptStatus.QUEUED,
    )
    repos.attempts.insert(attempt)
    return attempt


def new_batch(repos: Repos, root: Task, batch_id: str, expected: list[str]) -> str:
    if repos.batches.get(batch_id) is None:
        repos.batches.create(batch_id, root.task_id, expected)
    else:
        repos.batches.set_expected(batch_id, expected)
    return batch_id


def now() -> datetime:
    return datetime.now(timezone.utc)
