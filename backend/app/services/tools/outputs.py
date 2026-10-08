"""Tools that persist a role's structured output as immutable artifacts.

Specialized data (findings, verification reports) never sits in the shared state:
it is validated here against its schema and stored as a referencable artifact,
which is what the TaskResult points at (§7.2, §7.3).
"""

from __future__ import annotations

import uuid
import hashlib
import json

from pydantic import BaseModel, Field

from app.registry.tools import ToolContext, ToolResult
from app.schemas.enums import ArtifactType, FindingSeverity, FindingStatus
from app.schemas.results import CheckResult, Finding, VerificationReport


class FindingDraft(BaseModel):
    file_path: str = Field(min_length=1)
    rule: str = Field(min_length=1)
    message: str = Field(min_length=1)
    check_id: str | None = None
    goal_ref: str | None = None
    required_for_goal: bool = False
    line: int | None = None
    symbol: str | None = None
    severity: FindingSeverity = FindingSeverity.WARNING
    evidence_refs: list[str] = Field(default_factory=list)


class SubmitFindingsArgs(BaseModel):
    findings: list[FindingDraft] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    not_checked: list[str] = Field(default_factory=list)


class SubmitVerificationArgs(BaseModel):
    target_finding_ids: list[str] = Field(default_factory=list)
    check_results: list[CheckResult] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    not_run: list[str] = Field(default_factory=list)
    passed: bool | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    notes: str | None = None


async def submit_findings(ctx: ToolContext, args: SubmitFindingsArgs) -> ToolResult:
    created: list[Finding] = []
    for draft in args.findings:
        identity = [ctx.root_task_id, ctx.source_version, ctx.attempt_id,
                    draft.check_id, draft.file_path, draft.line, draft.symbol, draft.rule]
        finding_id = "F-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:20]
        finding = Finding(
            finding_id=finding_id,
            root_task_id=ctx.root_task_id,
            source_version=ctx.source_version,
            producer_attempt_id=ctx.attempt_id or "unknown",
            goal_ref=draft.goal_ref,
            required_for_goal=draft.required_for_goal,
            check_id=draft.check_id,
            file_path=draft.file_path,
            line=draft.line,
            symbol=draft.symbol,
            rule=draft.rule,
            severity=draft.severity,
            message=draft.message,
            evidence_refs=draft.evidence_refs,
            status=FindingStatus.OPEN,
        )
        ctx.repos.findings.upsert(finding)
        created.append(finding)

    artifact = ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.FINDING,
        data={
            "source_version": ctx.source_version,
            "findings": [f.model_dump(mode="json") for f in created],
            "coverage": args.coverage,
            "not_checked": args.not_checked,
        },
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=f"findings-{uuid.uuid4().hex[:8]}",
        metadata={"count": len(created), "required_count": sum(f.required_for_goal for f in created)},
    )
    return ToolResult.success(
        f"已保存 {len(created)} 条问题，覆盖 {len(args.coverage)} 项，未检查 {len(args.not_checked)} 项",
        {
            "artifact_id": artifact.artifact_id,
            "finding_ids": [f.finding_id for f in created],
            "coverage": args.coverage,
            "not_checked": args.not_checked,
        },
        artifact_refs=[artifact.artifact_id],
    )


async def submit_verification(ctx: ToolContext, args: SubmitVerificationArgs) -> ToolResult:
    if not ctx.contract_version:
        return ToolResult(
            ok=False,
            summary="缺少合同版本，无法保存验证报告",
            error=_error("MISSING_CONTRACT_VERSION", "本次尝试没有绑定合同版本"),
        )
    try:
        report = VerificationReport(
            verification_id=f"V-{uuid.uuid4().hex[:10]}",
            root_task_id=ctx.root_task_id,
            producer_attempt_id=ctx.attempt_id or "unknown",
            source_version=ctx.source_version,
            contract_version=ctx.contract_version,
            target_finding_ids=args.target_finding_ids,
            check_results=args.check_results,
            coverage=args.coverage,
            not_run=args.not_run,
            passed=args.passed,
            evidence_refs=args.evidence_refs,
            notes=args.notes,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the agent
        return ToolResult(
            ok=False,
            summary=f"验证报告不符合契约：{exc}",
            error=_error("VERIFICATION_INVALID", str(exc)),
        )

    for result in report.check_results:
        ctx.repos.check_results.insert(result, ctx.root_task_id)
    artifact = ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.VERIFICATION_REPORT,
        data=report.model_dump(mode="json"),
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=f"verification-{report.verification_id}",
        metadata={"passed": report.passed, "checks": len(report.check_results)},
    )
    ctx.repos.verifications.insert(report, artifact_id=artifact.artifact_id)
    return ToolResult.success(
        f"已保存验证报告 {report.verification_id}（passed={report.passed}）",
        {
            "verification_id": report.verification_id,
            "artifact_id": artifact.artifact_id,
            "passed": report.passed,
            "not_run": report.not_run,
        },
        artifact_refs=[artifact.artifact_id],
    )


def _error(code: str, message: str):  # type: ignore[no-untyped-def]
    from app.schemas.common import ErrorInfo
    from app.schemas.enums import ErrorCategory

    return ErrorInfo(code=code, category=ErrorCategory.TOOL_ERROR, message=message, recoverable=False)
