"""Source upload endpoint (§12.1).

Only relative paths inside the upload are accepted; sizes are checked against the
server limits. The endpoint produces an immutable manifest and never mints a
business task id.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, File, Request, UploadFile

from app.api.deps import services
from app.api.errors import AppError
from app.schemas.api import ErrorCode, SourceFileView, SourceUploadResponse
from app.schemas.artifacts import normalise_relpath

router = APIRouter(tags=["sources"])


@router.post("/sources", response_model=SourceUploadResponse, status_code=201)
async def upload_sources(request: Request, files: list[UploadFile] = File(...)) -> SourceUploadResponse:
    svc = services(request)
    settings = svc.settings
    if not files:
        raise AppError(ErrorCode.VALIDATION_ERROR, "至少需要一个文件")
    if len(files) > settings.max_upload_files:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"文件数超过上限 {settings.max_upload_files}",
        )

    payloads: list[tuple[str, bytes]] = []
    total = 0
    for upload in files:
        try:
            rel = normalise_relpath(upload.filename or "")
        except ValueError as exc:
            raise AppError(ErrorCode.VALIDATION_ERROR, f"非法相对路径：{exc}") from exc
        if not rel:
            raise AppError(ErrorCode.VALIDATION_ERROR, "存在空文件路径")
        suffix = "." + rel.rsplit(".", 1)[-1] if "." in rel else ""
        if settings.allowed_upload_suffixes and suffix not in settings.allowed_upload_suffixes:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"不支持的文件类型 {suffix or rel!r}；允许 {list(settings.allowed_upload_suffixes)}",
            )
        blob = await upload.read()
        if len(blob) > settings.max_upload_file_bytes:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"{rel} 超过单文件上限 {settings.max_upload_file_bytes} 字节",
            )
        total += len(blob)
        if total > settings.max_upload_total_bytes:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                f"上传总大小超过上限 {settings.max_upload_total_bytes} 字节",
            )
        payloads.append((rel, blob))

    manifest = svc.workspace.create_upload(payloads)
    return SourceUploadResponse(
        source_id=manifest.source_id,
        files=[
            SourceFileView(path=e.path, size=e.size, sha256=e.sha256) for e in manifest.files
        ],
        total_bytes=manifest.total_bytes,
        content_digest=manifest.content_digest,
        created_at=datetime.now(timezone.utc),
    )
