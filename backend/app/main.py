"""FastAPI application factory and lifecycle."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import build_app_services
from app.api.errors import install_error_handlers, set_request_id
from app.providers.base import LLMClient
from app.settings import Settings, get_settings

logger = logging.getLogger("hw2")
RECOVERY_SCAN_INTERVAL_SECONDS = 5.0


async def _monitor_interrupted_runs(services) -> None:
    while True:
        await asyncio.sleep(RECOVERY_SCAN_INTERVAL_SECONDS)
        try:
            interrupted = services.governance.recovery.recover_incomplete_runs(
                include_unleased_queued=False,
                skip_root_ids=set(services.runner.running_ids()),
            )
            if interrupted:
                logger.warning("marked interrupted after lease expiry: %s", interrupted)
        except Exception:
            logger.exception("interruption scan failed; will retry")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def create_app(
    settings: Settings | None = None,
    *,
    llm_factory: Callable[[], LLMClient] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    services = build_app_services(settings, llm_factory=llm_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # A restart must surface runs that lost their lease, so the workbench can
        # offer a resume entry instead of showing a stale "running" status (§11.1).
        interrupted = services.governance.recovery.recover_incomplete_runs()
        if interrupted:
            logger.warning("marked interrupted after restart: %s", interrupted)
        monitor = asyncio.create_task(_monitor_interrupted_runs(services))
        try:
            yield
        finally:
            monitor.cancel()
            try:
                await monitor
            except asyncio.CancelledError:
                pass
            await services.runner.shutdown()

    app = FastAPI(
        title="Homework 2 Workbench API",
        version="0.1.0",
        description="父 Agent 管理的代码审查与修复工作台 API",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.services = services

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        set_request_id(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    install_error_handlers(app)

    from app.api.agents import router as agents_router
    from app.api.artifacts import router as artifacts_router
    from app.api.health import router as health_router
    from app.api.sources import router as sources_router
    from app.api.tasks import router as tasks_router
    from app.api.workflows import router as workflows_router

    for router in (
        health_router,
        sources_router,
        agents_router,
        workflows_router,
        tasks_router,
        artifacts_router,
    ):
        app.include_router(router, prefix="/api")

    return app


app = create_app()
