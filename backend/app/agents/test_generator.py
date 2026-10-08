"""Test-generation role (P10 extension sample, §14.2).

Its only product is a test bundle written through the authorized
``submit_generated_tests`` tool. It never edits the frozen snapshot and it does
not decide any verdict: the bundle becomes a verifier *input*, so the base
verification path is unchanged and the new artifact is genuinely consumed.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, BaseAgent
from app.schemas.agents import AgentSpec
from app.schemas.artifacts import GeneratedTest
from app.schemas.common import ErrorInfo
from app.schemas.enums import ErrorCategory, ResultStatus
from app.schemas.results import TaskResult
from app.schemas.tasks import TaskEnvelope


class TestGeneratorFinal(BaseModel):
    summary: str = Field(min_length=1)
    tests: list[GeneratedTest] = Field(default_factory=list)
    target_finding_ids: list[str] = Field(default_factory=list)
    notes: str | None = None


class TestGeneratorAgent(BaseAgent):
    def __init__(self, spec: AgentSpec, prompt: str) -> None:
        super().__init__(spec, prompt, TestGeneratorFinal)

    async def run(
        self, task: TaskEnvelope, context: AgentContext, started_at: datetime
    ) -> TaskResult:
        tool_ctx = context.tool_context(task)
        contract = self.load_contract(task, context)
        findings = self.load_findings(task, context)
        checks = [
            c
            for c in contract.checks
            if c.required and any(f.check_id == c.check_id for f in findings)
        ] or [c for c in contract.checks if c.required]

        final, _ = await self.run_tool_loop(
            task,
            context,
            tool_ctx,
            opening=(
                "请为下面这些检查项生成最小复现测试，用 submit_generated_tests 保存。"
                "每个测试只针对一个检查项，必须能在未修复的版本上失败；不要修改被检查的源码。"
            ),
            extra_context={
                "required_checks": [c.model_dump(mode="json") for c in checks],
                "target_findings": [f.model_dump(mode="json") for f in findings],
                "source_version": task.source_version,
            },
        )
        assert isinstance(final, TestGeneratorFinal)

        evidence: list[str] = []
        result_refs: dict[str, str] = {}
        if final.tests:
            submitted = await context.tools.execute(
                "submit_generated_tests",
                tool_ctx,
                tests=[test.model_dump(mode="json") for test in final.tests],
                target_finding_ids=final.target_finding_ids,
                notes=final.notes,
            )
            if submitted.ok and submitted.data:
                artifact_id = submitted.data["artifact_id"]
                result_refs["test_artifact"] = artifact_id
                evidence.append(artifact_id)
                return self.build_result(
                    task,
                    status=ResultStatus.COMPLETED,
                    # the generator decides no verdict; the verifier does
                    passed=None,
                    summary=(
                        f"已生成 {submitted.data['count']} 个测试（检查项 "
                        f"{submitted.data['check_ids']}）。{final.summary}"
                    ),
                    started_at=started_at,
                    result_refs=result_refs,
                    evidence_refs=evidence,
                )
            return self.build_result(
                task,
                status=ResultStatus.FAILED,
                passed=None,
                summary=f"测试包保存失败：{submitted.summary}",
                started_at=started_at,
                error=submitted.error
                or ErrorInfo(
                    code="GENERATED_TESTS_NOT_SAVED",
                    category=ErrorCategory.TOOL_ERROR,
                    message=submitted.summary,
                    recoverable=True,
                ),
            )

        return self.build_result(
            task,
            status=ResultStatus.FAILED,
            passed=None,
            summary=f"没有产出任何测试：{final.summary}",
            started_at=started_at,
            error=ErrorInfo(
                code="NO_GENERATED_TESTS",
                category=ErrorCategory.INVALID_RESULT,
                message="测试生成角色未产出测试",
                recoverable=False,
            ),
        )
