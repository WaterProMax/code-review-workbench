"""Execution event contract for the tracking service (Desgin §8)."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import utcnow
from app.schemas.enums import EventType


class ExecutionEvent(BaseModel):
    """One tracked step, addressed by (root_task_id, sequence)."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)

    task_id: str | None = None
    attempt_id: str | None = None
    dispatch_batch_id: str | None = None
    source_version: str | None = None

    timestamp: datetime = Field(default_factory=utcnow)
    actor_id: str = Field(min_length=1)
    event_type: EventType
    payload: dict = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    duration_ms: int | None = Field(default=None, ge=0)

    @field_validator("timestamp")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event timestamp must be timezone-aware (store UTC)")
        return value.astimezone(timezone.utc)
