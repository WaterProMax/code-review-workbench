"""P01: the shipped configuration files parse into their contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.schemas.agents import AgentConfigFile
from app.schemas.enums import CheckMode
from app.schemas.workflows import WorkflowConfig

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

WORKFLOW_FILES = [
    "workflow.sequential.yaml",
    "workflow.parallel.yaml",
    "workflow.extension.yaml",
]


def _load(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def test_agents_config_is_valid() -> None:
    cfg = AgentConfigFile.model_validate(_load("agents.yaml"))
    assert {"parent", "reviewer", "fixer", "verifier"} <= set(cfg.agents)
    assert cfg.agents["reviewer"].version == "1.0"


@pytest.mark.parametrize("filename", WORKFLOW_FILES)
def test_workflow_templates_are_valid(filename: str) -> None:
    cfg = WorkflowConfig.model_validate(_load(filename))
    assert cfg.check_mode in {CheckMode.SEQUENTIAL, CheckMode.PARALLEL}
    assert cfg.node("parent") is not None
    assert cfg.node("end") is not None
    # the semantic hash is stable for the same content
    assert cfg.compute_semantic_hash() == cfg.compute_semantic_hash()


def test_layout_does_not_change_the_semantic_hash() -> None:
    cfg = WorkflowConfig.model_validate(_load("workflow.sequential.yaml"))
    moved = cfg.model_copy(
        update={"layout": []}  # only node coordinates differ
    )
    assert moved.compute_semantic_hash() == cfg.compute_semantic_hash()


def test_parallel_template_dispatches_recheck_with_verify() -> None:
    cfg = WorkflowConfig.model_validate(_load("workflow.parallel.yaml"))
    assert cfg.node("recheck") is not None
    assert cfg.node("recheck").agent_id == "reviewer"
    expanded = cfg.with_standard_expansion()
    artifacts = {(d.source_node, d.target_node, d.artifact) for d in expanded.dependencies}
    assert ("apply", "verify", "patch_application") in artifacts
    assert ("apply", "recheck", "patch_application") in artifacts
    assert expanded.tool_policy_versions == {"apply_patch": "1.0"}


def test_sequential_template_expands_without_recheck_dependency() -> None:
    cfg = WorkflowConfig.model_validate(_load("workflow.sequential.yaml"))
    expanded = cfg.with_standard_expansion()
    targets = {d.target_node for d in expanded.dependencies}
    assert targets == {"fix", "verify"}


def test_extension_template_is_registered_and_expands_from_the_plugin() -> None:
    """The extension node's dependencies come from the plugin, not the YAML."""
    from app.extensions import build_default_extension_registry, validate_workflow_config
    from app.registry.agents import build_default_agent_registry

    extensions = build_default_extension_registry()
    cfg = WorkflowConfig.model_validate(_load("workflow.extension.yaml"))
    # the template file itself carries no input dependencies
    assert cfg.dependencies == []

    expanded = cfg.with_standard_expansion(
        extra_dependencies=extensions.template_dependencies(cfg)
    )
    deps = {(d.source_node, d.target_node, d.artifact) for d in expanded.dependencies}
    assert ("review", "gen", "finding") in deps
    assert ("gen", "verify", "test_artifact") in deps
    # only a registered task kind may appear in a saved config
    registry = build_default_agent_registry(extensions=extensions)
    validate_workflow_config(expanded, registry, extensions)


def test_agents_config_declares_the_extension_role() -> None:
    cfg = AgentConfigFile.model_validate(_load("agents.yaml"))
    assert cfg.agents["test_generator"].version == "1.0"
    # the shipped sample declares its own retry budget field
    extension = WorkflowConfig.model_validate(_load("workflow.extension.yaml"))
    assert extension.budgets.max_generate_retries == 2

