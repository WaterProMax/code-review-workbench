"""EventSink: automatic execution tracking bound to a run context (§6.4, §8).

A sink carries the identities that every event must be correlated with, so
agents, tools, the control layer and the task service only supply the business
summary of what happened.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from app.schemas.enums import EventType
from app.schemas.events import ExecutionEvent
from app.storage.repositories import EventRepository


@dataclass(frozen=True)
class EventContext:
    root_task_id: str
    task_id: str | None = None
    attempt_id: str | None = None
    dispatch_batch_id: str | None = None
    actor_id: str = "system"
    source_version: str | None = None


class EventSink:
    """Writes correlated execution events in short transactions."""

    def __init__(self, repo: EventRepository, context: EventContext | None = None) -> None:
        self.repo = repo
        self.context = context

    # ---- context -----------------------------------------------------------
    def bind(self, **overrides: Any) -> "EventSink":
        if self.context is None:
            raise RuntimeError("cannot bind an unbound EventSink")
        return EventSink(self.repo, replace(self.context, **overrides))

    def for_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        dispatch_batch_id: str,
        actor_id: str,
        source_version: str | None = None,
    ) -> "EventSink":
        return self.bind(
            attempt_id=attempt_id,
            task_id=task_id,
            dispatch_batch_id=dispatch_batch_id,
            actor_id=actor_id,
            source_version=source_version,
        )

    # ---- emit --------------------------------------------------------------
    def emit(
        self,
        event_type: EventType,
        *,
        payload: dict | None = None,
        artifact_refs: list[str] | None = None,
        duration_ms: int | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        dispatch_batch_id: str | None = None,
        actor_id: str | None = None,
        source_version: str | None = None,
        conn=None,  # type: ignore[no-untyped-def]
    ) -> ExecutionEvent:
        ctx = self.context
        root_task_id = ctx.root_task_id if ctx else (payload or {}).get("root_task_id")
        if not root_task_id:
            raise RuntimeError("EventSink has no root_task_id context")
        event = ExecutionEvent(
            event_id=f"ev-{uuid.uuid4().hex[:16]}",
            root_task_id=root_task_id,
            sequence=1,  # replaced by the repository inside the transaction
            task_id=task_id if task_id is not None else (ctx.task_id if ctx else None),
            attempt_id=attempt_id if attempt_id is not None else (ctx.attempt_id if ctx else None),
            dispatch_batch_id=(
                dispatch_batch_id
                if dispatch_batch_id is not None
                else (ctx.dispatch_batch_id if ctx else None)
            ),
            source_version=(
                source_version
                if source_version is not None
                else (ctx.source_version if ctx else None)
            ),
            timestamp=datetime.now(timezone.utc),
            actor_id=actor_id if actor_id is not None else (ctx.actor_id if ctx else "system"),
            event_type=event_type,
            payload=payload or {},
            artifact_refs=artifact_refs or [],
            duration_ms=duration_ms,
        )
        return self.repo.append(event, conn=conn)


def unbound_sink(repo: EventRepository) -> EventSink:
    """A sink used before a root task exists (e.g. upload events)."""
    return EventSink(repo)
