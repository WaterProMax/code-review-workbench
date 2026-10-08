"""Shared pytest fixtures: isolated data dir, database, repos and services."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.artifacts import ArtifactService
from app.services.events import EventContext, EventSink
from app.services.execution_locks import ExecutionLockService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.database import Database
from app.storage.repositories import Repos


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    settings = Settings(
        data_dir=tmp_path / "data",
        model_api_key=None,
        max_repair_rounds=2,
        max_verification_retries=2,
    )
    settings.ensure_dirs()
    return settings


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.business_db_path)
    database.initialize()
    return database


@pytest.fixture
def repos(db: Database) -> Repos:
    return Repos(db)


@pytest.fixture
def artifacts(settings: Settings, repos: Repos) -> ArtifactService:
    return ArtifactService(settings, repos.artifacts)


@pytest.fixture
def workspace(settings: Settings, artifacts: ArtifactService, repos: Repos) -> WorkspaceService:
    return WorkspaceService(settings, artifacts, repos)


@pytest.fixture
def locks(repos: Repos) -> ExecutionLockService:
    return ExecutionLockService(repos.controls, repos.budget)


@pytest.fixture
def sink(repos: Repos) -> EventSink:
    return EventSink(repos.events, EventContext(root_task_id="unbound", actor_id="system"))
