"""Results, detection, check contract, findings and control accounting (§5.1–5.3)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import SCHEMA_VERSION, ErrorInfo, utcnow
from app.schemas.enums import (
    ApplicablePhase,
    CheckMethod,
    CheckStatus,
    DetectionCategory,
    FindingSeverity,
    FindingStatus,
    ResultStatus,
    TaskKind,
    TerminalOrigin,
    TerminalOutcome,
)

# --------------------------------------------------------------------------- #
# TaskResult
# --------------------------------------------------------------------------- #


class TaskResult(BaseModel):
    """A child agent's structured report back to the parent (Desgin §4.2).

    ``status`` is the execution outcome; ``passed`` is the business verdict and
    must stay ``None`` where no boolean applies (e.g. a fixer producing a patch).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    result_id: str = Field(min_length=1)

    # identity: must match the dispatch exactly
    root_task_id: str = Field(min_length=1)
    parent_task_id: str | None = None
    task_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    dispatch_batch_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    agent_version: str = Field(min_length=1)
    task_kind: str = Field(min_length=1)

    # versions the report is bound to
    source_version: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)

    status: ResultStatus
    passed: bool | None = None
    summary: str = Field(min_length=1)
    result_refs: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    error: ErrorInfo | None = None
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def _check_semantics(self) -> "TaskResult":
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")

        if self.task_kind == TaskKind.FIX.value and self.passed is not None:
            raise ValueError("fix results must keep passed=None (patch is not proof)")

        if self.status is ResultStatus.FAILED:
            if self.passed is not None:
                raise ValueError("failed execution must keep passed=None")
            if self.error is None:
                raise ValueError("failed execution requires a structured error")
        else:  # completed
            if self.error is not None:
                raise ValueError(
                    "completed execution must not carry an execution error "
                    "(business failure belongs in passed/summary)"
                )
        return self

    def content_digest(self) -> str:
        """Stable digest over the semantic payload, used for idempotent receipt."""
        payload = {
            "task_id": self.task_id,
            "attempt_id": self.attempt_id,
            "dispatch_batch_id": self.dispatch_batch_id,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "status": self.status.value,
            "passed": self.passed,
            "summary": self.summary,
            "result_refs": dict(sorted(self.result_refs.items())),
            "evidence_refs": sorted(self.evidence_refs),
            "source_version": self.source_version,
            "contract_version": self.contract_version,
            "error": self.error.model_dump(mode="json") if self.error else None,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


class ProgressInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changed: bool = False
    previous_source_version: str | None = None
    current_source_version: str | None = None
    repeated_failure_signature: str | None = None
    patch_fingerprint: str | None = None


class DetectionResult(BaseModel):
    """Deterministic facts plus a clearly separated model explanation."""

    model_config = ConfigDict(extra="forbid")

    valid: bool
    category: DetectionCategory
    facts: dict[str, Any] = Field(default_factory=dict)
    explanation: str | None = None
    missing_items: list[str] = Field(default_factory=list)
    failed_check_ids: list[str] = Field(default_factory=list)
    related_finding_ids: list[str] = Field(default_factory=list)
    invalid_reason: str | None = None
    progress: ProgressInfo = Field(default_factory=ProgressInfo)
    suggestion: str | None = None

    @model_validator(mode="after")
    def _check_invalid(self) -> "DetectionResult":
        if not self.valid and not self.invalid_reason:
            raise ValueError("invalid result requires invalid_reason")
        if self.category is DetectionCategory.INVALID_RESULT and self.valid:
            raise ValueError("category=invalid_result must have valid=False")
        if self.category is DetectionCategory.PASS and not self.valid:
            raise ValueError("category=pass requires valid=True")
        return self


# --------------------------------------------------------------------------- #
# Check contract (§5.3)
# --------------------------------------------------------------------------- #


class CheckSpec(BaseModel):
    """One immutable check definition inside an acceptance contract."""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1)
    goal_ref: str = Field(min_length=1, description="user-goal item this check covers")
    scope: list[str] = Field(min_length=1, description="files/symbols or target problem")
    required: bool = True
    method: CheckMethod
    pass_condition: str = Field(min_length=1)
    evidence_requirements: list[str] = Field(default_factory=list)
    applicable_phase: ApplicablePhase = ApplicablePhase.BOTH
    checker_version: str = Field(default="1.0", min_length=1)
    # Implementation addition (documented in Desgin.md §5.1): a static_rule check
    # must name the registered rule ids it evaluates, so the verdict is
    # deterministic instead of guessed from free text.
    rule_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_method_params(self) -> "CheckSpec":
        if self.method is CheckMethod.STATIC_RULE and not self.rule_ids:
            raise ValueError("static_rule check must declare at least one rule_id")
        if self.method is not CheckMethod.STATIC_RULE and self.rule_ids:
            raise ValueError("only static_rule checks may declare rule_ids")
        return self


class AcceptanceContract(BaseModel):
    """Frozen mapping from the user goal to required checks (§5.3).

    The first version may only *append* checks (new contract version); it may not
    delete, downgrade or modify an existing required check.
    """

    model_config = ConfigDict(extra="forbid")

    contract_version: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    user_goal: str = Field(min_length=1)
    checks: list[CheckSpec] = Field(min_length=1)
    supersedes: str | None = None
    append_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check_contract(self) -> "AcceptanceContract":
        ids = [c.check_id for c in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate check_id in contract")
        if not any(c.required for c in self.checks):
            raise ValueError("contract requires at least one required check")
        goals = {c.goal_ref for c in self.checks}
        if not goals:
            raise ValueError("contract must map at least one goal item")
        if self.supersedes and not self.append_reason:
            raise ValueError("appending a contract version requires append_reason")
        return self

    def required_checks(self, phase: ApplicablePhase) -> list[CheckSpec]:
        wanted = {ApplicablePhase.BOTH, phase}
        return [c for c in self.checks if c.required and c.applicable_phase in wanted]

    def content_hash(self) -> str:
        blob = json.dumps(
            self.model_dump(mode="json", exclude={"created_at"}),
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def diff_required_checks(self, previous: "AcceptanceContract") -> set[str]:
        """Required check ids that would be lost relative to ``previous``."""
        prev_required = {c.check_id for c in previous.required_checks(ApplicablePhase.BOTH)}
        prev_required |= {
            c.check_id
            for c in previous.checks
            if c.required
            and c.applicable_phase in (ApplicablePhase.INITIAL_REVIEW, ApplicablePhase.POST_PATCH)
        }
        now_required = {c.check_id for c in self.checks if c.required}
        return prev_required - now_required


class CheckResult(BaseModel):
    """A per-version result of one check (never a zero-test pass)."""

    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    producer_attempt_id: str = Field(min_length=1)
    method: CheckMethod | None = None
    status: CheckStatus
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str | None = None

    # test-shaped evidence (required for behavior_test)
    test_count: int | None = Field(default=None, ge=0)
    passed_count: int | None = Field(default=None, ge=0)
    failed_count: int | None = Field(default=None, ge=0)
    skipped_count: int | None = Field(default=None, ge=0)
    test_suite_hash: str | None = None
    executed: bool = True

    @model_validator(mode="after")
    def _check_status_semantics(self) -> "CheckResult":
        counts = [self.test_count, self.passed_count, self.failed_count, self.skipped_count]
        if any(c is not None for c in counts):
            if any(c is None for c in counts):
                raise ValueError("test counts must be provided together or all omitted")
            assert self.test_count is not None
            total = (self.passed_count or 0) + (self.failed_count or 0) + (self.skipped_count or 0)
            if total != self.test_count:
                raise ValueError("test counts must sum to test_count")

        if self.status is CheckStatus.PASSED:
            self._reject_invalid_pass()
        if self.status is CheckStatus.PASSED and not self.evidence_refs:
            raise ValueError("a passed check requires at least one evidence_ref")
        if self.status is CheckStatus.NOT_RUN and self.executed:
            raise ValueError("status=not_run requires executed=False")
        return self

    def _reject_invalid_pass(self) -> None:
        if self.method is CheckMethod.BEHAVIOR_TEST:
            if not self.executed:
                raise ValueError("behavior check cannot pass without running")
            if self.test_count in (None, 0):
                raise ValueError("behavior check cannot pass with zero collected tests")
            if (self.passed_count or 0) < 1:
                raise ValueError("behavior check cannot pass with zero passing tests")
            if (self.skipped_count or 0) == self.test_count:
                raise ValueError("behavior check cannot pass when every test was skipped")
            if (self.failed_count or 0) > 0:
                raise ValueError("behavior check cannot pass with failing tests")


# --------------------------------------------------------------------------- #
# Findings / patches / verification / report
# --------------------------------------------------------------------------- #


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    producer_attempt_id: str = Field(min_length=1)
    goal_ref: str | None = None
    required_for_goal: bool = False
    check_id: str | None = None
    file_path: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    symbol: str | None = None
    rule: str = Field(min_length=1)
    severity: FindingSeverity = FindingSeverity.WARNING
    message: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    status: FindingStatus = FindingStatus.OPEN
    resolution_evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_resolution(self) -> "Finding":
        if self.status is FindingStatus.RESOLVED and not self.resolution_evidence_refs:
            raise ValueError(
                "resolved finding requires current-version resolution evidence "
                "(a fixer claim is not enough)"
            )
        return self


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    producer_attempt_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    target_finding_ids: list[str] = Field(default_factory=list)
    check_results: list[CheckResult] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    not_run: list[str] = Field(default_factory=list)
    passed: bool | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _check_passed(self) -> "VerificationReport":
        if self.passed is True:
            if not self.check_results:
                raise ValueError("passed verification requires check results")
            if any(r.status is not CheckStatus.PASSED for r in self.check_results):
                raise ValueError("verification cannot pass while a check is not passed")
        if self.passed is None and not self.not_run and not self.check_results:
            raise ValueError("inconclusive verification must explain what is missing")
        return self


class CheckSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str
    goal_ref: str
    method: CheckMethod
    required: bool
    status: CheckStatus
    latest_evidence_ref: str | None = None
    source_version: str | None = None
    note: str | None = None


class FinalReport(BaseModel):
    """The user-facing result: every required item, its evidence and leftovers."""

    model_config = ConfigDict(extra="forbid")

    report_id: str = Field(min_length=1)
    root_task_id: str = Field(min_length=1)
    final_status: str
    passed: bool | None = None
    goal: str
    conclusion_scope: str = Field(min_length=1)
    source_version: str | None = None
    workflow_version: str
    contract_version: str | None = None
    checks: list[CheckSummary] = Field(default_factory=list)
    unresolved_finding_ids: list[str] = Field(default_factory=list)
    not_run_items: list[str] = Field(default_factory=list)
    skipped_tasks: list[dict[str, str]] = Field(default_factory=list)
    versions: dict[str, str] = Field(default_factory=dict)
    execution_summary: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check_report(self) -> "FinalReport":
        if self.passed is True and any(
            c.required and c.status is not CheckStatus.PASSED for c in self.checks
        ):
            raise ValueError("a passing report cannot contain a non-passed required check")
        return self


# --------------------------------------------------------------------------- #
# Control accounting
# --------------------------------------------------------------------------- #


class BudgetLedgerEntry(BaseModel):
    """An append-only grant (delta>0) or consumption (delta<0) of budget."""

    model_config = ConfigDict(extra="forbid")

    root_task_id: str = Field(min_length=1)
    budget_kind: str = Field(min_length=1)
    scope_key: str = Field(min_length=1)
    operation_key: str = Field(min_length=1)
    delta: int
    reason: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check_delta(self) -> "BudgetLedgerEntry":
        if self.delta == 0:
            raise ValueError("ledger delta must be non-zero")
        return self


class TerminalRecord(BaseModel):
    """The single terminal outcome of an attempt (agent report or controller)."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1)
    dispatch_batch_id: str | None = None
    origin: TerminalOrigin
    outcome: TerminalOutcome
    result_id: str | None = None
    error_ref: str | None = None
    fencing_token: int = 0
    operation_key: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _check_origin(self) -> "TerminalRecord":
        if self.origin is TerminalOrigin.CONTROLLER:
            if self.outcome is not TerminalOutcome.FAILED:
                raise ValueError("controller terminals can only be failed")
            if self.error_ref is None:
                raise ValueError("controller terminal requires error_ref")
            if self.result_id is not None:
                raise ValueError("controller terminal must not carry a TaskResult")
        else:  # agent
            if self.outcome is TerminalOutcome.COMPLETED and self.result_id is None:
                raise ValueError("agent completed terminal requires result_id")
            if self.outcome is TerminalOutcome.FAILED and self.result_id is None and self.error_ref is None:
                raise ValueError("agent failed terminal requires result_id or error_ref")
        return self
