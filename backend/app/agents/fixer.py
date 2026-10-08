"""Fixer: proposes a candidate patch for the current version.

The fixer never applies its own patch and never claims the issue is fixed —
``passed`` stays null and the parent decides whether to apply and verify it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.agents.base import AgentContext, AgentExecutionError, BaseAgent
from app.schemas.agents import AgentSpec
from app.schemas.artifacts import StructuredEdit
from app.schemas.enums import ErrorCategory, PatchFormat, ResultStatus
from app.schemas.results import TaskResult
from app.schemas.tasks import TaskEnvelope


class FixerFinal(BaseModel):
    summary: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    format: PatchFormat = PatchFormat.UNIFIED_DIFF
    diff_text: str | None = None
    edits: list[StructuredEdit] = Field(default_factory=list)
    target_finding_ids: list[str] = Field(default_factory=list)


class FixerAgent(BaseAgent):
    def __init__(self, spec: AgentSpec, prompt: str) -> None:
        super().__init__(spec, prompt, FixerFinal)

    async def run(
        self, task: TaskEnvelope, context: AgentContext, started_at: datetime
    ) -> TaskResult:
        tool_ctx = context.tool_context(task)
        findings = self.load_findings(task, context)
        previous_patch = task.input_refs.previous_patch
        verification = task.input_refs.verification

        previous_context: dict = {}
        if previous_patch:
            try:
                previous_context["previous_patch"] = context.artifacts.read_json(previous_patch)
            except Exception:  # noqa: BLE001 - optional context
                pass
        if verification:
            try:
                previous_context["previous_verification"] = context.artifacts.read_json(verification)
            except Exception:  # noqa: BLE001 - optional context
                pass

        final, _ = await self.run_tool_loop(
            task,
            context,
            tool_ctx,
            opening=(
                "请针对当前版本生成候选补丁。先读取相关文件确认问题位置，"
                "再用 submit_patch 提交补丁；提交成功后给出最终说明。"
            ),
            extra_context={
                "findings": [f.model_dump(mode="json") for f in findings],
                "required_finding_ids": [f.finding_id for f in findings if f.required_for_goal],
                **previous_context,
            },
        )
        assert isinstance(final, FixerFinal)

        if not final.diff_text and not final.edits:
            raise AgentExecutionError(
                "PATCH_EMPTY",
                "修复 Agent 没有给出任何补丁内容",
                ErrorCategory.INVALID_RESULT,
            )

        result = await context.tools.execute(
            "submit_patch",
            tool_ctx,
            base_version=task.source_version,
            rationale=final.rationale,
            target_finding_ids=final.target_finding_ids
            or [f.finding_id for f in findings if f.required_for_goal],
            format=final.format,
            diff_text=final.diff_text,
            edits=[e.model_dump(mode="json") for e in final.edits],
        )
        if not result.ok or not result.data:
            raise AgentExecutionError(
                result.error.code if result.error else "PATCH_INVALID",
                f"补丁未被接受：{result.summary}",
                ErrorCategory.TOOL_ERROR,
            )

        patch_id = result.data["patch_id"]
        artifact_id = result.data["artifact_id"]
        return self.build_result(
            task,
            status=ResultStatus.COMPLETED,
            passed=None,
            summary=f"已生成候选补丁 {patch_id}，等待应用与验证。{final.summary}",
            started_at=started_at,
            result_refs={"patch": artifact_id},
            evidence_refs=[artifact_id],
        )
