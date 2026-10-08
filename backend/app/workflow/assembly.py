"""Production assembly of one workflow run (§5, §7).

Both the HTTP layer (and its background service) and the tests assemble the same
object graph here, so a test never drives a different wiring than production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.parent import ParentAgent
from app.extensions.base import ExtensionRegistry, default_input_adapters
from app.providers.base import LLMClient
from app.registry.agents import AgentRegistry, build_default_agent_registry
from app.registry.tools import ToolRegistry
from app.schemas.workflows import WorkflowConfig
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.task_service import TaskService
from app.services.tools import build_default_registry
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from app.workflow.builder import build_graph
from app.workflow.controller import ParentController
from app.workflow.detection import Detector
from app.workflow.nodes import WorkflowRuntime
from app.workflow.recovery import RecoveryCoordinator

BASE_VERDICT_KINDS = frozenset({"review", "fix", "verify"})


def build_services(
    *,
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService | None = None,
    workspace: WorkspaceService | None = None,
    locks: ExecutionLockService | None = None,
) -> tuple[ArtifactService, WorkspaceService, ExecutionLockService]:
    artifacts = artifacts or ArtifactService(settings, repos.artifacts)
    workspace = workspace or WorkspaceService(settings, artifacts, repos)
    locks = locks or ExecutionLockService(repos.controls, repos.budget)
    return artifacts, workspace, locks


@dataclass
class Governance:
    """The deterministic control layer: validation, receipts, detection, recovery."""

    detector: Detector
    task_service: TaskService
    controller: ParentController
    recovery: RecoveryCoordinator


def assemble_governance(
    *,
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
    registry: AgentRegistry | None = None,
    extensions: ExtensionRegistry | None = None,
) -> Governance:
    extensions = extensions or ExtensionRegistry()
    registry = registry or build_default_agent_registry(extensions=extensions)
    verdict_kinds = BASE_VERDICT_KINDS | {
        plugin.task_kind for plugin in extensions.plugins() if plugin.verdict_bearing
    }
    detector = Detector(repos, artifacts, verdict_kinds=frozenset(verdict_kinds))
    task_service = TaskService(repos, locks)
    controller = ParentController(
        repos=repos,
        task_service=task_service,
        registry=registry,
        workspace=workspace,
        artifacts=artifacts,
        detector=detector,
        settings=settings,
        input_adapters=[*default_input_adapters(), *extensions.input_adapters()],
    )
    recovery = RecoveryCoordinator(
        repos=repos,
        workspace=workspace,
        artifacts=artifacts,
        locks=locks,
        task_service=task_service,
        detector=detector,
        controller=controller,
        settings=settings,
    )
    return Governance(
        detector=detector,
        task_service=task_service,
        controller=controller,
        recovery=recovery,
    )


@dataclass
class WorkflowAssembly:
    runtime: WorkflowRuntime
    recovery: RecoveryCoordinator
    controller: ParentController
    task_service: TaskService
    detector: Detector
    workflow: WorkflowConfig


def assemble_workflow(
    *,
    settings: Settings,
    repos: Repos,
    workflow: WorkflowConfig,
    llm: LLMClient,
    artifacts: ArtifactService | None = None,
    workspace: WorkspaceService | None = None,
    locks: ExecutionLockService | None = None,
    registry: AgentRegistry | None = None,
    tools: ToolRegistry | None = None,
    extensions: ExtensionRegistry | None = None,
) -> WorkflowAssembly:
    artifacts, workspace, locks = build_services(
        settings=settings, repos=repos, artifacts=artifacts, workspace=workspace, locks=locks
    )
    extensions = extensions or ExtensionRegistry()
    registry = registry or build_default_agent_registry(extensions=extensions)
    tools = tools or build_default_registry()
    governance = assemble_governance(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        registry=registry,
        extensions=extensions,
    )
    parent = ParentAgent(llm=llm, registry=registry, settings=settings)
    runtime = WorkflowRuntime(
        settings=settings,
        repos=repos,
        workflow=workflow,
        registry=registry,
        tools=tools,
        llm=llm,
        workspace=workspace,
        artifacts=artifacts,
        locks=locks,
        task_service=governance.task_service,
        controller=governance.controller,
        detector=governance.detector,
        parent=parent,
    )
    return WorkflowAssembly(
        runtime=runtime,
        recovery=governance.recovery,
        controller=governance.controller,
        task_service=governance.task_service,
        detector=governance.detector,
        workflow=workflow,
    )


def compile_graph(assembly: WorkflowAssembly, *, checkpointer: Any = None) -> Any:
    return build_graph(assembly.runtime, checkpointer=checkpointer)
