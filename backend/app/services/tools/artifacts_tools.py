"""Tools for reading existing artifacts and saving new evidence (§7.2)."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.registry.tools import ToolContext, ToolResult
from app.schemas.artifacts import GeneratedTest, GeneratedTestSuite
from app.schemas.enums import ArtifactType
from app.services.artifacts import ArtifactError


class ReadArtifactArgs(BaseModel):
    artifact_id: str = Field(min_length=1)
    max_bytes: int = Field(default=20000, ge=1, le=200000)


class SaveEvidenceArgs(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    content: str
    kind: Literal["evidence", "test_artifact", "diff", "model_output"] = "evidence"
    metadata: dict = Field(default_factory=dict)


_KIND_TO_TYPE = {
    "evidence": ArtifactType.EVIDENCE,
    "test_artifact": ArtifactType.TEST_ARTIFACT,
    "diff": ArtifactType.DIFF,
    "model_output": ArtifactType.MODEL_OUTPUT,
}


async def read_artifact(ctx: ToolContext, args: ReadArtifactArgs) -> ToolResult:
    try:
        artifact = ctx.artifacts.verify_ownership(args.artifact_id, ctx.root_task_id)
        blob = ctx.artifacts.read_bytes(args.artifact_id)
    except ArtifactError as exc:
        return ToolResult(
            ok=False,
            summary=f"无法读取产物：{exc}",
            error=_artifact_error(str(exc)),
        )
    text = blob[: args.max_bytes].decode("utf-8", errors="replace")
    payload = None
    if artifact.artifact_type.value in {
        ArtifactType.FINDING.value,
        ArtifactType.PATCH.value,
        ArtifactType.VERIFICATION_REPORT.value,
        ArtifactType.ACCEPTANCE_CONTRACT.value,
        ArtifactType.CHECK_RESULT.value,
        ArtifactType.TEST_ARTIFACT.value,
    }:
        try:
            payload = ctx.artifacts.read_json(args.artifact_id)
        except Exception:  # noqa: BLE001 - non-JSON evidence is still readable as text
            payload = None
    return ToolResult.success(
        f"读取产物 {args.artifact_id}（{artifact.artifact_type.value}）",
        {
            "artifact_id": args.artifact_id,
            "artifact_type": artifact.artifact_type.value,
            "source_version": artifact.source_version,
            "content": text,
            "json": payload,
        },
        artifact_refs=[args.artifact_id],
    )


async def save_evidence(ctx: ToolContext, args: SaveEvidenceArgs) -> ToolResult:
    artifact_type = _KIND_TO_TYPE[args.kind]
    artifact = ctx.artifacts.save_text(
        root_task_id=ctx.root_task_id,
        artifact_type=artifact_type,
        text=args.content,
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=args.name,
        metadata=args.metadata,
    )
    return ToolResult.success(
        f"保存{args.kind}产物 {artifact.artifact_id}",
        {"artifact_id": artifact.artifact_id, "size": artifact.size, "hash": artifact.hash},
        artifact_refs=[artifact.artifact_id],
    )


class SubmitGeneratedTestsArgs(BaseModel):
    tests: list[GeneratedTest] = Field(min_length=1)
    target_finding_ids: list[str] = Field(default_factory=list)
    notes: str | None = None


async def submit_generated_tests(ctx: ToolContext, args: SubmitGeneratedTestsArgs) -> ToolResult:
    """Persist a test bundle as one artifact (never into the frozen snapshot).

    The bundle is a real input dependency: the verifier receives its id through
    ``input_refs.generated_tests`` and the check tool expands it into test files.
    """
    suite = GeneratedTestSuite(
        producer_attempt_id=ctx.attempt_id or "unknown",
        source_version=ctx.source_version,
        target_finding_ids=args.target_finding_ids,
        tests=args.tests,
        notes=args.notes,
    )
    artifact = ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.TEST_ARTIFACT,
        data=suite.model_dump(mode="json"),
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=f"generated-tests-{uuid.uuid4().hex[:8]}",
        metadata={
            "bundle": True,
            "count": len(args.tests),
            "check_ids": sorted({t.check_id for t in args.tests}),
        },
    )
    return ToolResult.success(
        f"已保存 {len(args.tests)} 个生成测试（{artifact.artifact_id}）",
        {
            "artifact_id": artifact.artifact_id,
            "check_ids": sorted({t.check_id for t in args.tests}),
            "count": len(args.tests),
        },
        artifact_refs=[artifact.artifact_id],
    )


def _artifact_error(message: str):  # type: ignore[no-untyped-def]
    from app.schemas.common import ErrorInfo
    from app.schemas.enums import ErrorCategory

    return ErrorInfo(
        code="ARTIFACT_NOT_ACCESSIBLE",
        category=ErrorCategory.TOOL_ERROR,
        message=message,
        recoverable=False,
    )
