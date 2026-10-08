"""Fixtures that assemble a runnable attempt for agent-level tests."""

from __future__ import annotations

from pathlib import Path

from app.agents.base import AgentContext
from app.providers.base import LLMClient
from app.registry.tools import ToolRegistry
from app.schemas.agents import AgentSpec
from app.schemas.common import TaskConstraints
from app.schemas.enums import ApplicablePhase, ArtifactType, CheckMethod, TaskLevel
from app.schemas.results import AcceptanceContract, CheckSpec
from app.schemas.tasks import InputRefs, Task, TaskEnvelope
from app.services.artifacts import ArtifactService
from app.services.events import EventContext, EventSink
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos

DEFAULT_SOURCE = {
    "helpers.py": (
        "def add_item(item, items=[]):\n"
        "    items.append(item)\n"
        "    return items\n"
        "\n"
        "\n"
        "def average(values):\n"
        "    return sum(values) / len(values)\n"
    )
}


def default_checks(include_model_review: bool = True) -> list[CheckSpec]:
    checks = [
        CheckSpec(
            check_id="CHK-SYNTAX",
            goal_ref="项目可解析",
            scope=["helpers.py"],
            required=True,
            method=CheckMethod.SYNTAX,
            pass_condition="所有 Python 文件可解析",
            applicable_phase=ApplicablePhase.BOTH,
        ),
        CheckSpec(
            check_id="CHK-MUTABLE",
            goal_ref="避免共享可变默认参数",
            scope=["helpers.py"],
            required=True,
            method=CheckMethod.STATIC_RULE,
            pass_condition="不存在共享可变默认参数",
            applicable_phase=ApplicablePhase.BOTH,
            rule_ids=["B006-mutable-default"],
        ),
        CheckSpec(
            check_id="CHK-AVG-BEHAVIOR",
            goal_ref="average 对空列表的行为",
            scope=["helpers.py"],
            required=True,
            method=CheckMethod.BEHAVIOR_TEST,
            pass_condition="空列表不抛 ZeroDivisionError",
            applicable_phase=ApplicablePhase.BOTH,
        ),
    ]
    if include_model_review:
        checks.append(
            CheckSpec(
                check_id="CHK-REVIEW",
                goal_ref="异常处理是否合理",
                scope=["helpers.py"],
                required=True,
                method=CheckMethod.MODEL_REVIEW,
                pass_condition="异常处理符合目标要求",
                applicable_phase=ApplicablePhase.INITIAL_REVIEW,
            )
        )
    return checks


def save_contract(
    artifacts: ArtifactService,
    repos: Repos,
    *,
    root_task_id: str,
    goal: str = "检查可变默认参数与异常处理并修复验证",
    checks: list[CheckSpec] | None = None,
    contract_version: str = "contract-1",
) -> tuple[AcceptanceContract, str]:
    contract = AcceptanceContract(
        contract_version=contract_version,
        root_task_id=root_task_id,
        user_goal=goal,
        checks=checks or default_checks(),
    )
    artifact = artifacts.save_json(
        root_task_id=root_task_id,
        artifact_type=ArtifactType.ACCEPTANCE_CONTRACT,
        data=contract.model_dump(mode="json"),
        name=f"contract-{contract_version}",
    )
    repos.contracts.insert(
        contract_version=contract.contract_version,
        root_task_id=root_task_id,
        content_hash=contract.content_hash(),
        artifact_id=artifact.artifact_id,
        supersedes=None,
        append_reason=None,
    )
    return contract, artifact.artifact_id


def build_task_tree(
    repos: Repos,
    *,
    root_task_id: str = "T100",
    workflow_version: str = "wf-1",
    source_version: str,
) -> tuple[Task, Task, str]:
    """Create root + the three child tasks; returns (root, fix_task, batch_id)."""
    root = Task(
        task_id=root_task_id,
        root_task_id=root_task_id,
        parent_task_id=None,
        task_level=TaskLevel.ROOT,
        goal="检查上传项目并按需修复",
        status="running",
        workflow_version=workflow_version,
    )
    repos.tasks.insert(root)
    children: dict[str, Task] = {}
    for task_id, kind, agent in (
        ("T101", "review", "reviewer"),
        ("T102", "fix", "fixer"),
        ("T103", "verify", "verifier"),
    ):
        child = Task(
            task_id=task_id,
            root_task_id=root_task_id,
            parent_task_id=root_task_id,
            task_level=TaskLevel.CHILD,
            agent_id=agent,
            agent_version="1.0",
            task_kind=kind,
            goal=f"{kind} 子任务",
            status="blocked",
            workflow_version=workflow_version,
        )
        repos.tasks.insert(child)
        children[kind] = child
    return root, children["fix"], "B1"


def publish_source(
    workspace: WorkspaceService,
    repos: Repos,
    artifacts: ArtifactService,
    *,
    root_task_id: str,
    files: dict[str, str] | None = None,
) -> tuple[str, str]:
    upload = workspace.create_upload(
        [(path, body.encode()) for path, body in (files or DEFAULT_SOURCE).items()]
    )
    manifest = workspace.publish_initial_snapshot(
        root_task_id=root_task_id, source_id=upload.source_id
    )
    source_artifact = workspace.register_source_artifact(
        root_task_id=root_task_id, source_version=manifest.source_version
    )
    return manifest.source_version, source_artifact


def make_envelope(
    *,
    root_task_id: str,
    task: Task,
    attempt_id: str,
    batch_id: str,
    source_version: str,
    contract_version: str,
    contract_artifact_id: str,
    source_artifact_id: str,
    goal: str | None = None,
    input_refs: dict[str, str] | None = None,
) -> TaskEnvelope:
    refs = {
        "source": source_artifact_id,
        "acceptance_contract": contract_artifact_id,
        **(input_refs or {}),
    }
    return TaskEnvelope(
        root_task_id=root_task_id,
        parent_task_id=root_task_id,
        task_id=task.task_id,
        attempt_id=attempt_id,
        dispatch_batch_id=batch_id,
        agent_id=task.agent_id or "reviewer",
        agent_version=task.agent_version or "1.0",
        task_kind=task.task_kind or "review",
        goal=goal or task.goal,
        input_refs=InputRefs(**refs),
        source_version=source_version,
        contract_version=contract_version,
        acceptance_criteria=["覆盖全部必需检查项"],
        constraints=TaskConstraints(max_tool_steps=8, timeout_seconds=60),
    )


def make_context(
    *,
    llm: LLMClient,
    tools: ToolRegistry,
    workspace: WorkspaceService,
    artifacts: ArtifactService,
    repos: Repos,
    sink: EventSink,
    spec: AgentSpec,
    max_tool_steps: int = 8,
    deadline_seconds: float = 60.0,
    scratch_dir: Path | None = None,
) -> AgentContext:
    return AgentContext(
        llm=llm,
        tools=tools,
        workspace=workspace,
        artifacts=artifacts,
        repos=repos,
        sink=sink,
        agent_spec=spec,
        deadline_seconds=deadline_seconds,
        tool_timeout_seconds=20.0,
        max_tool_steps=max_tool_steps,
        scratch_dir=scratch_dir,
        read_only=True,
    )


def root_sink(repos: Repos, root_task_id: str, actor: str = "parent") -> EventSink:
    return EventSink(repos.events, EventContext(root_task_id=root_task_id, actor_id=actor))


def register_attempt(
    repos: Repos,
    *,
    root_task_id: str,
    task: Task,
    attempt_id: str,
    batch_id: str,
    source_version: str,
    contract_version: str,
    input_refs: dict[str, str],
):
    from app.schemas.enums import AttemptStatus
    from app.schemas.tasks import Attempt

    repos.batches.create(batch_id, root_task_id, [attempt_id])
    attempt = Attempt(
        attempt_id=attempt_id,
        task_id=task.task_id,
        root_task_id=root_task_id,
        dispatch_batch_id=batch_id,
        attempt_no=1,
        source_version=source_version,
        contract_version=contract_version,
        input_refs=input_refs,
        constraints=TaskConstraints(max_tool_steps=8, timeout_seconds=60),
        status=AttemptStatus.QUEUED,
    )
    repos.attempts.insert(attempt)
    return attempt


def settings_for(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", model_api_key=None)
    settings.ensure_dirs()
    return settings
