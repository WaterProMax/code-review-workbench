"""LangGraph nodes: the real execution path of the parent/child workflow (§8.2).

Every node is a small, auditable step. The parent control layer owns the state
transitions; child agents only ever return a report that the receiving control
layer validates before it may influence the verdict.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Literal, TypedDict

from langgraph.types import Command, Overwrite, Send, interrupt

from app.agents.base import AgentContext
from app.agents.parent import ParentAgent, ParentPlan
from app.providers.base import LLMClient, LLMError
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
    AttemptStatus,
    ErrorCategory,
    EventType,
    RetryReason,
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


class WorkflowNodeError(RuntimeError):
    """A graph node hit an invariant it cannot record as a normal rejection."""


class AttemptPayload(TypedDict):
    """Input schema for one ``Send`` branch. Kept off the parent channels."""

    envelope: dict


def _resume_overrides_from(decision: dict) -> dict[str, Any]:
    """Map a human resume payload onto channels. Rejections start a new segment."""
    if not isinstance(decision, dict):
        raise WorkflowNodeError("恢复决定必须是对象")
    update = {
        key: value
        for key, value in decision.items()
        if key not in {"action", "grants"}
    }
    update["action_rejections"] = Overwrite([])
    update["branch_faults"] = Overwrite([])
    update["rejection_count"] = 0
    update.setdefault("status", RootStatus.RUNNING.value)
    update.setdefault("next_action", None)
    update.setdefault("fatal_error", None)
    update.setdefault("waiting_reason", None)
    update.setdefault("required_action", None)
    return update


def action_goto(next_action: dict | None) -> str:
    """Successor of an already validated parent action.

    Missing or unknown actions go back through the step-budget gate.
    """
    if not isinstance(next_action, dict):
        return "budget"
    return {
        "dispatch_task": "dispatch",
        "dispatch_batch": "dispatch",
        "apply_patch": "apply_patch",
        "wait_for_recovery": "enter_wait",
        "finish": "check_finish",
    }.get(str(next_action.get("action") or ""), "budget")


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
        self.cancellation = asyncio.Event()
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
        context.cancellation = self.cancellation

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

    # ---- routing (conditional edges the diagram must show) ----------------
    def route_after_initialize(self, state: dict) -> Literal["enter_wait", "plan", "budget"]:
        if state.get("fatal_error"):
            return "enter_wait"
        if state.get("contract"):
            return "budget"
        return "plan"

    def route_after_validate_plan(
        self, state: dict
    ) -> Literal["replan", "commit_plan", "enter_wait"]:
        if not state.get("plan_problems"):
            return "commit_plan"
        if int(state.get("plan_corrections") or 0) < self.settings.max_parent_corrections:
            return "replan"
        return "enter_wait"

    def route_after_budget(self, state: dict) -> Literal["enter_wait", "parent_decide"]:
        steps, limit = self._next_graph_step(state)
        return "enter_wait" if steps > limit else "parent_decide"

    def _next_graph_step(self, state: dict) -> tuple[int, int]:
        """The business step the next parent decision would consume, and its grant.

        This ledger is ``graph_steps`` / ``graph_steps_granted``. It is not the
        framework ``recursion_limit``.
        """
        steps = int(state.get("graph_steps") or 0) + 1
        control = self.repos.controls.get(state["root_task_id"])
        limit = control.graph_steps_granted if control else self.workflow.budgets.max_graph_steps
        return steps, limit

    def _plan_files(self, state: dict) -> list[str]:
        version = state.get("initial_source_version") or state.get("source_version")
        return self.workspace.list_files(state["root_task_id"], version)

    # ---- initialize / plan correction --------------------------------------
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
        steps = int(state.get("graph_steps") or 0) + 1

        contract_row = self.repos.contracts.latest(root_task_id)
        if contract_row is None:
            # Planning is a graph loop (plan → validate_plan → replan), not an
            # inner while. One business step covers the whole initialization.
            return {
                "status": RootStatus.RUNNING.value,
                "graph_steps": steps,
                "plan_corrections": 0,
                "plan_problems": [],
                "fatal_error": None,
            }

        contract = AcceptanceContract.model_validate(
            self.artifacts.read_json(contract_row["artifact_id"])
        )
        contract_artifact = contract_row["artifact_id"]
        self.controller.create_standard_children(
            root_task_id=root_task_id, workflow=self.workflow, goal=goal
        )
        return {
            "contract": contract.model_dump(mode="json"),
            "contract_version": contract.contract_version,
            "contract_artifact": contract_artifact,
            "acceptance_contract_ref": contract_artifact,
            "review_scope": state.get("review_scope") or [],
            "acceptance_criteria": state.get("acceptance_criteria") or [],
            "task_tree": self._task_tree(root_task_id),
            "children_created": True,
            "status": RootStatus.RUNNING.value,
            "graph_steps": steps,
            "fatal_error": None,
        }

    async def n_plan(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        files = self._plan_files(state)
        plan = await self.parent.plan(
            root_task_id=root_task_id,
            goal=state["goal"],
            files=files,
            operation_key=root_task_id,
        )
        return {"draft_plan": plan.model_dump(mode="json"), "plan_problems": []}

    async def n_validate_plan(self, state: dict) -> dict:
        plan = ParentPlan.model_validate(state["draft_plan"])
        problems = self.controller.validate_plan(
            plan.checks, goal=state["goal"], files=self._plan_files(state)
        )
        corrections = int(state.get("plan_corrections") or 0)
        update: dict[str, Any] = {"plan_problems": problems}
        if problems and corrections >= self.settings.max_parent_corrections:
            update.update(
                {
                    "status": RootStatus.WAITING_RECOVERY.value,
                    "waiting_reason": "父模型未能给出合法的检查合同",
                    "required_action": "检查目标描述与模型配置后恢复任务",
                    "fatal_error": {"code": "INVALID_PLAN", "problems": problems},
                }
            )
        else:
            update["fatal_error"] = None
        return update

    async def n_replan(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        previous = ParentPlan.model_validate(state["draft_plan"])
        plan = await self.parent.replan(
            root_task_id=root_task_id,
            goal=state["goal"],
            files=self._plan_files(state),
            problems=list(state.get("plan_problems") or []),
            previous=previous,
            operation_key=root_task_id,
        )
        return {
            "draft_plan": plan.model_dump(mode="json"),
            "plan_corrections": int(state.get("plan_corrections") or 0) + 1,
        }

    async def n_commit_plan(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        goal = state["goal"]
        plan = ParentPlan.model_validate(state["draft_plan"])
        contract, contract_artifact = self.controller.persist_contract(
            root_task_id=root_task_id, goal=goal, checks=plan.checks
        )
        self.controller.create_standard_children(
            root_task_id=root_task_id, workflow=self.workflow, goal=goal
        )
        return {
            "contract": contract.model_dump(mode="json"),
            "contract_version": contract.contract_version,
            "contract_artifact": contract_artifact,
            "acceptance_contract_ref": contract_artifact,
            "review_scope": plan.review_scope,
            "acceptance_criteria": plan.acceptance_criteria,
            "task_tree": self._task_tree(root_task_id),
            "children_created": True,
            "status": RootStatus.RUNNING.value,
            "plan_problems": [],
            "fatal_error": None,
        }

    async def n_budget(self, state: dict) -> dict:
        """Business step-budget gate. The conditional edge is budget → enter_wait."""
        steps, limit = self._next_graph_step(state)
        if steps <= limit:
            return {}
        reason = f"图执行步数达到上限 {limit}"
        required = "追加图执行步数许可后恢复任务"
        return {
            "next_action": WaitForRecoveryAction(
                reason=reason, required_action=required
            ).model_dump(mode="json"),
            "waiting_reason": reason,
            "required_action": required,
        }

    # ---- parent decision ---------------------------------------------------
    async def n_parent_decide(self, state: dict) -> Command[Literal["validate_action"]]:
        root_task_id = state["root_task_id"]
        steps = int(state.get("graph_steps") or 0) + 1
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
            return Command(
                update={
                    "next_action": WaitForRecoveryAction(
                        reason=reason, required_action=required
                    ).model_dump(mode="json"),
                    "waiting_reason": reason,
                    "required_action": required,
                    "graph_steps": steps,
                },
                goto="validate_action",
            )

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
        return Command(
            update={
                "next_action": action.model_dump(mode="json"),
                "decision_ids": [f"d-{uuid.uuid4().hex[:12]}"],
                "feedback": [decision.reasoning],
                "graph_steps": steps,
            },
            goto="validate_action",
        )

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
        # Resume clears the channel with Overwrite. An empty list is this segment.
        return list(state.get("action_rejections") or [])

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
    async def n_validate_action(
        self, state: dict
    ) -> Command[Literal["dispatch", "apply_patch", "check_finish", "enter_wait", "budget"]]:
        raw = state.get("next_action")
        if not raw:
            return Command(goto="budget")
        try:
            action = parse_parent_action(raw)
        except Exception as exc:  # noqa: BLE001 - an unparseable proposal is rejected
            return Command(
                update=self._reject(state, "UNPARSEABLE_ACTION", f"动作无法解析：{exc}"),
                goto="budget",
            )

        problems = self.controller.validate_action(
            action,
            root_task_id=state["root_task_id"],
            workflow=self.workflow,
            source_version=state["source_version"],
        )
        if problems:
            return Command(
                update=self._reject(
                    state, "ILLEGAL_ACTION", "; ".join(problems), details={"action": raw}
                ),
                goto="budget",
            )

        update: dict[str, Any] = {"rejection_count": 0}
        if isinstance(action, WaitForRecoveryAction):
            update["waiting_reason"] = action.reason
            update["required_action"] = action.required_action
            update["passed"] = action.known_passed
        return Command(update=update, goto=action_goto(raw))

    # ---- dispatch ----------------------------------------------------------
    async def n_dispatch(
        self, state: dict
    ) -> Command[Literal["run_attempt", "budget", "collect_results"]]:
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
            return Command(
                update=self._reject(
                    state, getattr(exc, "code", "DISPATCH_REJECTED"), str(exc)
                ),
                goto="budget",
            )
        except BudgetExhausted as exc:
            return Command(
                update=self._reject(
                    state,
                    "BUDGET_EXHAUSTED",
                    f"额度不足，需要追加 {exc.required_additions}",
                    details={"required_additions": exc.required_additions},
                ),
                goto="budget",
            )

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
                return Command(
                    update=self._reject(
                        state, "BUDGET_EXHAUSTED",
                        f"恢复未完成分支需要追加额度 {exc.required_additions}",
                        details={"required_additions": exc.required_additions},
                    ),
                    goto="budget",
                )
            for i, retry in zip(interrupted, retries):
                plan.attempts[i] = retry
            plan.batch_id = retry_batch
        envelopes = [
            self.controller.build_envelope(attempt, task, item, self.workflow)
            for attempt, task, item in zip(plan.attempts, plan.tasks, plan.items)
        ]
        update = {
            "attempts": {a.attempt_id: a.model_dump(mode="json") for a in plan.attempts},
            "pending_attempt_ids": [envelope.attempt_id for envelope in envelopes],
            "current_batch_id": plan.batch_id,
            "dispatch_ok": True,
            "branch_faults": Overwrite([]),
            "controller_faults": [],
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
        # An empty Send list does not advance the superstep. Collect instead.
        if not envelopes:
            return Command(update=update, goto="collect_results")
        return Command(
            update=update,
            goto=[
                Send("run_attempt", {"envelope": envelope.model_dump(mode="json")})
                for envelope in envelopes
            ],
        )

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

    def _branch_fault(self, envelope: TaskEnvelope, *, recoverable: bool = True) -> dict:
        return {
            "attempt_id": envelope.attempt_id,
            "task_id": envelope.task_id,
            "code": "CONTROLLER_TERMINAL",
            "recoverable": recoverable,
        }

    def _with_live_fence(self, envelope: TaskEnvelope) -> TaskEnvelope:
        """Rebind a checkpointed envelope to the lease this process holds.

        ``Send`` freezes the token from dispatch. After a crash the new owner
        holds a higher token. A stale executor never receives that token, so
        updating the attempt row here does not let a fenced caller publish.
        """
        token = int(self.fencing_token or 0)
        if token <= 0 or envelope.fencing_token == token:
            return envelope
        control = self.repos.controls.get(envelope.root_task_id)
        if control is None or int(control.fencing_token) != token:
            return envelope
        attempt = self.repos.attempts.get(envelope.attempt_id)
        if attempt is not None and int(attempt.fencing_token) != token:
            with self.repos.db.transaction() as conn:
                conn.execute(
                    "UPDATE attempts SET fencing_token = ? WHERE attempt_id = ?",
                    (token, envelope.attempt_id),
                )
        return envelope.model_copy(update={"fencing_token": token})

    async def n_run_attempt(self, payload: AttemptPayload) -> dict:
        """Run one attempt from a ``Send`` payload. Sibling branches do not share this call."""
        raw = payload.get("envelope") if isinstance(payload, dict) else None
        try:
            envelope = TaskEnvelope.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - a bad payload is one branch's fault
            return {
                "branch_faults": [
                    {
                        "attempt_id": None,
                        "code": "CONTROLLER_TERMINAL",
                        "recoverable": False,
                        "message": f"派发封套无效：{exc}"[:400],
                    }
                ]
            }
        existing = self.repos.results.get_by_attempt(envelope.attempt_id)
        if existing is not None:
            return {"results": {envelope.attempt_id: existing.model_dump(mode="json")}}
        envelope = self._with_live_fence(envelope)
        # An interrupted attempt already has finished_at. Re-running it sets a
        # new started_at; leaving the old finish time makes the row invalid.
        prior = self.repos.attempts.get(envelope.attempt_id)
        if prior is not None and prior.finished_at is not None:
            with self.repos.db.transaction() as conn:
                conn.execute(
                    "UPDATE attempts SET finished_at = NULL WHERE attempt_id = ?",
                    (envelope.attempt_id,),
                )
        timeout = envelope.constraints.timeout_seconds
        try:
            result = await asyncio.wait_for(self.adapter.execute(envelope), timeout=timeout)
            self.task_service.receive_result(envelope, result)
            return {"results": {envelope.attempt_id: result.model_dump(mode="json")}}
        except TimeoutError:
            self._controller_terminal(
                envelope,
                "ATTEMPT_TIMEOUT",
                f"尝试在 {timeout}s 内未结束，控制层撤销该尝试并记录故障终态",
                category=ErrorCategory.ATTEMPT_TIMEOUT,
            )
            return {"branch_faults": [self._branch_fault(envelope)]}
        except ResultIdentityError as exc:
            self._controller_terminal(
                envelope, "RESULT_IDENTITY_MISMATCH", "; ".join(exc.problems)
            )
            return {"branch_faults": [self._branch_fault(envelope)]}
        except LLMError as exc:
            if exc.recoverable:
                raise
            self._controller_terminal(
                envelope,
                exc.code,
                exc.message[:400],
                category=exc.category,
            )
            return {"branch_faults": [self._branch_fault(envelope, recoverable=False)]}
        except Exception as exc:  # noqa: BLE001 - one branch must not kill the batch
            try:
                self._controller_terminal(
                    envelope,
                    "ATTEMPT_EXECUTION_ERROR",
                    f"{type(exc).__name__}: {exc}"[:400],
                    category=ErrorCategory.UNKNOWN,
                )
            except Exception:
                # The fault channel still has to move; a second ledger error is not fatal.
                pass
            return {"branch_faults": [self._branch_fault(envelope)]}

    # ---- collect results ---------------------------------------------------
    async def n_collect_results(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        terminals: dict[str, dict] = {}
        processed: list[str] = []
        fault_ids = {
            str(item.get("attempt_id"))
            for item in (state.get("branch_faults") or [])
            if isinstance(item, dict) and item.get("attempt_id")
        }
        results = state.get("results") or {}
        for attempt_id in list(state.get("pending_attempt_ids") or []):
            if attempt_id in fault_ids and attempt_id not in results:
                continue
            raw = results.get(attempt_id)
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
        branch_faults = list(state.get("branch_faults") or [])
        legacy_faults = list(state.get("controller_faults") or [])
        faults = branch_faults if branch_faults else legacy_faults
        detection = self.detector.detect(
            root_task_id=root_task_id,
            contract=contract,
            source_version=state["source_version"],
            patched=bool(state.get("patched")),
            reports=reports,
            application=application,
            application_error=application_error,
            controller_faults=faults,
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
            "branch_faults": Overwrite([]),
            "controller_faults": [],
            "feedback": [detection.suggestion or ""],
            "task_tree": self._task_tree(root_task_id),
        }

    # ---- apply patch -------------------------------------------------------
    async def n_apply_patch(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        action = parse_parent_action(state["next_action"])
        if not isinstance(action, ApplyPatchAction):
            return self._reject(state, "ACTION_MISMATCH", "当前节点只接受 apply_patch")
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

    # ---- wait: side effects once, then a pure interrupt --------------------
    async def n_enter_wait(self, state: dict) -> dict:
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

    async def n_await_human(
        self, state: dict
    ) -> Command[Literal["budget", "finalize_terminated"]]:
        """Park until a human resumes. No writes: this node replays from the top."""
        decision = interrupt(
            {
                "reason": state.get("waiting_reason"),
                "required_action": state.get("required_action"),
                "budgets": self.locks.budget_state(
                    state["root_task_id"], self.workflow.budgets
                ),
            }
        )
        if not isinstance(decision, dict):
            raise WorkflowNodeError("恢复决定必须是对象")
        if decision.get("action") == "terminate":
            return Command(
                update={
                    "waiting_reason": decision.get("reason") or state.get("waiting_reason")
                },
                goto="finalize_terminated",
            )
        return Command(update=_resume_overrides_from(decision), goto="budget")

    def _termination_verdict(self, state: dict) -> bool | None:
        raw = state.get("contract") or {}
        if not raw or not state.get("source_version"):
            return None
        contract = AcceptanceContract.model_validate(raw)
        required_ids = self.detector.required_check_ids(
            contract, patched=bool(state.get("patched"))
        )
        _can_finish, passed, _problems, _unresolved = self.task_service.detect_completion(
            root_task_id=state["root_task_id"],
            source_version=state["source_version"],
            required_check_ids=required_ids,
        )
        return passed

    async def n_finalize_terminated(self, state: dict) -> dict:
        """Project a terminate decision. Skip the ledger when recovery already wrote it."""
        root_task_id = state["root_task_id"]
        reason = state.get("waiting_reason") or "用户终止任务"
        root = self.repos.tasks.get(root_task_id)
        existing = self.repos.reports.latest(root_task_id, "final")
        if (
            existing is not None
            and root is not None
            and root.status in TERMINAL_ROOT_STATUSES
        ):
            report = self.artifacts.read_json(existing["artifact_id"])
            return {
                "status": root.status,
                "passed": root.passed,
                "report_ref": existing["artifact_id"],
                "final_report": report if isinstance(report, dict) else None,
                "waiting_reason": None,
                "required_action": None,
                "next_action": None,
                "task_tree": self._task_tree(root_task_id),
            }
        passed = self._termination_verdict(state)
        final_status = (
            RootStatus.PARTIAL.value if passed is None else RootStatus.COMPLETED.value
        )
        skipped = self.controller.finalize_skips(root_task_id=root_task_id, passed=passed)
        contract = (
            AcceptanceContract.model_validate(state["contract"])
            if state.get("contract")
            else None
        )
        source_version = state.get("source_version")
        latest = (
            self.detector.current_checks(root_task_id, source_version)
            if source_version
            else {}
        )
        if contract is None:
            not_run = ["检查合同尚未建立"]
        else:
            not_run = [
                check.check_id
                for check in contract.checks
                if check.check_id not in latest
                or latest[check.check_id].status.value in {"not_run", "inconclusive"}
            ]
        unresolved = [
            finding.finding_id
            for finding in self.detector.open_required_findings(root_task_id)
        ]
        report, report_ref = self.controller.build_report(
            root_task_id=root_task_id,
            workflow=self.workflow,
            contract=contract,
            source_version=source_version,
            status=final_status,
            passed=passed,
            goal=state.get("goal") or (root.goal if root else ""),
            conclusion_scope=f"用户终止任务：{reason}；结论仅依据终止前已有证据",
            unresolved=unresolved,
            not_run=not_run,
            skipped=skipped,
            execution_summary={
                "terminated": True,
                "termination_reason": reason,
                "attempts": len(self.repos.attempts.list_by_root(root_task_id)),
            },
        )
        self.repos.tasks.update(
            root_task_id,
            status=final_status,
            passed=passed,
            passed_set=True,
            bump_revision=True,
        )
        self.repos.controls.update(root_task_id, required_action="")
        self.sink(root_task_id).emit(
            EventType.TASK_COMPLETED,
            payload={
                "terminated": True,
                "reason": reason,
                "final_status": final_status,
                "passed": passed,
                "report_ref": report_ref,
            },
            artifact_refs=[report_ref],
        )
        return {
            "status": final_status,
            "passed": passed,
            "report_ref": report_ref,
            "final_report": report.model_dump(mode="json"),
            "skipped_tasks": {item["task_id"]: item["skip_reason"] for item in skipped},
            "waiting_reason": None,
            "required_action": None,
            "next_action": None,
            "task_tree": self._task_tree(root_task_id),
        }

    # ---- finish gate -------------------------------------------------------
    def _assess_finish(self, state: dict) -> tuple[dict | None, dict | None]:
        """Decide whether a finish proposal may close the task.

        Returns ``(reject_update, sketch)``. The reject update already records
        the denial. The sketch is the accepted verdict; it has no side effects.
        """
        root_task_id = state["root_task_id"]
        raw = state.get("next_action")
        try:
            action = parse_parent_action(raw)
        except Exception as exc:  # noqa: BLE001 - an unparseable proposal is rejected
            return self._reject(state, "UNPARSEABLE_ACTION", f"动作无法解析：{exc}"), None
        if not isinstance(action, FinishAction):
            return self._reject(state, "ACTION_MISMATCH", "当前节点只接受 finish"), None
        contract = self._contract(state)
        source_version = state["source_version"]
        required_ids = self.detector.required_check_ids(contract, patched=bool(state.get("patched")))

        if action.proposed_passed:
            can_finish, passed, problems, unresolved = self.task_service.detect_completion(
                root_task_id=root_task_id,
                source_version=source_version,
                required_check_ids=required_ids,
            )
            if not (can_finish and passed is True):
                return (
                    self._reject(
                        state,
                        "FINISH_NOT_SATISFIED",
                        "; ".join(problems) or "完成条件尚未满足，不能标记通过",
                    ),
                    None,
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
                return (
                    self._reject(
                        state,
                        "FINISH_NOT_SUPPORTED",
                        "证据不足以判定不通过：当前版本缺少明确失败的必需检查（应进入等待或补查）",
                    ),
                    None,
                )
            if self.task_service.has_in_flight_work(root_task_id):
                return (
                    self._reject(
                        state, "FINISH_NOT_SUPPORTED", "仍有未结束的执行尝试或未收齐的批次，不能结案"
                    ),
                    None,
                )
            passed = False
            unresolved = failed
            unfixable_reason = action.reason
        return None, {
            "passed": passed,
            "unresolved": list(unresolved),
            "unfixable_reason": unfixable_reason,
        }

    async def n_check_finish(
        self, state: dict
    ) -> Command[Literal["finalize", "budget"]]:
        reject, sketch = self._assess_finish(state)
        if reject is not None:
            return Command(update=reject, goto="budget")
        return Command(update={"finish_sketch": sketch}, goto="finalize")

    # ---- finalize (only outgoing edge is END) ------------------------------
    async def n_finalize(self, state: dict) -> dict:
        root_task_id = state["root_task_id"]
        sketch = state.get("finish_sketch")
        if not sketch:
            raise WorkflowNodeError("finalize 只能在完成条件已经成立时执行")
        passed = sketch["passed"]
        unresolved = sketch["unresolved"]
        unfixable_reason = sketch["unfixable_reason"]
        contract = self._contract(state)
        source_version = state["source_version"]
        patched = bool(state.get("patched"))

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
