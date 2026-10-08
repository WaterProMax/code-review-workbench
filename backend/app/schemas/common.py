"""Small shared value objects used across task, result and event schemas."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.enums import ErrorCategory

SCHEMA_VERSION = "1.0"


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp (Desgin §4.2 requires an offset)."""
    return datetime.now(timezone.utc)


class ErrorInfo(BaseModel):
    """Structured execution error (not a business verdict)."""

    model_config = ConfigDict(extra="forbid")

    code: str
    category: ErrorCategory = ErrorCategory.UNKNOWN
    message: str
    recoverable: bool = True
    detail_ref: str | None = None

    @field_validator("recoverable", mode="before")
    @classmethod
    def _default_recoverable(cls, value, info):  # noqa: ANN001
        if value is None:
            category = info.data.get("category", ErrorCategory.UNKNOWN)
            if isinstance(category, ErrorCategory):
                return category.default_recoverable
            return True
        return value


class TaskConstraints(BaseModel):
    """Execution limits attached to a dispatch; the ceiling lives in settings."""

    model_config = ConfigDict(extra="forbid")

    max_tool_steps: int = Field(default=12, ge=1)
    timeout_seconds: float = Field(default=180.0, gt=0)
    model_timeout_seconds: float | None = Field(default=None, gt=0)
    tool_timeout_seconds: float | None = Field(default=None, gt=0)
    allowed_paths: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list)
    read_only: bool = False

    def merged_over(self, base: "TaskConstraints") -> "TaskConstraints":
        """Return these constraints, taking tighter limits over the base."""
        return TaskConstraints(
            max_tool_steps=min(self.max_tool_steps, base.max_tool_steps),
            timeout_seconds=min(self.timeout_seconds, base.timeout_seconds),
            model_timeout_seconds=_min_opt(
                self.model_timeout_seconds, base.model_timeout_seconds
            ),
            tool_timeout_seconds=_min_opt(
                self.tool_timeout_seconds, base.tool_timeout_seconds
            ),
            allowed_paths=self.allowed_paths or base.allowed_paths,
            tool_names=sorted(set(self.tool_names) & set(base.tool_names))
            if base.tool_names
            else self.tool_names,
            read_only=self.read_only or base.read_only,
        )


def _min_opt(a: float | None, b: float | None) -> float | None:
    values = [v for v in (a, b) if v is not None]
    return min(values) if values else None


class TaskDep(BaseModel):
    """A concrete input dependency: task + attempt + condition + artifacts."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    attempt_id: str | None = None
    condition: str
    artifact_refs: list[str] = Field(default_factory=list)


class VersionRefs(BaseModel):
    """The versions fixed for a run (never mutated by later config edits)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    workflow_version: str
    contract_version: str | None = None
    source_version: str | None = None
    agent_versions: dict[str, str] = Field(default_factory=dict)
