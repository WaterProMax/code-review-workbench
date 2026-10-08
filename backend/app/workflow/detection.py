"""Deterministic detection over persisted evidence (§2.2, §9.1).

Detection produces the *facts* about whether the current version satisfies the
frozen contract. Those facts are recomputed from the database — check results,
findings, patch applications and the accepted reports — never taken from a claim
inside a child report. The parent model then interprets the facts and proposes
the next action.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Sequence

from app.schemas.artifacts import Patch, PatchApplication
from app.schemas.common import ErrorInfo
from app.schemas.enums import (
    ApplicablePhase,
    CheckStatus,
    DetectionCategory,
    ErrorCategory,
    FindingStatus,
    ResultStatus,
    TaskKind,
)
from app.schemas.results import (
    AcceptanceContract,
    CheckResult,
    DetectionResult,
    Finding,
    ProgressInfo,
    TaskResult,
)
from app.services.artifacts import ArtifactService
from app.storage.repositories import Repos, i2b, loads


def failure_signature(failed_check_ids: Sequence[str], finding_keys: Sequence[str]) -> str:
    """A stable signature of the current failing evidence set."""
    payload = json.dumps(
        {"checks": sorted(failed_check_ids), "findings": sorted(finding_keys)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sig-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def finding_key(finding: Finding) -> str:
    return f"{finding.rule}:{finding.file_path}:{finding.line or 0}:{finding.symbol or ''}"


@dataclass
class PhaseVerdict:
    """Required checks of one phase evaluated against the current version."""

    failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    open_required: list[Finding] = field(default_factory=list)

    @property
    def satisfied(self) -> bool:
        return not self.failed and not self.missing and not self.open_required


class Detector:
    def __init__(
        self,
        repos: Repos,
        artifacts: ArtifactService,
        verdict_kinds: frozenset[str] | None = None,
    ) -> None:
        self.repos = repos
        self.artifacts = artifacts
        # Task kinds whose reports carry a phase verdict. An extension kind can opt
        # out (P10): its report is still recorded and a *failure* is still an
        # execution fault, but its success is not mistaken for phase evidence.
        self.verdict_kinds = verdict_kinds or frozenset(
            {"review", "fix", "verify"}
        )

    # ---- evidence readers --------------------------------------------------
    def current_checks(self, root_task_id: str, source_version: str) -> dict[str, CheckResult]:
        latest: dict[str, CheckResult] = {}
        for result in self.repos.check_results.list_for_version(root_task_id, source_version):
            latest[result.check_id] = result
        return latest

    def open_required_findings(self, root_task_id: str) -> list[Finding]:
        """Required findings that are still open on their latest recorded state.

        A problem accepted for the user goal stays open until current-version
        recheck/verification evidence closes it; a fixer claim never resolves it.
        """
        seen: set[str] = set()
        out: list[Finding] = []
        for finding in self.repos.findings.list_by_root(root_task_id):
            if finding.finding_id in seen:
                continue
            seen.add(finding.finding_id)
            latest = self.repos.findings.latest(root_task_id, finding.finding_id)
            if latest is not None and latest.required_for_goal and latest.status is FindingStatus.OPEN:
                out.append(latest)
        return sorted(out, key=lambda f: f.finding_id)

    def evaluate_phase(
        self,
        *,
        root_task_id: str,
        contract: AcceptanceContract,
        source_version: str,
        phase: ApplicablePhase,
    ) -> PhaseVerdict:
        latest = self.current_checks(root_task_id, source_version)
        verdict = PhaseVerdict(open_required=self.open_required_findings(root_task_id))
        for spec in contract.required_checks(phase):
            result = latest.get(spec.check_id)
            if result is None:
                verdict.missing.append(spec.check_id)
            elif result.status is CheckStatus.FAILED:
                verdict.failed.append(spec.check_id)
            elif result.status is not CheckStatus.PASSED:
                verdict.missing.append(spec.check_id)
        return verdict

    def required_check_ids(
        self, contract: AcceptanceContract, *, patched: bool
    ) -> list[str]:
        phase = ApplicablePhase.POST_PATCH if patched else ApplicablePhase.INITIAL_REVIEW
        return [c.check_id for c in contract.required_checks(phase)]

    def resolve_required_findings(
        self, *, root_task_id: str, source_version: str
    ) -> list[str]:
        """Close required findings proven fixed on the *current* version.

        §5.3.5: only current-version recheck/verification evidence may close an
        accepted problem — never a fixer's claim, a changed summary or a lower
        severity. A passing verification that targets the finding, plus a passed
        current-version result for the finding's own check, is what closes it.
        """
        passing = [
            row
            for row in self.repos.verifications.list_by_root(root_task_id)
            if row["source_version"] == source_version and i2b(row["passed"]) is True
        ]
        if not passing:
            return []

        resolved: list[str] = []
        for finding in self.open_required_findings(root_task_id):
            if finding.source_version == source_version:
                continue
            for row in passing:
                if finding.finding_id not in loads(row["target_finding_ids"], []):
                    continue
                if not self._check_covered(root_task_id, finding, source_version):
                    continue
                refs = list(dict.fromkeys(loads(row["evidence_refs"], [])))
                if row["artifact_id"] and row["artifact_id"] not in refs:
                    refs.append(row["artifact_id"])
                if not refs:
                    continue
                if self.repos.findings.resolve(
                    finding.finding_id, finding.source_version, refs
                ):
                    resolved.append(finding.finding_id)
                break
        return resolved

    def _check_covered(
        self, root_task_id: str, finding: Finding, source_version: str
    ) -> bool:
        if not finding.check_id:
            return True
        result = self.repos.check_results.latest_for_check(
            root_task_id, finding.check_id, source_version
        )
        return result is not None and result.status is CheckStatus.PASSED

    def patch_fingerprint(self, patch_ref: str | None) -> str | None:
        if not patch_ref:
            return None
        try:
            return Patch.model_validate(self.artifacts.read_json(patch_ref)).fingerprint()
        except Exception:  # noqa: BLE001 - a broken patch artifact is not a verdict
            return None

    # ---- detection ---------------------------------------------------------
    def detect(
        self,
        *,
        root_task_id: str,
        contract: AcceptanceContract,
        source_version: str,
        patched: bool = False,
        reports: Sequence[TaskResult] = (),
        application: PatchApplication | None = None,
        application_error: ErrorInfo | None = None,
        controller_faults: Sequence[dict] = (),
        invalid_reason: str | None = None,
        previous_signature: str | None = None,
        previous_source_version: str | None = None,
        previous_patch_fingerprint: str | None = None,
    ) -> DetectionResult:
        reports = list(reports)
        facts: dict = {
            "source_version": source_version,
            "patched": patched,
            "reports": [_report_fact(r) for r in reports],
        }

        if invalid_reason:
            facts["invalid_reason"] = invalid_reason
            return DetectionResult(
                valid=False,
                category=DetectionCategory.INVALID_RESULT,
                invalid_reason=invalid_reason,
                facts=facts,
                suggestion="拒绝该回报，按有效执行状态恢复",
            )

        if controller_faults:
            faults = [dict(f) for f in controller_faults]
            facts["controller_faults"] = faults
            recoverable = all(f.get("recoverable", True) for f in faults)
            return self._build(
                DetectionCategory.EXECUTION_FAULT,
                facts,
                source_version=source_version,
                previous_source_version=previous_source_version,
                signature=None,
                previous_signature=previous_signature,
                patch_fingerprint=None,
                suggestion=(
                    "控制层判定尝试超时或失效：先处理可恢复原因，再重派该子任务"
                    if recoverable
                    else "不可自动恢复，进入待处理状态等待人工处理"
                ),
            )

        if application_error is not None:
            category = _fault_category_for_application(application_error)
            facts["application_error"] = application_error.model_dump(mode="json")
            return self._build(
                category,
                facts,
                source_version=source_version,
                previous_source_version=previous_source_version,
                signature=None,
                previous_signature=previous_signature,
                patch_fingerprint=None,
                suggestion=(
                    "补丁未能应用：携带匹配错误重新派发修复"
                    if category is DetectionCategory.CODE_DEFECT
                    else "补丁应用遇到执行故障：先处理可恢复原因，再决定重试或等待"
                ),
            )

        failed_reports = [r for r in reports if r.status is ResultStatus.FAILED]
        if failed_reports:
            recoverable = all(
                (r.error is None or r.error.recoverable) for r in failed_reports
            )
            facts["recoverable"] = recoverable
            facts["error_codes"] = [
                r.error.code for r in failed_reports if r.error is not None
            ]
            return self._build(
                DetectionCategory.EXECUTION_FAULT,
                facts,
                source_version=source_version,
                previous_source_version=previous_source_version,
                signature=None,
                previous_signature=previous_signature,
                patch_fingerprint=None,
                suggestion=(
                    "可有限重试失败的子任务"
                    if recoverable
                    else "不可自动恢复，进入待处理状态等待人工处理"
                ),
            )

        if application is not None and not reports:
            facts["application"] = application.model_dump(mode="json")
            return self._build(
                DetectionCategory.INSUFFICIENT_EVIDENCE,
                facts,
                source_version=source_version,
                previous_source_version=previous_source_version,
                signature=None,
                previous_signature=previous_signature,
                patch_fingerprint=None,
                suggestion=f"派发验证任务，绑定已应用版本 {application.result_version}",
            )

        kinds = {r.task_kind for r in reports if r.task_kind in self.verdict_kinds}
        if kinds and kinds <= {TaskKind.FIX.value}:
            return self._detect_fix(
                facts,
                reports,
                source_version=source_version,
                previous_source_version=previous_source_version,
                previous_signature=previous_signature,
                previous_patch_fingerprint=previous_patch_fingerprint,
            )

        verdict = self.evaluate_phase(
            root_task_id=root_task_id,
            contract=contract,
            source_version=source_version,
            phase=(
                ApplicablePhase.POST_PATCH if patched else ApplicablePhase.INITIAL_REVIEW
            ),
        )
        keys = [finding_key(f) for f in verdict.open_required]
        signature = failure_signature(verdict.failed, keys)
        facts["failed_check_ids"] = verdict.failed
        facts["missing_check_ids"] = verdict.missing
        facts["open_required_finding_ids"] = [f.finding_id for f in verdict.open_required]
        facts["failure_signature"] = signature

        if verdict.failed or verdict.open_required:
            category = DetectionCategory.CODE_DEFECT
            suggestion = "重新派发修复任务，附上失败检查与问题位置"
        elif verdict.missing:
            category = DetectionCategory.INSUFFICIENT_EVIDENCE
            suggestion = "派发补充审查或验证，补齐缺失证据"
        else:
            category = DetectionCategory.PASS
            suggestion = "完成条件满足，可由父 Agent 提议 finish"

        if (
            category is not DetectionCategory.PASS
            and signature == previous_signature
            and previous_source_version == source_version
            and previous_signature is not None
        ):
            category = DetectionCategory.NO_PROGRESS
            suggestion = "重复相同的失败且源码版本未变化，需要调整策略或进入待处理状态"

        return self._build(
            category,
            facts,
            source_version=source_version,
            previous_source_version=previous_source_version,
            signature=signature,
            previous_signature=previous_signature,
            patch_fingerprint=None,
            missing=verdict.missing,
            failed=verdict.failed,
            findings=verdict.open_required,
            suggestion=suggestion,
        )

    # ---- helpers -----------------------------------------------------------
    def _detect_fix(
        self,
        facts: dict,
        reports: list[TaskResult],
        *,
        source_version: str,
        previous_source_version: str | None,
        previous_signature: str | None,
        previous_patch_fingerprint: str | None,
    ) -> DetectionResult:
        patch_refs = [
            r.result_refs["patch"]
            for r in reports
            if r.result_refs.get("patch")
        ]
        facts["patch_refs"] = patch_refs
        fingerprint = self.patch_fingerprint(patch_refs[-1]) if patch_refs else None
        facts["patch_fingerprint"] = fingerprint

        if not patch_refs:
            category = DetectionCategory.INSUFFICIENT_EVIDENCE
            suggestion = "修复未产出可用补丁，需要补充证据或重新修复"
        elif fingerprint is not None and fingerprint == previous_patch_fingerprint:
            category = DetectionCategory.NO_PROGRESS
            suggestion = "补丁与上一轮完全相同，需要改变修复策略或进入待处理状态"
        else:
            category = DetectionCategory.INSUFFICIENT_EVIDENCE
            suggestion = "应用候选补丁，然后派发验证任务"

        return self._build(
            category,
            facts,
            source_version=source_version,
            previous_source_version=previous_source_version,
            signature=None,
            previous_signature=previous_signature,
            patch_fingerprint=fingerprint,
            suggestion=suggestion,
        )

    @staticmethod
    def _build(
        category: DetectionCategory,
        facts: dict,
        *,
        source_version: str,
        previous_source_version: str | None,
        signature: str | None,
        previous_signature: str | None,
        patch_fingerprint: str | None,
        suggestion: str,
        missing: Sequence[str] = (),
        failed: Sequence[str] = (),
        findings: Sequence[Finding] = (),
    ) -> DetectionResult:
        repeated = (
            signature if signature is not None and signature == previous_signature else None
        )
        return DetectionResult(
            valid=True,
            category=category,
            facts=facts,
            missing_items=list(missing),
            failed_check_ids=list(failed),
            related_finding_ids=[f.finding_id for f in findings],
            progress=ProgressInfo(
                changed=(
                    previous_source_version is not None
                    and previous_source_version != source_version
                ),
                previous_source_version=previous_source_version,
                current_source_version=source_version,
                repeated_failure_signature=repeated,
                patch_fingerprint=patch_fingerprint,
            ),
            suggestion=suggestion,
        )


def _report_fact(report: TaskResult) -> dict:
    return {
        "attempt_id": report.attempt_id,
        "task_id": report.task_id,
        "agent_id": report.agent_id,
        "task_kind": report.task_kind,
        "status": report.status.value,
        "passed": report.passed,
        "summary": report.summary[:300],
        "source_version": report.source_version,
        "evidence_refs": list(report.evidence_refs),
    }


def _fault_category_for_application(error: ErrorInfo) -> DetectionCategory:
    if error.category in (ErrorCategory.INVALID_PATCH, ErrorCategory.PATCH_CONFLICT):
        return DetectionCategory.CODE_DEFECT
    return DetectionCategory.EXECUTION_FAULT
