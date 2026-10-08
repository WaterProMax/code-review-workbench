"""Shared enumerations for the whole contract surface.

Keeping these in one module avoids duplicating string literals across schemas,
storage, workflow and API layers, and lets validation reference a single source
of truth (ImplementationPlan §5.2, Desgin.md §3.4).
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """String-backed enum so members serialise as their value."""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.value


# --- task / attempt lifecycle ----------------------------------------------
class TaskLevel(StrEnum):
    ROOT = "root"
    CHILD = "child"


class RootStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_RECOVERY = "waiting_recovery"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class TaskStatus(StrEnum):
    BLOCKED = "blocked"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    INVALIDATED = "invalidated"


class ResultStatus(StrEnum):
    """Execution outcome carried by a TaskResult (never a business verdict)."""

    COMPLETED = "completed"
    FAILED = "failed"


# Final root statuses that cannot be resumed (first version).
FINAL_ROOT_STATUSES = frozenset(
    {RootStatus.COMPLETED, RootStatus.PARTIAL, RootStatus.FAILED}
)
RESUMABLE_ROOT_STATUSES = frozenset(
    {RootStatus.WAITING_RECOVERY, RootStatus.INTERRUPTED}
)

# Root statuses that supersede an earlier projected child status.
ACTIVE_ROOT_STATUSES = frozenset({RootStatus.QUEUED, RootStatus.RUNNING})


# --- roles / task kinds -----------------------------------------------------
class TaskKind(StrEnum):
    REVIEW = "review"
    FIX = "fix"
    VERIFY = "verify"


BASE_TASK_KINDS = (TaskKind.REVIEW.value, TaskKind.FIX.value, TaskKind.VERIFY.value)


class AgentId(StrEnum):
    PARENT = "parent"
    REVIEWER = "reviewer"
    FIXER = "fixer"
    VERIFIER = "verifier"
    TEST_GENERATOR = "test_generator"  # P10 extension sample


# --- checks / findings ------------------------------------------------------
class CheckMethod(StrEnum):
    SYNTAX = "syntax"
    STATIC_RULE = "static_rule"
    BEHAVIOR_TEST = "behavior_test"
    MODEL_REVIEW = "model_review"


class ApplicablePhase(StrEnum):
    INITIAL_REVIEW = "initial_review"
    POST_PATCH = "post_patch"
    BOTH = "both"


class CheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    INCONCLUSIVE = "inconclusive"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FindingStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


# --- parent decisions / detection ------------------------------------------
class ActionType(StrEnum):
    DISPATCH_TASK = "dispatch_task"
    DISPATCH_BATCH = "dispatch_batch"
    APPLY_PATCH = "apply_patch"
    WAIT_FOR_RECOVERY = "wait_for_recovery"
    FINISH = "finish"


class DetectionCategory(StrEnum):
    CODE_DEFECT = "code_defect"
    EXECUTION_FAULT = "execution_fault"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID_RESULT = "invalid_result"
    NO_PROGRESS = "no_progress"
    PASS = "pass"


class SkipReason(StrEnum):
    FIRST_REVIEW_PASSED = "first_review_passed"
    NOT_REQUIRED = "not_required"
    PRIOR_FAILURE = "prior_failure"
    TERMINATED = "terminated"


# --- budgets / terminals ----------------------------------------------------
class BudgetKind(StrEnum):
    REPAIR_ROUND = "repair_round"
    VERIFICATION_RETRY = "verification_retry"
    REVIEW_RETRY = "review_retry"
    FIX_RETRY = "fix_retry"
    EVIDENCE_RETRY = "evidence_retry"
    # declared by the P10 extension sample so a new task kind never falls into an
    # unbounded-retry default (§5, §14.2)
    GENERATE_RETRY = "generate_retry"


class RetryReason(StrEnum):
    """Mutually exclusive reasons for creating a new attempt (plan §9.4)."""

    EXECUTION_FAULT = "execution_fault"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    BUSINESS_REPAIR = "business_repair"
    POST_PATCH_FIRST_CHECK = "post_patch_first_check"
    INITIAL = "initial"


class TerminalOrigin(StrEnum):
    AGENT = "agent"
    CONTROLLER = "controller"


class TerminalOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class ErrorCategory(StrEnum):
    MODEL_ERROR = "model_error"
    MODEL_TIMEOUT = "model_timeout"
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    MISSING_DEPENDENCY = "missing_dependency"
    INVALID_PATCH = "invalid_patch"
    PATCH_CONFLICT = "patch_conflict"
    ATTEMPT_TIMEOUT = "attempt_timeout"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    CYCLE_LIMIT = "cycle_limit"
    GRAPH_STEP_LIMIT = "graph_step_limit"
    INVALID_ACTION = "invalid_action"
    INVALID_RESULT = "invalid_result"
    UNKNOWN = "unknown"

    @property
    def default_recoverable(self) -> bool:
        return self not in {
            ErrorCategory.INVALID_PATCH,
            ErrorCategory.PATCH_CONFLICT,
            ErrorCategory.INVALID_ACTION,
            ErrorCategory.INVALID_RESULT,
            ErrorCategory.CANCELLED,
        }


# --- events -----------------------------------------------------------------
class EventType(StrEnum):
    TASK_CREATED = "task_created"
    TASK_DISPATCHED = "task_dispatched"
    ATTEMPT_STARTED = "attempt_started"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    RESULT_RECEIVED = "result_received"
    DETECTION_COMPLETED = "detection_completed"
    RETRY_SCHEDULED = "retry_scheduled"
    TASK_SKIPPED = "task_skipped"
    WAITING_RECOVERY = "waiting_recovery"
    TASK_COMPLETED = "task_completed"
    TASK_INTERRUPTED = "task_interrupted"
    TASK_RESUMED = "task_resumed"
    PATCH_APPLIED = "patch_applied"
    PATCH_APPLICATION_FAILED = "patch_application_failed"
    BUDGET_GRANTED = "budget_granted"
    BUDGET_CONSUMED = "budget_consumed"
    PARENT_DECIDED = "parent_decided"
    ACTION_REJECTED = "action_rejected"
    TERMINAL_RECORDED = "terminal_recorded"
    LATE_RESULT_AUDIT = "late_result_audit"
    CHECK_RECORDED = "check_recorded"


# --- artifacts --------------------------------------------------------------
class ArtifactType(StrEnum):
    UPLOAD_MANIFEST = "upload_manifest"
    SOURCE_SNAPSHOT = "source_snapshot"
    ACCEPTANCE_CONTRACT = "acceptance_contract"
    FINDING = "finding"
    PATCH = "patch"
    PATCH_APPLICATION = "patch_application"
    VERIFICATION_REPORT = "verification_report"
    CHECK_RESULT = "check_result"
    FINAL_REPORT = "final_report"
    REPORT_MARKDOWN = "report_markdown"
    TEST_ARTIFACT = "test_artifact"
    DETECTION = "detection"
    EVIDENCE = "evidence"
    DIFF = "diff"
    MODEL_OUTPUT = "model_output"


class PatchFormat(StrEnum):
    UNIFIED_DIFF = "unified_diff"
    STRUCTURED_EDITS = "structured_edits"


class PatchApplicationStatus(StrEnum):
    PREPARED = "prepared"
    COMMITTED = "committed"
    FAILED = "failed"


class CheckMode(StrEnum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class WorkflowNodeType(StrEnum):
    PARENT = "parent"
    AGENT = "agent"
    TOOL = "tool"
    END = "end"


# --- actors -----------------------------------------------------------------
class ActorId(StrEnum):
    PARENT = "parent"
    REVIEWER = "reviewer"
    FIXER = "fixer"
    VERIFIER = "verifier"
    CONTROLLER = "controller"
    TOOL = "tool"
    TASK_SERVICE = "task_service"
    SYSTEM = "system"
