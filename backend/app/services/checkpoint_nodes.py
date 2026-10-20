"""Best-effort read of the graph nodes waiting at the latest checkpoint."""

from __future__ import annotations

import logging

from app.providers.null import NullLLM
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.checkpoints import open_checkpointer, thread_id_for
from app.storage.repositories import Repos
from app.workflow.assembly import assemble_workflow, compile_graph

logger = logging.getLogger("hw2.checkpoints")

_HIDDEN = {"__start__", "__end__"}


async def current_nodes(
    *,
    settings: Settings,
    repos: Repos,
    root_task_id: str,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
    registry: object,
    extensions: object,
) -> list[str]:
    """Return ``list(snapshot.next)`` or ``[]`` if the checkpoint cannot be read.

    A missing checkpoint, a missing workflow, or any read error leaves the detail
    request successful with an empty list.
    """
    path = settings.checkpoints_db_path
    if not path.exists():
        return []
    try:
        root = repos.tasks.get(root_task_id)
        if root is None:
            return []
        workflow = repos.workflows.get(root.workflow_version)
        if workflow is None:
            return []
        llm = NullLLM()
        try:
            assembly = assemble_workflow(
                settings=settings,
                repos=repos,
                workflow=workflow,
                llm=llm,
                artifacts=artifacts,
                workspace=workspace,
                locks=locks,
                registry=registry,  # type: ignore[arg-type]
                extensions=extensions,  # type: ignore[arg-type]
            )
            async with open_checkpointer(path) as checkpointer:
                graph = compile_graph(assembly, checkpointer=checkpointer)
                snapshot = await graph.aget_state(
                    {"configurable": {"thread_id": thread_id_for(root_task_id)}}
                )
        finally:
            await llm.aclose()
        nxt = getattr(snapshot, "next", ()) or ()
        return [str(node) for node in nxt if str(node) not in _HIDDEN]
    except Exception:
        logger.debug("current_nodes unavailable for %s", root_task_id, exc_info=True)
        return []
