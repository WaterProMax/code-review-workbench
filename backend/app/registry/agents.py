"""Agent registry: capability discovery and role resolution (§7.1, §7.4).

Registrations are explicit — a role is only resolvable if its implementation was
registered under a fixed version. Nothing imports a module based on a model's
output, and duplicate or conflicting registrations are rejected.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from app.agents.base import AgentProtocol
from app.agents.fixer import FixerAgent
from app.agents.reviewer import ReviewerAgent
from app.agents.verifier import VerifierAgent
from app.schemas.agents import AgentConfigFile, AgentSpec, CapabilityListing

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.extensions.base import ExtensionRegistry

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "agents" / "prompts"

PARENT_VERSION = "1.0"

PARENT_SPEC = AgentSpec(
    agent_id="parent",
    version=PARENT_VERSION,
    description="父 Agent：理解目标、建立任务树、派发、接收回报、检测与决策",
    supported_task_kinds=["orchestrate"],
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    tool_names=["create_task", "dispatch_task", "apply_patch"],
    permissions=["orchestrate"],
    required_input_keys=["source", "acceptance_contract"],
)


class AgentRegistrationError(RuntimeError):
    pass


class AgentRegistry:
    def __init__(self) -> None:
        self._specs: dict[tuple[str, str], AgentSpec] = {}
        self._impls: dict[tuple[str, str], AgentProtocol] = {}

    # ---- registration ------------------------------------------------------
    def register(self, spec: AgentSpec, implementation: AgentProtocol | None = None) -> None:
        key = spec.key()
        existing = self._specs.get(key)
        if existing is not None:
            raise AgentRegistrationError(
                f"角色 {key[0]}@{key[1]} 已注册；相同版本不能重复或冲突注册"
            )
        self._specs[key] = spec
        if implementation is not None:
            self._impls[key] = implementation

    def register_spec_only(self, spec: AgentSpec) -> None:
        self.register(spec, None)

    # ---- lookup ------------------------------------------------------------
    def has(self, agent_id: str, version: str) -> bool:
        return (agent_id, version) in self._specs

    def get_spec(self, agent_id: str, version: str) -> AgentSpec:
        spec = self._specs.get((agent_id, version))
        if spec is None:
            raise AgentRegistrationError(f"未知角色或版本：{agent_id}@{version}")
        return spec

    def resolve(self, agent_id: str, version: str) -> AgentProtocol:
        impl = self._impls.get((agent_id, version))
        if impl is None:
            raise AgentRegistrationError(
                f"角色 {agent_id}@{version} 没有可用的执行实现（可能只是能力声明）"
            )
        return impl

    def list_capabilities(self) -> list[CapabilityListing]:
        return [
            CapabilityListing.from_spec(spec)
            for (_, _), spec in sorted(self._specs.items())
            if (spec.agent_id, spec.version) in self._impls
        ]

    def versions(self) -> dict[str, str]:
        """agent_id -> version, including the parent role for config validation."""
        out = {"parent": PARENT_VERSION}
        for (agent_id, version), spec in self._specs.items():
            out[agent_id] = version
        return out

    def resolve_role_specs(self, agent_ids: list[str]) -> dict[str, AgentSpec]:
        out: dict[str, AgentSpec] = {}
        for agent_id in agent_ids:
            candidates = [spec for (aid, _), spec in self._specs.items() if aid == agent_id]
            if len(candidates) != 1:
                raise AgentRegistrationError(
                    f"角色 {agent_id} 的可用版本不唯一：{[c.version for c in candidates]}"
                )
            out[agent_id] = candidates[0]
        return out


def load_prompt(name: str) -> tuple[str, str]:
    """Return ``(prompt_body, prompt_version)`` for a role prompt file."""
    path = PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise AgentRegistrationError(f"缺少提示词文件 {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    version = "unknown"
    body: list[str] = []
    for line in lines:
        if line.startswith("prompt_version:"):
            version = line.split(":", 1)[1].strip()
        elif line.startswith("role:"):
            continue
        else:
            body.append(line)
    return "\n".join(body).strip(), version


def _reviewer_spec() -> AgentSpec:
    return AgentSpec(
        agent_id="reviewer",
        version="1.0",
        description="审查 Agent：按冻结检查项读取快照并生成 Finding 与逐项检查结论",
        supported_task_kinds=["review"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tool_names=[
            "list_files",
            "read_file",
            "search_code",
            "ast_check",
            "read_artifact",
            "run_checks",
            "submit_findings",
            "submit_generated_tests",
        ],
        permissions=["read_workspace", "write_scratch"],
        produced_artifact_types=["finding", "check_result", "evidence", "test_artifact"],
        required_input_keys=["source", "acceptance_contract"],
        retry_budget={"review": "review_retry"},
    )


def _fixer_spec() -> AgentSpec:
    return AgentSpec(
        agent_id="fixer",
        version="1.0",
        description="修复 Agent：依据问题与失败证据生成针对当前版本的候选补丁",
        supported_task_kinds=["fix"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tool_names=["list_files", "read_file", "search_code", "read_artifact", "submit_patch"],
        permissions=["read_workspace", "produce_patch"],
        produced_artifact_types=["patch"],
        required_input_keys=["source", "acceptance_contract", "findings"],
        retry_budget={"fix": "fix_retry"},
    )


def _verifier_spec() -> AgentSpec:
    return AgentSpec(
        agent_id="verifier",
        version="1.0",
        description="验证 Agent：执行约定检查与最小复现测试，返回实际证据",
        supported_task_kinds=["verify"],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tool_names=[
            "list_files",
            "read_file",
            "search_code",
            "ast_check",
            "read_artifact",
            "run_checks",
            "save_evidence",
            "submit_verification",
        ],
        permissions=["read_workspace", "write_scratch"],
        produced_artifact_types=["verification_report", "check_result", "test_artifact", "evidence"],
        required_input_keys=["source", "acceptance_contract"],
        retry_budget={"verify": "verification_retry"},
    )


def build_default_agent_registry(
    extra: list[tuple[AgentSpec, AgentProtocol]] | None = None,
    extensions: "ExtensionRegistry | None" = None,
) -> AgentRegistry:
    registry = AgentRegistry()
    registry.register_spec_only(PARENT_SPEC)

    reviewer_prompt, _ = load_prompt("reviewer")
    reviewer_spec = _reviewer_spec()
    registry.register(reviewer_spec, ReviewerAgent(reviewer_spec, reviewer_prompt))

    fixer_prompt, _ = load_prompt("fixer")
    fixer_spec = _fixer_spec()
    registry.register(fixer_spec, FixerAgent(fixer_spec, fixer_prompt))

    verifier_prompt, _ = load_prompt("verifier")
    verifier_spec = _verifier_spec()
    registry.register(verifier_spec, VerifierAgent(verifier_spec, verifier_prompt))

    # P10: extension roles go through the same registration mechanism
    for spec, implementation in (extensions.registrations() if extensions else []):
        registry.register(spec, implementation)

    for spec, implementation in extra or []:
        registry.register(spec, implementation)
    return registry


def assert_config_versions(config: AgentConfigFile, registry: AgentRegistry) -> None:
    """Every version a workflow config may reference must be registered."""
    available = registry.versions()
    for agent_id, entry in config.agents.items():
        if agent_id not in available:
            raise AgentRegistrationError(f"配置引用了未注册的角色 {agent_id}")
        if available[agent_id] != entry.version:
            raise AgentRegistrationError(
                f"配置中的 {agent_id} 版本 {entry.version} 与已注册版本 {available[agent_id]} 不一致"
            )
