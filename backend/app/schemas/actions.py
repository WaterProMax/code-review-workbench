"""The parent agent's structured decision surface (Desgin §4.4).

All model proposals go through one discriminated union so the control layer can
validate a single, closed set of actions. The model never invents business task
IDs: a ``DispatchItem`` either leaves ``task_id`` null (the controlled creation
tool assigns one) or references an existing child task to re-run.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import TaskConstraints, TaskDep
from app.schemas.enums import ActionType
from app.schemas.tasks import InputRefs


def action_names() -> list[str]:
    return [a.value for a in ActionType]


class DispatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str = Field(min_length=1)
    task_kind: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    input_refs: InputRefs
    acceptance_criteria: list[str] = Field(min_length=1)
    task_id: str | None = None
    depends_on: list[TaskDep] = Field(default_factory=list)
    constraints: TaskConstraints | None = None
    reason: str = Field(min_length=1)


class DispatchTaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["dispatch_task"] = "dispatch_task"
    task: DispatchItem


class DispatchBatchAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["dispatch_batch"] = "dispatch_batch"
    tasks: list[DispatchItem] = Field(min_length=2)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_batch(self) -> "DispatchBatchAction":
        kinds = {t.task_kind for t in self.tasks}
        if len(kinds) < 2:
            raise ValueError(
                "dispatch_batch must contain independent branches "
                "(distinct task_kinds/existing task ids)"
            )
        task_ids = [t.task_id for t in self.tasks if t.task_id]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("dispatch_batch cannot reference the same task twice")
        return self


class ApplyPatchAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["apply_patch"] = "apply_patch"
    patch_ref: str = Field(min_length=1)
    base_version: str = Field(min_length=1)
    repair_attempt_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class WaitForRecoveryAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["wait_for_recovery"] = "wait_for_recovery"
    reason: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    known_passed: bool | None = None


class FinishAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["finish"] = "finish"
    proposed_passed: bool
    report_refs: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


ParentAction = Annotated[
    Union[
        DispatchTaskAction,
        DispatchBatchAction,
        ApplyPatchAction,
        WaitForRecoveryAction,
        FinishAction,
    ],
    Field(discriminator="action"),
]

ACTION_MODELS = {
    "dispatch_task": DispatchTaskAction,
    "dispatch_batch": DispatchBatchAction,
    "apply_patch": ApplyPatchAction,
    "wait_for_recovery": WaitForRecoveryAction,
    "finish": FinishAction,
}


def parse_parent_action(payload: dict) -> "ParentAction":
    """Parse a model proposal, rejecting any unsupported action name."""
    if not isinstance(payload, dict):
        raise ValueError("parent action must be a JSON object")
    name = payload.get("action")
    if name not in ACTION_MODELS:
        raise ValueError(
            f"unsupported action {name!r}; expected one of {action_names()}"
        )
    return ACTION_MODELS[name].model_validate(payload)  # type: ignore[return-value]
