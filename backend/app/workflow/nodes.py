"""LangGraph nodes: the real execution path of the parent/child workflow (§8.2).

Every node is a small, auditable step. The parent control layer owns the state
transitions; child agents only ever return a report that the receiving control
layer validates before it may influence the verdict.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.agents.base import AgentContext
from app.agents.parent import ParentAgent, ParentPlan
from app.providers.base import LLMClient
from app.registry.agents import AgentRegistry
from app.registry.tools import ToolRegistry
from app.schemas.actions import (
    ApplyPatchAction,
    FinishAction,
    WaitForRecoveryAction,
    parse_parent_action,
)
from app.schemas.agents import AgentSpec
from app.schemas.artifacts import PatchApplication
from app.schemas.common import ErrorInfo
from app.schemas.enums import (
    ArtifactType,
    ErrorCategory,
    EventType,
    RootStatus,
    WorkflowNodeType,
)
from app.schemas.results import AcceptanceContract, TaskResult
from app.schemas.tasks import InputRefs, TaskEnvelope
from app.schemas.workflows import WorkflowConfig
from app.services.artifacts import ArtifactService
from app.services.events import EventContext, EventSink
from app.services.execution_locks import BudgetExhausted, ExecutionLockService
from app.services.task_creation import TaskCreationService
from app.services.task_service import DispatchRejected, ReceiptRejected, TaskService
from app.services.workspace import PatchRejected, WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from app.workflow.adapter import AgentNodeAdapter, ResultIdentityError
from app.workflow.controller import ActionRejected, ParentController
from app.workflow.detection import Detector

TERMINAL_ROOT_STATUSES = {
    RootStatus.COMPLETED.value,
    RootStatus.PARTIAL.value,
    RootStatus.FAILED.value,
}


class WorkflowRuntime:
    """Holds the runtime dependencies and implements the graph node behaviour."""

    def __init__(
        self,
        *,
        settings: Settings,
        repos: Repos,
        workflow: WorkflowConfig,
        registry: AgentRegistry,
        tools: ToolRegistry,
        llm: LLMClient,
        workspace: WorkspaceService,
        artifacts: ArtifactService,
        locks: ExecutionLockService,
        task_service: TaskService,
        controller: ParentController,
        detector: Detector,
        parent: ParentAgent,
    ) -> None:
        self.fencing_token = 0
        self.settings = settings
        self.repos = repos
        self.workflow = workflow
        self.registry = registry
        self.tools = tools
        self.llm = llm
        self.workspace = workspace
        self.artifacts = artifacts
        self.locks = locks
        self.task_service = task_service
        self.controller = controller
        self.detector = detector
        self.parent = parent
        self.adapter = AgentNodeAdapter(
            registry=registry,
            repos=repos,
            sink=self.sink("unbound"),
            context_factory=self.make_context,
        )

    # ---- helpers -----------------------------------------------------------
    def sink(self, root_task_id: str, actor: str = "parent") -> EventSink:
        return EventSink(
            self.repos.events, EventContext(root_task_id=root_task_id, actor_id=actor)
        )

    def make_context(self, task: TaskEnvelope, spec: AgentSpec) -> AgentContext:
        constraints = task.constraints
        scratch = self.settings.data_dir / "scratch" / task.root_task_id / task.attempt_id
        scratch.mkdir(parents=True, exist_ok=True)
        allowed = constraints.allowed_paths or self.workspace.list_files(
            task.root_task_id, task.source_version
        )
        return AgentContext(
            llm=self.llm,
            tools=self.tools,
            workspace=self.workspace,
            artifacts=self.artifacts,
            repos=self.repos,
            sink=self.sink(task.root_task_id),
            agent_spec=spec,
            deadline_seconds=constraints.timeout_seconds,
            model_timeout_seconds=(
                constraints.model_timeout_seconds or self.settings.model_timeout_seconds
            ),
            tool_timeout_seconds=(
                constraints.tool_timeout_seconds or self.settings.tool_timeout_seconds
            ),
            max_tool_steps=constraints.max_tool_steps,
            max_model_retries=self.settings.max_model_retries,
            scratch_dir=scratch,
            allowed_paths=allowed,
            read_only=True,
        )

    def _contract(self, state: dict) -> AcceptanceContract:
        return AcceptanceContract.model_validate(state["contract"])

    def _task_tree(self, root_task_id: str) -> dict[str, dict]:
        tree: dict[str, dict] = {}
        for task in self.repos.tasks.list_by_root(root_task_id):
            tree[task.task_id] = task.model_dump(mode="json")
        return tree

    def _attempt_map(self, root_task_id: str) -> dict[str, dict]:
        return {
            a.attempt_id: a.model_dump(mode="json")
            for a in self.repos.attempts.list_by_root(root_task_id)
        }

    def _batch_reports(self, state: dict) -> list[TaskResult]:
        batch_id = state.get("current_batch_id")
        if not batch_id:
            return []
        batch = self.repos.batches.get(batch_id)
        if batch is None:
            return []
        reports: list[TaskResult] = []
        for attempt_id in batch["expected_attempt_ids"]:
            result = self.repos.results.get_by_attempt(attempt_id)
            if result is not None:
                reports.append(result)
        return reports

    def _reject(
        self, state: dict, code: str, message: str, *, details: dict | None = None
    ) -> dict:
        entry = {
            "code": code,
            "message": message,
            "details": details or {},
            "proposed_action": state.get("next_action"),
        }
        self.sink(state["root_task_id"]).emit(
            EventType.ACTION_REJECTED,
            payload=entry,
        )
        return {
            "next_action": None,
            "dispatch_ok": False,
            "rejection_count": int(state.get("rejection_count") or 0) + 1,
            "action_rejections": [entry],
            "feedback": [f"动作被拒绝：{message}"],
        }

    # ---- initialize --------------------------------------------------------
    async def n_initialize(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        goal = state["goal"]
        creator = TaskCreationService(self.repos)
        root = self.repos.tasks.get(root_task_id)
        if root is None:
            root = creator.create_root(
                goal=goal,
                workflow_version=self.workflow.workflow_version or state["workflow_version"],
                root_task_id=root_task_id,
            )
        self.repos.tasks.update(root_task_id, status=RootStatus.RUNNING.value)

        contract_row = self.repos.contracts.latest(root_task_id)
        if contract_row is not None:
            contract = AcceptanceContract.model_validate(
                self.artifacts.read_json(contract_row["artifact_id"])
            )
            contract_artifact = contract_row["artifact_id"]
            plan_scope = state.get("review_scope") or []
            plan_criteria = state.get("acceptance_criteria") or []
        else:
            source_version = state.get("initial_source_version") or state["source_version"]
            files = self.workspace.list_files(root_task_id, source_version)
            plan, problems = await self._plan_with_corrections(
                root_task_id=root_task_id, goal=goal, files=files
            )
            if plan is None:
                return {
                    "status": RootStatus.WAITING_RECOVERY.value,
                    "waiting_reason": "父模型未能给出合法的检查合同",
                    "required_action": "检查目标描述与模型配置后恢复任务",
                    "fatal_error": {"code": "INVALID_PLAN", "problems": problems},
                    "graph_steps": int(state.get("graph_steps") or 0) + 1,
                }
            contract, contract_artifact = self.controller.persist_contract(
                root_task_id=root_task_id, goal=goal, checks=plan.checks
            )
            plan_scope = plan.review_scope
            plan_criteria = plan.acceptance_criteria

        self.controller.create_standard_children(
            root_task_id=root_task_id, workflow=self.workflow, goal=goal
        )
        tree = self._task_tree(root_task_id)
        return {
            "contract": contract.model_dump(mode="json"),
            "contract_version": contract.contract_version,
            "contract_artifact": contract_artifact,
            "acceptance_contract_ref": contract_artifact,
            "review_scope": plan_scope,
            "acceptance_criteria": plan_criteria,
            "task_tree": tree,
            "children_created": True,
            "status": RootStatus.RUNNING.value,
            "graph_steps": int(state.get("graph_steps") or 0) + 1,
        }

    async def _plan_with_corrections(
        self, *, root_task_id: str, goal: str, files: list[str]
    ) -> tuple[ParentPlan | None, list[str]]:
        plan = await self.parent.plan(
            root_task_id=root_task_id, goal=goal, files=files, operation_key=root_task_id
        )
        problems = self.controller.validate_plan(plan.checks, goal=goal, files=files)
        corrections = 0
        while problems and corrections < self.settings.max_parent_corrections:
            plan = await self.parent.replan(
                root_task_id=root_task_id,
                goal=goal,
                files=files,
                problems=problems,
                previous=plan,
                operation_key=root_task_id,
            )
            corrections += 1
            problems = self.controller.validate_plan(plan.checks, goal=goal, files=files)
        if problems:
            return None, problems
        return plan, []

    # ---- parent decision ---------------------------------------------------
    async def n_parent_decide(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        steps = int(state.get("graph_steps") or 0) + 1
        control = self.repos.controls.get(root_task_id)
        limit = control.graph_steps_granted if control else self.workflow.budgets.max_graph_steps
        if steps > limit:
            return {
                "next_action": WaitForRecoveryAction(
                    reason=f"图执行步数达到上限 {limit}",
                    required_action="追加图执行步数许可后恢复任务",
                ).model_dump(mode="json"),
                "waiting_reason": f"图执行步数达到上限 {limit}",
                "required_action": "追加图执行步数许可后恢复任务",
                "graph_steps": steps - 1,
            }
        rejections = self._current_rejections(state)
        if int(state.get("rejection_count") or 0) >= self.settings.max_parent_corrections:
            last = rejections[-1] if rejections else {}
            details = last.get("details") or {}
            additions = details.get("required_additions")
            if additions:
                reason = f"额度耗尽：{last.get('message')}"
                required = f"追加 {additions} 后恢复任务"
            else:
                reason = f"父模型连续给出非法动作：{last.get('message')}"
                required = "人工检查模型输出或调整工作流配置后恢复任务"
            return {
                "next_action": WaitForRecoveryAction(
                    reason=reason, required_action=required
                ).model_dump(mode="json"),
                "waiting_reason": reason,
                "required_action": required,
                "graph_steps": steps,
            }

        decision = await self.parent.decide(
            context=self._decision_context(state),
            rejections=rejections[-self.settings.max_parent_corrections :],
            operation_key=f"{root_task_id}:{steps}",
        )
        action = decision.action
        self.sink(root_task_id).emit(
            EventType.PARENT_DECIDED,
            payload={
                "action": action.action,
                "reasoning": decision.reasoning,
                "detail": action.model_dump(mode="json"),
            },
        )
        return {
            "next_action": action.model_dump(mode="json"),
            "decision_ids": [f"d-{uuid.uuid4().hex[:12]}"],
            "feedback": [decision.reasoning],
            "graph_steps": steps,
        }

    def _node_ids(self, root_task_id: str) -> dict[str, str]:
        """task_id -> workflow node id.

        ``create_standard_children`` registers one child per agent node in node
        order, so this positional pairing is stable across replays; it lets the
        parent address the parallel template's ``recheck`` task explicitly.
        """
        nodes = [
            n
            for n in self.workflow.nodes
            if n.type is WorkflowNodeType.AGENT and n.agent_id
        ]
        children = self.repos.tasks.list_children(root_task_id)
        return {task.task_id: node.id for node, task in zip(nodes, children)}

    def _current_rejections(self, state: dict) -> list[dict]:
        return list(state.get("action_rejections") or [])[int(state.get("rejection_history_start") or 0):]

    def _decision_context(self, state: dict) -> dict[str, Any]:
        root_task_id = state["root_task_id"]
        contract = self._contract(state)
        budget = self.locks.budget_state(root_task_id, self.workflow.budgets)
        node_ids = self._node_ids(root_task_id)
        return {
            "root_task_id": root_task_id,
            "goal": state["goal"],
            "check_mode": state["check_mode"],
            "workflow_version": state["workflow_version"],
            "source_version": state["source_version"],
            "initial_source_version": state.get("initial_source_version"),
            "patched": bool(state.get("patched")),
            "contract": {
                "contract_version": contract.contract_version,
                "user_goal": contract.user_goal,
                "checks": [
                    {
                        "check_id": c.check_id,
                        "goal_ref": c.goal_ref,
                        "required": c.required,
                        "method": c.method.value,
                        "applicable_phase": c.applicable_phase.value,
                        "pass_condition": c.pass_condition,
                    }
                    for c in contract.checks
                ],
            },
            "task_tree": [
                {
                    "task_id": t.task_id,
                    "node_id": node_ids.get(t.task_id),
                    "task_kind": t.task_kind,
                    "agent_id": t.agent_id,
                    "status": t.status,
                    "passed": t.passed,
                    "skip_reason": t.skip_reason,
                    "depends_on": [
                        {
                            "task_id": d.task_id,
                            "attempt_id": d.attempt_id,
                            "condition": d.condition,
                        }
                        for d in t.depends_on
                    ],
                }
                for t in self.repos.tasks.list_children(root_task_id)
            ],
            "attempts": [
                {
                    "attempt_id": a.attempt_id,
                    "task_id": a.task_id,
                    "attempt_no": a.attempt_no,
                    "status": a.status.value,
                    "source_version": a.source_version,
                    "retry_reason": a.retry_reason,
                }
                for a in self.repos.attempts.list_by_root(root_task_id)
            ],
            "latest_reports": [
                {
                    "attempt_id": r.attempt_id,
                    "task_id": r.task_id,
                    "node_id": node_ids.get(r.task_id),
                    "task_kind": r.task_kind,
                    "status": r.status.value,
                    "passed": r.passed,
                    "summary": r.summary[:300],
                    "result_refs": r.result_refs,
                    "source_version": r.source_version,
                }
                for r in self.repos.results.list_by_root(root_task_id)
            ],
            "open_required_findings": [
                {
                    "finding_id": f.finding_id,
                    "rule": f.rule,
                    "file_path": f.file_path,
                    "line": f.line,
                    "severity": f.severity.value,
                }
                for f in self.detector.open_required_findings(root_task_id)
            ],
            "detection": state.get("detection"),
            "last_application": state.get("last_application"),
            "last_application_error": state.get("last_application_error"),
            "unapplied_fix_attempt_id": self.controller.unapplied_fix_attempt(root_task_id),
            "last_verified_source_version": (
                (self.repos.verifications.latest(root_task_id) or {}).get("source_version")
            ),
            "repair_rounds_remaining": budget.get("repair_round", {}).get("remaining", 0),
            "budgets": budget,
            "action_rejections": self._current_rejections(state)[
                -self.settings.max_parent_corrections :
            ],
        }

    # ---- validate action ---------------------------------------------------
    async def n_validate_action(self, state: dict) -> dict:
        raw = state.get("next_action")
        if not raw:
            return {}
        try:
            action = parse_parent_action(raw)
        except Exception as exc:  # noqa: BLE001 - an unparseable proposal is rejected
            return self._reject(state, "UNPARSEABLE_ACTION", f"动作无法解析：{exc}")

        problems = self.controller.validate_action(
            action,
            root_task_id=state["root_task_id"],
            workflow=self.workflow,
            source_version=state["source_version"],
        )
        if problems:
            return self._reject(
                state, "ILLEGAL_ACTION", "; ".join(problems), details={"action": raw}
            )

        update: dict[str, Any] = {"rejection_count": 0}
        if isinstance(action, WaitForRecoveryAction):
            update["waiting_reason"] = action.reason
            update["required_action"] = action.required_action
            update["passed"] = action.known_passed
        return update

    # ---- dispatch ----------------------------------------------------------
    async def n_dispatch(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        action = parse_parent_action(state["next_action"])
        try:
            plan = self.controller.register_dispatch(
                action,
                root_task_id=root_task_id,
                workflow=self.workflow,
                source_version=state["source_version"],
                contract_version=state["contract_version"],
                contract_artifact=state["contract_artifact"],
                operation_key=f"dispatch:{root_task_id}:{state['decision_ids'][-1]}",
                fencing_token=self.fencing_token,
            )
        except (DispatchRejected, ActionRejected) as exc:
            return self._reject(
                state, getattr(exc, "code", "DISPATCH_REJECTED"), str(exc)
            )
        except BudgetExhausted as exc:
            return self._reject(
                state,
                "BUDGET_EXHAUSTED",
                f"额度不足，需要追加 {exc.required_additions}",
                details={"required_additions": exc.required_additions},
            )

        from app.schemas.enums import AttemptStatus, RetryReason
        interrupted = [i for i, a in enumerate(plan.attempts) if a.status is AttemptStatus.INTERRUPTED]
        if interrupted:
            retry_items = [plan.items[i] for i in interrupted]
            try:
                retry_batch, retries = self.task_service.register_dispatch(
                    root_task_id=root_task_id, items=retry_items, workflow=self.workflow,
                    source_version=state["source_version"], contract_version=state["contract_version"],
                    retry_reason=RetryReason.EXECUTION_FAULT.value, fencing_token=self.fencing_token,
                    operation_key=f"dispatch-recovery:{plan.batch_id}:{state['run_segment_id']}",
                )
            except BudgetExhausted as exc:
                return self._reject(
                    state, "BUDGET_EXHAUSTED",
                    f"恢复未完成分支需要追加额度 {exc.required_additions}",
                    details={"required_additions": exc.required_additions},
                )
            for i, retry in zip(interrupted, retries):
                plan.attempts[i] = retry
            plan.batch_id = retry_batch
        reports: dict[str, dict] = {}
        faults: list[dict] = []
        envelopes = [
            self.controller.build_envelope(attempt, task, item, self.workflow)
            for attempt, task, item in zip(plan.attempts, plan.tasks, plan.items)
        ]

        async def run_one(envelope: TaskEnvelope) -> tuple[TaskEnvelope, Any]:
            timeout = envelope.constraints.timeout_seconds
            try:
                existing = self.repos.results.get_by_attempt(envelope.attempt_id)
                if existing is not None:
                    return envelope, existing
                result = await asyncio.wait_for(self.adapter.execute(envelope), timeout=timeout)
                self.task_service.receive_result(envelope, result)
                return envelope, result
            except asyncio.TimeoutError:
                self._controller_terminal(
                    envelope,
                    "ATTEMPT_TIMEOUT",
                    f"尝试在 {timeout}s 内未结束，控制层撤销该尝试并记录故障终态",
                    category=ErrorCategory.ATTEMPT_TIMEOUT,
                )
                return envelope, None
            except ResultIdentityError as exc:
                self._controller_terminal(
                    envelope, "RESULT_IDENTITY_MISMATCH", "; ".join(exc.problems)
                )
                return envelope, None
            except Exception as exc:  # noqa: BLE001 - normalize an unexpected failure
                self._controller_terminal(
                    envelope,
                    "ATTEMPT_EXECUTION_ERROR",
                    f"{type(exc).__name__}: {exc}"[:400],
                    category=ErrorCategory.UNKNOWN,
                )
                return envelope, None

        # A batch runs its attempts concurrently: they read the same frozen
        # snapshot and must not share a context or a database connection.
        outcomes = await asyncio.gather(*(run_one(e) for e in envelopes))
        for envelope, result in outcomes:
            if result is None:
                faults.append(
                    {
                        "attempt_id": envelope.attempt_id,
                        "task_id": envelope.task_id,
                        "code": "CONTROLLER_TERMINAL",
                        "recoverable": True,
                    }
                )
                continue
            reports[envelope.attempt_id] = result.model_dump(mode="json")

        return {
            "results": reports,
            "attempts": {a.attempt_id: a.model_dump(mode="json") for a in plan.attempts},
            "pending_attempt_ids": [a.attempt_id for a in plan.attempts],
            "current_batch_id": plan.batch_id,
            "dispatch_ok": True,
            "controller_faults": faults,
            "dispatch_history": [
                {
                    "batch_id": plan.batch_id,
                    "attempt_ids": [a.attempt_id for a in plan.attempts],
                    "task_kinds": [t.task_kind for t in plan.tasks],
                    "action": state["next_action"]["action"],
                }
            ],
            "next_action": None,
            "last_application": None,
            "last_application_error": None,
            "task_tree": self._task_tree(root_task_id),
        }

    def _controller_terminal(
        self,
        envelope: TaskEnvelope,
        code: str,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.INVALID_RESULT,
    ) -> None:
        token = envelope.fencing_token
        error_artifact = self.artifacts.save_json(
            root_task_id=envelope.root_task_id,
            artifact_type=ArtifactType.EVIDENCE,
            data={
                "code": code,
                "category": category.value,
                "message": message,
                "attempt_id": envelope.attempt_id,
            },
            source_version=envelope.source_version,
            name=f"controller-terminal-{envelope.attempt_id}",
        )
        self.task_service.record_controller_terminal(
            envelope=envelope,
            code=code,
            message=message,
            fencing_token=token,
            error_ref=error_artifact.artifact_id,
        )

    # ---- collect results ---------------------------------------------------
    async def n_collect_results(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        terminals: dict[str, dict] = {}
        processed: list[str] = []
        for attempt_id in list(state.get("pending_attempt_ids") or []):
            raw = (state.get("results") or {}).get(attempt_id)
            if raw is None:
                continue
            result = TaskResult.model_validate(raw)
            envelope = self._envelope_for(result)
            try:
                terminal = self.repos.terminals.get(attempt_id)
                if terminal is None:
                    terminal = self.task_service.receive_result(envelope, result)
            except ReceiptRejected as exc:
                self.sink(root_task_id, "controller").emit(
                    EventType.LATE_RESULT_AUDIT,
                    attempt_id=attempt_id,
                    payload={"code": exc.code, "message": exc.message},
                )
                continue
            terminals[attempt_id] = terminal.model_dump(mode="json")
            processed.append(result.result_id)
        return {
            "terminals": terminals,
            "processed_result_ids": processed,
            "pending_attempt_ids": [],
            "task_tree": self._task_tree(root_task_id),
            "attempts": self._attempt_map(root_task_id),
        }

    def _envelope_for(self, result: TaskResult) -> TaskEnvelope:
        attempt = self.repos.attempts.get(result.attempt_id)
        task = self.repos.tasks.get(result.task_id)
        if attempt is None or task is None:
            raise ReceiptRejected("UNKNOWN_ATTEMPT", f"未知 attempt {result.attempt_id}")
        return TaskEnvelope(
            root_task_id=attempt.root_task_id,
            parent_task_id=task.parent_task_id,
            task_id=attempt.task_id,
            attempt_id=attempt.attempt_id,
            dispatch_batch_id=attempt.dispatch_batch_id or "",
            agent_id=task.agent_id or "",
            agent_version=task.agent_version or "",
            task_kind=task.task_kind or "",
            goal=task.goal,
            input_refs=InputRefs(**attempt.input_refs),
            source_version=attempt.source_version,
            contract_version=attempt.contract_version or "",
            acceptance_criteria=["覆盖全部必需检查项"],
            constraints=attempt.constraints,
            fencing_token=attempt.fencing_token,
        )

    # ---- detect ------------------------------------------------------------
    async def n_detect(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        contract = self._contract(state)
        reports = self._batch_reports(state)
        application = (
            PatchApplication.model_validate(state["last_application"])
            if state.get("last_application")
            else None
        )
        application_error = (
            ErrorInfo.model_validate(state["last_application_error"])
            if state.get("last_application_error")
            else None
        )
        resolved_findings = self.detector.resolve_required_findings(
            root_task_id=root_task_id, source_version=state["source_version"]
        )
        detection = self.detector.detect(
            root_task_id=root_task_id,
            contract=contract,
            source_version=state["source_version"],
            patched=bool(state.get("patched")),
            reports=reports,
            application=application,
            application_error=application_error,
            controller_faults=list(state.get("controller_faults") or []),
            previous_signature=state.get("failure_signature"),
            previous_source_version=state.get("detected_source_version"),
            previous_patch_fingerprint=state.get("patch_fingerprint"),
        )
        self.sink(root_task_id).emit(
            EventType.DETECTION_COMPLETED,
            payload={
                "category": detection.category.value,
                "valid": detection.valid,
                "suggestion": detection.suggestion,
                "missing_items": detection.missing_items,
                "failed_check_ids": detection.failed_check_ids,
                "resolved_findings": resolved_findings,
            },
        )
        # The verdict is also persisted by version so the workbench can explain
        # "why" without replaying the graph (Desgin §6.3).
        self.artifacts.save_json(
            root_task_id=root_task_id,
            artifact_type=ArtifactType.DETECTION,
            data=detection.model_dump(mode="json"),
            source_version=state["source_version"],
        )
        signature = detection.facts.get("failure_signature")
        return {
            "detection": detection.model_dump(mode="json"),
            "failure_signature": signature or state.get("failure_signature"),
            "patch_fingerprint": (
                detection.progress.patch_fingerprint or state.get("patch_fingerprint")
            ),
            "detected_source_version": state["source_version"],
            "controller_faults": [],
            "feedback": [detection.suggestion or ""],
            "task_tree": self._task_tree(root_task_id),
        }

    # ---- apply patch -------------------------------------------------------
    async def n_apply_patch(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        action = parse_parent_action(state["next_action"])
        assert isinstance(action, ApplyPatchAction)
        try:
            application = self.controller.apply_patch(action, root_task_id=root_task_id)
        except (PatchRejected, ActionRejected) as exc:
            code = getattr(exc, "code", "PATCH_REJECTED")
            error = ErrorInfo(
                code=code,
                category=(
                    ErrorCategory.PATCH_CONFLICT
                    if code == "PATCH_CONFLICT"
                    else ErrorCategory.INVALID_PATCH
                ),
                message=str(exc),
                recoverable=False,
            )
            self.sink(root_task_id).emit(
                EventType.PATCH_APPLICATION_FAILED,
                payload={"code": code, "message": str(exc), "action": action.model_dump(mode="json")},
            )
            return {
                "last_application": None,
                "last_application_error": error.model_dump(mode="json"),
                "errors": [error.model_dump(mode="json")],
                "next_action": None,
                "current_batch_id": None,
                "pending_attempt_ids": [],
            }

        if application.status.value != "committed":
            error = ErrorInfo(
                code="PATCH_APPLICATION_FAILED",
                category=ErrorCategory.INVALID_PATCH,
                message=application.error or "补丁未能应用",
                recoverable=False,
            )
            self.sink(root_task_id).emit(
                EventType.PATCH_APPLICATION_FAILED,
                payload={"code": error.code, "message": error.message},
            )
            return {
                "last_application": None,
                "last_application_error": error.model_dump(mode="json"),
                "errors": [error.model_dump(mode="json")],
                "next_action": None,
                "current_batch_id": None,
                "pending_attempt_ids": [],
            }

        self.sink(root_task_id).emit(
            EventType.PATCH_APPLIED,
            payload={
                "application_id": application.application_id,
                "base_version": application.base_version,
                "result_version": application.result_version,
            },
        )
        return {
            "last_application": application.model_dump(mode="json"),
            "last_application_error": None,
            "patch_applications": {
                application.application_id: application.model_dump(mode="json")
            },
            "source_version": application.result_version,
            "source_versions": [application.result_version] if application.result_version else [],
            "patched": True,
            "current_batch_id": None,
            "pending_attempt_ids": [],
            "next_action": None,
            "task_tree": self._task_tree(root_task_id),
        }

    # ---- wait --------------------------------------------------------------
    async def n_wait(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        reason = state.get("waiting_reason") or "存在需要人工处理的问题"
        required = state.get("required_action") or "处理后请求恢复任务"
        # The known verdict is re-derived, never taken from the proposal: a
        # confirmed failing required item must stay false, not become "success".
        passed = state.get("passed")
        raw_contract = state.get("contract") or {}
        if raw_contract:
            contract = AcceptanceContract.model_validate(raw_contract)
            required_ids = self.detector.required_check_ids(
                contract, patched=bool(state.get("patched"))
            )
            _, computed, _, _unresolved = self.task_service.detect_completion(
                root_task_id=root_task_id,
                source_version=state["source_version"],
                required_check_ids=required_ids,
            )
            if computed is not None:
                passed = computed
        self.sink(root_task_id).emit(
            EventType.WAITING_RECOVERY,
            payload={
                "reason": reason,
                "required_action": required,
                "known_passed": passed,
                "proposed_passed": state.get("passed"),
            },
        )
        self.repos.tasks.update(
            root_task_id,
            status=RootStatus.WAITING_RECOVERY.value,
            passed=passed,
            passed_set=passed is not None,
            bump_revision=True,
        )
        # the blocking reason is persisted so the workbench can show it after a
        # restart without replaying the graph
        self.repos.controls.update(root_task_id, required_action=required)
        return {
            "status": RootStatus.WAITING_RECOVERY.value,
            "waiting_reason": reason,
            "required_action": required,
            "next_action": None,
            "task_tree": self._task_tree(root_task_id),
        }

    # ---- finalize ----------------------------------------------------------
    async def n_finalize(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        action = parse_parent_action(state["next_action"])
        assert isinstance(action, FinishAction)
        contract = self._contract(state)
        source_version = state["source_version"]
        patched = bool(state.get("patched"))
        required_ids = self.detector.required_check_ids(contract, patched=patched)

        if action.proposed_passed:
            can_finish, passed, problems, unresolved = self.task_service.detect_completion(
                root_task_id=root_task_id,
                source_version=source_version,
                required_check_ids=required_ids,
            )
            if not (can_finish and passed is True):
                return self._reject(
                    state,
                    "FINISH_NOT_SATISFIED",
                    "; ".join(problems) or "完成条件尚未满足，不能标记通过",
                )
            unfixable_reason = None
        else:
            # finish(false) is only honoured for a *confirmed* current-version failure
            # with the parent naming the capability limit — never for a mere budget
            # exhaustion, a missing evidence item or a still-running attempt (§5.4).
            confirmed, failed = self.task_service.confirmed_required_failure(
                root_task_id=root_task_id,
                source_version=source_version,
                required_check_ids=required_ids,
            )
            if not confirmed:
                return self._reject(
                    state,
                    "FINISH_NOT_SUPPORTED",
                    "证据不足以判定不通过：当前版本缺少明确失败的必需检查（应进入等待或补查）",
                )
            if self.task_service.has_in_flight_work(root_task_id):
                return self._reject(
                    state, "FINISH_NOT_SUPPORTED", "仍有未结束的执行尝试或未收齐的批次，不能结案"
                )
            passed = False
            unresolved = failed
            unfixable_reason = action.reason

        skipped = self.controller.finalize_skips(root_task_id=root_task_id, passed=passed)
        latest = self.detector.current_checks(root_task_id, source_version)
        not_run = [c.check_id for c in contract.checks if c.check_id not in latest]
        if passed is True and patched:
            scope = "修改后的必需检查已在当前版本重新执行并通过，验证证据绑定当前版本"
        elif passed is True:
            scope = "首次审查完整通过，修复与验证未执行，结论仅覆盖已执行的审查检查"
        else:
            scope = f"已确认存在无法自动修复的必需缺陷 {unresolved}：{unfixable_reason}"
        report, report_ref = self.controller.build_report(
            root_task_id=root_task_id,
            workflow=self.workflow,
            contract=contract,
            source_version=source_version,
            status=RootStatus.COMPLETED.value,
            passed=passed,
            goal=state["goal"],
            conclusion_scope=scope,
            unresolved=unresolved,
            not_run=not_run,
            skipped=skipped,
            execution_summary={
                "attempts": len(self.repos.attempts.list_by_root(root_task_id)),
                "batches": len(self.repos.batches.list_by_root(root_task_id)),
                "repair_rounds": self.locks.budget_state(
                    root_task_id, self.workflow.budgets
                ).get("repair_round", {}).get("consumed", 0),
                "check_mode": state["check_mode"],
                "unfixable_reason": unfixable_reason,
            },
        )
        self.repos.tasks.update(
            root_task_id,
            status=RootStatus.COMPLETED.value,
            passed=passed,
            passed_set=True,
            bump_revision=True,
        )
        self.repos.controls.update(root_task_id, required_action="")
        self.sink(root_task_id).emit(
            EventType.TASK_COMPLETED,
            payload={
                "passed": passed,
                "report_ref": report_ref,
                "conclusion_scope": scope,
                "skipped": skipped,
            },
            artifact_refs=[report_ref],
        )
        return {
            "status": RootStatus.COMPLETED.value,
            "passed": passed,
            "report_ref": report_ref,
            "final_report": report.model_dump(mode="json"),
            "skipped_tasks": {s["task_id"]: s["skip_reason"] for s in skipped},
            "next_action": None,
            "task_tree": self._task_tree(root_task_id),
        }
