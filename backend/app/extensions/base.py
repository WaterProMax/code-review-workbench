"""Flow extension contract (§14.2).

An extension adds a role and one or more task kinds through the *normal*
mechanism, never through a special-case branch in the parent's dispatch path:

* a capability spec + a registered implementation (`AgentSpec`/`AgentProtocol`),
* explicit tool authorization granted to the new role,
* declared input adapters (which artifacts the new kind reads, and which of its
  artifacts other kinds may read),
* declared input dependencies for workflow templates,
* a declared fault-retry budget, so a new task kind can never fall into an
  implicit "unlimited retries" default (§5, §14.2).

Registering only a database row is not an integration: the checks in
``validate_workflow_config`` refuse a config that references an unregistered
role version or an undeclared task kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.agents.base import AgentProtocol
from app.schemas.agents import AgentSpec
from app.schemas.enums import ArtifactType, BudgetKind, WorkflowNodeType
from app.schemas.workflows import WorkflowConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.registry.agents import AgentRegistry


class ExtensionError(RuntimeError):
    """An extension registration or configuration is not usable."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# Where an input adapter reads its artifact from.
SCOPE_LATEST = "latest"  # newest artifact of that type for the root task
SCOPE_COMMITTED_APPLICATION = "committed_application"  # the version-bound applied patch


@dataclass(frozen=True)
class InputAdapter:
    """Fills one input-reference key of one task kind from a persisted artifact."""

    task_kind: str
    input_key: str
    artifact_type: ArtifactType
    scope: str = SCOPE_LATEST
    # for SCOPE_COMMITTED_APPLICATION: type of the artifact published at result_version
    versioned_artifact_type: ArtifactType | None = None


@dataclass(frozen=True)
class TaskKindPlugin:
    """Everything the control layer needs to run one extension task kind."""

    task_kind: str
    agent_id: str
    retry_budget_kind: str
    description: str = ""
    # an optional kind never blocks completion; a required one does
    required_for_completion: bool = False
    # a non-verdict kind reports progress but its success is never mistaken for a
    # phase verdict; its failure is still recorded as an execution fault
    verdict_bearing: bool = True
    # (source_node, target_node, artifact) input dependencies for templates
    dependencies: tuple[tuple[str, str, str], ...] = ()
    # input refs the new kind itself reads
    consumes: tuple[InputAdapter, ...] = ()
    # input refs *other* kinds gain from this kind's artifacts (e.g. verifier
    # accepting generated tests), so the consumer needs no new branch either
    feeds: tuple[InputAdapter, ...] = ()


@dataclass(frozen=True)
class FlowExtension:
    """A registered extension: role identity, capability spec and task kinds."""

    extension_id: str
    version: str
    spec: AgentSpec
    implementation: AgentProtocol
    plugins: tuple[TaskKindPlugin, ...] = ()
    files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def agent_id(self) -> str:
        return self.spec.agent_id


# task_kind -> fault-retry budget for the three base roles (§9.4)
BASE_TASK_KIND_FAULT_BUDGETS: dict[str, str] = {
    "review": BudgetKind.REVIEW_RETRY.value,
    "fix": BudgetKind.FIX_RETRY.value,
    "verify": BudgetKind.VERIFICATION_RETRY.value,
}


class ExtensionRegistry:
    """The registered extensions; the single source for task-kind budgets."""

    def __init__(self) -> None:
        self._extensions: dict[str, FlowExtension] = {}
        self._plugins: dict[str, TaskKindPlugin] = {}
        self._fault_budgets: dict[str, str] = dict(BASE_TASK_KIND_FAULT_BUDGETS)

    # ---- registration ------------------------------------------------------
    def register(self, extension: FlowExtension) -> None:
        if extension.extension_id in self._extensions:
            raise ExtensionError(
                "EXTENSION_DUPLICATE", f"扩展 {extension.extension_id} 已注册"
            )
        spec = extension.spec
        if spec.agent_id != extension.agent_id:
            raise ExtensionError("EXTENSION_INVALID", "扩展的 spec 与标识不一致")
        for plugin in extension.plugins:
            if plugin.agent_id != extension.agent_id:
                raise ExtensionError(
                    "EXTENSION_INVALID",
                    f"任务类型 {plugin.task_kind} 声明了其他角色 {plugin.agent_id}",
                )
            if plugin.task_kind in self._fault_budgets:
                raise ExtensionError(
                    "EXTENSION_TASK_KIND_CONFLICT",
                    f"任务类型 {plugin.task_kind} 已注册，不能重复占用",
                )
            if not plugin.retry_budget_kind:
                raise ExtensionError(
                    "EXTENSION_NO_RETRY_BUDGET",
                    f"任务类型 {plugin.task_kind} 必须声明重试预算，不能落入无限重试默认分支",
                )
            if plugin.task_kind not in spec.supported_task_kinds:
                raise ExtensionError(
                    "EXTENSION_INVALID",
                    f"spec 未声明支持任务类型 {plugin.task_kind}",
                )
            if spec.retry_budget.get(plugin.task_kind) != plugin.retry_budget_kind:
                raise ExtensionError(
                    "EXTENSION_INVALID",
                    f"任务类型 {plugin.task_kind} 的 spec.retry_budget 与插件声明不一致",
                )
        self._extensions[extension.extension_id] = extension
        for plugin in extension.plugins:
            self._plugins[plugin.task_kind] = plugin
            self._fault_budgets[plugin.task_kind] = plugin.retry_budget_kind

    # ---- lookup ------------------------------------------------------------
    def extensions(self) -> list[FlowExtension]:
        return [self._extensions[key] for key in sorted(self._extensions)]

    def registrations(self) -> list[tuple[AgentSpec, AgentProtocol]]:
        return [(ext.spec, ext.implementation) for ext in self.extensions()]

    def plugins(self) -> list[TaskKindPlugin]:
        return [self._plugins[key] for key in sorted(self._plugins)]

    def plugin(self, task_kind: str) -> TaskKindPlugin | None:
        return self._plugins.get(task_kind)

    def task_kinds(self) -> frozenset[str]:
        return frozenset(self._fault_budgets)

    def fault_budget(self, task_kind: str) -> str:
        kind = self._fault_budgets.get(task_kind)
        if kind is None:
            raise ExtensionError(
                "UNKNOWN_TASK_KIND",
                f"任务类型 {task_kind!r} 未声明重试预算，拒绝以无限额度派发",
                {"allowed": sorted(self._fault_budgets)},
            )
        return kind

    def required_kinds(self) -> frozenset[str]:
        return frozenset(
            plugin.task_kind for plugin in self._plugins.values() if plugin.required_for_completion
        )

    # ---- controller wiring -------------------------------------------------
    def input_adapters(self) -> list[InputAdapter]:
        adapters: list[InputAdapter] = []
        for plugin in self.plugins():
            adapters.extend(plugin.consumes)
            adapters.extend(plugin.feeds)
        return adapters

    def template_dependencies(self, workflow: WorkflowConfig) -> list[tuple[str, str, str]]:
        """Plugin-provided input dependencies that apply to this template."""
        out: list[tuple[str, str, str]] = []
        for plugin in self.plugins():
            for source, target, artifact in plugin.dependencies:
                if workflow.node(source) is not None and workflow.node(target) is not None:
                    out.append((source, target, artifact))
        return out


def default_input_adapters() -> list[InputAdapter]:
    """The three base roles' input references, expressed as adapters (§14.1.1)."""
    return [
        InputAdapter("fix", "findings", ArtifactType.FINDING),
        InputAdapter("fix", "previous_patch", ArtifactType.PATCH),
        InputAdapter("fix", "verification", ArtifactType.VERIFICATION_REPORT),
        InputAdapter("verify", "findings", ArtifactType.FINDING),
        InputAdapter(
            "verify",
            "patch_application",
            ArtifactType.PATCH_APPLICATION,
            scope=SCOPE_COMMITTED_APPLICATION,
        ),
    ]


def validate_workflow_config(
    config: WorkflowConfig,
    registry: "AgentRegistry",
    extensions: ExtensionRegistry,
) -> None:
    """Reject a config that references roles or task kinds that are not runnable.

    Every problem names the node or the field, so the canvas can point at it.
    """
    for agent_id, version in config.agents.items():
        if not registry.has(agent_id, version):
            raise ExtensionError(
                "UNKNOWN_AGENT_VERSION",
                f"配置引用了未注册的角色 {agent_id}@{version}",
                {"field": f"agents.{agent_id}", "agent_id": agent_id, "version": version},
            )

    known = extensions.task_kinds()
    for node in config.nodes:
        if node.task_kind and node.task_kind not in known:
            raise ExtensionError(
                "UNKNOWN_TASK_KIND",
                f"节点 {node.id} 的任务类型 {node.task_kind!r} 未注册",
                {"field": f"nodes.{node.id}.task_kind", "node_id": node.id},
            )
        if node.type is WorkflowNodeType.AGENT and node.agent_id and node.task_kind:
            version = config.agents.get(node.agent_id)
            if version is None or not registry.has(node.agent_id, version):
                raise ExtensionError(
                    "UNKNOWN_AGENT_VERSION",
                    f"节点 {node.id} 引用了未注册的角色 {node.agent_id}",
                    {"field": f"nodes.{node.id}.agent_id", "node_id": node.id},
                )
            spec = registry.get_spec(node.agent_id, version)
            if node.task_kind not in spec.supported_task_kinds:
                raise ExtensionError(
                    "ROLE_TASK_KIND_MISMATCH",
                    f"角色 {node.agent_id} 不支持任务类型 {node.task_kind}",
                    {"field": f"nodes.{node.id}.task_kind", "node_id": node.id},
                )

    required = extensions.required_kinds()
    present = {node.task_kind for node in config.nodes if node.task_kind}
    missing = sorted(required - present)
    if missing:
        raise ExtensionError(
            "EXTENSION_INCOMPLETE",
            f"工作流缺少已注册为必需的任务类型 {missing}",
            {"field": "nodes", "missing": missing},
        )
