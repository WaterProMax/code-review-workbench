"""Verifier: executes the agreed checks against the applied version.

Generated tests are written to an isolated run directory through the evidence
tool (never into the verified snapshot), proven against the pre-patch version,
then run on the current version together with the project's existing tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from app.agents.base import (
    AgentContext,
    BaseAgent,
    deterministic_checks,
    required_checks,
)
from app.schemas.agents import AgentSpec
from app.schemas.artifacts import GeneratedTest, GeneratedTestSuite
from app.schemas.common import ErrorInfo
from app.schemas.enums import CheckStatus, ErrorCategory, ResultStatus, FindingStatus
from app.schemas.results import CheckResult
from app.schemas.results import TaskResult
from app.schemas.tasks import TaskEnvelope


class VerifierFinal(BaseModel):
    summary: str = Field(min_length=1)
    generated_tests: list[GeneratedTest] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    not_run: list[str] = Field(default_factory=list)
    notes: str | None = None


class VerifierAgent(BaseAgent):
    def __init__(self, spec: AgentSpec, prompt: str) -> None:
        super().__init__(spec, prompt, VerifierFinal)

    async def run(
        self, task: TaskEnvelope, context: AgentContext, started_at: datetime
    ) -> TaskResult:
        tool_ctx = context.tool_context(task)
        contract = self.load_contract(task, context)
        checks = self.checks_for(contract, task)
        required = required_checks(contract, task.task_kind)
        det = deterministic_checks(checks)
        findings = self.load_findings(task, context)
        # The latest findings bundle can omit older still-open findings (for
        # example after a retry). Verification must address the whole ledger.
        by_id = {f.finding_id: f for f in findings}
        for finding in context.repos.findings.list_by_root(task.root_task_id):
            if finding.required_for_goal and finding.status is FindingStatus.OPEN:
                by_id[finding.finding_id] = finding
        findings = list(by_id.values())
        baseline_version = self._baseline_version(task, context)
        provided = _provided_tests(task, context)

        final, _ = await self.run_tool_loop(
            task,
            context,
            tool_ctx,
            opening=(
                "请验证修改后的版本。对行为检查，先编写最小复现测试（保存为 test_artifact），"
                "再据此说明预期值来源；执行器会同时在基础版本上运行同一测试。"
                "直接在 final.generated_tests 中返回测试内容；执行器会自动保存、运行并提交报告，"
                "不必手工调用 save_evidence、run_checks、submit_verification。已有 provided_tests 应优先复用。"
            ),
            extra_context={
                "required_checks": [c.model_dump(mode="json") for c in required],
                "all_checks": [c.model_dump(mode="json") for c in checks],
                "target_findings": [f.model_dump(mode="json") for f in findings],
                "baseline_version": baseline_version,
                "test_execution_layout": "pytest 的 cwd 和 PYTHONPATH 均为快照根目录。直接 import 被测模块；不要通过测试 __file__ 的兄弟路径加载源码。",
                "provided_tests": provided.describe() if provided else None,
            },
        )
        assert isinstance(final, VerifierFinal)

        tool_ctx.steps.limit += len(final.generated_tests) + 2

        # tests handed over by another role (P10 extension) are an input dependency:
        # they run alongside the ones this attempt writes itself
        generated_by_check: dict[str, list[str]] = dict(provided.by_check) if provided else {}
        evidence: list[str] = list(provided.artifact_refs) if provided else []
        for test in final.generated_tests:
            saved = await context.tools.execute(
                "save_evidence",
                tool_ctx,
                name=f"gen-{test.check_id}-{_safe(test.filename)}",
                content=test.content,
                kind="test_artifact",
                metadata={
                    "check_id": test.check_id,
                    "filename": test.filename,
                    "expected_source": test.expected_source,
                },
            )
            if saved.ok and saved.data:
                generated_by_check.setdefault(test.check_id, []).append(saved.data["artifact_id"])
                evidence.append(saved.data["artifact_id"])

        repair_check_ids = None
        required_findings = [f for f in findings if f.required_for_goal]
        if required_findings and all(f.check_id for f in required_findings):
            targets = {f.check_id for f in required_findings}
            if baseline_version:
                targets.update(r.check_id for r in context.repos.check_results.list_for_version(
                    task.root_task_id, baseline_version) if r.status is CheckStatus.FAILED)
            repair_check_ids = sorted(targets)

        check_result = await context.tools.execute(
            "run_checks",
            tool_ctx,
            checks=det,
            contract_version=contract.contract_version,
            generated_tests=generated_by_check,
            include_existing_tests=True,
            baseline_version=baseline_version,
            repair_check_ids=repair_check_ids,
        )
        results: list[CheckResult] = []
        faults: list[dict] = []
        if check_result.ok and check_result.data:
            results = [CheckResult.model_validate(r) for r in check_result.data["check_results"]]
            evidence.extend(check_result.artifact_refs)
            faults = list(check_result.faults)
        else:
            faults = check_result.faults or [
                {
                    "code": check_result.error.code if check_result.error else "CHECK_TOOL_ERROR",
                    "category": "tool_error",
                    "message": check_result.summary,
                    "recoverable": True,
                }
            ]

        required_ids = {c.check_id for c in required}
        relevant_faults = [f for f in faults if f.get("check_id") in required_ids or not f.get("check_id")]
        passed = _verdict(required, results)

        verified = await context.tools.execute(
            "submit_verification",
            tool_ctx,
            target_finding_ids=[f.finding_id for f in findings if f.required_for_goal],
            check_results=[r.model_dump(mode="json") for r in results],
            coverage=final.coverage,
            not_run=final.not_run + [c.check_id for c in required if c.check_id not in {r.check_id for r in results}],
            passed=passed,
            evidence_refs=evidence + check_result.artifact_refs,
            notes=final.notes,
        )
        result_refs: dict[str, str] = {}
        if verified.ok and verified.data:
            result_refs["verification"] = verified.data["artifact_id"]
            evidence.append(verified.data["artifact_id"])
        elif not verified.ok:
            relevant_faults.append(
                {
                    "code": verified.error.code if verified.error else "VERIFICATION_INVALID",
                    "category": "tool_error",
                    "message": verified.summary,
                    "recoverable": True,
                }
            )

        if relevant_faults:
            first = relevant_faults[0]
            return self.build_result(
                task,
                status=ResultStatus.FAILED,
                passed=None,
                summary=f"验证执行故障：{first.get('message', '未知')}；本结论不成立。{final.summary}",
                started_at=started_at,
                result_refs=result_refs,
                evidence_refs=evidence,
                error=ErrorInfo(
                    code=str(first.get("code", "CHECK_TOOL_ERROR")),
                    category=_category(first.get("category")),
                    message=str(first.get("message", "验证执行故障")),
                    recoverable=bool(first.get("recoverable", True)),
                ),
            )

        return self.build_result(
            task,
            status=ResultStatus.COMPLETED,
            passed=passed,
            summary=_summary(passed, results, final.summary),
            started_at=started_at,
            result_refs=result_refs,
            evidence_refs=evidence,
        )

    @staticmethod
    def _baseline_version(task: TaskEnvelope, context: AgentContext) -> str | None:
        ref = task.input_refs.patch_application
        if not ref:
            return None
        try:
            application = context.artifacts.read_json(ref)
        except Exception:  # noqa: BLE001 - baseline proof is optional
            return None
        return application.get("base_version")


@dataclass(frozen=True)
class ProvidedTests:
    """A test bundle produced by another role and dispatched as verifier input."""

    artifact_refs: tuple[str, ...]
    by_check: dict[str, list[str]]
    test_count: int

    def describe(self) -> dict:
        return {
            "artifact_refs": list(self.artifact_refs),
            "by_check": {key: list(value) for key, value in self.by_check.items()},
            "test_count": self.test_count,
        }


def _provided_tests(task: TaskEnvelope, context: AgentContext) -> ProvidedTests | None:
    """Read ``input_refs.generated_tests``; a missing or broken ref is not fatal."""
    ref = task.input_refs.generated_tests
    if not ref:
        return None
    try:
        payload = context.artifacts.read_json(ref)
        suite = GeneratedTestSuite.model_validate(payload)
    except Exception:  # noqa: BLE001 - an unreadable bundle is simply not used
        return None
    by_check: dict[str, list[str]] = {check_id: [ref] for check_id in suite.by_check()}
    return ProvidedTests(
        artifact_refs=(ref,), by_check=by_check, test_count=len(suite.tests)
    )


def _verdict(required, results: list[CheckResult]) -> bool | None:
    by_id = {r.check_id: r for r in results}
    statuses = [by_id.get(c.check_id) for c in required if c.required]
    if any(s is not None and s.status is CheckStatus.FAILED for s in statuses):
        return False
    if any(s is None or s.status is not CheckStatus.PASSED for s in statuses):
        return None
    return True if statuses else None


def _summary(passed: bool | None, results: list[CheckResult], note: str) -> str:
    verdict = {True: "通过", False: "未通过", None: "证据不足"}[passed]
    detail = ", ".join(f"{r.check_id}={r.status.value}" for r in results) or "无检查结果"
    return f"验证结论 {verdict}（{detail}）。{note}"


def _category(value: object) -> ErrorCategory:
    try:
        return ErrorCategory(str(value))
    except ValueError:
        return ErrorCategory.TOOL_ERROR


def _safe(name: str) -> str:
    return "".join(ch for ch in name if ch.isalnum() or ch in "-_.")[:40] or "test"
