"""Workflow configuration contract with legal-connection validation (§14.1.1)."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.enums import CheckMode, WorkflowNodeType
from app.settings import (
    CAP_MAX_EVIDENCE_RETRIES,
    CAP_MAX_FIX_RETRIES,
    CAP_MAX_GENERATE_RETRIES,
    CAP_MAX_GRAPH_STEPS,
    CAP_MAX_MODEL_RETRIES,
    CAP_MAX_PARENT_CORRECTIONS,
    CAP_MAX_REPAIR_ROUNDS,
    CAP_MAX_REVIEW_RETRIES,
    CAP_MAX_TOOL_STEPS,
    CAP_MAX_VERIFICATION_RETRIES,
    CAP_ATTEMPT_TIMEOUT_SECONDS,
    CAP_MODEL_TIMEOUT_SECONDS,
    CAP_TOOL_TIMEOUT_SECONDS,
)


class BudgetConfig(BaseModel):
    """Per-run budgets; omitted items are expanded to defaults when saved."""

    model_config = ConfigDict(extra="forbid")

    max_repair_rounds: int = Field(default=2, ge=0, le=CAP_MAX_REPAIR_ROUNDS)
    max_verification_retries: int = Field(default=2, ge=0, le=CAP_MAX_VERIFICATION_RETRIES)
    max_review_retries: int = Field(default=2, ge=0, le=CAP_MAX_REVIEW_RETRIES)
    max_fix_retries: int = Field(default=2, ge=0, le=CAP_MAX_FIX_RETRIES)
    max_evidence_retries: int = Field(default=2, ge=0, le=CAP_MAX_EVIDENCE_RETRIES)
    max_generate_retries: int = Field(default=2, ge=0, le=CAP_MAX_GENERATE_RETRIES)
    max_parent_corrections: int = Field(default=2, ge=0, le=CAP_MAX_PARENT_CORRECTIONS)
    max_model_retries: int = Field(default=2, ge=0, le=CAP_MAX_MODEL_RETRIES)
    max_tool_steps: int = Field(default=12, ge=1, le=CAP_MAX_TOOL_STEPS)
    model_timeout_seconds: float = Field(default=60.0, gt=0, le=CAP_MODEL_TIMEOUT_SECONDS)
    tool_timeout_seconds: float = Field(default=30.0, gt=0, le=CAP_TOOL_TIMEOUT_SECONDS)
    attempt_timeout_seconds: float = Field(default=180.0, gt=0, le=CAP_ATTEMPT_TIMEOUT_SECONDS)
    max_graph_steps: int = Field(default=256, ge=1, le=CAP_MAX_GRAPH_STEPS)


class WorkflowNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    type: WorkflowNodeType
    agent_id: str | None = None
    task_kind: str | None = None
    tool_name: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "WorkflowNode":
        if self.type is WorkflowNodeType.AGENT:
            if not self.agent_id or not self.task_kind:
                raise ValueError("agent node requires agent_id and task_kind")
        elif self.type is WorkflowNodeType.TOOL:
            if not self.tool_name:
                raise ValueError("tool node requires tool_name")
        elif self.type is WorkflowNodeType.PARENT:
            if self.agent_id != "parent":
                raise ValueError("parent node must reference agent_id 'parent'")
        return self


class WorkflowEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)


class InputDependency(BaseModel):
    """A *data* dependency, distinct from a dispatch/return edge (§14.1.1)."""

    model_config = ConfigDict(extra="forbid")

    source_node: str = Field(min_length=1)
    target_node: str = Field(min_length=1)
    artifact: str = Field(min_length=1)


class WorkflowLayoutItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    x: float
    y: float


class WorkflowConfig(BaseModel):
    """The saved, immutable orchestration configuration."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    check_mode: CheckMode
    agents: dict[str, str] = Field(min_length=1)
    nodes: list[WorkflowNode] = Field(min_length=1)
    edges: list[WorkflowEdge] = Field(min_length=1)
    dependencies: list[InputDependency] = Field(default_factory=list)
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)
    layout: list[WorkflowLayoutItem] = Field(default_factory=list)

    # populated when saved
    workflow_version: str | None = None
    semantic_hash: str | None = None
    tool_policy_versions: dict[str, str] = Field(default_factory=dict)

    # ---- helpers -----------------------------------------------------------
    def node(self, node_id: str) -> WorkflowNode | None:
        return next((n for n in self.nodes if n.id == node_id), None)

    def nodes_of_type(self, node_type: WorkflowNodeType) -> list[WorkflowNode]:
        return [n for n in self.nodes if n.type is node_type]

    def edges_from(self, node_id: str) -> list[str]:
        return [e.target for e in self.edges if e.source == node_id]

    def edges_to(self, node_id: str) -> list[str]:
        return [e.source for e in self.edges if e.target == node_id]

    def compute_semantic_hash(self) -> str:
        """Hash of everything that changes execution; layout is excluded."""
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "check_mode": self.check_mode.value,
            "agents": dict(sorted(self.agents.items())),
            "nodes": sorted(
                (
                    {
                        "id": n.id,
                        "type": n.type.value,
                        "agent_id": n.agent_id,
                        "task_kind": n.task_kind,
                        "tool_name": n.tool_name,
                    }
                    for n in self.nodes
                ),
                key=lambda d: d["id"],
            ),
            "edges": sorted(((e.source, e.target) for e in self.edges)),
            "dependencies": sorted(
                (d.source_node, d.target_node, d.artifact) for d in self.dependencies
            ),
            "budgets": self.budgets.model_dump(),
            "tool_policy_versions": dict(sorted(self.tool_policy_versions.items())),
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return "wf-" + hashlib.sha256(blob).hexdigest()[:32]

    # ---- validation --------------------------------------------------------
    @model_validator(mode="after")
    def _validate_structure(self) -> "WorkflowConfig":
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate node id")

        parents = self.nodes_of_type(WorkflowNodeType.PARENT)
        ends = self.nodes_of_type(WorkflowNodeType.END)
        if len(parents) != 1:
            raise ValueError("workflow requires exactly one parent node")
        if len(ends) != 1:
            raise ValueError("workflow requires exactly one end node")
        parent_id = parents[0].id
        end_id = ends[0].id

        # referenced agents must be declared with a fixed version
        for node in self.nodes:
            if node.agent_id and node.agent_id not in self.agents:
                raise ValueError(
                    f"node {node.id!r} references unknown agent {node.agent_id!r}"
                )
        if "parent" not in self.agents:
            raise ValueError("agents must declare the parent role version")

        # dispatch edges only parent -> agent/tool ; returns only agent/tool -> parent
        for edge in self.edges:
            src = self.node(edge.source)
            tgt = self.node(edge.target)
            if src is None or tgt is None:
                raise ValueError(
                    f"edge {edge.source!r}->{edge.target!r} references an unknown node"
                )
            if src.type is WorkflowNodeType.PARENT and tgt.type in (
                WorkflowNodeType.AGENT,
                WorkflowNodeType.TOOL,
            ):
                continue
            if src.type in (WorkflowNodeType.AGENT, WorkflowNodeType.TOOL) and tgt.type is (
                WorkflowNodeType.PARENT
            ):
                continue
            if src.type is WorkflowNodeType.PARENT and tgt.type is WorkflowNodeType.END:
                continue
            raise ValueError(
                f"illegal connection {edge.source!r}->{edge.target!r}: "
                "only parent→agent/tool, agent/tool→parent and parent→end are allowed"
            )

        # every working node must both be dispatched by and report to the parent
        for node in self.nodes:
            if node.type in (WorkflowNodeType.PARENT, WorkflowNodeType.END):
                continue
            if parent_id not in self.edges_from(node.id):
                raise ValueError(f"node {node.id!r} is missing a return edge to the parent")
            if node.id not in self.edges_from(parent_id):
                raise ValueError(f"node {node.id!r} is not reachable from the parent")

        if self.edges_to(end_id) != [parent_id]:
            raise ValueError("only the parent may connect to the end node")

        # mode-specific template requirements
        if self.check_mode is CheckMode.PARALLEL:
            recheck = self.node("recheck")
            if recheck is None or recheck.agent_id != "reviewer":
                raise ValueError("parallel mode requires a 'recheck' node reusing the reviewer")
        else:
            if self.node("recheck") is not None:
                raise ValueError("sequential mode must not declare a recheck node")

        required_kinds = {"review", "fix", "verify"}
        present_kinds = {n.task_kind for n in self.nodes if n.task_kind}
        missing = required_kinds - present_kinds
        if missing:
            raise ValueError(f"workflow is missing required task kinds: {sorted(missing)}")

        # input dependencies must reference known nodes and never carry returns
        for dep in self.dependencies:
            if self.node(dep.source_node) is None or self.node(dep.target_node) is None:
                raise ValueError("input dependency references an unknown node")
            if dep.source_node == parent_id or dep.target_node == parent_id:
                raise ValueError(
                    "input dependencies connect working nodes, not the parent "
                    "(they are not execution edges)"
                )
        return self

    def bind(self, workflow_version: str) -> "WorkflowConfig":
        """Return a copy with the version and semantic hash fixed."""
        return self.model_copy(
            update={
                "workflow_version": workflow_version,
                "semantic_hash": self.compute_semantic_hash(),
            }
        )

    def with_standard_expansion(
        self, extra_dependencies: Iterable[tuple[str, str, str]] = ()
    ) -> "WorkflowConfig":
        """Fill standard input dependencies and fixed tool policy versions.

        The graph builder persists this expanded form so a saved version does not
        depend on later changes to the global defaults (§14.1.1). ``extra_dependencies``
        carries the input dependencies declared by registered flow extensions, so a
        template does not hand-write them.
        """
        pairs = [
            *STANDARD_DEPENDENCIES[self.check_mode],
            *extra_dependencies,
        ]
        deps: list[InputDependency] = []
        seen: set[tuple[str, str, str]] = set()
        for src, tgt, artifact in pairs:
            key = (src, tgt, artifact)
            if key in seen:
                continue
            if self.node(src) is None or self.node(tgt) is None:
                continue
            seen.add(key)
            deps.append(InputDependency(source_node=src, target_node=tgt, artifact=artifact))
        return self.model_copy(
            update={
                "dependencies": deps,
                "tool_policy_versions": dict(STANDARD_TOOL_POLICY_VERSIONS),
            }
        )


# Fixed template dependencies: review findings feed the fixer; an applied patch
# feeds verification (and, in parallel mode, the recheck). These are input data
# edges, never execution edges (§14.1.1).
STANDARD_DEPENDENCIES: dict[CheckMode, list[tuple[str, str, str]]] = {
    CheckMode.SEQUENTIAL: [
        ("review", "fix", "finding"),
        ("apply", "verify", "patch_application"),
    ],
    CheckMode.PARALLEL: [
        ("review", "fix", "finding"),
        ("apply", "verify", "patch_application"),
        ("apply", "recheck", "patch_application"),
    ],
}

# Tool policy is versioned and frozen into a saved config.
STANDARD_TOOL_POLICY_VERSIONS: dict[str, str] = {"apply_patch": "1.0"}
