"""Workflow configuration endpoints (§12.1, §14.1).

A saved workflow version is immutable: re-posting the same semantic content is
idempotent, while reusing a version name for different content is a conflict.
A config is validated against the *registered* roles and task kinds before it is
stored, so a canvas cannot save an unrunnable graph, and the errors point at the
offending node or field.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.api.deps import services
from app.api.errors import AppError
from app.config_loader import load_workflow_templates
from app.extensions import ExtensionError, validate_workflow_config
from app.schemas.api import (
    ErrorCode,
    TemplateListing,
    WorkflowListItem,
    WorkflowSaveResponse,
)
from app.schemas.workflows import WorkflowConfig

router = APIRouter(tags=["workflows"])


def _validated(svc, payload: WorkflowConfig) -> WorkflowConfig:
    """Expand, validate against the registries and freeze the temporary version."""
    extra = svc.extensions.template_dependencies(payload)
    candidate = payload.with_standard_expansion(extra_dependencies=extra)
    try:
        validate_workflow_config(candidate, svc.registry, svc.extensions)
    except ExtensionError as exc:
        raise AppError(ErrorCode.VALIDATION_ERROR, exc.message, exc.details) from exc
    return candidate


@router.get("/workflows/templates", response_model=list[TemplateListing])
async def list_templates(request: Request) -> list[TemplateListing]:
    """The shipped templates the canvas can start from (§14.1)."""
    svc = services(request)
    out: list[TemplateListing] = []
    for name, description, config in load_workflow_templates(svc.settings.config_dir):
        _validated(svc, config)
        out.append(
            TemplateListing(
                name=name,
                description=description,
                check_mode=config.check_mode,
                config=config.with_standard_expansion(
                    extra_dependencies=svc.extensions.template_dependencies(config)
                ),
            )
        )
    return out


@router.post("/workflows", response_model=WorkflowSaveResponse, status_code=201)
async def save_workflow(request: Request, payload: WorkflowConfig) -> WorkflowSaveResponse:
    svc = services(request)
    config = _validated(svc, payload)
    semantic = config.compute_semantic_hash()
    version = payload.workflow_version or semantic

    existing = svc.repos.workflows.get(version)
    if existing is not None:
        if existing.semantic_hash != semantic:
            raise AppError(
                ErrorCode.CONFIG_MODE_CONFLICT,
                f"工作流版本 {version} 已存在且语义不同，不能覆盖不可变配置",
                {"semantic_hash": existing.semantic_hash, "incoming": semantic},
            )
        return WorkflowSaveResponse(
            workflow_version=version,
            semantic_hash=existing.semantic_hash or existing.compute_semantic_hash(),
            check_mode=existing.check_mode,
            config=existing,
        )

    bound = config.bind(version)
    svc.repos.workflows.insert(bound)
    saved = svc.repos.workflows.get(version)
    assert saved is not None
    return WorkflowSaveResponse(
        workflow_version=version,
        semantic_hash=saved.semantic_hash or saved.compute_semantic_hash(),
        check_mode=saved.check_mode,
        config=saved,
    )


@router.get("/workflows", response_model=list[WorkflowListItem])
async def list_workflows(request: Request) -> list[WorkflowListItem]:
    svc = services(request)
    items: list[WorkflowListItem] = []
    for row in svc.repos.workflows.list():
        items.append(
            WorkflowListItem(
                workflow_version=row["workflow_version"],
                semantic_hash=row["semantic_hash"],
                check_mode=row["check_mode"],
                created_at=row["created_at"] or datetime.now(timezone.utc),
            )
        )
    return items


@router.get("/workflows/{workflow_version}", response_model=WorkflowConfig)
async def get_workflow(request: Request, workflow_version: str) -> WorkflowConfig:
    return services(request).workflow(workflow_version)
