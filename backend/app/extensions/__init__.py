"""Registered flow extensions (P10 §14.2)."""

from __future__ import annotations

from app.extensions.base import (
    BASE_TASK_KIND_FAULT_BUDGETS,
    ExtensionError,
    ExtensionRegistry,
    FlowExtension,
    InputAdapter,
    TaskKindPlugin,
    default_input_adapters,
    validate_workflow_config,
)
from app.extensions.test_generator import build_test_generator_extension


def build_default_extension_registry(*, with_samples: bool = True) -> ExtensionRegistry:
    """The extension registry; the shipped sample is registered by default.

    Registering the sample only makes it *discoverable* (``GET /api/agents``);
    the base three-role workflow is unchanged until a saved workflow config
    references the extension role.
    """
    registry = ExtensionRegistry()
    if with_samples:
        registry.register(build_test_generator_extension())
    return registry


__all__ = [
    "BASE_TASK_KIND_FAULT_BUDGETS",
    "ExtensionError",
    "ExtensionRegistry",
    "FlowExtension",
    "InputAdapter",
    "TaskKindPlugin",
    "build_default_extension_registry",
    "default_input_adapters",
    "validate_workflow_config",
]
