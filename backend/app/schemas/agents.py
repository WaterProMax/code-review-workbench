"""Agent capability description shared by the registry and the API (§7.1)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import ArtifactType


class AgentSpec(BaseModel):
    """A registered role's capability contract.

    ``input_schema`` / ``output_schema`` are JSON-schema fragments used to
    validate dispatches and reports. ``tool_names`` and ``permissions`` are the
    authorization the adapter enforces — the model cannot widen them.
    """

    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    supported_task_kinds: list[str] = Field(min_length=1)

    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    tool_names: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)

    # artifacts a run of this role is expected to produce / consume
    produced_artifact_types: list[ArtifactType] = Field(default_factory=list)
    required_input_keys: list[str] = Field(default_factory=list)

    # P10: a new task_kind must declare its retry budget instead of falling into
    # an implicit "unlimited retries" branch.
    retry_budget: dict[str, str] = Field(default_factory=dict)

    # extension hooks (P10); empty for the three base roles
    detection_plugin: str | None = None
    dependency_plugin: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "AgentSpec":
        if not self.version.strip():
            raise ValueError("agent version must be non-empty")
        if "acceptance_contract" in self.required_input_keys and "source" not in self.required_input_keys:
            raise ValueError("an agent requiring the contract must also require the source")
        for kind in self.supported_task_kinds:
            if kind in ("review", "fix", "verify") and kind not in self.retry_budget:
                raise ValueError(f"base task_kind {kind!r} must declare a retry budget")
        return self

    def key(self) -> tuple[str, str]:
        return (self.agent_id, self.version)


class CapabilityListing(BaseModel):
    """Public projection of a capability (no secrets, no implementation paths)."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    version: str
    description: str
    supported_task_kinds: list[str]
    tool_names: list[str]
    required_input_keys: list[str]
    produced_artifact_types: list[str]
    retry_budget: dict[str, str]

    @classmethod
    def from_spec(cls, spec: AgentSpec) -> "CapabilityListing":
        return cls(
            agent_id=spec.agent_id,
            version=spec.version,
            description=spec.description,
            supported_task_kinds=list(spec.supported_task_kinds),
            tool_names=list(spec.tool_names),
            required_input_keys=list(spec.required_input_keys),
            produced_artifact_types=[t.value for t in spec.produced_artifact_types],
            retry_budget=dict(spec.retry_budget),
        )


class RegisteredAgentConfig(BaseModel):
    """One enabled role in ``config/agents.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1)
    description: str = Field(min_length=1)
    tool_names: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class AgentConfigFile(BaseModel):
    """Declares which registered versions a workflow config may reference."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(min_length=1)
    agents: dict[str, RegisteredAgentConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_parent(self) -> "AgentConfigFile":
        if "parent" not in self.agents:
            raise ValueError("agent config must declare the parent role")
        return self
