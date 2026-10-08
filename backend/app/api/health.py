"""Health check endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.settings import Settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    return {
        "status": "ok",
        "version": "0.1.0",
        "model_configured": settings.model_configured,
        "model_provider": settings.model_provider,
        "model_name": settings.model_name,
        "data_dir": str(settings.data_dir),
    }
