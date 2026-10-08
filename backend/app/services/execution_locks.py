"""Execution rights, leases and the append-only budget ledger (§6.1, §9.4, §11.1).

The lease, fencing token and budget accounting all live in the database, so
recovery mutexes work across processes and a stale executor cannot publish
results after its token has been superseded.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.schemas.enums import BudgetKind
from app.schemas.tasks import TaskControl
from app.schemas.workflows import BudgetConfig
from app.settings import (
    CAP_MAX_EVIDENCE_RETRIES,
    CAP_MAX_FIX_RETRIES,
    CAP_MAX_GENERATE_RETRIES,
    CAP_MAX_REPAIR_ROUNDS,
    CAP_MAX_REVIEW_RETRIES,
    CAP_MAX_VERIFICATION_RETRIES,
)
from app.storage.repositories import BudgetRepository, ControlRepository


class ExecutionRightError(RuntimeError):
    """The caller does not hold the current valid execution right."""


class BudgetExhausted(RuntimeError):
    """One or more required budgets are not available."""

    def __init__(self, required_additions: dict[str, int], message: str = "budget exhausted") -> None:
        super().__init__(message)
        self.required_additions = required_additions


class BudgetNotAllowed(RuntimeError):
    """A grant would push a budget past its server-side cap."""


# budget kind -> workflow budget field
BUDGET_CONFIG_FIELD: dict[str, str] = {
    BudgetKind.REPAIR_ROUND.value: "max_repair_rounds",
    BudgetKind.VERIFICATION_RETRY.value: "max_verification_retries",
    BudgetKind.REVIEW_RETRY.value: "max_review_retries",
    BudgetKind.FIX_RETRY.value: "max_fix_retries",
    BudgetKind.EVIDENCE_RETRY.value: "max_evidence_retries",
    BudgetKind.GENERATE_RETRY.value: "max_generate_retries",
}

BUDGET_CAPS: dict[str, int] = {
    BudgetKind.REPAIR_ROUND.value: CAP_MAX_REPAIR_ROUNDS,
    BudgetKind.VERIFICATION_RETRY.value: CAP_MAX_VERIFICATION_RETRIES,
    BudgetKind.REVIEW_RETRY.value: CAP_MAX_REVIEW_RETRIES,
    BudgetKind.FIX_RETRY.value: CAP_MAX_FIX_RETRIES,
    BudgetKind.EVIDENCE_RETRY.value: CAP_MAX_EVIDENCE_RETRIES,
    BudgetKind.GENERATE_RETRY.value: CAP_MAX_GENERATE_RETRIES,
}

# task_kind -> the retry budget consumed when re-dispatching after execution faults
TASK_KIND_FAULT_BUDGET: dict[str, str] = {
    "review": BudgetKind.REVIEW_RETRY.value,
    "fix": BudgetKind.FIX_RETRY.value,
    "verify": BudgetKind.VERIFICATION_RETRY.value,
    # P10 extension task kind: declared here *and* by the extension plugin, so it
    # can never silently fall into the unbounded-retry default (§5, §14.2)
    "generate_tests": BudgetKind.GENERATE_RETRY.value,
}


def base_limits(budgets: BudgetConfig) -> dict[str, int]:
    return {kind: getattr(budgets, field) for kind, field in BUDGET_CONFIG_FIELD.items()}


def request_fingerprint(payload: object) -> str:
    """Stable fingerprint of a request body, used for Idempotency-Key checks."""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


class ExecutionLockService:
    def __init__(
        self,
        controls: ControlRepository,
        ledger: BudgetRepository,
    ) -> None:
        self.controls = controls
        self.ledger = ledger

    # ---- leases ------------------------------------------------------------
    def current(self, root_task_id: str) -> TaskControl | None:
        return self.controls.get(root_task_id)

    def acquire(
        self,
        root_task_id: str,
        owner: str,
        ttl_seconds: float = 60.0,
        *,
        expected_revision: int | None = None,
        conn=None,  # type: ignore[no-untyped-def]
    ) -> tuple[bool, TaskControl]:
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        return self.controls.acquire_lease(
            root_task_id, owner, expires, expected_revision=expected_revision, conn=conn
        )

    def heartbeat(self, root_task_id: str, owner: str, ttl_seconds: float = 60.0) -> bool:
        control = self.controls.get(root_task_id)
        if control is None or control.lease_owner != owner:
            return False
        self.controls.update(
            root_task_id,
            heartbeat=True,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )
        return True

    def release(self, root_task_id: str, owner: str) -> bool:
        control = self.controls.get(root_task_id)
        if control is None or control.lease_owner != owner:
            return False
        self.controls.update(root_task_id, release_lease=True)
        return True

    # ---- execution right (fencing) ----------------------------------------
    def has_right(self, root_task_id: str, fencing_token: int, owner: str | None = None) -> bool:
        control = self.controls.get(root_task_id)
        if control is None:
            return False
        if control.fencing_token != fencing_token:
            return False
        if owner is not None and control.lease_owner != owner:
            return False
        return control.lease_valid(datetime.now(timezone.utc)) or owner is None

    def require_right(
        self, root_task_id: str, fencing_token: int, owner: str | None = None
    ) -> TaskControl:
        control = self.controls.get(root_task_id)
        if control is None:
            raise ExecutionRightError(f"no control record for task {root_task_id}")
        if control.fencing_token != fencing_token:
            raise ExecutionRightError(
                f"stale fencing token {fencing_token} (current {control.fencing_token})"
            )
        if owner is not None and control.lease_owner not in (None, owner):
            raise ExecutionRightError(
                f"lease held by {control.lease_owner!r}, not {owner!r}"
            )
        return control

    # ---- budget state ------------------------------------------------------
    def budget_state(self, root_task_id: str, budgets: BudgetConfig) -> dict[str, dict[str, int]]:
        base = base_limits(budgets)
        totals = self.ledger.totals(root_task_id)
        state: dict[str, dict[str, int]] = {}
        for kind, base_limit in base.items():
            additional = totals.get(kind, {}).get("granted", 0)
            consumed = totals.get(kind, {}).get("consumed", 0)
            granted_max = min(base_limit + additional, BUDGET_CAPS[kind])
            state[kind] = {
                "consumed": consumed,
                "granted_max": granted_max,
                "remaining": max(granted_max - consumed, 0),
            }
        return state

    def available(self, root_task_id: str, kind: str, budgets: BudgetConfig) -> int:
        return self.budget_state(root_task_id, budgets)[kind]["remaining"]

    # ---- consumption / grants --------------------------------------------
    def consume(
        self,
        root_task_id: str,
        kind: str,
        operation_key: str,
        reason: str,
        *,
        scope_key: str = "root",
        conn=None,  # type: ignore[no-untyped-def]
    ) -> bool:
        """Record a consumption; False when this operation key was already used."""
        return self.ledger.append(
            root_task_id=root_task_id,
            budget_kind=kind,
            scope_key=scope_key,
            operation_key=operation_key,
            delta=-1,
            reason=reason,
            conn=conn,
        )

    def ensure_available(
        self,
        root_task_id: str,
        kinds: list[str],
        budgets: BudgetConfig,
        *,
        conn=None,  # type: ignore[no-untyped-def]
    ) -> None:
        """Raise ``BudgetExhausted`` if any required budget has no remaining room.

        The check runs inside the caller's transaction so a failed check produces
        no attempt and no ledger entry (plan §9.4).
        """
        state = self.budget_state(root_task_id, budgets)
        missing = {k: 1 for k in kinds if state[k]["remaining"] <= 0}
        if missing:
            raise BudgetExhausted(missing)

    def grant(
        self,
        root_task_id: str,
        kind: str,
        delta: int,
        operation_key: str,
        reason: str,
        budgets: BudgetConfig,
        *,
        scope_key: str = "root",
        conn=None,  # type: ignore[no-untyped-def]
    ) -> bool:
        """Grant additional budget, refusing to exceed the server-side cap."""
        if delta <= 0:
            raise BudgetNotAllowed("grant delta must be positive")
        current = self.budget_state(root_task_id, budgets)[kind]["granted_max"]
        if current + delta > BUDGET_CAPS[kind]:
            raise BudgetNotAllowed(
                f"{kind}: granted max {current} + {delta} exceeds cap {BUDGET_CAPS[kind]}"
            )
        return self.ledger.append(
            root_task_id=root_task_id,
            budget_kind=kind,
            scope_key=scope_key,
            operation_key=operation_key,
            delta=delta,
            reason=reason,
            conn=conn,
        )
