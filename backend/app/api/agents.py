"""Agent capability listing (§12.1).

Returns the actually registered capabilities with versions, filtered so no secret
or implementation path leaks.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import services
from app.schemas.agents import CapabilityListing

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=list[CapabilityListing])
async def list_agents(request: Request) -> list[CapabilityListing]:
    return services(request).registry.list_capabilities()
