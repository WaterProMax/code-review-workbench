"""The deterministic control layer around the parent's proposals (§4.4, §8.2).

The parent model proposes; this module validates every proposal against the
frozen contract, the task tree, the registry, dependencies and the budget ledger,
and performs the resulting side effects (registering a batch, applying a patch,
finalising a report). Nothing here trusts a model claim: identities, versions,
budgets and completion conditions are re-derived from persisted state.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from app.extensions.base import (
    SCOPE_COMMITTED_APPLICATION,
    InputAdapter,
    default_input_adapters,
)
from app.registry.agents import AgentRegistry
from app.schemas.actions import (
    ApplyPatchAction,
    DispatchBatchAction,
    DispatchItem,
    DispatchTaskAction,
    FinishAction,
    ParentAction,
)
from app.schemas.artifacts import PatchApplication
from app.schemas.common import TaskConstraints
from app.schemas.enums import (
    ApplicablePhase,
    ArtifactType,
    AttemptStatus,
    CheckMethod,
    CheckStatus,
    EventType,
    PatchApplicationStatus,
    RetryReason,
    ResultStatus,
    SkipReason,
    TaskKind,
    TaskStatus,
    TerminalOrigin,
    WorkflowNodeType,
)
from app.schemas.results import (
    AcceptanceContract,
    CheckSpec,
    CheckSummary,
    FinalReport,
)
from app.schemas.tasks import Attempt, InputRefs, Task, TaskEnvelope
from app.schemas.workflows import WorkflowConfig, WorkflowNode
from app.services.artifacts import ArtifactService
from app.services.events import EventSink
from app.services.task_creation import TaskCreationService
from app.services.task_service import TaskService
from app.services.workspace import WorkspaceService
from app.settings import Settings
from app.storage.repositories import Repos
from app.workflow.detection import Detector

DEFAULT_NODE_FOR_KIND = {
    TaskKind.REVIEW.value: "review",
    TaskKind.FIX.value: "fix",
    TaskKind.VERIFY.value: "verify",
}

# Input artifact -> the execution condition it implies for a dependent node.
ARTIFACT_CONDITION = {
    "finding": "review_completed",
    "patch_application": "patch_applied",
    "test_artifact": "task_completed",
}

AUTO = "auto"


class ActionRejected(RuntimeError):
    """The proposed action is not legal for the current state."""

    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def role_map(workflow: WorkflowConfig) -> dict[str, str]:
    """task_kind -> agent_id for the standard roles of this workflow.

    Only the first node of each kind is reported, so the parallel template's
    ``recheck`` (which reuses the reviewer) does not shadow the initial review.
    """
    out: dict[str, str] = {}
    for kind, preferred in DEFAULT_NODE_FOR_KIND.items():
        node = workflow.node(preferred)
        if node is not None and node.task_kind == kind and node.agent_id:
            out[kind] = node.agent_id
            continue
        for candidate in workflow.nodes:
            if candidate.type is WorkflowNodeType.AGENT and candidate.task_kind == kind:
                out[kind] = candidate.agent_id or ""
                break
    return out


@dataclass
class DispatchPlan:
    batch_id: str
    attempts: list[Attempt]
    items: list[DispatchItem]
    tasks: list[Task]


class ParentController:
    def __init__(
        self,
        *,
        repos: Repos,
        task_service: TaskService,
        registry: AgentRegistry,
        workspace: WorkspaceService,
        artifacts: ArtifactService,
        detector: Detector,
        settings: Settings,
        input_adapters: list[InputAdapter] | None = None,
    ) -> None:
        self.repos = repos
        self.task_service = task_service
        self.registry = registry
        self.workspace = workspace
        self.artifacts = artifacts
        self.detector = detector
        self.settings = settings
        # Input references are filled by registered adapters: the three base roles
        # plus any registered extension (P10), so no task kind needs its own branch.
        self.input_adapters = list(input_adapters or default_input_adapters())

    # ---- sinks -------------------------------------------------------------
    def sink(self, root_task_id: str, actor: str = "parent") -> EventSink:
        from app.services.events import EventContext

        return EventSink(self.repos.events, EventContext(root_task_id=root_task_id, actor_id=actor))

    # ---- planning / contract ----------------------------------------------
    def validate_plan(
        self, checks: Iterable[CheckSpec], *, goal: str, files: Iterable[str]
    ) -> list[str]:
        checks = list(checks)
        problems: list[str] = []
        if not checks:
            return ["检查合同至少需要一项检查"]

        required = [c for c in checks if c.required]
        if not required:
            problems.append("检查合同至少需要一项 required 检查")

        initial = [
            c
            for c in required
            if c.applicable_phase in (ApplicablePhase.BOTH, ApplicablePhase.INITIAL_REVIEW)
        ]
        if not initial:
            problems.append(
                "用户必需目标不能只放在可跳过的 post_patch 阶段，"
                "至少一项 required 检查的 applicable_phase 必须是 initial_review 或 both"
            )

        both_goals = {
            c.goal_ref
            for c in required
            if c.applicable_phase
            in (ApplicablePhase.BOTH, ApplicablePhase.POST_PATCH)
        }
        for check in required:
            if (
                check.method is CheckMethod.MODEL_REVIEW
                and check.applicable_phase is not ApplicablePhase.INITIAL_REVIEW
            ):
                problems.append(
                    f"必需检查 {check.check_id} 使用 model_review，它不是可执行证据，"
                    "不能作为修改后（both/post_patch）的必需检查；"
                    "请改为 initial_review，或改用 syntax/static_rule/behavior_test"
                )
            if (
                check.applicable_phase is ApplicablePhase.INITIAL_REVIEW
                and check.goal_ref not in both_goals
            ):
                problems.append(
                    f"必需检查 {check.check_id} 只在 initial_review 阶段适用，"
                    f"其用户目标 {check.goal_ref!r} 缺少 both/post_patch 的等价必需检查，"
                    "修改后无法重新证明该目标；请补充等价检查或把该检查设为 required=false"
                )

        known_files = set(files)
        for check in checks:
            runtime_goal = check.goal_ref + " " + check.pass_condition
            if (any(word in runtime_goal for word in ("并行复审", "并行审查", "并行验证", "分支均", "收齐", "dispatch_batch"))
                    and any(word in runtime_goal for word in ("版本", "分支", "回报", "派发"))):
                problems.append(f"检查 {check.check_id} 是工作流运行要求，请移至 execution_requirements；不得用源码 behavior_test 验证分支派发/收齐。")
            scope = [s.split(":")[0].strip() for s in check.scope]
            unknown = [s for s in scope if s and s not in known_files and "*" not in s]
            if unknown:
                problems.append(
                    f"检查 {check.check_id} 的范围引用了不存在的文件：{sorted(unknown)}"
                )
        if not goal.strip():
            problems.append("缺少用户目标，无法建立检查合同")
        return problems

    def persist_contract(
        self,
        *,
        root_task_id: str,
        goal: str,
        checks: list[CheckSpec],
        supersedes: str | None = None,
        append_reason: str | None = None,
    ) -> tuple[AcceptanceContract, str]:
        existing = self.repos.contracts.list_by_root(root_task_id)
        version = supersedes or (existing[-1]["contract_version"] if existing else "contract-1")
        if existing and supersedes is None:
            # append a new immutable version instead of mutating the old one
            version = f"contract-{len(existing) + 1}"
        contract = AcceptanceContract(
            contract_version=version,
            root_task_id=root_task_id,
            user_goal=goal,
            checks=checks,
            supersedes=supersedes,
            append_reason=append_reason,
        )
        if supersedes:
            previous = self.repos.contracts.get(supersedes, root_task_id)
            if previous is None:
                raise ActionRejected("UNKNOWN_CONTRACT", f"未知的合同版本 {supersedes}")
            artifact_id = previous["artifact_id"]
            previous_contract = AcceptanceContract.model_validate(
                self.artifacts.read_json(artifact_id)
            )
            lost = contract.diff_required_checks(previous_contract)
            if lost:
                raise ActionRejected(
                    "CONTRACT_DOWNGRADE",
                    f"追加合同不得删除或降级已有必需检查：{sorted(lost)}",
                )

        artifact = self.artifacts.save_json(
            root_task_id=root_task_id,
            artifact_type=ArtifactType.ACCEPTANCE_CONTRACT,
            data=contract.model_dump(mode="json"),
            name=f"contract-{contract.contract_version}",
        )
        self.repos.contracts.insert(
            contract_version=contract.contract_version,
            root_task_id=root_task_id,
            content_hash=contract.content_hash(),
            artifact_id=artifact.artifact_id,
            supersedes=supersedes,
            append_reason=append_reason,
        )
        return contract, artifact.artifact_id

    # ---- task tree ---------------------------------------------------------
    def create_standard_children(
        self, *, root_task_id: str, workflow: WorkflowConfig, goal: str
    ) -> dict[str, Task]:
        """Register one child task per agent node (registration is not execution).

        Child tasks are keyed by workflow node id, so the parallel template's
        ``recheck`` node gets its own task while reusing the reviewer role — the
        role count does not grow, the task count does (§5, §9).
        """
        root = self.repos.tasks.get(root_task_id)
        if root is None:
            raise ActionRejected("UNKNOWN_ROOT", f"未知总任务 {root_task_id}")

        nodes = [
            n
            for n in workflow.nodes
            if n.type is WorkflowNodeType.AGENT and n.agent_id and n.task_kind
        ]
        if not nodes:
            raise ActionRejected("WORKFLOW_INCOMPLETE", "工作流没有可执行的 Agent 节点")
        by_kind = {n.task_kind: n for n in nodes if n.id in DEFAULT_NODE_FOR_KIND.values()}
        missing = [k for k in DEFAULT_NODE_FOR_KIND if k not in by_kind]
        if missing:
            raise ActionRejected("WORKFLOW_INCOMPLETE", f"工作流缺少任务类型 {missing}")

        children: dict[str, Task] = {}
        for node in nodes:
            assert node.agent_id and node.task_kind
            depends = self._depends_for(
                node.id, by_kind, children, workflow=workflow, nodes=nodes
            )
            children[node.id] = self._create_child(
                root,
                workflow,
                node.agent_id,
                node.task_kind,
                f"{node.task_kind}：{goal}（节点 {node.id}）",
                depends,
                node_id=node.id,
            )
        return children

    def _depends_for(
        self,
        node_id: str,
        by_kind: dict[str, WorkflowNode],
        children: dict[str, Task],
        *,
        workflow: WorkflowConfig | None = None,
        nodes: list[WorkflowNode] | None = None,
    ) -> list:  # type: ignore[type-arg]
        """Execution prerequisites of one node.

        The three base kinds keep their fixed rules. An *extension* kind declares
        its prerequisites as input dependencies in the template, so the plugin
        supplies the gate instead of the parent's dispatch path growing a branch.
        """
        depends: list = []  # type: ignore[type-arg]

        def add(task_id: str, condition: str) -> None:
            if any(dep.task_id == task_id for dep in depends):
                return
            depends.append(self._dep(task_id, condition))

        if node_id in ("fix", "recheck"):
            review = children.get("review")
            if review is not None:
                add(review.task_id, "review_completed")
        if node_id in ("verify", "recheck"):
            fix = children.get("fix")
            if fix is not None:
                add(fix.task_id, "patch_applied")

        if workflow is not None and nodes is not None:
            base_node_ids = set(DEFAULT_NODE_FOR_KIND.values())
            extension_kinds = {
                n.task_kind
                for n in nodes
                if n.id not in base_node_ids and n.task_kind
            }
            target = next((n for n in nodes if n.id == node_id), None)
            if target is not None and target.task_kind in extension_kinds:
                for dep in workflow.dependencies:
                    if dep.target_node != node_id:
                        continue
                    source = children.get(dep.source_node)
                    if source is None:
                        continue
                    add(source.task_id, ARTIFACT_CONDITION.get(dep.artifact, "task_completed"))
        return depends

    @staticmethod
    def _dep(task_id: str, condition: str):  # type: ignore[no-untyped-def]
        from app.schemas.common import TaskDep

        return TaskDep(task_id=task_id, condition=condition)

    def _create_child(
        self,
        root: Task,
        workflow: WorkflowConfig,
        agent_id: str,
        task_kind: str,
        goal: str,
        depends_on: list,  # type: ignore[type-arg]
        *,
        node_id: str,
    ) -> Task:
        operation_key = f"{root.task_id}:create:{node_id}"
        fresh = self.repos.idempotency.get(operation_key) is None
        creator = TaskCreationService(self.repos)
        task = creator.create_child(
            root,
            agent_id=agent_id,
            agent_version=workflow.agents[agent_id],
            task_kind=task_kind,
            goal=goal,
            depends_on=depends_on,
            operation_key=operation_key,
        )
        if fresh:
            self.sink(root.task_id).emit(
                EventType.TASK_CREATED,
                task_id=task.task_id,
                payload={
                    "agent_id": agent_id,
                    "task_kind": task_kind,
                    "node_id": node_id,
                    "depends_on": [d.model_dump(mode="json") for d in depends_on],
                },
            )
        return task

    def skip_task(self, root_task_id: str, task_id: str, reason: str, detail: str) -> None:
        if self.repos.idempotency.get(f"{root_task_id}:skip:{task_id}") is not None:
            return
        self.repos.tasks.update(task_id, status=TaskStatus.SKIPPED.value, skip_reason=reason)
        self.repos.idempotency.put(
            operation_key=f"{root_task_id}:skip:{task_id}",
            operation_kind="skip_task",
            request_fingerprint=task_id,
            response_ref=task_id,
            root_task_id=root_task_id,
        )
        self.sink(root_task_id).emit(
            EventType.TASK_SKIPPED,
            task_id=task_id,
            payload={"skip_reason": reason, "detail": detail},
        )

    # ---- action validation -------------------------------------------------
    def validate_action(
        self,
        action: ParentAction,
        *,
        root_task_id: str,
        workflow: WorkflowConfig,
        source_version: str,
    ) -> list[str]:
        if isinstance(action, DispatchTaskAction):
            return self._validate_items(
                [action.task], root_task_id=root_task_id, workflow=workflow, source_version=source_version
            )
        if isinstance(action, DispatchBatchAction):
            return self._validate_items(
                action.tasks, root_task_id=root_task_id, workflow=workflow, source_version=source_version
            )
        if isinstance(action, ApplyPatchAction):
            return self._validate_apply(
                action, root_task_id=root_task_id, source_version=source_version
            )
        if isinstance(action, FinishAction):
            return []
        return []

    def _validate_items(
        self,
        items: list[DispatchItem],
        *,
        root_task_id: str,
        workflow: WorkflowConfig,
        source_version: str,
    ) -> list[str]:
        problems: list[str] = []
        for item in items:
            version = workflow.agents.get(item.agent_id)
            if version is None:
                problems.append(f"工作流未启用角色 {item.agent_id}")
                continue
            if not self.registry.has(item.agent_id, version):
                problems.append(f"角色 {item.agent_id}@{version} 未注册")
                continue
            spec = self.registry.get_spec(item.agent_id, version)
            if item.task_kind not in spec.supported_task_kinds:
                problems.append(f"角色 {item.agent_id} 不支持任务类型 {item.task_kind}")
            if item.task_id:
                task = self.repos.tasks.get(item.task_id)
                if task is None or task.root_task_id != root_task_id:
                    problems.append(f"未知子任务 {item.task_id}")
            if item.task_kind == TaskKind.VERIFY.value:
                application = self.committed_application_for(root_task_id, source_version)
                if application is None:
                    problems.append(
                        "修改后验证必须先有一个已提交、结果版本等于当前版本 "
                        f"({source_version}) 的补丁应用"
                    )
        problems.extend(
            self.task_service.precheck_dispatch(
                root_task_id=root_task_id,
                items=items,
                workflow=workflow,
                retry_reason=RetryReason.INITIAL.value,
            )
        )
        return problems

    def _validate_apply(
        self, action: ApplyPatchAction, *, root_task_id: str, source_version: str
    ) -> list[str]:
        problems: list[str] = []
        patch_ref = self._resolve_patch_ref(root_task_id, action.patch_ref)
        if patch_ref is None:
            problems.append("没有可应用的补丁产物")
        if action.base_version != source_version:
            problems.append(
                f"补丁基础版本 {action.base_version} 与当前源码版本 {source_version} 不一致"
            )
        attempt = self.repos.attempts.get(action.repair_attempt_id)
        if attempt is None or attempt.root_task_id != root_task_id:
            problems.append(f"未知修复 attempt {action.repair_attempt_id}")
        else:
            owner = self.repos.tasks.get(attempt.task_id)
            if owner is None or owner.task_kind != TaskKind.FIX.value:
                problems.append("只有修复任务的 attempt 才能应用补丁")
            elif attempt.status not in (
                AttemptStatus.COMPLETED.value,
                AttemptStatus.FAILED.value,
            ):
                problems.append(f"修复 attempt {attempt.attempt_id} 尚未结束")
        return problems

    # ---- action execution --------------------------------------------------
    def resolve_dispatch_items(
        self,
        action: ParentAction,
        *,
        root_task_id: str,
        source_version: str,
        contract_artifact: str,
    ) -> list[DispatchItem]:
        raw = [action.task] if isinstance(action, DispatchTaskAction) else action.tasks  # type: ignore[attr-defined]
        resolved: list[DispatchItem] = []
        for item in raw:
            refs = self._build_refs(
                task_kind=item.task_kind,
                root_task_id=root_task_id,
                source_version=source_version,
                contract_artifact=contract_artifact,
            )
            resolved.append(item.model_copy(update={"input_refs": refs}))
        return resolved

    def register_dispatch(
        self,
        action: ParentAction,
        *,
        root_task_id: str,
        workflow: WorkflowConfig,
        source_version: str,
        contract_version: str,
        contract_artifact: str,
        operation_key: str | None = None,
        fencing_token: int = 0,
    ) -> DispatchPlan:
        items = self.resolve_dispatch_items(
            action,
            root_task_id=root_task_id,
            source_version=source_version,
            contract_artifact=contract_artifact,
        )
        batch_id, attempts = self.task_service.register_dispatch(
            root_task_id=root_task_id,
            items=items,
            workflow=workflow,
            source_version=source_version,
            contract_version=contract_version,
            retry_reasons=[self.retry_reason(item) for item in items],
            operation_key=operation_key,
            fencing_token=fencing_token,
        )
        tasks = [self.repos.tasks.get(a.task_id) for a in attempts]
        return DispatchPlan(
            batch_id=batch_id,
            attempts=attempts,
            items=items,
            tasks=[t for t in tasks if t is not None],
        )

    def retry_reason(self, item: DispatchItem) -> str:
        """Derive the retry reason from what actually happened last time (§9.4).

        The reason decides which budgets a new attempt consumes, so it must come
        from persisted evidence — not from the model's narration.
        """
        if not item.task_id:
            return RetryReason.INITIAL.value
        attempts = self.repos.attempts.list_by_task(item.task_id)
        if not attempts:
            return RetryReason.INITIAL.value
        last = attempts[-1]
        terminal = self.repos.terminals.get(last.attempt_id)
        if terminal is not None and terminal.origin is TerminalOrigin.CONTROLLER:
            # the attempt was invalidated by the control layer (timeout/interruption)
            return RetryReason.EXECUTION_FAULT.value
        result = self.repos.results.get_by_attempt(last.attempt_id)
        if result is not None and result.status is ResultStatus.FAILED:
            return RetryReason.EXECUTION_FAULT.value
        if item.task_kind == TaskKind.FIX.value:
            return RetryReason.BUSINESS_REPAIR.value
        return RetryReason.INSUFFICIENT_EVIDENCE.value

    def build_envelope(
        self, attempt: Attempt, task: Task, item: DispatchItem, workflow: WorkflowConfig
    ) -> TaskEnvelope:
        return TaskEnvelope(
            root_task_id=attempt.root_task_id,
            parent_task_id=task.parent_task_id,
            task_id=attempt.task_id,
            attempt_id=attempt.attempt_id,
            dispatch_batch_id=attempt.dispatch_batch_id or "",
            agent_id=task.agent_id or "",
            agent_version=task.agent_version or workflow.agents.get(task.agent_id or "", "1.0"),
            task_kind=task.task_kind or "",
            goal=task.goal,
            input_refs=InputRefs(**attempt.input_refs),
            source_version=attempt.source_version,
            contract_version=attempt.contract_version or "",
            acceptance_criteria=item.acceptance_criteria,
            constraints=attempt.constraints or TaskConstraints(),
            fencing_token=attempt.fencing_token,
        )

    def apply_patch(
        self, action: ApplyPatchAction, *, root_task_id: str
    ) -> PatchApplication:
        patch_ref = self._resolve_patch_ref(root_task_id, action.patch_ref)
        if patch_ref is None:
            raise ActionRejected("NO_PATCH", "没有可应用的补丁产物")
        key = f"apply:{root_task_id}:{patch_ref}:{action.base_version}"
        return self.workspace.apply_patch(
            root_task_id=root_task_id,
            patch_artifact_id=patch_ref,
            repair_attempt_id=action.repair_attempt_id,
            base_version=action.base_version,
            idempotency_key=key,
        )

    def finalize_skips(self, *, root_task_id: str, passed: bool | None) -> list[dict[str, str]]:
        """Project still-unneeded blocked/queued children as skipped (§5.4)."""
        skipped: list[dict[str, str]] = []
        for task in self.repos.tasks.list_children(root_task_id):
            if task.status not in (TaskStatus.BLOCKED.value, TaskStatus.QUEUED.value):
                continue
            attempts = self.repos.attempts.list_by_task(task.task_id)
            if attempts:
                continue
            if passed is True and task.task_kind == TaskKind.REVIEW.value:
                continue
            reason = (
                SkipReason.FIRST_REVIEW_PASSED.value
                if passed is True
                else SkipReason.NOT_REQUIRED.value
            )
            detail = (
                "首次审查通过，后续修复与验证未执行"
                if passed is True
                else "任务结束前该子任务不再需要执行"
            )
            self.skip_task(root_task_id, task.task_id, reason, detail)
            skipped.append({"task_id": task.task_id, "skip_reason": reason, "detail": detail})
        return skipped

    def build_report(
        self,
        *,
        root_task_id: str,
        workflow: WorkflowConfig,
        contract: AcceptanceContract,
        source_version: str,
        status: str,
        passed: bool | None,
        goal: str,
        conclusion_scope: str,
        unresolved: list[str],
        not_run: list[str],
        skipped: list[dict[str, str]],
        execution_summary: dict[str, Any],
    ) -> tuple[FinalReport, str]:
        latest = self.detector.current_checks(root_task_id, source_version)
        checks: list[CheckSummary] = []
        for spec in contract.checks:
            result = latest.get(spec.check_id)
            checks.append(
                CheckSummary(
                    check_id=spec.check_id,
                    goal_ref=spec.goal_ref,
                    method=spec.method,
                    required=spec.required,
                    status=result.status if result else CheckStatus.NOT_RUN,
                    latest_evidence_ref=(result.evidence_refs[0] if result and result.evidence_refs else None),
                    source_version=source_version if result else None,
                    note=result.reason if result else "未执行",
                )
            )
        report = FinalReport(
            report_id=f"report-{uuid.uuid4().hex[:12]}",
            root_task_id=root_task_id,
            final_status=status,
            passed=passed,
            goal=goal,
            conclusion_scope=conclusion_scope,
            source_version=source_version,
            workflow_version=workflow.workflow_version or "",
            contract_version=contract.contract_version,
            checks=checks,
            unresolved_finding_ids=unresolved,
            not_run_items=not_run,
            skipped_tasks=skipped,
            versions={
                "workflow_version": workflow.workflow_version or "",
                "contract_version": contract.contract_version,
                "source_version": source_version,
            },
            execution_summary=execution_summary,
        )
        artifact = self.artifacts.save_json(
            root_task_id=root_task_id,
            artifact_type=ArtifactType.FINAL_REPORT,
            data=report.model_dump(mode="json"),
            source_version=source_version,
            name=f"report-{report.report_id}",
        )
        self.repos.reports.insert(
            report_id=report.report_id,
            root_task_id=root_task_id,
            kind="final",
            artifact_id=artifact.artifact_id,
            final_status=status,
            passed=passed,
        )
        return report, artifact.artifact_id

    # ---- helpers -----------------------------------------------------------
    def committed_application_for(
        self, root_task_id: str, source_version: str
    ) -> PatchApplication | None:
        for application in self.repos.patch_applications.list_by_root(root_task_id):
            if (
                application.status is PatchApplicationStatus.COMMITTED
                and application.result_version == source_version
            ):
                return application
        return None

    def latest_application(self, root_task_id: str) -> PatchApplication | None:
        applications = self.repos.patch_applications.list_by_root(root_task_id)
        return applications[-1] if applications else None

    def unapplied_fix_attempt(self, root_task_id: str) -> str | None:
        """Newest fix attempt that produced a patch no committed application used.

        This is what makes "a fix report came back but the patch was not applied"
        a deterministic fact instead of something the parent has to remember.
        """
        applied = {
            application.repair_attempt_id
            for application in self.repos.patch_applications.list_by_root(root_task_id)
            if application.status is PatchApplicationStatus.COMMITTED
        }
        fix_tasks = [
            t for t in self.repos.tasks.list_children(root_task_id)
            if t.task_kind == TaskKind.FIX.value
        ]
        candidates: list[str] = []
        for task in fix_tasks:
            for attempt in self.repos.attempts.list_by_task(task.task_id):
                if attempt.attempt_id in applied:
                    continue
                result = self.repos.results.get_by_attempt(attempt.attempt_id)
                if result is not None and result.result_refs.get("patch"):
                    candidates.append(attempt.attempt_id)
        return candidates[-1] if candidates else None

    def latest_artifact(
        self, root_task_id: str, artifact_type: ArtifactType, source_version: str | None = None
    ) -> str | None:
        candidates = [
            a
            for a in self.artifacts.list_by_root(root_task_id, artifact_type)
            if source_version is None or a.source_version == source_version
        ]
        return candidates[-1].artifact_id if candidates else None

    def source_artifact(self, root_task_id: str, source_version: str) -> str:
        for artifact in self.artifacts.list_by_root(root_task_id, ArtifactType.SOURCE_SNAPSHOT):
            if artifact.source_version == source_version:
                return artifact.artifact_id
        return self.workspace.register_source_artifact(
            root_task_id=root_task_id, source_version=source_version
        )

    def _resolve_patch_ref(self, root_task_id: str, patch_ref: str) -> str | None:
        if patch_ref and patch_ref != AUTO:
            artifact = self.repos.artifacts.get(patch_ref)
            if artifact is None or artifact.root_task_id != root_task_id:
                return None
            return patch_ref
        return self.latest_artifact(root_task_id, ArtifactType.PATCH)

    def _build_refs(
        self,
        *,
        task_kind: str,
        root_task_id: str,
        source_version: str,
        contract_artifact: str,
    ) -> InputRefs:
        refs: dict[str, str] = {
            "source": self.source_artifact(root_task_id, source_version),
            "acceptance_contract": contract_artifact,
        }
        for adapter in self.input_adapters:
            if adapter.task_kind != task_kind:
                continue
            reference = self._resolve_adapter(adapter, root_task_id, source_version)
            if reference:
                refs[adapter.input_key] = reference
        # Use the latest structured bundle, never a standalone verifier scratch file.
        if task_kind in {"review", "verify"} and "generated_tests" not in refs:
            for artifact in reversed(self.repos.artifacts.list_by_root(root_task_id)):
                if artifact.artifact_type is ArtifactType.TEST_ARTIFACT and artifact.metadata.get("bundle"):
                    refs["generated_tests"] = artifact.artifact_id
                    break
        return InputRefs(**refs)

    def _resolve_adapter(
        self, adapter: InputAdapter, root_task_id: str, source_version: str
    ) -> str | None:
        """Resolve one input reference from persisted artifacts (§4.1)."""
        if adapter.scope == SCOPE_COMMITTED_APPLICATION:
            application = self.committed_application_for(root_task_id, source_version)
            if application is None or application.result_version is None:
                return None
            return self.latest_artifact(
                root_task_id,
                adapter.versioned_artifact_type or adapter.artifact_type,
                application.result_version,
            )
        return self.latest_artifact(root_task_id, adapter.artifact_type)
