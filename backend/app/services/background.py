"""Application-level background task service (§12.2).

Long tasks must not block the submit request and must not be cancelled when the
client disconnects. This service owns one asyncio handle per root task, drives it
through the leased runner, and releases the lease on cancellation or shutdown so
a fresh process can resume from the checkpoint.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.extensions.base import ExtensionRegistry
from app.extensions import build_default_extension_registry
from app.providers.base import LLMClient
from app.providers.factory import build_llm_client
from app.schemas.enums import EventType, RootStatus
from app.schemas.workflows import WorkflowConfig
from app.services.events import EventContext, EventSink
from app.services.execution_context import ExecutionFenced
from app.workflow.recovery import RunRightDenied
from app.settings import Settings
from app.storage.checkpoints import open_checkpointer
from app.storage.repositories import Repos
from app.workflow.assembly import assemble_workflow, compile_graph
from app.workflow.runner import WorkflowRunner

logger = logging.getLogger("hw2.background")


@dataclass
class RunHandle:
    root_task_id: str
    kind: str
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


class TaskRunnerService:
    def __init__(
        self,
        *,
        settings: Settings,
        repos: Repos,
        llm_factory: Callable[[], LLMClient] | None = None,
        owner: str | None = None,
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.repos = repos
        self.llm_factory = llm_factory or (lambda: build_llm_client(settings))
        self.owner = owner or f"service-{uuid.uuid4().hex[:12]}"
        self.extensions = extensions or build_default_extension_registry()
        self._handles: dict[str, RunHandle] = {}

    # ---- model readiness ---------------------------------------------------
    def ensure_model_ready(self) -> None:
        """Fail fast (503) when no model is configured, before minting a task.

        The factory is called synchronously so a ``ConfigurationError`` reaches the
        request instead of being swallowed by a detached task.
        """
        client = self.llm_factory()
        close = getattr(client, "aclose", None)
        if close is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - no loop in sync context
            return
        loop.create_task(close())

    # ---- introspection -----------------------------------------------------
    def is_running(self, root_task_id: str) -> bool:
        handle = self._handles.get(root_task_id)
        return handle is not None and handle.task is not None and not handle.task.done()

    def running_ids(self) -> list[str]:
        return [
            root
            for root, handle in self._handles.items()
            if handle.task is not None and not handle.task.done()
        ]

    async def wait(self, root_task_id: str, timeout: float | None = None) -> None:
        """Await the background run (used by tests and shutdown)."""
        handle = self._handles.get(root_task_id)
        if handle is None:
            return
        await asyncio.wait_for(handle.done.wait(), timeout=timeout)

    # ---- lifecycle ---------------------------------------------------------
    async def start_task(
        self,
        *,
        root_task_id: str,
        goal: str,
        workflow_version: str,
        check_mode: str,
        source_version: str,
        source_artifact: str,
    ) -> None:
        await self._schedule(
            root_task_id,
            "start",
            self._run(
                root_task_id=root_task_id,
                workflow_version=workflow_version,
                goal=goal,
                check_mode=check_mode,
                source_version=source_version,
                source_artifact=source_artifact,
            ),
        )

    async def resume_task(self, root_task_id: str) -> None:
        await self._schedule(root_task_id, "resume", self._run(root_task_id=root_task_id))

    async def _schedule(self, root_task_id: str, kind: str, coro: Any) -> None:
        if self.is_running(root_task_id):
            raise RuntimeError(f"任务 {root_task_id} 已有后台运行")
        handle = RunHandle(root_task_id=root_task_id, kind=kind)
        handle.task = asyncio.create_task(self._guard(handle, coro))
        self._handles[root_task_id] = handle

    async def _guard(self, handle: RunHandle, coro: Any) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            logger.info("background run cancelled: %s", handle.root_task_id)
            raise
        except (ExecutionFenced, RunRightDenied):
            logger.info("execution right lost, leaving current owner in control: %s", handle.root_task_id)
        except Exception as exc:  # noqa: BLE001 - a task must never crash the service
            logger.exception("background run failed: %s", handle.root_task_id)
            self._mark_internal_error(handle.root_task_id, exc)
        finally:
            handle.done.set()

    def _mark_internal_error(self, root_task_id: str, exc: Exception) -> None:
        message = f"内部错误：{type(exc).__name__}: {exc}"[:400]
        try:
            self.repos.controls.update(root_task_id, required_action=message)
            self.repos.tasks.update(
                root_task_id, status=RootStatus.WAITING_RECOVERY.value, bump_revision=True
            )
            self._sink(root_task_id).emit(
                EventType.TASK_INTERRUPTED,
                payload={"reason": message, "recoverable": True},
            )
        except Exception:  # pragma: no cover - best effort bookkeeping
            logger.exception("failed to record internal error for %s", root_task_id)

    def _sink(self, root_task_id: str) -> EventSink:
        return EventSink(
            self.repos.events, EventContext(root_task_id=root_task_id, actor_id="task_service")
        )

    async def shutdown(self) -> None:
        """Cancel every running handle; each runner releases its lease first."""
        handles = [h for h in self._handles.values() if h.task is not None and not h.task.done()]
        for handle in handles:
            handle.task.cancel()  # type: ignore[union-attr]
        for handle in handles:
            try:
                await handle.task  # type: ignore[misc]
            except BaseException:  # noqa: BLE001 - shutdown must not raise
                pass

    # ---- drivers -----------------------------------------------------------
    async def _run(self, *, root_task_id: str, workflow_version: str | None = None, **kwargs: Any) -> None:
        workflow = self._workflow_for(root_task_id, workflow_version)
        async with open_checkpointer(self.settings.checkpoints_db_path) as checkpointer:
            assembly = assemble_workflow(
                settings=self.settings,
                repos=self.repos,
                workflow=workflow,
                llm=self.llm_factory(),
                extensions=self.extensions,
            )
            graph = compile_graph(assembly, checkpointer=checkpointer)
            runner = WorkflowRunner(
                runtime=assembly.runtime,
                graph=graph,
                recovery=assembly.recovery,
                owner=self.owner,
            )
            if workflow_version is None:
                await runner.resume(root_task_id=root_task_id)
            else:
                await runner.start(
                    root_task_id=root_task_id,
                    goal=kwargs["goal"],
                    workflow_version=workflow.workflow_version or workflow_version,
                    check_mode=kwargs.get("check_mode") or workflow.check_mode.value,
                    source_version=kwargs["source_version"],
                    source_artifact=kwargs["source_artifact"],
                )

    def _workflow_for(self, root_task_id: str, workflow_version: str | None) -> WorkflowConfig:
        if workflow_version is None:
            root = self.repos.tasks.get(root_task_id)
            if root is None:
                raise KeyError(f"unknown root task {root_task_id}")
            workflow_version = root.workflow_version
        workflow = self.repos.workflows.get(workflow_version)
        if workflow is None:
            raise KeyError(f"unknown workflow version {workflow_version}")
        return workflow
