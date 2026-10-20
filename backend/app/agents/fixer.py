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

        opening = (
            "请针对当前版本生成候选补丁。先读取相关文件确认问题位置，"
            "然后在 final 里给出补丁内容。不要调用 submit_patch，"
            "执行器会用 final 里的 diff_text 或 edits 提交唯一的一份补丁。"
            "不得通过修改 docstring、注释或规格说明来重新定义缺陷。"
        )
        extra_context = {
            "findings": [f.model_dump(mode="json") for f in findings],
            "required_finding_ids": [f.finding_id for f in findings if f.required_for_goal],
            **previous_context,
        }
        result = None
        final = None
        # A dry-run rejection stays inside this attempt so the model can correct
        # the diff. It does not open another repair round.
        for _ in range(3):
            final, _ = await self.run_tool_loop(
                task,
                context,
                tool_ctx,
                opening=opening,
                extra_context=extra_context,
            )
            assert isinstance(final, FixerFinal)
            blocked = spec_rewrite_rejection(final)
            if blocked:
                raise AgentExecutionError(
                    "PATCH_SPEC_REWRITE",
                    blocked,
                    ErrorCategory.INVALID_PATCH,
                )
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
            if result.ok and result.data:
                break
            code = result.error.code if result.error else "PATCH_INVALID"
            if code not in {"PATCH_INVALID", "PATCH_CONFLICT"}:
                raise AgentExecutionError(
                    code,
                    f"补丁未被接受：{result.summary}",
                    ErrorCategory.TOOL_ERROR,
                )
            opening = (
                f"上一次补丁未被接受（{code}）：{result.summary}\n"
                "请根据当前文件内容修正 diff 后重新给出 final。"
                "这次失败不消耗新的修复轮次。"
            )
        else:
            raise AgentExecutionError(
                result.error.code if result and result.error else "PATCH_INVALID",
                f"补丁未被接受：{result.summary if result else ''}",
                ErrorCategory.INVALID_PATCH,
            )

        assert result is not None and result.data is not None and final is not None
        patch_id = result.data["patch_id"]
        artifact_id = result.data["artifact_id"]
        summary = f"已生成候选补丁 {patch_id}，等待应用与验证。{final.summary}"
        return self.build_result(
            task,
            status=ResultStatus.COMPLETED,
            passed=None,
            summary=summary,
            started_at=started_at,
            result_refs={"patch": artifact_id},
            evidence_refs=[artifact_id],
        )


def spec_rewrite_rejection(final: FixerFinal) -> str | None:
    """Reject a patch that only rewrites comments or docstrings.

    A change that also edits executable code is kept. Whether the original
    defect still reproduces is decided by the frozen baseline checks in
    ``app.services.tools.checks``, not by this scan.
    """
    chunks: list[str] = []
    if final.diff_text:
        chunks.append(final.diff_text)
    for edit in final.edits:
        removed = "\n".join(f"-{line}" for line in edit.find.splitlines())
        added = "\n".join(f"+{line}" for line in edit.replace.splitlines())
        chunks.append(f"{removed}\n{added}")
    if not chunks or not any(_hunk_has_change(chunk) for chunk in chunks):
        return None
    if all(_hunk_is_spec_only(chunk) for chunk in chunks):
        return "补丁只改写了注释或文档字符串，不能用改规格代替修代码"
    return None


def _hunk_has_change(diff_text: str) -> bool:
    for line in diff_text.splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            if line[1:].strip():
                return True
    return False


def _hunk_is_spec_only(diff_text: str) -> bool:
    """True when every changed line is a comment or sits inside a docstring."""
    in_doc = False
    quote = ""
    changed_code = False
    changed_any = False
    for raw in diff_text.splitlines():
        if raw.startswith(("+++", "---", "@@", "\\")):
            continue
        if not raw:
            continue
        marker = raw[0]
        text = raw[1:] if marker in "+- " else raw
        stripped = text.strip()
        is_changed = marker in "+-"
        if not in_doc:
            opener = _docstring_quote(stripped)
            if opener is not None:
                if is_changed and stripped:
                    changed_any = True
                if stripped.count(opener) == 1:
                    in_doc = True
                    quote = opener
                continue
            if stripped.startswith("#"):
                if is_changed and stripped:
                    changed_any = True
                continue
            if is_changed and stripped:
                changed_any = True
                changed_code = True
            continue
        if is_changed and stripped:
            changed_any = True
        if quote and quote in stripped:
            in_doc = False
            quote = ""
    return changed_any and not changed_code


def _docstring_quote(stripped: str) -> str | None:
    triple_double = stripped.find('"""')
    triple_single = stripped.find("'''")
    if triple_double < 0 and triple_single < 0:
        return None
    if triple_double < 0:
        return "'''"
    if triple_single < 0:
        return '"""'
    return '"""' if triple_double < triple_single else "'''"
