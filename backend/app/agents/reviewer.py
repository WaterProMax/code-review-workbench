"""Reviewer: runs contract checks over a frozen snapshot and records findings."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.verifier import _provided_tests
from app.agents.base import (
    AgentContext,
    BaseAgent,
    deterministic_checks,
    model_review_checks,
    required_checks,
    verdict_from_checks,
)
from app.schemas.agents import AgentSpec
from app.schemas.artifacts import GeneratedTest, GeneratedTestSuite
from app.schemas.common import ErrorInfo
from app.schemas.enums import (
    ArtifactType,
    CheckMethod,
    CheckStatus,
    ErrorCategory,
    ResultStatus,
)
from app.schemas.results import CheckResult, CheckSpec
from app.schemas.results import TaskResult
from app.schemas.tasks import TaskEnvelope
from app.services.tools.outputs import FindingDraft

_MODEL_STATUS = {
    "passed": CheckStatus.PASSED,
    "failed": CheckStatus.FAILED,
    "inconclusive": CheckStatus.INCONCLUSIVE,
}


class ModelReviewOutcome(BaseModel):
    """One ``model_review`` verdict with the ranges the model actually read."""

    check_id: str = Field(min_length=1)
    status: Literal["passed", "failed", "inconclusive"]
    reason: str = Field(min_length=1)
    read_scope: list[str] = Field(default_factory=list)
    evidence_positions: list[str] = Field(default_factory=list)


class ReviewerFinal(BaseModel):
    summary: str = Field(min_length=1)
    model_review_results: list[ModelReviewOutcome] = Field(default_factory=list)
    generated_tests: list[GeneratedTest] = Field(default_factory=list)
    findings: list[FindingDraft] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    not_checked: list[str] = Field(default_factory=list)


class ReviewerAgent(BaseAgent):
    def __init__(self, spec: AgentSpec, prompt: str) -> None:
        super().__init__(spec, prompt, ReviewerFinal)

    async def run(
        self, task: TaskEnvelope, context: AgentContext, started_at: datetime
    ) -> TaskResult:
        tool_ctx = context.tool_context(task)
        contract = self.load_contract(task, context)
        checks = self.checks_for(contract, task)
        required = required_checks(contract, "review")
        provided = _provided_tests(task, context)

        det_results: list[CheckResult] = []
        faults: list[dict] = []
        evidence: list[str] = []
        det = deterministic_checks(checks)
        if det:
            result = await context.tools.execute(
                "run_checks",
                tool_ctx,
                checks=det,
                contract_version=contract.contract_version,
                include_existing_tests=True,
                generated_tests=provided.by_check if provided else {},
            )
            if result.ok and result.data:
                det_results = [CheckResult.model_validate(r) for r in result.data["check_results"]]
                evidence.extend(result.artifact_refs)
                faults = list(result.faults)
            else:
                faults = result.faults or [
                    {
                        "code": result.error.code if result.error else "CHECK_TOOL_ERROR",
                        "category": "tool_error",
                        "message": result.summary,
                        "recoverable": True,
                    }
                ]

        final, transcript = await self.run_tool_loop(
            task,
            context,
            tool_ctx,
            opening=(
                "请审查当前版本并给出结论。已由执行器完成的确定性检查如下，"
                "请实际读取源码，对未运行的 behavior_test 在 final.generated_tests 中提供 pytest 最小测试，"
                "执行器将自动保存并执行。每个 behavior_test 的 check_id 都必须有单独 GeneratedTest 条目，"
                "不要把全部测试只绑定第一个检查。不要为了补充证据而修改正确源码。"
                "无需手工提交 findings；在 final 中返回 findings 和 model_review_results 即可。"
            ),
            extra_context={
                "deterministic_check_results": [r.model_dump(mode="json") for r in det_results],
                "model_review_checks": [
                    c.model_dump(mode="json") for c in model_review_checks(checks)
                ],
                "provided_tests": provided.describe() if provided else None,
                "test_execution_layout": "pytest 的 cwd 和 PYTHONPATH 均为快照根目录。用 from helpers import ... 或 calculator 包导入；不要按测试 __file__ 查找兄弟源码文件。已有 provided_tests 优先复用，无需重写。",
                "all_checks": [c.model_dump(mode="json") for c in checks],
                "required_check_ids": [c.check_id for c in required],
            },
        )
        assert isinstance(final, ReviewerFinal)

        # Final-payload tests and tests saved through a tool are equivalent inputs.
        # Always execute the frozen contract after the model loop: an early
        # not_run result must not overwrite evidence produced later in that loop.
        tool_ctx.steps.limit += 4
        tests_artifact = provided.artifact_refs[0] if provided else None
        if final.generated_tests:
            saved = await context.tools.execute(
                "submit_generated_tests", tool_ctx,
                tests=[t.model_dump(mode="json") for t in final.generated_tests],
            )
            if not saved.ok:
                faults.append({"code": "TEST_INPUT_INVALID", "message": saved.summary})
        generated_by_check: dict[str, list[str]] = dict(provided.by_check) if provided else {}
        for artifact in context.repos.artifacts.list_by_root(task.root_task_id):
            if artifact.artifact_type is not ArtifactType.TEST_ARTIFACT or not artifact.metadata.get("bundle"):
                continue
            suite = GeneratedTestSuite.model_validate(context.artifacts.read_json(artifact.artifact_id))
            if suite.source_version != task.source_version:
                continue
            tests_artifact = artifact.artifact_id
            evidence.append(artifact.artifact_id)
            for check_id in suite.by_check():
                generated_by_check[check_id] = [artifact.artifact_id]
        if generated_by_check:
            rerun = await context.tools.execute(
                "run_checks", tool_ctx, checks=det,
                contract_version=contract.contract_version,
                include_existing_tests=True, generated_tests=generated_by_check,
            )
            if rerun.ok and rerun.data:
                det_results = [CheckResult.model_validate(r) for r in rerun.data["check_results"]]
                evidence.extend(rerun.artifact_refs)
                faults = list(rerun.faults)
            else:
                faults = rerun.faults or [{"code": "CHECK_TOOL_ERROR", "message": rerun.summary}]

        # persist findings through the controlled tool (validation + events)
        findings_result = await context.tools.execute(
            "submit_findings",
            tool_ctx,
            findings=[f.model_dump(mode="json") for f in final.findings],
            coverage=final.coverage,
            not_checked=final.not_checked,
        )
        findings_artifact = None
        if findings_result.ok and findings_result.data:
            findings_artifact = findings_result.data["artifact_id"]
            evidence.append(findings_artifact)

        model_results = self._persist_model_review(
            context, task, contract, checks, final, evidence
        )

        all_results = det_results + model_results
        open_required = sum(1 for f in final.findings if f.required_for_goal)
        passed = verdict_from_checks(required, all_results, open_required)

        result_refs: dict[str, str] = {}
        if tests_artifact:
            result_refs["generated_tests"] = tests_artifact
        if findings_artifact:
            result_refs["findings"] = findings_artifact

        if faults:
            first = faults[0]
            return self.build_result(
                task,
                status=ResultStatus.FAILED,
                passed=None,
                summary=f"审查执行故障：{first.get('message', '未知')}（本结论不成立）",
                started_at=started_at,
                result_refs=result_refs,
                evidence_refs=evidence,
                error=ErrorInfo(
                    code=str(first.get("code", "CHECK_TOOL_ERROR")),
                    category=_category(first.get("category")),
                    message=str(first.get("message", "检查执行故障")),
                    recoverable=bool(first.get("recoverable", True)),
                ),
            )

        return self.build_result(
            task,
            status=ResultStatus.COMPLETED,
            passed=passed,
            summary=self._summary(final, passed, len(final.findings), len(final.coverage)),
            started_at=started_at,
            result_refs=result_refs,
            evidence_refs=evidence,
        )

    def validate_final(self, final: BaseModel, task: TaskEnvelope, context: AgentContext) -> None:
        assert isinstance(final, ReviewerFinal)
        produced = {t.check_id for t in final.generated_tests}
        for artifact in context.repos.artifacts.list_by_root(task.root_task_id):
            if artifact.artifact_type is ArtifactType.TEST_ARTIFACT and artifact.source_version == task.source_version:
                produced.update(artifact.metadata.get("check_ids", []))
        if not produced:
            return  # Explicitly unavailable evidence stays not_run, never a pass.
        contract = self.load_contract(task, context)
        missing = []
        for check in self.checks_for(contract, task):
            if not check.required or check.method is not CheckMethod.BEHAVIOR_TEST or check.check_id in produced:
                continue
            prior = context.repos.check_results.latest_for_check(task.root_task_id, check.check_id, task.source_version)
            if prior is None or prior.status is CheckStatus.NOT_RUN:
                missing.append(check.check_id)
        if missing:
            raise ValueError(f"行为测试必须逐项绑定 check_id，以下检查缺少 GeneratedTest 条目：{missing}。不要把多个检查的测试全部放在第一个 check_id 下。")

    # ---- helpers -----------------------------------------------------------
    def _persist_model_review(
        self,
        context: AgentContext,
        task: TaskEnvelope,
        contract,
        checks: list[CheckSpec],
        final: ReviewerFinal,
        evidence: list[str],
    ) -> list[CheckResult]:
        known = {c.check_id: c for c in model_review_checks(checks)}
        results: list[CheckResult] = []
        for outcome in final.model_review_results:
            if outcome.check_id not in known:
                continue
            scope_evidence = context.artifacts.save_json(
                root_task_id=task.root_task_id,
                artifact_type=ArtifactType.EVIDENCE,
                data={
                    "check_id": outcome.check_id,
                    "method": "model_review",
                    "read_scope": outcome.read_scope,
                    "evidence_positions": outcome.evidence_positions,
                    "reason": outcome.reason,
                    "judgement": "model",
                },
                producer_attempt_id=task.attempt_id,
                source_version=task.source_version,
                name=f"model-review-{outcome.check_id}",
            )
            evidence.append(scope_evidence.artifact_id)
            result = CheckResult(
                check_id=outcome.check_id,
                contract_version=contract.contract_version,
                source_version=task.source_version,
                producer_attempt_id=task.attempt_id,
                method=CheckMethod.MODEL_REVIEW,
                status=_MODEL_STATUS[outcome.status],
                evidence_refs=[scope_evidence.artifact_id],
                reason=outcome.reason,
            )
            context.repos.check_results.insert(result, task.root_task_id)
            results.append(result)

        if results:
            bundle = context.artifacts.save_json(
                root_task_id=task.root_task_id,
                artifact_type=ArtifactType.CHECK_RESULT,
                data={
                    "source_version": task.source_version,
                    "contract_version": contract.contract_version,
                    "method": "model_review",
                    "results": [r.model_dump(mode="json") for r in results],
                },
                producer_attempt_id=task.attempt_id,
                source_version=task.source_version,
                name="model-review-results",
            )
            evidence.append(bundle.artifact_id)
        return results

    @staticmethod
    def _summary(final: ReviewerFinal, passed: bool | None, findings: int, coverage: int) -> str:
        verdict = {True: "通过", False: "未通过", None: "证据不足"}[passed]
        return (
            f"审查结论 {verdict}；问题 {findings} 条；覆盖 {coverage} 项；"
            f"{final.summary}"
        )


def _category(value: object) -> ErrorCategory:
    try:
        return ErrorCategory(str(value))
    except ValueError:
        return ErrorCategory.TOOL_ERROR
