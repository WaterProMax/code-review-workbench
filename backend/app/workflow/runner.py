"""Task run entry point: lease ownership, heartbeats and checkpoint resume (§11.1).

The runner is the only place that drives the compiled graph for a root task. It
holds a DB-backed lease while it runs (so two processes never drive one task), and
it re-enters a graph that reached a wait/terminal state by correcting the current
version projection from the business ledger and continuing from the checkpoint.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from langgraph.types import Command, Overwrite

from app.schemas.enums import EventType
from app.storage.checkpoints import thread_id_for
from app.workflow.builder import GRAPH_RECURSION_LIMIT
from app.workflow.nodes import WorkflowRuntime, action_goto
from app.workflow.recovery import RecoveryCoordinator, RunRight
from app.workflow.state import WorkflowState, initial_state


class RunCancelled(RuntimeError):
    """The lease heartbeat failed and the in-flight drive was cancelled."""


class WorkflowRunner:
    def __init__(
        self,
        *,
        runtime: WorkflowRuntime,
        graph: Any,
        recovery: RecoveryCoordinator,
        owner: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.graph = graph
        self.recovery = recovery
        self.owner = owner or f"runner-{uuid.uuid4().hex[:8]}"
        self.heartbeat_interval = 20.0
        self.cancel_event = asyncio.Event()
        self._drive_task: asyncio.Task | None = None

    def config(self, root_task_id: str) -> dict[str, Any]:
        # recursion_limit bounds framework supersteps. The business step budget
        # stays on the budget node and is not this number.
        return {
            "recursion_limit": GRAPH_RECURSION_LIMIT,
            "configurable": {"thread_id": thread_id_for(root_task_id)},
        }

    # ---- start -------------------------------------------------------------
    async def start(
        self,
        *,
        root_task_id: str,
        goal: str,
        workflow_version: str,
        check_mode: str,
        source_version: str,
        source_artifact: str,
        agent_versions: dict[str, str] | None = None,
        run_segment_id: str = "seg-1",
        ttl_seconds: float = 60.0,
    ) -> WorkflowState:
        # The root row is minted through the controlled creation service (never by
        # the model), then the lease is taken before any work starts.
        from app.services.task_creation import TaskCreationService

        TaskCreationService(self.runtime.repos).create_root(
            goal=goal, workflow_version=workflow_version, root_task_id=root_task_id
        )
        self.runtime.repos.controls.ensure(root_task_id, run_segment_id, self.runtime.workflow.budgets.max_graph_steps)
        right = self.recovery.acquire_run_right(
            root_task_id, self.owner, ttl_seconds=ttl_seconds, run_segment_id=run_segment_id
        )
        state = initial_state(
            root_task_id=root_task_id,
            goal=goal,
            workflow_version=workflow_version,
            check_mode=check_mode,
            run_segment_id=right.run_segment_id,
            source_version=source_version,
            source_artifact=source_artifact,
            agent_versions=agent_versions,
        )
        return await self._drive(right, state, ttl_seconds)

    # ---- resume ------------------------------------------------------------
    async def resume(self, *, root_task_id: str, ttl_seconds: float = 60.0) -> WorkflowState:
        config = self.config(root_task_id)
        snapshot = await self.graph.aget_state(config)
        values: dict[str, Any] = dict(snapshot.values or {})

        # Take the fence before reconcile writes the patch ledger.
        right = self.recovery.acquire_run_right(
            root_task_id, self.owner, ttl_seconds=ttl_seconds
        )
        reconciliation = self.recovery.reconcile(root_task_id, values.get("source_version"))
        overrides = self._resume_overrides(reconciliation, right)
        # Audit rows stay in the event table. This segment starts with no denials.
        overrides["action_rejections"] = Overwrite([])
        overrides["branch_faults"] = Overwrite([])
        overrides["rejection_count"] = 0

        if values.get("contract"):
            next_nodes = tuple(getattr(snapshot, "next", ()) or ())
            if _snapshot_interrupted(snapshot):
                # The interrupt node applies the decision. Do not forge a detect step.
                payload = _resume_command_payload(
                    overrides, self._latest_resume_grants(root_task_id)
                )
                return await self._drive(right, Command(resume=payload), ttl_seconds)
            if "run_attempt" in next_nodes:
                # A finished Send branch is a pending checkpoint write. Continuing
                # replays only the branch that did not finish.
                return await self._drive(right, None, ttl_seconds)
            # Old checkpoints stopped at END, or next still names ``wait`` /
            # ``dispatch`` / another control node, and there is no interrupt.
            replay_dispatch = bool(
                values.get("next_action")
                and values["next_action"].get("action") in {"dispatch_task", "dispatch_batch"}
                and "dispatch" in next_nodes
            )
            if replay_dispatch:
                overrides["next_action"] = values["next_action"]
                # Command routing is not replayed by a plain state write, so the
                # resumed dispatch has to name its successor explicitly.
                await self.graph.aupdate_state(
                    config,
                    Command(update=overrides, goto=action_goto(overrides["next_action"])),
                    as_node="validate_action",
                )
            else:
                await self.graph.aupdate_state(config, overrides, as_node="detect")
            return await self._drive(right, None, ttl_seconds)

        # No usable checkpoint state: replay the graph from its entry point with a
        # seeded input. The initialize node is idempotent (the frozen contract and
        # the child tasks are reused from the business tables).
        root = self.runtime.repos.tasks.get(root_task_id)
        seed: dict[str, Any] = {
            "root_task_id": root_task_id,
            "goal": root.goal if root else "",
            "workflow_version": self.runtime.workflow.workflow_version or "",
            "check_mode": self.runtime.workflow.check_mode.value,
            "run_segment_id": right.run_segment_id,
            "source_version": overrides.get("source_version") or values.get("source_version"),
            "initial_source_version": values.get("initial_source_version"),
            "source_artifact": values.get("source_artifact"),
            "fatal_error": None,
            "next_action": None,
            "patched": overrides.get("patched", False),
        }
        return await self._drive(right, seed, ttl_seconds)

    def _resume_overrides(
        self, reconciliation: Any, right: RunRight
    ) -> dict[str, Any]:
        overrides: dict[str, Any] = {
            "status": "running",
            "next_action": None,
            "fatal_error": None,
            "waiting_reason": None,
            "required_action": None,
            "controller_faults": [],
            "dispatch_ok": False,
            "run_segment_id": right.run_segment_id,
        }
        if reconciliation.source_version is not None:
            overrides["source_version"] = reconciliation.source_version
        overrides["patched"] = reconciliation.patched
        if reconciliation.application is not None:
            overrides["last_application"] = reconciliation.application.model_dump(mode="json")
            overrides["last_application_error"] = None
        return overrides

    def _latest_resume_grants(self, root_task_id: str) -> Any:
        events = self.runtime.repos.events.list_after(root_task_id, 0, limit=5000)
        granted: Any = []
        for event in events:
            if event.event_type is EventType.TASK_RESUMED:
                granted = event.payload.get("granted") or []
        return granted

    # ---- drive -------------------------------------------------------------
    async def _drive(
        self, right: RunRight, state: WorkflowState | Command | None, ttl_seconds: float
    ) -> WorkflowState:
        from app.services.execution_context import active_execution
        self.runtime.fencing_token = right.fencing_token
        self.cancel_event.clear()
        cancellation = getattr(self.runtime, "cancellation", None)
        if cancellation is not None:
            cancellation.clear()
        context_token = active_execution.set((right.root_task_id, right.fencing_token))
        config = self.config(right.root_task_id)
        drive_task = asyncio.current_task()
        self._drive_task = drive_task
        beat = asyncio.create_task(self._heartbeat_loop(right, ttl_seconds, drive_task))
        try:
            kwargs: dict[str, Any] = {}
            if getattr(self.graph, "checkpointer", None) is not None:
                # Durable checkpoints: a hard process kill must never lose a
                # completed step, otherwise resume could not trust the position.
                kwargs["durability"] = "sync"
            if state is None:
                final = await self.graph.ainvoke(None, config, **kwargs)
            else:
                final = await self.graph.ainvoke(state, config, **kwargs)
        except asyncio.CancelledError:
            if self.cancel_event.is_set():
                if not self.recovery.holds_right(right):
                    from app.services.execution_context import ExecutionFenced
                    raise ExecutionFenced("执行权已转移，旧执行器停止")
                raise RunCancelled("执行租约心跳失败，已停止当前运行")
            raise
        except Exception as exc:
            # The outer service's error bookkeeping must never mutate a task
            # after this invocation has lost its execution right.
            if not self.recovery.holds_right(right):
                from app.services.execution_context import ExecutionFenced
                raise ExecutionFenced("执行权已转移，旧执行器停止错误回写") from exc
            raise
        finally:
            beat.cancel()
            try:
                await beat
            except asyncio.CancelledError:
                pass
            active_execution.reset(context_token)
            if self.recovery.holds_right(right):
                self.recovery.release(right)
            if self._drive_task is drive_task:
                self._drive_task = None
        return final

    async def _heartbeat_loop(
        self, right: RunRight, ttl_seconds: float, drive_task: asyncio.Task | None
    ) -> None:
        interval = min(self.heartbeat_interval, max(ttl_seconds / 3.0, 0.05))
        try:
            while True:
                await asyncio.sleep(interval)
                if not self.recovery.heartbeat(right, ttl_seconds=ttl_seconds):
                    self.cancel_event.set()
                    cancellation = getattr(self.runtime, "cancellation", None)
                    if cancellation is not None:
                        cancellation.set()
                    if drive_task is not None and not drive_task.done():
                        drive_task.cancel()
                    return
        except asyncio.CancelledError:
            raise


def _snapshot_interrupted(snapshot: Any) -> bool:
    tasks = getattr(snapshot, "tasks", None) or ()
    return any(getattr(task, "interrupts", None) for task in tasks)


def _resume_command_payload(overrides: dict[str, Any], grants: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"action": "resume", "grants": grants}
    for key, value in overrides.items():
        if isinstance(value, Overwrite):
            continue
        payload[key] = value
    return payload
