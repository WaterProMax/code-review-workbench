"""Artifact download endpoint (§12.1).

Every read is resolved inside the data directory by ``ArtifactService`` and
verified against the stored hash, so no arbitrary server file can be read.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.api.deps import services
from app.api.errors import AppError
from app.schemas.api import ErrorCode
from app.services.artifacts import ArtifactError

router = APIRouter(tags=["artifacts"])

_JSON_SUFFIX = ".json"


@router.get("/artifacts/{artifact_id}")
async def get_artifact(request: Request, artifact_id: str) -> Response:
    svc = services(request)
    try:
        artifact = svc.artifacts.get(artifact_id)
        data = svc.artifacts.read_bytes(artifact_id)
    except ArtifactError as exc:
        raise AppError(ErrorCode.NOT_FOUND, f"未知或不可读产物 {artifact_id}：{exc}") from exc

    if artifact.storage_ref.endswith(_JSON_SUFFIX):
        return JSONResponse(content=json.loads(data.decode("utf-8")))
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{artifact_id}"'},
    )
