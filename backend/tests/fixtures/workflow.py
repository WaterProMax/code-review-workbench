"""Fixtures that assemble a runnable parent/child workflow (P04+)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.agents.parent import ParentAgent
from app.providers.base import LLMRequest
from app.providers.scripted import ScriptedLLMClient
from app.registry.agents import build_default_agent_registry
from app.registry.tools import ToolRegistry
from app.schemas.enums import CheckMode, WorkflowNodeType
from app.schemas.workflows import (
    BudgetConfig,
    WorkflowConfig,
    WorkflowEdge,
    WorkflowNode,
)
from app.services.artifacts import ArtifactService
from app.services.execution_locks import ExecutionLockService
from app.services.task_service import TaskService
from app.services.tools import build_default_registry
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from app.workflow.assembly import assemble_workflow, compile_graph
from app.workflow.builder import build_graph
from app.workflow.controller import ParentController
from app.workflow.detection import Detector
from app.workflow.nodes import WorkflowRuntime
from app.workflow.recovery import RecoveryCoordinator
from app.workflow.state import initial_state

CLEAN_SOURCE = {
    "helpers.py": (
        "def add_item(item, items=None):\n"
        "    if items is None:\n"
        "        items = []\n"
        "    items.append(item)\n"
        "    return items\n"
        "\n"
        "\n"
        "def average(values):\n"
        "    return sum(values) / len(values)\n"
    )
}

BUGGY_SOURCE = {
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

FIX_DIFF = (
    "--- a/helpers.py\n"
    "+++ b/helpers.py\n"
    "@@ -1,3 +1,5 @@\n"
    "-def add_item(item, items=[]):\n"
    "+def add_item(item, items=None):\n"
    "+    if items is None:\n"
    "+        items = []\n"
    "     items.append(item)\n"
    "     return items\n"
)

# A "repair" that changes the file but leaves the defect in place: it proves that
# a later round and the verification evidence — not the fixer's claim — decide.
BOGUS_DIFF = (
    "--- a/helpers.py\n"
    "+++ b/helpers.py\n"
    "@@ -1,1 +1,2 @@\n"
    "+# repair round applied, defect still present\n"
    " def add_item(item, items=[]):\n"
)

# The finding a reviewer would record for BUGGY_SOURCE.
MUTABLE_DEFAULT_FINDING = {
    "file_path": "helpers.py",
    "rule": "B006-mutable-default",
    "message": "共享可变默认参数 items=[]",
    "check_id": "CHK-MUTABLE",
    "goal_ref": "避免共享可变默认参数",
    "required_for_goal": True,
    "line": 1,
    "severity": "error",
}


def sequential_workflow(
    *,
    workflow_version: str = "wf-1",
    max_repair_rounds: int = 2,
    attempt_timeout_seconds: float = 180.0,
) -> WorkflowConfig:
    nodes = [
        WorkflowNode(id="parent", type=WorkflowNodeType.PARENT, agent_id="parent"),
        WorkflowNode(
            id="review", type=WorkflowNodeType.AGENT, agent_id="reviewer", task_kind="review"
        ),
        WorkflowNode(
            id="fix", type=WorkflowNodeType.AGENT, agent_id="fixer", task_kind="fix"
        ),
        WorkflowNode(id="apply", type=WorkflowNodeType.TOOL, tool_name="apply_patch"),
        WorkflowNode(
            id="verify", type=WorkflowNodeType.AGENT, agent_id="verifier", task_kind="verify"
        ),
        WorkflowNode(id="end", type=WorkflowNodeType.END),
    ]
    edges = [
        WorkflowEdge(source="parent", target="review"),
        WorkflowEdge(source="review", target="parent"),
        WorkflowEdge(source="parent", target="fix"),
        WorkflowEdge(source="fix", target="parent"),
        WorkflowEdge(source="parent", target="apply"),
        WorkflowEdge(source="apply", target="parent"),
        WorkflowEdge(source="parent", target="verify"),
        WorkflowEdge(source="verify", target="parent"),
        WorkflowEdge(source="parent", target="end"),
    ]
    config = WorkflowConfig(
        check_mode=CheckMode.SEQUENTIAL,
        agents={"parent": "1.0", "reviewer": "1.0", "fixer": "1.0", "verifier": "1.0"},
        nodes=nodes,
        edges=edges,
        budgets=BudgetConfig(
            max_repair_rounds=max_repair_rounds,
            attempt_timeout_seconds=attempt_timeout_seconds,
        ),
    )
    return config.with_standard_expansion().bind(workflow_version)


def parallel_workflow(
    *,
    workflow_version: str = "wf-p1",
    max_repair_rounds: int = 2,
    attempt_timeout_seconds: float = 180.0,
) -> WorkflowConfig:
    """Sequential path plus the post-patch parallel recheck + verify batch."""
    nodes = [
        WorkflowNode(id="parent", type=WorkflowNodeType.PARENT, agent_id="parent"),
        WorkflowNode(
            id="review", type=WorkflowNodeType.AGENT, agent_id="reviewer", task_kind="review"
        ),
        WorkflowNode(id="fix", type=WorkflowNodeType.AGENT, agent_id="fixer", task_kind="fix"),
        WorkflowNode(id="apply", type=WorkflowNodeType.TOOL, tool_name="apply_patch"),
        WorkflowNode(
            id="recheck", type=WorkflowNodeType.AGENT, agent_id="reviewer", task_kind="review"
        ),
        WorkflowNode(
            id="verify", type=WorkflowNodeType.AGENT, agent_id="verifier", task_kind="verify"
        ),
        WorkflowNode(id="end", type=WorkflowNodeType.END),
    ]
    edges = [
        WorkflowEdge(source="parent", target=node_id)
        for node_id in ("review", "fix", "apply", "recheck", "verify")
    ] + [
        WorkflowEdge(source=node_id, target="parent")
        for node_id in ("review", "fix", "apply", "recheck", "verify")
    ] + [WorkflowEdge(source="parent", target="end")]
    config = WorkflowConfig(
        check_mode=CheckMode.PARALLEL,
        agents={"parent": "1.0", "reviewer": "1.0", "fixer": "1.0", "verifier": "1.0"},
        nodes=nodes,
        edges=edges,
        budgets=BudgetConfig(
            max_repair_rounds=max_repair_rounds,
            attempt_timeout_seconds=attempt_timeout_seconds,
        ),
    )
    return config.with_standard_expansion().bind(workflow_version)


# The test the extension role generates for the mutable-default defect: it fails
# on BUGGY_SOURCE (the second call sees the first call's list) and passes on the
# fixed version, so it is a genuine reproduction executed by the fixed executor.
MUTABLE_DEFAULT_TEST = (
    "import helpers\n"
    "\n"
    "\n"
    "def test_add_item_does_not_share_state():\n"
    "    assert helpers.add_item(1) == [1]\n"
    "    assert helpers.add_item(2) == [2]\n"
)


def extension_workflow(
    *,
    workflow_version: str = "wf-ext-1",
    max_repair_rounds: int = 2,
    extensions: Any = None,
) -> WorkflowConfig:
    """The sequential template plus the P10 test-generation extension node."""
    from app.extensions import build_default_extension_registry

    extensions = extensions or build_default_extension_registry()
    nodes = [
        WorkflowNode(id="parent", type=WorkflowNodeType.PARENT, agent_id="parent"),
        WorkflowNode(
            id="review", type=WorkflowNodeType.AGENT, agent_id="reviewer", task_kind="review"
        ),
        WorkflowNode(
            id="gen",
            type=WorkflowNodeType.AGENT,
            agent_id="test_generator",
            task_kind="generate_tests",
        ),
        WorkflowNode(id="fix", type=WorkflowNodeType.AGENT, agent_id="fixer", task_kind="fix"),
        WorkflowNode(id="apply", type=WorkflowNodeType.TOOL, tool_name="apply_patch"),
        WorkflowNode(
            id="verify", type=WorkflowNodeType.AGENT, agent_id="verifier", task_kind="verify"
        ),
        WorkflowNode(id="end", type=WorkflowNodeType.END),
    ]
    edges = [
        WorkflowEdge(source="parent", target=node_id)
        for node_id in ("review", "gen", "fix", "apply", "verify")
    ] + [
        WorkflowEdge(source=node_id, target="parent")
        for node_id in ("review", "gen", "fix", "apply", "verify")
    ] + [WorkflowEdge(source="parent", target="end")]
    config = WorkflowConfig(
        check_mode=CheckMode.SEQUENTIAL,
        agents={
            "parent": "1.0",
            "reviewer": "1.0",
            "fixer": "1.0",
            "verifier": "1.0",
            "test_generator": "1.0",
        },
        nodes=nodes,
        edges=edges,
        budgets=BudgetConfig(
            max_repair_rounds=max_repair_rounds,
            max_generate_retries=2,
        ),
    )
    expanded = config.with_standard_expansion(
        extra_dependencies=extensions.template_dependencies(config)
    )
    return expanded.bind(workflow_version)


def build_runtime(
    *,
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
    llm: ScriptedLLMClient,
    workflow: WorkflowConfig,
    tools: ToolRegistry | None = None,
) -> WorkflowRuntime:
    return build_harness(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=llm,
        workflow=workflow,
        tools=tools,
    ).runtime


@dataclass
class WorkflowHarness:
    """Everything needed to drive and recover one workflow."""

    runtime: WorkflowRuntime
    graph: Any
    recovery: RecoveryCoordinator
    task_service: TaskService
    controller: ParentController
    detector: Detector
    llm: ScriptedLLMClient


def build_harness(
    *,
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
    llm: ScriptedLLMClient,
    workflow: WorkflowConfig,
    tools: ToolRegistry | None = None,
    checkpointer: Any = None,
    extensions: Any = None,
) -> WorkflowHarness:
    from app.extensions import build_default_extension_registry

    assembly = assemble_workflow(
        settings=settings,
        repos=repos,
        workflow=workflow,
        llm=llm,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        tools=tools,
        extensions=extensions or build_default_extension_registry(),
    )
    graph = compile_graph(assembly, checkpointer=checkpointer)
    return WorkflowHarness(
        runtime=assembly.runtime,
        graph=graph,
        recovery=assembly.recovery,
        task_service=assembly.task_service,
        controller=assembly.controller,
        detector=assembly.detector,
        llm=llm,
    )



def prepare_source(
    workspace: WorkspaceService,
    repos: Repos,
    *,
    root_task_id: str,
    files: dict[str, str],
) -> tuple[str, str]:
    """Freeze an upload as the first immutable snapshot; returns (version, artifact)."""
    upload = workspace.create_upload([(p, b.encode()) for p, b in files.items()])
    manifest = workspace.publish_initial_snapshot(
        root_task_id=root_task_id, source_id=upload.source_id
    )
    source_artifact = workspace.register_source_artifact(
        root_task_id=root_task_id, source_version=manifest.source_version
    )
    repos.uploads.attach_root(upload.source_id, root_task_id)
    return manifest.source_version, source_artifact


async def run_workflow(
    *,
    settings: Settings,
    repos: Repos,
    artifacts: ArtifactService,
    workspace: WorkspaceService,
    locks: ExecutionLockService,
    root_task_id: str,
    files: dict[str, str],
    goal: str = "检查可变默认参数与异常处理，发现问题后修复并验证",
    workflow: WorkflowConfig | None = None,
    llm: ScriptedLLMClient | None = None,
    findings: list[dict] | None = None,
    fix_diffs: list[str] | None = None,
    latency_seconds: float = 0.0,
    checkpointer: Any = None,
):
    """Run one full graph invocation and return ``(final_state, llm)``."""
    source_version, source_artifact = prepare_source(
        workspace, repos, root_task_id=root_task_id, files=files
    )
    workflow = workflow or sequential_workflow()
    repos.workflows.insert(workflow)
    llm = llm or make_scripted_client(
        findings=findings, fix_diffs=fix_diffs, latency_seconds=latency_seconds
    )
    runtime = build_runtime(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        locks=locks,
        llm=llm,
        workflow=workflow,
    )
    graph = build_graph(runtime, checkpointer=checkpointer)
    state = initial_state(
        root_task_id=root_task_id,
        goal=goal,
        workflow_version=workflow.workflow_version or "wf-1",
        check_mode=workflow.check_mode.value,
        run_segment_id="seg-1",
        source_version=source_version,
        source_artifact=source_artifact,
        agent_versions=dict(workflow.agents),
    )
    final = await graph.ainvoke(
        state, config={"configurable": {"thread_id": f"hw2-task:{root_task_id}"}}
    )
    return final, llm


def run_parent_decide(context: dict[str, Any]) -> dict[str, Any]:
    """Parallel-aware parent logic: the post-patch batch is recheck + verify."""
    if context.get("check_mode") == "parallel":
        return parallel_parent_decide(context)
    return parent_decide(context, [])


def parallel_parent_decide(context: dict[str, Any]) -> dict[str, Any]:
    tree = {
        t["node_id"]: t for t in context.get("task_tree", []) if t.get("node_id")
    }
    reports = context.get("latest_reports", [])
    ran_nodes = {r["node_id"] for r in reports if r.get("node_id")}
    detection = context.get("detection") or {}
    category = detection.get("category")
    patched = bool(context.get("patched"))
    source_version = context.get("source_version")

    def dispatch(node_id: str, goal: str, reason: str) -> dict[str, Any]:
        task = tree[node_id]
        return {
            "action": "dispatch_task",
            "task": {
                "agent_id": task["agent_id"],
                "task_kind": task["task_kind"],
                "task_id": task["task_id"],
                "goal": goal,
                "input_refs": {"source": "auto", "acceptance_contract": "auto"},
                "acceptance_criteria": ["覆盖全部必需检查项"],
                "reason": reason,
            },
        }

    def item(node_id: str, goal: str, reason: str) -> dict[str, Any]:
        task = tree[node_id]
        return {
            "agent_id": task["agent_id"],
            "task_kind": task["task_kind"],
            "task_id": task["task_id"],
            "goal": goal,
            "input_refs": {"source": "auto", "acceptance_contract": "auto"},
            "acceptance_criteria": ["覆盖全部必需检查项"],
            "reason": reason,
        }

    if "review" not in ran_nodes:
        return dispatch("review", "审查当前版本并给出问题与结论", "首次审查")
    if context.get("last_application_error") is not None:
        return {
            "action": "wait_for_recovery",
            "reason": "补丁无法应用",
            "required_action": "人工检查补丁与基础版本后恢复任务",
        }
    if context.get("unapplied_fix_attempt_id"):
        return {
            "action": "apply_patch",
            "patch_ref": "auto",
            "base_version": source_version,
            "repair_attempt_id": context["unapplied_fix_attempt_id"],
            "reason": "应用修复 Agent 的候选补丁",
        }
    if patched and context.get("last_verified_source_version") != source_version:
        # one batch, two independent branches on the same frozen snapshot
        return {
            "action": "dispatch_batch",
            "tasks": [
                item("recheck", "复审修改后版本", "补丁后并行复审"),
                item("verify", "验证修改后版本", "补丁后并行验证"),
            ],
            "reason": "同一快照上并行复审与验证",
        }
    if category == "pass":
        return {
            "action": "finish",
            "proposed_passed": True,
            "report_refs": [],
            "reason": "并行复审与验证均通过",
        }
    if category in ("code_defect", "no_progress"):
        if int(context.get("repair_rounds_remaining") or 0) <= 0:
            return {
                "action": "wait_for_recovery",
                "reason": f"修复额度耗尽（{detection.get('failed_check_ids')}）",
                "required_action": "追加 repair_round 额度后恢复任务",
            }
        return dispatch("fix", "根据失败证据生成新的候选补丁", "并行检查后仍有缺陷")
    return {
        "action": "wait_for_recovery",
        "reason": "当前无法自动继续",
        "required_action": "人工确认后恢复任务",
    }


# --------------------------------------------------------------------------- #
# scripted parent/child model
# --------------------------------------------------------------------------- #


def default_plan(*, include_model_review: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        {
            "check_id": "CHK-SYNTAX",
            "goal_ref": "项目可解析",
            "scope": ["helpers.py"],
            "required": True,
            "method": "syntax",
            "pass_condition": "所有 Python 文件可解析",
            "applicable_phase": "both",
        },
        {
            "check_id": "CHK-MUTABLE",
            "goal_ref": "避免共享可变默认参数",
            "scope": ["helpers.py"],
            "required": True,
            "method": "static_rule",
            "pass_condition": "不存在共享可变默认参数",
            "applicable_phase": "both",
            "rule_ids": ["B006-mutable-default"],
        },
    ]
    if include_model_review:
        checks.append(
            {
                "check_id": "CHK-REVIEW",
                "goal_ref": "异常处理是否合理",
                "scope": ["helpers.py"],
                # a model review is not executable evidence, so it is a
                # supplementary (non-required) check that informs findings
                "required": False,
                "method": "model_review",
                "pass_condition": "异常处理符合目标要求",
                "applicable_phase": "initial_review",
            }
        )
    return {
        "review_scope": ["helpers.py"],
        "acceptance_criteria": ["覆盖全部必需检查项"],
        "checks": checks,
        "reason": "依据用户目标建立基础检查合同",
    }


def _context_of(request: LLMRequest) -> dict[str, Any]:
    for message in request.messages:
        if message.content.startswith("当前运行上下文："):
            return json.loads(message.content.split("：", 1)[1])
    return {}


def _child_context(request: LLMRequest) -> dict[str, Any]:
    for message in request.messages:
        if message.content.startswith("任务上下文："):
            return json.loads(message.content.split("：", 1)[1])
    return {}


def _mutable_default_still_present(request: LLMRequest) -> bool:
    """A scripted reviewer reports the defect only while the check still fails."""
    for result in _child_context(request).get("deterministic_check_results", []):
        if result.get("check_id") == "CHK-MUTABLE":
            return result.get("status") == "failed"
    return False


def parent_decide(context: dict[str, Any], rejections: list[dict[str, Any]]) -> dict[str, Any]:
    """A deterministic stand-in for the parent model's decision logic."""
    tree = {t["task_kind"]: t for t in context.get("task_tree", []) if t.get("task_kind")}
    reports = context.get("latest_reports", [])
    ran = {r["task_kind"] for r in reports}
    detection = context.get("detection") or {}
    category = detection.get("category")
    patched = bool(context.get("patched"))
    source_version = context.get("source_version")

    def dispatch(kind: str, agent: str, goal: str, reason: str) -> dict[str, Any]:
        return {
            "action": "dispatch_task",
            "task": {
                "agent_id": agent,
                "task_kind": kind,
                "task_id": tree[kind]["task_id"],
                "goal": goal,
                "input_refs": {"source": "auto", "acceptance_contract": "auto"},
                "acceptance_criteria": ["覆盖全部必需检查项"],
                "reason": reason,
            },
        }

    def wait(reason: str, required: str, known_passed: bool | None = None) -> dict[str, Any]:
        action: dict[str, Any] = {
            "action": "wait_for_recovery",
            "reason": reason,
            "required_action": required,
        }
        if known_passed is not None:
            action["known_passed"] = known_passed
        return action

    if "review" not in ran:
        return dispatch("review", "reviewer", "审查当前版本并给出问题与结论", "首次审查")
    if context.get("last_application_error") is not None:
        return wait("补丁无法应用且脚本无法自动改策", "人工检查补丁与基础版本后恢复任务")
    if context.get("unapplied_fix_attempt_id"):
        return {
            "action": "apply_patch",
            "patch_ref": "auto",
            "base_version": source_version,
            "repair_attempt_id": context["unapplied_fix_attempt_id"],
            "reason": "应用修复 Agent 的候选补丁",
        }
    if patched and context.get("last_verified_source_version") != source_version:
        return dispatch("verify", "verifier", "验证修改后版本是否满足约定检查", "补丁已应用")
    if category == "pass":
        return {
            "action": "finish",
            "proposed_passed": True,
            "report_refs": [],
            "reason": "当前版本的必需检查全部通过",
        }
    if category in ("code_defect", "no_progress"):
        if int(context.get("repair_rounds_remaining") or 0) <= 0:
            return wait(
                f"修复额度耗尽且仍有未解决的必需项（{detection.get('failed_check_ids')}）",
                "追加 repair_round 额度后恢复任务",
                known_passed=False,
            )
        return dispatch("fix", "fixer", "根据失败证据生成新的候选补丁", "修复后仍有缺陷")
    if "fix" not in ran:
        return wait(
            f"审查证据不足（{detection.get('missing_items')}）",
            "补充检查或人工确认后恢复任务",
        )
    return wait("当前无法自动继续", "人工确认后恢复任务")


def make_scripted_client(
    *,
    findings: list[dict] | None = None,
    fix_diffs: list[str] | None = None,
    latency_seconds: float = 0.0,
    handler_hook: Any = None,
    plan: dict[str, Any] | None = None,
    decide: Any = None,
    generated_test_check_id: str = "CHK-BEHAVIOR",
) -> ScriptedLLMClient:
    """A scripted model: parent plan/decide plus one step per child role.

    ``fix_diffs`` gives the diff each successive fix attempt submits, so a test
    can model "first repair does not help, second one does".

    ``plan`` / ``decide`` override the parent's planning and decision logic, so an
    extension scenario can add a step without touching the base scenarios.

    ``handler_hook`` runs at the top of every handler call; the process
    interruption test uses it to crash deterministically at a chosen step.
    """
    review_findings = findings if findings is not None else []
    diffs = fix_diffs if fix_diffs is not None else [FIX_DIFF]
    fix_state = {"n": 0}

    def handler(request: LLMRequest) -> Any:
        if handler_hook is not None:
            handler_hook(request)
        key = request.script_key or ""
        if key == "parent:plan":
            return plan if plan is not None else default_plan()
        if key == "parent:decide":
            context = _context_of(request)
            action = decide(context) if decide is not None else run_parent_decide(context)
            return {
                "reasoning": "依据检测事实选择下一步",
                "action": action,
            }
        if key.endswith(":reviewer:step"):
            findings = review_findings if _mutable_default_still_present(request) else []
            return {
                "reason": "已执行确定性检查并复核 model_review 项",
                "final": {
                    "summary": "审查完成",
                    "model_review_results": [
                        {
                            "check_id": "CHK-REVIEW",
                            "status": "passed",
                            "reason": "读取 helpers.py 后确认异常处理符合目标",
                            "read_scope": ["helpers.py"],
                            "evidence_positions": ["helpers.py:1"],
                        }
                    ],
                    "findings": findings,
                    "coverage": ["CHK-SYNTAX", "CHK-MUTABLE", "CHK-REVIEW"],
                    "not_checked": [],
                },
            }
        if key.endswith(":fixer:step"):
            index = min(fix_state["n"], len(diffs) - 1)
            fix_state["n"] += 1
            return {
                "reason": "已针对问题生成补丁并提交",
                "final": {
                    "summary": f"第 {index + 1} 轮候选补丁",
                    "rationale": "针对 B006 生成最小补丁",
                    "format": "unified_diff",
                    "diff_text": diffs[index],
                    "target_finding_ids": [],
                },
            }
        if key.endswith(":test_generator:step"):
            return {
                "reason": "已为行为检查项编写最小复现测试",
                "final": {
                    "summary": "测试生成完成",
                    "tests": [
                        {
                            "check_id": generated_test_check_id,
                            "filename": "test_mutable_default.py",
                            "content": MUTABLE_DEFAULT_TEST,
                            "expected_source": "验收目标：add_item 不共享默认列表",
                        }
                    ],
                    "target_finding_ids": [],
                    "notes": "一个检查项一个测试文件",
                },
            }
        if key.endswith(":verifier:step"):
            return {
                "reason": "执行器已在修改后版本运行约定检查",
                "final": {
                    "summary": "验证完成",
                    "generated_tests": [],
                    "coverage": ["CHK-SYNTAX", "CHK-MUTABLE"],
                    "not_run": [],
                    "notes": "复用确定性检查结果",
                },
            }
        raise AssertionError(f"unexpected script_key {key!r}")

    return ScriptedLLMClient(handler, latency_seconds=latency_seconds)
