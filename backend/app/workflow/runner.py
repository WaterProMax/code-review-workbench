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

from app.storage.checkpoints import thread_id_for
from app.workflow.nodes import WorkflowRuntime
from app.workflow.recovery import RecoveryCoordinator, RunRight
from app.workflow.state import WorkflowState, initial_state


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

    def config(self, root_task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id_for(root_task_id)}}

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

        reconciliation = self.recovery.reconcile(root_task_id, values.get("source_version"))

        right = self.recovery.acquire_run_right(
            root_task_id, self.owner, ttl_seconds=ttl_seconds
        )
        overrides = self._resume_overrides(reconciliation, right)
        # Keep the audit history, but old denials do not constrain a new run
        # segment after the user has changed its execution allowance.
        overrides["rejection_history_start"] = len(values.get("action_rejections") or [])
        overrides["rejection_count"] = 0

        if values.get("contract"):
            # Correct the checkpointed projection, then continue from the parent
            # decision. Only scalar channels are written so accumulated list/map
            # channels are preserved rather than duplicated.
            replay_dispatch = bool(values.get("next_action") and values["next_action"].get("action") in {"dispatch_task", "dispatch_batch"} and "dispatch" in snapshot.next)
            if replay_dispatch:
                overrides["next_action"] = values["next_action"]
            await self.graph.aupdate_state(config, overrides, as_node="validate_action" if replay_dispatch else "detect")
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

    # ---- drive -------------------------------------------------------------
    async def _drive(
        self, right: RunRight, state: WorkflowState | None, ttl_seconds: float
    ) -> WorkflowState:
        from app.services.execution_context import active_execution
        self.runtime.fencing_token = right.fencing_token
        context_token = active_execution.set((right.root_task_id, right.fencing_token))
        config = self.config(right.root_task_id)
        beat = asyncio.create_task(self._heartbeat_loop(right, ttl_seconds))
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
        return final

    async def _heartbeat_loop(self, right: RunRight, ttl_seconds: float) -> None:
        interval = min(self.heartbeat_interval, max(ttl_seconds / 3.0, 0.05))
        try:
            while True:
                await asyncio.sleep(interval)
                if not self.recovery.heartbeat(right, ttl_seconds=ttl_seconds):
                    return
        except asyncio.CancelledError:
            raise
