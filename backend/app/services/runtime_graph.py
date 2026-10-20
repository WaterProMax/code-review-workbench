"""Serialize a compiled workflow graph for the HTTP API."""

from __future__ import annotations

from app.providers.null import NullLLM
from app.schemas.api import GraphEdgeView, GraphNodeView, WorkflowGraphResponse
from app.schemas.workflows import WorkflowConfig
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from app.workflow.assembly import assemble_workflow, compile_graph

_HIDDEN = {"__start__", "__end__"}


def build_workflow_graph(
    *,
    settings: Settings,
    repos: Repos,
    workflow: WorkflowConfig,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
    registry: object,
    extensions: object,
) -> WorkflowGraphResponse:
    """Assemble the runtime graph and project nodes, edges and mermaid."""
    llm = NullLLM()
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
    compiled = compile_graph(assembly, checkpointer=None)
    drawable = compiled.get_graph()
    node_ids = [node_id for node_id in drawable.nodes if node_id not in _HIDDEN]
    edges = [
        GraphEdgeView(
            source=edge.source,
            target=edge.target,
            conditional=bool(edge.conditional),
        )
        for edge in drawable.edges
        if edge.source not in _HIDDEN and edge.target not in _HIDDEN
    ]
    mermaid = drawable.draw_mermaid()
    return WorkflowGraphResponse(
        mermaid=mermaid,
        nodes=[GraphNodeView(id=node_id) for node_id in node_ids],
        edges=edges,
    )
