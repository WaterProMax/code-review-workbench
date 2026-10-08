"""The P10 extension sample: a test-generation role (§14.2).

Registered through the normal mechanism only — a capability spec, an
implementation, explicit tool authorization (``submit_generated_tests`` plus the
shared read tools), declared input adapters, a template dependency and a declared
retry budget. No ``dispatch_test_generator`` action and no change to the parent's
generic dispatch path.
"""

from __future__ import annotations

from app.agents.test_generator import TestGeneratorAgent
from app.extensions.base import (
    FlowExtension,
    InputAdapter,
    TaskKindPlugin,
)
from app.registry.agents import load_prompt
from app.schemas.agents import AgentSpec
from app.schemas.enums import ArtifactType, BudgetKind

TASK_KIND = "generate_tests"
AGENT_ID = "test_generator"
VERSION = "1.0"

# Files this extension adds or touches; listed so the integration cost is visible
# (docs/AgentExtension.md mirrors this list).
EXTENSION_FILES = (
    "backend/app/agents/test_generator.py",
    "backend/app/agents/prompts/test_generator.txt",
    "backend/app/extensions/base.py",
    "backend/app/extensions/test_generator.py",
    "backend/app/services/tools/artifacts_tools.py",
    "backend/app/services/tools/__init__.py",
    "config/agents.yaml",
    "config/workflow.extension.yaml",
)


def build_test_generator_extension() -> FlowExtension:
    prompt, _ = load_prompt("test_generator")
    spec = AgentSpec(
        agent_id=AGENT_ID,
        version=VERSION,
        description="测试生成 Agent：为可复现的行为缺陷生成最小测试包，供验证 Agent 作为输入执行",
        supported_task_kinds=[TASK_KIND],
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        tool_names=[
            "list_files",
            "read_file",
            "search_code",
            "read_artifact",
            "submit_generated_tests",
        ],
        permissions=["read_workspace", "write_scratch"],
        produced_artifact_types=[ArtifactType.TEST_ARTIFACT],
        required_input_keys=["source", "acceptance_contract", "findings"],
        # a new task kind must declare its budget instead of defaulting to unlimited
        retry_budget={TASK_KIND: BudgetKind.GENERATE_RETRY.value},
        detection_plugin="verdict_exempt",
        dependency_plugin="test_generator",
    )
    plugin = TaskKindPlugin(
        task_kind=TASK_KIND,
        agent_id=AGENT_ID,
        retry_budget_kind=BudgetKind.GENERATE_RETRY.value,
        description="为检查项生成最小复现测试包，作为验证输入",
        required_for_completion=False,
        verdict_bearing=False,
        # template input dependency: the reviewer's findings feed the generator,
        # and the generated tests feed verification (a data edge, not a dispatch edge)
        dependencies=(
            ("review", "gen", "finding"),
            ("gen", "verify", "test_artifact"),
        ),
        consumes=(InputAdapter(TASK_KIND, "findings", ArtifactType.FINDING),),
        # the verifier gains an input from this kind without any new branch there
        feeds=(
            InputAdapter("verify", "generated_tests", ArtifactType.TEST_ARTIFACT),
        ),
    )
    return FlowExtension(
        extension_id=AGENT_ID,
        version=VERSION,
        spec=spec,
        implementation=TestGeneratorAgent(spec, prompt),
        plugins=(plugin,),
        files=EXTENSION_FILES,
    )
