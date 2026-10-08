"""Controlled task creation: the only place business task IDs are minted.

The model never invents IDs. It asks for a task by role/kind and the service
assigns a stable ``task_id`` (and remembers it under a stable operation key, so a
replayed graph run returns the original ID instead of creating a duplicate tree).
"""

from __future__ import annotations

import uuid

from app.schemas.common import TaskDep
from app.schemas.enums import TaskLevel, TaskStatus
from app.schemas.tasks import Task
from app.storage.repositories import Repos

ROOT_PREFIX = "T"


class TaskCreationError(RuntimeError):
    pass


class TaskCreationService:
    def __init__(self, repos: Repos) -> None:
        self.repos = repos

    # ---- ids ---------------------------------------------------------------
    @staticmethod
    def new_root_task_id() -> str:
        return f"{ROOT_PREFIX}{uuid.uuid4().hex[:10]}"

    def next_child_task_id(self, root_task_id: str, conn=None) -> str:  # type: ignore[no-untyped-def]
        ordinal = self.repos.tasks.next_child_ordinal(root_task_id, conn=conn)
        return f"{root_task_id}-C{ordinal:03d}"

    def next_attempt_id(self, task_id: str, attempt_no: int) -> str:
        return f"{task_id}-A{attempt_no}"

    def next_batch_id(self, root_task_id: str) -> str:
        existing = len(self.repos.batches.list_by_root(root_task_id))
        return f"{root_task_id}-B{existing + 1}"

    # ---- creation ----------------------------------------------------------
    def create_root(
        self,
        *,
        goal: str,
        workflow_version: str,
        root_task_id: str | None = None,
        conn=None,  # type: ignore[no-untyped-def]
    ) -> Task:
        root_task_id = root_task_id or self.new_root_task_id()
        existing = self.repos.tasks.get(root_task_id, conn=conn)
        if existing is not None:
            return existing
        task = Task(
            task_id=root_task_id,
            root_task_id=root_task_id,
            parent_task_id=None,
            task_level=TaskLevel.ROOT,
            goal=goal,
            status="queued",
            workflow_version=workflow_version,
        )
        self.repos.tasks.insert(task, conn=conn)
        return task

    def create_child(
        self,
        root: Task,
        *,
        agent_id: str,
        agent_version: str,
        task_kind: str,
        goal: str,
        depends_on: list[TaskDep] | None = None,
        task_id: str | None = None,
        status: str = TaskStatus.BLOCKED.value,
        operation_key: str | None = None,
        conn=None,  # type: ignore[no-untyped-def]
    ) -> Task:
        """Create (or return the already created) child task for an operation key."""
        if task_id:
            existing = self.repos.tasks.get(task_id, conn=conn)
            if existing is not None:
                self._assert_compatible(existing, root, agent_id, task_kind)
                return existing
        if operation_key:
            record = self.repos.idempotency.get(operation_key, conn=conn)
            if record and record.get("response_ref"):
                existing = self.repos.tasks.get(record["response_ref"], conn=conn)
                if existing is not None:
                    self._assert_compatible(existing, root, agent_id, task_kind)
                    return existing

        task_id = task_id or self.next_child_task_id(root.task_id, conn=conn)
        task = Task(
            task_id=task_id,
            root_task_id=root.task_id,
            parent_task_id=root.task_id,
            task_level=TaskLevel.CHILD,
            agent_id=agent_id,
            agent_version=agent_version,
            task_kind=task_kind,
            goal=goal,
            depends_on=depends_on or [],
            status=status,
            workflow_version=root.workflow_version,
        )
        self.repos.tasks.insert(task, conn=conn)
        if operation_key:
            self.repos.idempotency.put(
                operation_key=operation_key,
                operation_kind="create_child_task",
                request_fingerprint=f"{root.task_id}:{agent_id}:{task_kind}:{goal}",
                response_ref=task.task_id,
                root_task_id=root.task_id,
                conn=conn,
            )
        return task

    def mark_skipped(self, task_id: str, reason: str) -> None:
        self.repos.tasks.update(task_id, status=TaskStatus.SKIPPED.value, skip_reason=reason)

    def mark_status(self, task_id: str, status: str) -> None:
        self.repos.tasks.update(task_id, status=status)

    def set_passed(self, task_id: str, status: str, passed: bool | None) -> None:
        self.repos.tasks.update(task_id, status=status, passed=passed, passed_set=True)

    # ---- helpers -----------------------------------------------------------
    @staticmethod
    def _assert_compatible(task: Task, root: Task, agent_id: str, task_kind: str) -> None:
        if task.root_task_id != root.task_id:
            raise TaskCreationError(f"任务 {task.task_id} 属于其他总任务")
        if task.agent_id != agent_id or task.task_kind != task_kind:
            raise TaskCreationError(
                f"任务 {task.task_id} 的角色/类型为 {task.agent_id}/{task.task_kind}，"
                f"与请求的 {agent_id}/{task_kind} 不一致"
            )

    def ensure_control(self, root_task_id: str, run_segment_id: str, graph_steps: int):
        return self.repos.controls.ensure(root_task_id, run_segment_id, graph_steps)
