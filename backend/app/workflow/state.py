"""WorkflowState: the serializable channel set shared by the graph (Desgin §6.1).

Everything in the state is plain JSON-able data — references and metadata only,
never source code, tool output bodies or runtime objects — so the framework can
checkpoint it.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from app.workflow.reducers import append_unique, extend, merge_map


class WorkflowState(TypedDict, total=False):
    # --- identity / fixed configuration ------------------------------------
    root_task_id: str
    workflow_version: str
    check_mode: str
    schema_version: str
    agent_versions: dict[str, str]

    # --- goal / frozen contract -------------------------------------------
    goal: str
    review_scope: list[str]
    acceptance_criteria: list[str]
    contract_version: str
    acceptance_contract_ref: str
    contract: dict[str, Any]

    # --- task tree, attempts, batches (map channels merge per key) ---------
    root_task: dict[str, Any]
    task_tree: Annotated[dict[str, dict], merge_map]
    attempts: Annotated[dict[str, dict], merge_map]
    results: Annotated[dict[str, dict], merge_map]
    terminals: Annotated[dict[str, dict], merge_map]
    processed_result_ids: Annotated[list[str], append_unique]

    # --- dispatch bookkeeping (written by the parent control layer only) ----
    current_batch_id: str | None
    pending_attempt_ids: list[str]
    dispatch_history: Annotated[list[dict], extend]
    children_created: bool
    dispatch_ok: bool

    # --- versions / artifacts ---------------------------------------------
    source_version: str | None
    initial_source_version: str | None
    source_artifact: str | None
    contract_artifact: str | None
    source_versions: Annotated[list[str], append_unique]
    artifact_refs: Annotated[list[str], extend]
    last_application: dict[str, Any] | None
    last_application_error: dict[str, Any] | None
    controller_faults: list[dict]
    patched: bool
    failure_signature: str | None
    detected_source_version: str | None
    patch_fingerprint: str | None
    findings: Annotated[dict[str, dict], merge_map]
    required_finding_ids: Annotated[list[str], append_unique]
    patches: Annotated[dict[str, dict], merge_map]
    patch_applications: Annotated[dict[str, dict], merge_map]
    verification_refs: Annotated[dict[str, str], merge_map]
    check_results: Annotated[dict[str, dict], merge_map]

    # --- budgets -----------------------------------------------------------
    repair_round: int
    verification_retry_count: int
    review_retry_count: int
    fix_retry_count: int
    evidence_retry_count: int
    parent_corrections: int
    graph_steps: int
    run_segment_id: str

    # --- detection / decisions --------------------------------------------
    detection: dict[str, Any] | None
    feedback: Annotated[list[str], extend]
    next_action: dict[str, Any] | None
    decision_ids: Annotated[list[str], append_unique]
    action_rejections: Annotated[list[dict], extend]
    rejection_history_start: int
    rejection_count: int

    # --- outcome ----------------------------------------------------------
    status: str
    passed: bool | None
    errors: Annotated[list[dict], extend]
    report_ref: str | None
    final_report: dict[str, Any] | None
    skipped_tasks: Annotated[dict[str, str], merge_map]
    waiting_reason: str | None
    required_action: str | None
    fatal_error: dict[str, Any] | None


def initial_state(
    *,
    root_task_id: str,
    goal: str,
    workflow_version: str,
    check_mode: str,
    run_segment_id: str,
    source_version: str | None = None,
    source_artifact: str | None = None,
    schema_version: str = "1.0",
    agent_versions: dict[str, str] | None = None,
) -> WorkflowState:
    return WorkflowState(
        root_task_id=root_task_id,
        goal=goal,
        workflow_version=workflow_version,
        check_mode=check_mode,
        schema_version=schema_version,
        agent_versions=agent_versions or {},
        review_scope=[],
        acceptance_criteria=[],
        contract_version="",
        acceptance_contract_ref="",
        contract={},
        task_tree={},
        attempts={},
        results={},
        terminals={},
        processed_result_ids=[],
        current_batch_id=None,
        pending_attempt_ids=[],
        dispatch_history=[],
        children_created=False,
        dispatch_ok=False,
        source_version=source_version,
        initial_source_version=source_version,
        source_artifact=source_artifact,
        contract_artifact=None,
        source_versions=[],
        artifact_refs=[],
        last_application=None,
        last_application_error=None,
        controller_faults=[],
        patched=False,
        failure_signature=None,
        detected_source_version=None,
        patch_fingerprint=None,
        findings={},
        required_finding_ids=[],
        patches={},
        patch_applications={},
        verification_refs={},
        check_results={},
        repair_round=0,
        verification_retry_count=0,
        review_retry_count=0,
        fix_retry_count=0,
        evidence_retry_count=0,
        parent_corrections=0,
        graph_steps=0,
        run_segment_id=run_segment_id,
        detection=None,
        feedback=[],
        next_action=None,
        decision_ids=[],
        action_rejections=[],
        rejection_count=0,
        status="running",
        passed=None,
        errors=[],
        report_ref=None,
        final_report=None,
        skipped_tasks={},
        waiting_reason=None,
        required_action=None,
        fatal_error=None,
    )
