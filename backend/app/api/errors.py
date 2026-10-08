"""Unified error type and FastAPI exception handlers."""

from __future__ import annotations

import logging
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.api import ErrorCode, ErrorResponse
from app.settings import ConfigurationError

logger = logging.getLogger("hw2.errors")

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(value: str | None) -> None:
    _request_id.set(value)


def current_request_id() -> str | None:
    return _request_id.get()


# HTTP status per error code.
_STATUS: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.CONFIG_MODE_CONFLICT: 409,
    ErrorCode.CONFIG_DEFAULT_FROZEN: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.BUDGET_EXHAUSTED: 409,
    ErrorCode.CONFLICT: 409,
    ErrorCode.NOT_RESUMABLE: 409,
    ErrorCode.NOT_TERMINABLE: 409,
    ErrorCode.NOT_RECOVERABLE: 422,
    ErrorCode.MODEL_NOT_CONFIGURED: 503,
    ErrorCode.MODEL_REQUEST_FAILED: 502,
    ErrorCode.TEMPORARY_FAILURE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class AppError(Exception):
    """Domain error carrying a stable code, message and structured details."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status or _STATUS.get(code, 500)


def _payload(err: AppError) -> dict:
    return ErrorResponse(
        code=err.code,
        message=err.message,
        details=err.details,
        request_id=current_request_id(),
    ).model_dump(mode="json")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=_payload(exc))

    @app.exception_handler(ConfigurationError)
    async def _config_error(_: Request, exc: ConfigurationError) -> JSONResponse:
        err = AppError(ErrorCode.MODEL_NOT_CONFIGURED, exc.message)
        return JSONResponse(status_code=err.http_status, content=_payload(err))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        err = AppError(
            ErrorCode.VALIDATION_ERROR,
            "请求参数校验失败",
            {"errors": _jsonable_errors(exc.errors())},
        )
        return JSONResponse(status_code=err.http_status, content=_payload(err))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.VALIDATION_ERROR
        message = str(exc.detail) if exc.detail else f"HTTP {exc.status_code}"
        err = AppError(code, message, http_status=exc.status_code)
        return JSONResponse(status_code=err.http_status, content=_payload(err))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        err = AppError(ErrorCode.INTERNAL_ERROR, "服务器内部错误")
        return JSONResponse(status_code=err.http_status, content=_payload(err))


def _jsonable_errors(raw: list) -> list:
    out = []
    for item in raw:
        item = dict(item)
        # ctx may contain non-serialisable exception instances
        if "ctx" in item and isinstance(item["ctx"], dict):
            item["ctx"] = {k: str(v) for k, v in item["ctx"].items()}
        out.append(item)
    return out
