"""Loading the shipped YAML configuration files (§14.1.1).

The files under ``config/`` are the declarative form of the templates the canvas
starts from and of the role versions a workflow config may reference. Keeping the
loader in one place means the API, the tests and the graph builder read the same
files.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from app.schemas.agents import AgentConfigFile
from app.schemas.workflows import WorkflowConfig

TEMPLATE_FILES: tuple[tuple[str, str], ...] = (
    ("workflow.sequential.yaml", "顺序模板：审查 → 修复 → 应用 → 验证"),
    ("workflow.parallel.yaml", "修复后并行模板：同一批次派发复审与验证"),
    ("workflow.extension.yaml", "扩展模板：在顺序模板上增加测试生成角色"),
)


class ConfigFileError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigFileError("CONFIG_FILE_MISSING", f"缺少配置文件 {path}")
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)
    if not isinstance(loaded, dict):
        raise ConfigFileError("CONFIG_FILE_INVALID", f"配置文件 {path.name} 不是映射结构")
    return loaded


def load_workflow_config(config_dir: Path, filename: str) -> WorkflowConfig:
    return WorkflowConfig.model_validate(load_yaml(config_dir / filename))


def load_workflow_templates(config_dir: Path) -> list[tuple[str, str, WorkflowConfig]]:
    """``(name, description, config)`` for every shipped template that exists."""
    out: list[tuple[str, str, WorkflowConfig]] = []
    for filename, description in TEMPLATE_FILES:
        if not (config_dir / filename).exists():
            continue
        out.append((filename, description, load_workflow_config(config_dir, filename)))
    return out


def load_agent_config(config_dir: Path) -> AgentConfigFile:
    return AgentConfigFile.model_validate(load_yaml(config_dir / "agents.yaml"))
