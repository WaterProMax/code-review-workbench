"""P01 acceptance: legal/illegal protocol samples and round-trip serialisation.

Every legal sample must validate, serialise and validate again unchanged.
Every illegal sample must be rejected — identity mismatches, illegal state
combinations, missing versions and unsupported actions are contract boundaries
the rest of the system relies on (ImplementationPlan §5.2).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from app.schemas.actions import parse_parent_action
from app.schemas.agents import AgentSpec
from app.schemas.artifacts import Artifact, FileEntry, Patch, PatchApplication
from app.schemas.events import ExecutionEvent
from app.schemas.results import (
    AcceptanceContract,
    BudgetLedgerEntry,
    CheckResult,
    CheckSpec,
    FinalReport,
    Finding,
    TaskResult,
    TerminalRecord,
    VerificationReport,
)
from app.schemas.tasks import Attempt, IdempotencyRecord, Task, TaskControl, TaskEnvelope
from app.schemas.workflows import WorkflowConfig

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "protocol"

MODEL_FOR: dict[str, type[BaseModel]] = {
    "Task": Task,
    "Attempt": Attempt,
    "TaskEnvelope": TaskEnvelope,
    "TaskResult": TaskResult,
    "AcceptanceContract": AcceptanceContract,
    "CheckSpec": CheckSpec,
    "CheckResult": CheckResult,
    "Finding": Finding,
    "Patch": Patch,
    "PatchApplication": PatchApplication,
    "VerificationReport": VerificationReport,
    "FinalReport": FinalReport,
    "TerminalRecord": TerminalRecord,
    "BudgetLedgerEntry": BudgetLedgerEntry,
    "Artifact": Artifact,
    "FileEntry": FileEntry,
    "ExecutionEvent": ExecutionEvent,
    "AgentSpec": AgentSpec,
    "TaskControl": TaskControl,
    "IdempotencyRecord": IdempotencyRecord,
    "WorkflowConfig": WorkflowConfig,
}

# ParentAction is a discriminated union, so it uses a parser instead of a model.
PARSER_SCHEMAS = {"ParentAction"}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _legal_cases() -> list[tuple[str, str, dict]]:
    data = _load("legal.json")
    return [(schema, name, entry) for schema, entries in data.items() for name, entry in entries.items()]


def _illegal_cases() -> list[tuple[str, str, dict, str]]:
    data = _load("illegal.json")
    return [
        (schema, name, entry["payload"], entry["reason"])
        for schema, entries in data.items()
        for name, entry in entries.items()
    ]


def _parse(schema: str, payload: dict):
    if schema in PARSER_SCHEMAS:
        return parse_parent_action(payload)
    return MODEL_FOR[schema].model_validate(payload)


def _roundtrip(parsed):
    dumped = parsed.model_dump(mode="json")
    # the dump must be plain JSON (no datetime/enum leakage)
    json.dumps(dumped, ensure_ascii=False)
    return _parse(_schema_of(parsed), dumped)


def _schema_of(parsed) -> str:
    if type(parsed) in MODEL_FOR.values():
        return next(k for k, v in MODEL_FOR.items() if v is type(parsed))
    return "ParentAction"


LEGAL = _legal_cases()
ILLEGAL = _illegal_cases()


def test_every_schema_has_a_legal_sample() -> None:
    covered = {schema for schema, _, _ in LEGAL}
    assert covered >= set(MODEL_FOR) | PARSER_SCHEMAS


@pytest.mark.parametrize(
    "schema,name,payload", LEGAL, ids=[f"{s}.{n}" for s, n, _ in LEGAL]
)
def test_legal_sample_validates_and_roundtrips(schema: str, name: str, payload: dict) -> None:
    parsed = _parse(schema, payload)
    again = _roundtrip(parsed)
    assert again == parsed, f"{schema}.{name} did not survive a serialise/parse round trip"


@pytest.mark.parametrize(
    "schema,name,payload,reason", ILLEGAL, ids=[f"{s}.{n}" for s, n, _, _ in ILLEGAL]
)
def test_illegal_sample_is_rejected(
    schema: str, name: str, payload: dict, reason: str
) -> None:
    with pytest.raises((ValidationError, ValueError)) as exc:  # noqa: PT011
        _parse(schema, payload)
    assert reason, "each illegal sample must document why it is rejected"
    assert str(exc.value), "rejection must explain the violated rule"
