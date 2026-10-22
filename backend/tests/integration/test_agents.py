"""P03 acceptance: role execution through one adapter, permissions, evidence,
scripted-vs-real model separation (ImplementationPlan §7)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.providers.base import LLMRequest
from app.providers.factory import build_llm_client
from app.providers.scripted import ScriptedLLMClient
from app.registry.agents import build_default_agent_registry
from app.registry.tools import StepCounter, ToolContext
from app.schemas.enums import ResultStatus
from app.schemas.results import TaskResult
from app.services.artifacts import ArtifactService
from app.services.tools import build_default_registry
from app.services.workspace import WorkspaceService
from app.settings import ConfigurationError, Settings
from app.storage.database import Database
from app.storage.repositories import Repos
from app.workflow.adapter import AgentNodeAdapter, ResultIdentityError
from tests.fixtures import agents as fx


def _setup(tmp_path):
    settings = fx.settings_for(tmp_path)
    db = Database(settings.business_db_path)
    db.initialize()
    repos = Repos(db)
    artifacts = ArtifactService(settings, repos.artifacts)
    workspace = WorkspaceService(settings, artifacts, repos)
    root, _fix_task, batch_id = fx.build_task_tree(repos, source_version="ignored")
    source_version, source_artifact = fx.publish_source(
        workspace, repos, artifacts, root_task_id=root.task_id
    )
    contract, contract_artifact = fx.save_contract(artifacts, repos, root_task_id=root.task_id)
    return dict(
        settings=settings,
        repos=repos,
        artifacts=artifacts,
        workspace=workspace,
        root=root,
        review_task=repos.tasks.get("T101"),
        contract=contract,
        contract_artifact=contract_artifact,
        source_version=source_version,
        source_artifact=source_artifact,
        batch_id=batch_id,
    )


def _prepare_attempt(ctx, task_id="T101", task_kind="review", attempt_id="T101-A1"):
    repos = ctx["repos"]
    task = repos.tasks.get(task_id)
    envelope = fx.make_envelope(
        root_task_id=ctx["root"].task_id,
        task=task,
        attempt_id=attempt_id,
        batch_id=ctx["batch_id"],
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        contract_artifact_id=ctx["contract_artifact"],
        source_artifact_id=ctx["source_artifact"],
        input_refs={"findings": ctx.get("findings_artifact")} if ctx.get("findings_artifact") else None,
    )
    fx.register_attempt(
        repos,
        root_task_id=ctx["root"].task_id,
        task=task,
        attempt_id=attempt_id,
        batch_id=ctx["batch_id"],
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        input_refs=envelope.input_refs.as_mapping(),
    )
    return envelope


def test_llm_client_requires_real_credentials() -> None:
    with pytest.raises(ConfigurationError):
        build_llm_client(Settings(model_api_key=None))


def test_scripted_model_never_replaces_a_missing_real_model() -> None:
    with pytest.raises(ConfigurationError):
        build_llm_client(Settings(model_api_key="   "))
    client = build_llm_client(Settings(model_api_key="sk-test", force_scripted_model=False))
    assert client.__class__.__name__ == "DeepSeekClient"


async def test_reviewer_runs_deterministic_checks_and_model_review(tmp_path) -> None:
    ctx = _setup(tmp_path)
    repos, artifacts = ctx["repos"], ctx["artifacts"]
    envelope = _prepare_attempt(ctx)

    state = {"calls": 0}

    def handler(request: LLMRequest):
        state["calls"] += 1
        last = request.messages[-1].content
        if "工具观察结果" not in last:
            return {"reason": "读取被检查文件", "tool": "read_file", "args": {"path": "helpers.py"}}
        return {
            "reason": "给出审查结论",
            "final": {
                "summary": "发现共享可变默认参数与缺少输入校验",
                "model_review_results": [
                    {
                        "check_id": "CHK-REVIEW",
                        "status": "failed",
                        "reason": "average 未处理空输入",
                        "read_scope": ["helpers.py"],
                        "evidence_positions": ["helpers.py:7"],
                    }
                ],
                "findings": [
                    {
                        "file_path": "helpers.py",
                        "rule": "B006-mutable-default",
                        "message": "add_item 使用共享可变默认参数",
                        "check_id": "CHK-MUTABLE",
                        "goal_ref": "避免共享可变默认参数",
                        "required_for_goal": True,
                        "line": 1,
                        "symbol": "add_item",
                    }
                ],
                "coverage": ["helpers.py"],
                "not_checked": ["其他文件"],
            },
        }

    registry = build_default_agent_registry()
    spec = registry.get_spec("reviewer", "1.0")
    context = fx.make_context(
        llm=ScriptedLLMClient(handler),
        tools=build_default_registry(),
        workspace=ctx["workspace"],
        artifacts=artifacts,
        repos=repos,
        sink=fx.root_sink(repos, ctx["root"].task_id),
        spec=spec,
    )
    adapter = AgentNodeAdapter(
        registry=registry,
        repos=repos,
        sink=fx.root_sink(repos, ctx["root"].task_id),
        context_factory=lambda task, s: context,
    )

    result = await adapter.execute(envelope)

    assert result.status is ResultStatus.COMPLETED
    assert result.passed is False  # a required static rule failed and a required finding is open
    assert "findings" in result.result_refs
    bundle = artifacts.read_json(result.result_refs["findings"])
    assert bundle["findings"][0]["required_for_goal"] is True
    assert repos.findings.list_by_root(ctx["root"].task_id)

    recorded = {
        r.check_id: r
        for r in repos.check_results.list_for_version(ctx["root"].task_id, ctx["source_version"])
    }
    assert recorded["CHK-SYNTAX"].status.value == "passed"
    assert recorded["CHK-MUTABLE"].status.value == "failed"
    assert recorded["CHK-REVIEW"].method.value == "model_review"
    assert repos.attempts.get("T101-A1").status.value == "running"

    kinds = {e.event_type.value for e in repos.events.list_after(ctx["root"].task_id)}
    assert {"attempt_started", "tool_started", "tool_finished"} <= kinds
    assert state["calls"] >= 2


async def test_reviewer_reports_insufficient_evidence_as_null(tmp_path) -> None:
    """A model_review that cannot conclude must yield passed=None, never True."""
    ctx = _setup(tmp_path)
    # a clean source so the deterministic checks pass and only the model_review is open
    clean = {
        "helpers.py": (
            "def add_item(item, items=None):\n"
            "    if items is None:\n"
            "        items = []\n"
            "    items.append(item)\n"
            "    return items\n"
            "\n"
            "\n"
            "def average(values):\n"
            "    if not values:\n"
            "        raise ValueError('empty input')\n"
            "    return sum(values) / len(values)\n"
        )
    }
    source_version, source_artifact = fx.publish_source(
        ctx["workspace"], ctx["repos"], ctx["artifacts"], root_task_id=ctx["root"].task_id, files=clean
    )
    ctx["source_version"] = source_version
    ctx["source_artifact"] = source_artifact
    envelope = _prepare_attempt(ctx)

    def handler(request: LLMRequest):
        if "工具观察结果" not in request.messages[-1].content:
            return {"reason": "read", "tool": "list_files", "args": {}}
        return {
            "reason": "无法判断",
            "final": {
                "summary": "异常处理目标证据不足",
                "model_review_results": [
                    {"check_id": "CHK-REVIEW", "status": "inconclusive", "reason": "缺少需求说明"}
                ],
                "findings": [],
                "coverage": [],
                "not_checked": ["异常处理目标"],
            },
        }

    registry = build_default_agent_registry()
    context = fx.make_context(
        llm=ScriptedLLMClient(handler),
        tools=build_default_registry(),
        workspace=ctx["workspace"],
        artifacts=ctx["artifacts"],
        repos=ctx["repos"],
        sink=fx.root_sink(ctx["repos"], ctx["root"].task_id),
        spec=registry.get_spec("reviewer", "1.0"),
    )
    adapter = AgentNodeAdapter(
        registry=registry,
        repos=ctx["repos"],
        sink=fx.root_sink(ctx["repos"], ctx["root"].task_id),
        context_factory=lambda task, s: context,
    )
    result = await adapter.execute(envelope)
    assert result.status is ResultStatus.COMPLETED
    assert result.passed is None


async def test_adapter_rejects_a_report_whose_identity_mismatches(tmp_path) -> None:
    ctx = _setup(tmp_path)
    envelope = _prepare_attempt(ctx)
    registry = build_default_agent_registry()
    context = fx.make_context(
        llm=ScriptedLLMClient(lambda request: {"reason": "noop", "final": {"summary": "noop"}}),
        tools=build_default_registry(),
        workspace=ctx["workspace"],
        artifacts=ctx["artifacts"],
        repos=ctx["repos"],
        sink=fx.root_sink(ctx["repos"], ctx["root"].task_id),
        spec=registry.get_spec("reviewer", "1.0"),
    )
    adapter = AgentNodeAdapter(
        registry=registry,
        repos=ctx["repos"],
        sink=fx.root_sink(ctx["repos"], ctx["root"].task_id),
        context_factory=lambda t, s: context,
    )

    class Rogue:
        spec = registry.get_spec("reviewer", "1.0")

        async def execute(self, task, context):
            now = datetime.now(timezone.utc)
            return TaskResult(
                result_id="R-rogue",
                root_task_id=task.root_task_id,
                parent_task_id=task.parent_task_id,
                task_id=task.task_id,
                attempt_id="T999-A9",  # wrong attempt id
                dispatch_batch_id=task.dispatch_batch_id,
                agent_id=task.agent_id,
                agent_version=task.agent_version,
                task_kind=task.task_kind,
                source_version=task.source_version,
                contract_version=task.contract_version,
                status=ResultStatus.COMPLETED,
                passed=True,
                summary="伪造通过",
                started_at=now,
                finished_at=now,
            )

    registry._impls[("reviewer", "1.0")] = Rogue()  # noqa: SLF001 - test-only override
    with pytest.raises(ResultIdentityError):
        await adapter.execute(envelope)


async def test_tool_permissions_block_cross_role_tools(tmp_path) -> None:
    ctx = _setup(tmp_path)
    tools = build_default_registry()
    tool_ctx = ToolContext(
        actor_id="fixer",
        root_task_id=ctx["root"].task_id,
        workspace=ctx["workspace"],
        artifacts=ctx["artifacts"],
        repos=ctx["repos"],
        sink=fx.root_sink(ctx["repos"], ctx["root"].task_id),
        source_version=ctx["source_version"],
        steps=StepCounter(limit=4),
    )
    denied = await tools.execute("run_checks", tool_ctx, checks=[], contract_version="contract-1")
    assert denied.ok is False
    assert denied.error is not None and denied.error.code == "TOOL_FORBIDDEN"


async def test_tool_step_budget_is_enforced(tmp_path) -> None:
    ctx = _setup(tmp_path)
    tools = build_default_registry()
    tool_ctx = ToolContext(
        actor_id="reviewer",
        root_task_id=ctx["root"].task_id,
        workspace=ctx["workspace"],
        artifacts=ctx["artifacts"],
        repos=ctx["repos"],
        sink=fx.root_sink(ctx["repos"], ctx["root"].task_id),
        source_version=ctx["source_version"],
        steps=StepCounter(limit=1),
    )
    first = await tools.execute("list_files", tool_ctx)
    second = await tools.execute("list_files", tool_ctx)
    assert first.ok is True
    assert second.ok is False
    assert second.error is not None and second.error.code == "TOOL_STEP_LIMIT"


async def test_fixer_submits_a_patch_for_the_current_version(tmp_path) -> None:
    ctx = _setup(tmp_path)
    repos = ctx["repos"]
    fix_task = repos.tasks.get("T102")

    # findings the fixer must address
    review_task = ctx["review_task"]
    review_envelope = fx.make_envelope(
        root_task_id=ctx["root"].task_id,
        task=review_task,
        attempt_id="T101-A1",
        batch_id=ctx["batch_id"],
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        contract_artifact_id=ctx["contract_artifact"],
        source_artifact_id=ctx["source_artifact"],
    )
    fx.register_attempt(
        repos,
        root_task_id=ctx["root"].task_id,
        task=review_task,
        attempt_id="T101-A1",
        batch_id=ctx["batch_id"],
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        input_refs=review_envelope.input_refs.as_mapping(),
    )
    from app.agents.reviewer import ReviewerAgent

    findings_result = await _run_reviewer_for_findings(ctx)

    from app.schemas.tasks import InputRefs

    envelope = fx.make_envelope(
        root_task_id=ctx["root"].task_id,
        task=fix_task,
        attempt_id="T102-A1",
        batch_id="B2",
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        contract_artifact_id=ctx["contract_artifact"],
        source_artifact_id=ctx["source_artifact"],
        input_refs={"findings": findings_result},
    )
    fx.register_attempt(
        repos,
        root_task_id=ctx["root"].task_id,
        task=fix_task,
        attempt_id="T102-A1",
        batch_id="B2",
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        input_refs=envelope.input_refs.as_mapping(),
    )

    def fixer_handler(request: LLMRequest):
        if "工具观察结果" not in request.messages[-1].content:
            return {"reason": "查看问题位置", "tool": "read_file", "args": {"path": "helpers.py"}}
        return {
            "reason": "给出补丁",
            "final": {
                "summary": "修复共享可变默认参数",
                "rationale": "改用 None 作为默认值",
                "format": "structured_edits",
                "edits": [
                    {
                        "file_path": "helpers.py",
                        "find": "def add_item(item, items=[]):\n    items.append(item)\n    return items\n",
                        "replace": (
                            "def add_item(item, items=None):\n"
                            "    if items is None:\n"
                            "        items = []\n"
                            "    items.append(item)\n"
                            "    return items\n"
                        ),
                    }
                ],
                "target_finding_ids": [],
            },
        }

    registry = build_default_agent_registry()
    context = fx.make_context(
        llm=ScriptedLLMClient(fixer_handler),
        tools=build_default_registry(),
        workspace=ctx["workspace"],
        artifacts=ctx["artifacts"],
        repos=repos,
        sink=fx.root_sink(repos, ctx["root"].task_id),
        spec=registry.get_spec("fixer", "1.0"),
    )
    adapter = AgentNodeAdapter(
        registry=registry,
        repos=repos,
        sink=fx.root_sink(repos, ctx["root"].task_id),
        context_factory=lambda task, s: context,
    )
    result = await adapter.execute(envelope)
    assert result.status is ResultStatus.COMPLETED
    assert result.passed is None, "a fixer must never claim a business verdict"
    patch_artifact = result.result_refs["patch"]
    patch = ctx["artifacts"].read_json(patch_artifact)
    assert patch["base_version"] == ctx["source_version"]
    assert patch["target_finding_ids"], "the required finding must be targeted"


async def _run_reviewer_for_findings(ctx) -> str:
    """Run a reviewer attempt and return the findings artifact id."""
    from app.agents.reviewer import ReviewerAgent

    repos = ctx["repos"]
    review_task = repos.tasks.get("T101")
    envelope = fx.make_envelope(
        root_task_id=ctx["root"].task_id,
        task=review_task,
        attempt_id="T101-A1",
        batch_id=ctx["batch_id"],
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        contract_artifact_id=ctx["contract_artifact"],
        source_artifact_id=ctx["source_artifact"],
    )

    def handler(request: LLMRequest):
        if "工具观察结果" not in request.messages[-1].content:
            return {"reason": "read", "tool": "list_files", "args": {}}
        return {
            "reason": "final",
            "final": {
                "summary": "发现可变默认参数",
                "model_review_results": [],
                "findings": [
                    {
                        "file_path": "helpers.py",
                        "rule": "B006-mutable-default",
                        "message": "共享可变默认参数",
                        "required_for_goal": True,
                        "line": 1,
                        "symbol": "add_item",
                    }
                ],
                "coverage": ["helpers.py"],
                "not_checked": [],
            },
        }

    registry = build_default_agent_registry()
    context = fx.make_context(
        llm=ScriptedLLMClient(handler),
        tools=build_default_registry(),
        workspace=ctx["workspace"],
        artifacts=ctx["artifacts"],
        repos=repos,
        sink=fx.root_sink(repos, ctx["root"].task_id),
        spec=registry.get_spec("reviewer", "1.0"),
    )
    adapter = AgentNodeAdapter(
        registry=registry,
        repos=repos,
        sink=fx.root_sink(repos, ctx["root"].task_id),
        context_factory=lambda task, s: context,
    )
    result = await adapter.execute(envelope)
    return result.result_refs["findings"]


async def test_verifier_proves_a_defect_with_a_generated_test(tmp_path) -> None:
    ctx = _setup(tmp_path)
    repos = ctx["repos"]
    verify_task = repos.tasks.get("T103")
    from app.schemas.tasks import InputRefs

    envelope = fx.make_envelope(
        root_task_id=ctx["root"].task_id,
        task=verify_task,
        attempt_id="T103-A1",
        batch_id="B3",
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        contract_artifact_id=ctx["contract_artifact"],
        source_artifact_id=ctx["source_artifact"],
    )
    fx.register_attempt(
        repos,
        root_task_id=ctx["root"].task_id,
        task=verify_task,
        attempt_id="T103-A1",
        batch_id="B3",
        source_version=ctx["source_version"],
        contract_version=ctx["contract"].contract_version,
        input_refs=envelope.input_refs.as_mapping(),
    )

    def handler(request: LLMRequest):
        if "工具观察结果" not in request.messages[-1].content:
            return {"reason": "查看代码", "tool": "read_file", "args": {"path": "helpers.py"}}
        return {
            "reason": "给出最小复现测试",
            "final": {
                "summary": "average 对空输入会抛 ZeroDivisionError",
                "generated_tests": [
                    {
                        "check_id": "CHK-AVG-BEHAVIOR",
                        "filename": "test_average_empty.py",
                        "content": (
                            "from helpers import average\n\n"
                            "def test_average_empty_raises_value_error():\n"
                            "    try:\n"
                            "        average([])\n"
                            "    except ValueError:\n"
                            "        return\n"
                            "    raise AssertionError('expected ValueError for empty input')\n"
                        ),
                        "expected_source": "用户目标：average 应对空输入显式报错",
                    }
                ],
                "coverage": ["helpers.py"],
                "not_run": [],
            },
        }

    registry = build_default_agent_registry()
    context = fx.make_context(
        llm=ScriptedLLMClient(handler),
        tools=build_default_registry(),
        workspace=ctx["workspace"],
        artifacts=ctx["artifacts"],
        repos=repos,
        sink=fx.root_sink(repos, ctx["root"].task_id),
        spec=registry.get_spec("verifier", "1.0"),
        max_tool_steps=10,
    )
    adapter = AgentNodeAdapter(
        registry=registry,
        repos=repos,
        sink=fx.root_sink(repos, ctx["root"].task_id),
        context_factory=lambda task, s: context,
    )
    result = await adapter.execute(envelope)

    # the generated test fails on the un-fixed version -> verified as a real defect
    assert result.status is ResultStatus.COMPLETED
    assert result.passed is False
    recorded = {
        r.check_id: r
        for r in repos.check_results.list_for_version(ctx["root"].task_id, ctx["source_version"])
    }
    behavior = recorded["CHK-AVG-BEHAVIOR"]
    assert behavior.status.value == "failed"
    assert behavior.test_count and behavior.test_count >= 1
    assert behavior.test_suite_hash
    assert "verification" in result.result_refs


async def test_reviewer_generates_and_executes_behavior_evidence(tmp_path):
    ctx = _setup(tmp_path)
    envelope = _prepare_attempt(ctx)
    llm = ScriptedLLMClient(lambda request: {"reason": "复现空输入缺陷", "final": {
        "summary": "空输入未抛 ValueError", "generated_tests": [{
            "check_id": "CHK-AVG-BEHAVIOR", "filename": "test_average.py",
            "content": "import pytest\nfrom helpers import average\ndef test_empty():\n    with pytest.raises(ValueError): average([])\n",
            "expected_source": "冻结检查合同的空输入约定",
        }], "findings": [], "coverage": ["helpers.py"], "not_checked": [],
    }})
    registry = build_default_agent_registry()
    spec = registry.get_spec("reviewer", "1.0")
    context = fx.make_context(llm=llm, tools=build_default_registry(),
        workspace=ctx["workspace"], artifacts=ctx["artifacts"], repos=ctx["repos"],
        sink=fx.root_sink(ctx["repos"], ctx["root"].task_id), spec=spec)
    result = await registry.resolve("reviewer", "1.0").execute(envelope, context)
    assert result.status is ResultStatus.COMPLETED, result
    assert result.passed is False
    assert result.result_refs["generated_tests"]
    row = ctx["repos"].check_results.latest_for_check(ctx["root"].task_id, "CHK-AVG-BEHAVIOR", ctx["source_version"])
    assert row.status.value == "failed"


async def test_findings_submission_is_idempotent_with_reworded_message(tmp_path):
    from app.services.tools.outputs import submit_findings, SubmitFindingsArgs
    ctx = _setup(tmp_path)
    envelope = _prepare_attempt(ctx)
    tools = build_default_registry()
    spec = build_default_agent_registry().get_spec("reviewer", "1.0")
    context = fx.make_context(llm=ScriptedLLMClient(lambda _: {}), tools=tools,
        workspace=ctx["workspace"], artifacts=ctx["artifacts"], repos=ctx["repos"],
        sink=fx.root_sink(ctx["repos"], ctx["root"].task_id), spec=spec)
    draft = dict(file_path="helpers.py", line=1, symbol="add_item", rule="B006-mutable-default",
        check_id="CHK-MUTABLE", goal_ref="避免共享默认列表", required_for_goal=True, message="共享列表")
    first = await submit_findings(context.tool_context(envelope), SubmitFindingsArgs(findings=[draft]))
    draft["message"] = "同一位置的共享默认列表"
    second = await submit_findings(context.tool_context(envelope), SubmitFindingsArgs(findings=[draft]))
    assert first.data["finding_ids"] == second.data["finding_ids"]
    assert len(ctx["repos"].findings.list_by_root(ctx["root"].task_id)) == 1


async def test_reviewer_rejects_partial_generated_test_mapping(tmp_path):
    from app.agents.reviewer import ReviewerFinal
    ctx = _setup(tmp_path)
    checks = ctx["contract"].checks + [ctx["contract"].checks[2].model_copy(update={"check_id": "SECOND-BEHAVIOR"})]
    contract, ref = fx.save_contract(ctx["artifacts"], ctx["repos"], root_task_id=ctx["root"].task_id, checks=checks, contract_version="contract-2")
    ctx.update(contract=contract, contract_artifact=ref)
    envelope = _prepare_attempt(ctx)
    registry = build_default_agent_registry()
    spec = registry.get_spec("reviewer", "1.0")
    context = fx.make_context(llm=ScriptedLLMClient(lambda _: {}), tools=build_default_registry(), workspace=ctx["workspace"], artifacts=ctx["artifacts"], repos=ctx["repos"], sink=fx.root_sink(ctx["repos"], ctx["root"].task_id), spec=spec)
    final = ReviewerFinal(summary="partial", generated_tests=[dict(check_id="CHK-AVG-BEHAVIOR", filename="test_x.py", content="def test_x(): pass", expected_source="contract")])
    with pytest.raises(ValueError, match="SECOND-BEHAVIOR"):
        registry.resolve("reviewer", "1.0").validate_final(final, envelope, context)

    context.llm = ScriptedLLMClient(lambda _: {"reason": "partial", "final": final.model_dump(mode="json")})
    context.max_tool_steps = 1
    result = await registry.resolve("reviewer", "1.0").execute(envelope, context)
    assert result.status is ResultStatus.FAILED
    assert result.error.code == "TOOL_STEP_LIMIT"
