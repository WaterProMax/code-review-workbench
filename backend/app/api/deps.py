"""App-wide service container and FastAPI dependencies (§12.1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

from app.api.errors import AppError
from app.extensions import ExtensionRegistry, build_default_extension_registry
from app.providers.base import LLMClient
from app.registry.agents import AgentRegistry, build_default_agent_registry
from app.schemas.api import ErrorCode
from app.services.artifacts import ArtifactService
from app.services.background import TaskRunnerService
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.database import Database
from app.storage.repositories import Repos
from app.workflow.assembly import Governance, assemble_governance, build_services


@dataclass
class AppServices:
    settings: Settings
    database: Database
    repos: Repos
    artifacts: ArtifactService
    workspace: WorkspaceService
    locks: ExecutionLockService
    registry: AgentRegistry
    extensions: ExtensionRegistry
    governance: Governance
    runner: TaskRunnerService

    def workflow(self, workflow_version: str):
        config = self.repos.workflows.get(workflow_version)
        if config is None:
            raise AppError(ErrorCode.NOT_FOUND, f"未知工作流版本 {workflow_version}")
        return config


def build_app_services(
    settings: Settings,
    *,
    llm_factory: Callable[[], LLMClient] | None = None,
    extensions: ExtensionRegistry | None = None,
) -> AppServices:
    settings.ensure_dirs()
    database = Database(settings.business_db_path)
    database.initialize()
    repos = Repos(database)
    artifacts, workspace, locks = build_services(settings=settings, repos=repos)
    extensions = extensions or build_default_extension_registry()
    registry = build_default_agent_registry(extensions=extensions)
    governance = assemble_governance(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        registry=registry,
        extensions=extensions,
    )
    runner = TaskRunnerService(
        settings=settings,
        repos=repos,
        llm_factory=llm_factory,
        extensions=extensions,
    )
    return AppServices(
        settings=settings,
        database=database,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        registry=registry,
        extensions=extensions,
        governance=governance,
        runner=runner,
    )


def services(request: Request) -> AppServices:
    return request.app.state.services  # type: ignore[no-any-return]
