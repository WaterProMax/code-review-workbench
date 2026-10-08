"""``submit_patch``: turn a fixer's proposal into a verifiable Patch artifact.

The patch is *not* applied here — it is only registered as an immutable artifact.
Application is a separate, parent-controlled step (§1.2: no "patch agent").
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, model_validator

from app.registry.tools import ToolContext, ToolError, ToolResult
from app.schemas.artifacts import Patch, StructuredEdit
from app.schemas.enums import ArtifactType, PatchFormat
from app.services.tools.common import require_path_in_scope
from app.services.workspace import is_protected_test_path, parse_unified_diff


class SubmitPatchArgs(BaseModel):
    base_version: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    target_finding_ids: list[str] = Field(default_factory=list)
    format: PatchFormat = PatchFormat.UNIFIED_DIFF
    diff_text: str | None = None
    edits: list[StructuredEdit] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "SubmitPatchArgs":
        if self.format is PatchFormat.UNIFIED_DIFF and not self.diff_text:
            raise ValueError("unified_diff 补丁必须提供 diff_text")
        if self.format is PatchFormat.STRUCTURED_EDITS and not self.edits:
            raise ValueError("structured_edits 补丁必须提供至少一个 edit")
        return self


async def submit_patch(ctx: ToolContext, args: SubmitPatchArgs) -> ToolResult:
    if args.base_version != ctx.source_version:
        raise ToolError(
            "PATCH_STALE_BASE",
            f"补丁必须针对当前版本 {ctx.source_version}，收到 {args.base_version}",
        )

    # validate format and scope before persisting anything
    try:
        patch = Patch(
            patch_id="pending",
            root_task_id=ctx.root_task_id,
            producer_attempt_id=ctx.attempt_id or "unknown",
            base_version=args.base_version,
            target_finding_ids=args.target_finding_ids,
            format=args.format,
            diff_text=args.diff_text,
            edits=args.edits,
            rationale=args.rationale,
        )
    except Exception as exc:  # noqa: BLE001 - reported to the agent as bad input
        raise ToolError("PATCH_INVALID", f"补丁结构不合法：{exc}") from exc

    paths = _target_paths(patch)
    for path in paths:
        require_path_in_scope(ctx, path)
    protected = sorted(p for p in paths if is_protected_test_path(p))
    if protected:
        raise ToolError(
            "PATCH_PROTECTED_TEST",
            f"候选补丁不得修改已有验收测试或测试发现配置：{protected}；"
            "确需改变验收测试请新建任务",
        )

    patch = patch.model_copy(update={"patch_id": f"P-{_short(ctx)}"})
    artifact = ctx.artifacts.save_json(
        root_task_id=ctx.root_task_id,
        artifact_type=ArtifactType.PATCH,
        data=patch.model_dump(mode="json"),
        producer_attempt_id=ctx.attempt_id,
        source_version=ctx.source_version,
        name=f"patch-{patch.patch_id}",
        metadata={
            "patch_id": patch.patch_id,
            "base_version": patch.base_version,
            "target_files": sorted(paths),
            "target_finding_ids": patch.target_finding_ids,
        },
    )
    return ToolResult.success(
        f"已提交候选补丁 {patch.patch_id}（{', '.join(sorted(paths))}）",
        {
            "patch_id": patch.patch_id,
            "artifact_id": artifact.artifact_id,
            "base_version": patch.base_version,
            "target_files": sorted(paths),
            "fingerprint": patch.fingerprint(),
        },
        artifact_refs=[artifact.artifact_id],
    )


def _target_paths(patch: Patch) -> set[str]:
    if patch.format is PatchFormat.UNIFIED_DIFF and patch.diff_text:
        return {fp.path for fp in parse_unified_diff(patch.diff_text)}
    return {e.file_path for e in patch.edits}


def _short(ctx: ToolContext) -> str:
    return uuid.uuid4().hex[:10]
