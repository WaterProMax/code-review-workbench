"""Repositories: every business write goes through these methods (§6.1).

Each method accepts an optional ``conn`` so callers can compose several writes
into one business transaction; without it the method opens its own short
transaction. Rows are converted to and from the Pydantic contracts so the rest
of the system never touches raw SQL.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.schemas.artifacts import Artifact, PatchApplication
from app.schemas.common import ErrorInfo, TaskConstraints, TaskDep
from app.schemas.enums import (
    AttemptStatus,
    CheckStatus,
    FindingStatus,
    PatchApplicationStatus,
    ResultStatus,
    TerminalOrigin,
    TerminalOutcome,
)
from app.schemas.events import ExecutionEvent
from app.schemas.results import (
    CheckMethod,
    CheckResult,
    Finding,
    TaskResult,
    TerminalRecord,
    VerificationReport,
)
from app.schemas.tasks import Attempt, Task, TaskControl
from app.schemas.workflows import WorkflowConfig
from app.storage.database import Database


# --------------------------------------------------------------------------- #
# serialisation helpers
# --------------------------------------------------------------------------- #
def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str | None, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def b2i(value: bool | None) -> int | None:
    return None if value is None else int(value)


def i2b(value: int | None) -> bool | None:
    return None if value is None else bool(value)


class ConflictError(RuntimeError):
    """Raised when an idempotent operation is repeated with different content."""


class BaseRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    @contextmanager
    def _tx(self, conn: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if conn is not None:
            yield conn
        else:
            with self.db.transaction() as own:
                yield own


# --------------------------------------------------------------------------- #
# tasks
# --------------------------------------------------------------------------- #
class TaskRepository(BaseRepo):
    def insert(self, task: Task, conn: sqlite3.Connection | None = None) -> Task:
        with self._tx(conn) as c:
            if task.parent_task_id is None:
                # the root row references itself; insert it first without the
                # self-referencing FK active, then the DB keeps it consistent.
                c.execute("PRAGMA defer_foreign_keys = ON")
            c.execute(
                """
                INSERT INTO tasks (task_id, root_task_id, parent_task_id, task_level,
                    agent_id, agent_version, task_kind, goal, depends_on, status,
                    passed, skip_reason, workflow_version, revision, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task.task_id,
                    task.root_task_id,
                    task.parent_task_id,
                    task.task_level.value,
                    task.agent_id,
                    task.agent_version,
                    task.task_kind,
                    task.goal,
                    dumps([d.model_dump(mode="json") for d in task.depends_on]),
                    task.status,
                    b2i(task.passed),
                    task.skip_reason,
                    task.workflow_version,
                    task.revision,
                    iso(task.created_at),
                    iso(task.updated_at),
                ),
            )
        return task

    def get(self, task_id: str, conn: sqlite3.Connection | None = None) -> Task | None:
        with self._tx(conn) as c:
            row = c.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def list_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[Task]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM tasks WHERE root_task_id = ? ORDER BY task_level DESC, created_at",
                (root_task_id,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_children(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[Task]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM tasks WHERE root_task_id = ? AND task_level = 'child' ORDER BY created_at",
                (root_task_id,),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def list_roots(
        self, limit: int = 50, offset: int = 0, conn: sqlite3.Connection | None = None
    ) -> list[Task]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM tasks WHERE task_level = 'root' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_task(r) for r in rows]

    def count_roots(self, conn: sqlite3.Connection | None = None) -> int:
        with self._tx(conn) as c:
            return int(c.execute("SELECT COUNT(*) AS n FROM tasks WHERE task_level='root'").fetchone()["n"])

    def update(
        self,
        task_id: str,
        *,
        status: str | None = None,
        passed: bool | None = None,
        passed_set: bool = False,
        skip_reason: str | None = None,
        bump_revision: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Update a task row and return the new revision."""
        with self._tx(conn) as c:
            row = c.execute("SELECT revision FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown task {task_id}")
            revision = int(row["revision"]) + (1 if bump_revision else 0)
            sets = ["updated_at = ?", "revision = ?"]
            params: list[Any] = [iso(datetime.now(timezone.utc)), revision]
            if status is not None:
                sets.append("status = ?")
                params.append(status)
            if passed_set:
                sets.append("passed = ?")
                params.append(b2i(passed))
            if skip_reason is not None:
                sets.append("skip_reason = ?")
                params.append(skip_reason)
            params.append(task_id)
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE task_id = ?", params)
            return revision

    def next_child_ordinal(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> int:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE root_task_id = ? AND task_level='child'",
                (root_task_id,),
            ).fetchone()
        return int(row["n"]) + 1


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        root_task_id=row["root_task_id"],
        parent_task_id=row["parent_task_id"],
        task_level=row["task_level"],
        agent_id=row["agent_id"],
        agent_version=row["agent_version"],
        task_kind=row["task_kind"],
        goal=row["goal"],
        depends_on=[TaskDep.model_validate(d) for d in loads(row["depends_on"], [])],
        status=row["status"],
        passed=i2b(row["passed"]),
        skip_reason=row["skip_reason"],
        workflow_version=row["workflow_version"],
        revision=int(row["revision"]),
        created_at=parse_dt(row["created_at"]),
        updated_at=parse_dt(row["updated_at"]),
    )


# --------------------------------------------------------------------------- #
# attempts
# --------------------------------------------------------------------------- #
class AttemptRepository(BaseRepo):
    def insert(self, attempt: Attempt, conn: sqlite3.Connection | None = None) -> Attempt:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO attempts (attempt_id, task_id, root_task_id, dispatch_batch_id,
                    attempt_no, source_version, contract_version, input_refs, constraints,
                    retry_reason, status, error, started_at, finished_at, created_at, fencing_token)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.root_task_id,
                    attempt.dispatch_batch_id,
                    attempt.attempt_no,
                    attempt.source_version,
                    attempt.contract_version,
                    dumps(attempt.input_refs),
                    dumps(attempt.constraints.model_dump(mode="json")),
                    attempt.retry_reason,
                    attempt.status.value,
                    attempt.error,
                    iso(attempt.started_at),
                    iso(attempt.finished_at),
                    iso(attempt.created_at),
                    attempt.fencing_token,
                ),
            )
        return attempt

    def get(self, attempt_id: str, conn: sqlite3.Connection | None = None) -> Attempt | None:
        with self._tx(conn) as c:
            row = c.execute("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return _row_to_attempt(row) if row else None

    def update_status(
        self,
        attempt_id: str,
        status: AttemptStatus,
        *,
        error: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                UPDATE attempts SET status = ?,
                    error = COALESCE(?, error),
                    started_at = COALESCE(?, started_at),
                    finished_at = COALESCE(?, finished_at)
                WHERE attempt_id = ?
                """,
                (status.value, error, iso(started_at), iso(finished_at), attempt_id),
            )

    def list_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[Attempt]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM attempts WHERE root_task_id = ? ORDER BY created_at, attempt_no",
                (root_task_id,),
            ).fetchall()
        return [_row_to_attempt(r) for r in rows]

    def list_by_task(self, task_id: str, conn: sqlite3.Connection | None = None) -> list[Attempt]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_no",
                (task_id,),
            ).fetchall()
        return [_row_to_attempt(r) for r in rows]

    def list_by_batch(self, batch_id: str, conn: sqlite3.Connection | None = None) -> list[Attempt]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM attempts WHERE dispatch_batch_id = ? ORDER BY created_at",
                (batch_id,),
            ).fetchall()
        return [_row_to_attempt(r) for r in rows]

    def list_active(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[Attempt]:
        with self._tx(conn) as c:
            rows = c.execute(
                """
                SELECT * FROM attempts
                WHERE root_task_id = ? AND status IN ('queued', 'running')
                ORDER BY created_at
                """,
                (root_task_id,),
            ).fetchall()
        return [_row_to_attempt(r) for r in rows]

    def next_attempt_no(self, task_id: str, conn: sqlite3.Connection | None = None) -> int:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) AS n FROM attempts WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return int(row["n"]) + 1


def _row_to_attempt(row: sqlite3.Row) -> Attempt:
    return Attempt(
        attempt_id=row["attempt_id"],
        task_id=row["task_id"],
        root_task_id=row["root_task_id"],
        dispatch_batch_id=row["dispatch_batch_id"],
        attempt_no=int(row["attempt_no"]),
        source_version=row["source_version"],
        contract_version=row["contract_version"],
        input_refs=loads(row["input_refs"], {}),
        constraints=TaskConstraints.model_validate(loads(row["constraints"], {})),
        retry_reason=row["retry_reason"],
        fencing_token=int(row["fencing_token"]),
        status=row["status"],
        error=row["error"],
        started_at=parse_dt(row["started_at"]),
        finished_at=parse_dt(row["finished_at"]),
        created_at=parse_dt(row["created_at"]),
    )


# --------------------------------------------------------------------------- #
# dispatch batches
# --------------------------------------------------------------------------- #
class BatchRepository(BaseRepo):
    def create(
        self,
        batch_id: str,
        root_task_id: str,
        expected_attempt_ids: list[str],
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO dispatch_batches (dispatch_batch_id, root_task_id,
                    expected_attempt_ids, received_attempt_ids, outcome_origins, status, consumed, created_at)
                VALUES (?,?,?,'[]','{}','open',0,?)
                """,
                (batch_id, root_task_id, dumps(expected_attempt_ids), iso(datetime.now(timezone.utc))),
            )

    def get(self, batch_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM dispatch_batches WHERE dispatch_batch_id = ?", (batch_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "dispatch_batch_id": row["dispatch_batch_id"],
            "root_task_id": row["root_task_id"],
            "expected_attempt_ids": loads(row["expected_attempt_ids"], []),
            "received_attempt_ids": loads(row["received_attempt_ids"], []),
            "outcome_origins": loads(row["outcome_origins"], {}),
            "status": row["status"],
            "consumed": bool(row["consumed"]),
        }

    def add_received(
        self,
        batch_id: str,
        attempt_id: str,
        origin: TerminalOrigin,
        conn: sqlite3.Connection | None = None,
    ) -> dict:
        """Record a legal terminal for an expected attempt; idempotent."""
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM dispatch_batches WHERE dispatch_batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown dispatch batch {batch_id}")
            expected = loads(row["expected_attempt_ids"], [])
            received = loads(row["received_attempt_ids"], [])
            origins = loads(row["outcome_origins"], {})
            if attempt_id not in expected:
                raise ValueError(f"attempt {attempt_id} is not expected in batch {batch_id}")
            if attempt_id not in received:
                received.append(attempt_id)
                origins[attempt_id] = origin.value
            complete = set(expected) <= set(received)
            status = "complete" if complete else "open"
            c.execute(
                """
                UPDATE dispatch_batches
                SET received_attempt_ids = ?, outcome_origins = ?, status = ?
                WHERE dispatch_batch_id = ?
                """,
                (dumps(received), dumps(origins), status, batch_id),
            )
        return {
            "dispatch_batch_id": batch_id,
            "expected_attempt_ids": expected,
            "received_attempt_ids": received,
            "outcome_origins": origins,
            "status": status,
            "consumed": bool(row["consumed"]),
        }

    def set_expected(
        self, batch_id: str, expected_attempt_ids: list[str], conn: sqlite3.Connection | None = None
    ) -> None:
        """Replace the expected set (used when attempts are registered after the batch)."""
        with self._tx(conn) as c:
            c.execute(
                "UPDATE dispatch_batches SET expected_attempt_ids = ? WHERE dispatch_batch_id = ?",
                (dumps(expected_attempt_ids), batch_id),
            )

    def mark_consumed(self, batch_id: str, conn: sqlite3.Connection | None = None) -> bool:
        """Returns True if this call was the one that consumed the batch."""
        with self._tx(conn) as c:
            cur = c.execute(
                "UPDATE dispatch_batches SET consumed = 1 WHERE dispatch_batch_id = ? AND consumed = 0",
                (batch_id,),
            )
            return cur.rowcount == 1

    def list_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT dispatch_batch_id FROM dispatch_batches WHERE root_task_id = ? ORDER BY created_at",
                (root_task_id,),
            ).fetchall()
        return [self.get(r["dispatch_batch_id"], conn=conn) for r in rows]


# --------------------------------------------------------------------------- #
# results / terminals
# --------------------------------------------------------------------------- #
class ResultRepository(BaseRepo):
    def insert(self, result: TaskResult, conn: sqlite3.Connection | None = None) -> tuple[TaskResult, bool]:
        """Insert the single accepted report for an attempt.

        Returns ``(result, created)``. Repeating the same content is idempotent
        (created=False); conflicting content for the same attempt is rejected.
        """
        digest = result.content_digest()
        with self._tx(conn) as c:
            existing = c.execute(
                "SELECT * FROM task_results WHERE attempt_id = ?", (result.attempt_id,)
            ).fetchone()
            if existing is not None:
                if existing["content_digest"] != digest:
                    raise ConflictError(
                        f"attempt {result.attempt_id} already has a different accepted result"
                    )
                return _row_to_result(existing), False
            c.execute(
                """
                INSERT INTO task_results (result_id, attempt_id, root_task_id, task_id,
                    dispatch_batch_id, agent_id, agent_version, task_kind, source_version,
                    contract_version, status, passed, summary, result_refs, evidence_refs,
                    error, content_digest, started_at, finished_at, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.result_id,
                    result.attempt_id,
                    result.root_task_id,
                    result.task_id,
                    result.dispatch_batch_id,
                    result.agent_id,
                    result.agent_version,
                    result.task_kind,
                    result.source_version,
                    result.contract_version,
                    result.status.value,
                    b2i(result.passed),
                    result.summary,
                    dumps(result.result_refs),
                    dumps(result.evidence_refs),
                    dumps(result.error.model_dump(mode="json")) if result.error else None,
                    digest,
                    iso(result.started_at),
                    iso(result.finished_at),
                    iso(datetime.now(timezone.utc)),
                ),
            )
        return result, True

    def get_by_attempt(self, attempt_id: str, conn: sqlite3.Connection | None = None) -> TaskResult | None:
        with self._tx(conn) as c:
            row = c.execute("SELECT * FROM task_results WHERE attempt_id = ?", (attempt_id,)).fetchone()
        return _row_to_result(row) if row else None

    def list_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[TaskResult]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM task_results WHERE root_task_id = ? ORDER BY created_at",
                (root_task_id,),
            ).fetchall()
        return [_row_to_result(r) for r in rows]


def _row_to_result(row: sqlite3.Row) -> TaskResult:
    raw_error = loads(row["error"])
    return TaskResult(
        result_id=row["result_id"],
        root_task_id=row["root_task_id"],
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        dispatch_batch_id=row["dispatch_batch_id"],
        agent_id=row["agent_id"],
        agent_version=row["agent_version"],
        task_kind=row["task_kind"],
        source_version=row["source_version"],
        contract_version=row["contract_version"],
        status=ResultStatus(row["status"]),
        passed=i2b(row["passed"]),
        summary=row["summary"],
        result_refs=loads(row["result_refs"], {}),
        evidence_refs=loads(row["evidence_refs"], []),
        error=ErrorInfo.model_validate(raw_error) if raw_error else None,
        started_at=parse_dt(row["started_at"]),
        finished_at=parse_dt(row["finished_at"]),
    )


class TerminalRepository(BaseRepo):
    def record(
        self, terminal: TerminalRecord, conn: sqlite3.Connection | None = None
    ) -> tuple[TerminalRecord, bool]:
        """Insert the unique terminal for an attempt; returns (terminal, created)."""
        with self._tx(conn) as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO attempt_terminals (attempt_id, dispatch_batch_id, origin,
                    outcome, result_id, error_ref, fencing_token, operation_key, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    terminal.attempt_id,
                    terminal.dispatch_batch_id,
                    terminal.origin.value,
                    terminal.outcome.value,
                    terminal.result_id,
                    terminal.error_ref,
                    terminal.fencing_token,
                    terminal.operation_key,
                    iso(terminal.created_at),
                ),
            )
            created = cur.rowcount == 1
            if not created:
                row = c.execute(
                    "SELECT * FROM attempt_terminals WHERE attempt_id = ?", (terminal.attempt_id,)
                ).fetchone()
                return _row_to_terminal(row), False
        return terminal, True

    def get(self, attempt_id: str, conn: sqlite3.Connection | None = None) -> TerminalRecord | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM attempt_terminals WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return _row_to_terminal(row) if row else None


def _row_to_terminal(row: sqlite3.Row) -> TerminalRecord:
    return TerminalRecord(
        attempt_id=row["attempt_id"],
        dispatch_batch_id=row["dispatch_batch_id"],
        origin=TerminalOrigin(row["origin"]),
        outcome=TerminalOutcome(row["outcome"]),
        result_id=row["result_id"],
        error_ref=row["error_ref"],
        fencing_token=int(row["fencing_token"]),
        operation_key=row["operation_key"],
        created_at=parse_dt(row["created_at"]),
    )


# --------------------------------------------------------------------------- #
# budget ledger
# --------------------------------------------------------------------------- #
class BudgetRepository(BaseRepo):
    def append(
        self,
        *,
        root_task_id: str,
        budget_kind: str,
        scope_key: str,
        operation_key: str,
        delta: int,
        reason: str,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        """Append one ledger entry; returns False if the operation already exists."""
        with self._tx(conn) as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO budget_ledger (root_task_id, budget_kind, scope_key,
                    operation_key, delta, reason, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (root_task_id, budget_kind, scope_key, operation_key, delta, reason, iso(datetime.now(timezone.utc))),
            )
            return cur.rowcount == 1

    def consumed(self, root_task_id: str, budget_kind: str, conn: sqlite3.Connection | None = None) -> int:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT COALESCE(-SUM(CASE WHEN delta < 0 THEN delta ELSE 0 END), 0) AS n "
                "FROM budget_ledger WHERE root_task_id = ? AND budget_kind = ?",
                (root_task_id, budget_kind),
            ).fetchone()
        return int(row["n"])

    def granted(self, root_task_id: str, budget_kind: str, conn: sqlite3.Connection | None = None) -> int:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END), 0) AS n "
                "FROM budget_ledger WHERE root_task_id = ? AND budget_kind = ?",
                (root_task_id, budget_kind),
            ).fetchone()
        return int(row["n"])

    def totals(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> dict[str, dict[str, int]]:
        with self._tx(conn) as c:
            rows = c.execute(
                """
                SELECT budget_kind,
                       COALESCE(-SUM(CASE WHEN delta < 0 THEN delta ELSE 0 END), 0) AS consumed,
                       COALESCE(SUM(CASE WHEN delta > 0 THEN delta ELSE 0 END), 0) AS granted
                FROM budget_ledger WHERE root_task_id = ?
                GROUP BY budget_kind
                """,
                (root_task_id,),
            ).fetchall()
        return {
            r["budget_kind"]: {"consumed": int(r["consumed"]), "granted": int(r["granted"])}
            for r in rows
        }

    def entries(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM budget_ledger WHERE root_task_id = ? ORDER BY entry_id",
                (root_task_id,),
            ).fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #
class ArtifactRepository(BaseRepo):
    def insert(self, artifact: Artifact, conn: sqlite3.Connection | None = None) -> Artifact:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO artifacts (artifact_id, root_task_id, producer_attempt_id,
                    artifact_type, source_version, storage_ref, hash, size, metadata, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    artifact.artifact_id,
                    artifact.root_task_id,
                    artifact.producer_attempt_id,
                    artifact.artifact_type.value,
                    artifact.source_version,
                    artifact.storage_ref,
                    artifact.hash,
                    artifact.size,
                    dumps(artifact.metadata),
                    iso(artifact.created_at),
                ),
            )
        return artifact

    def get(self, artifact_id: str, conn: sqlite3.Connection | None = None) -> Artifact | None:
        with self._tx(conn) as c:
            row = c.execute("SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
        return _row_to_artifact(row) if row else None

    def list_by_root(
        self,
        root_task_id: str,
        artifact_type: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[Artifact]:
        with self._tx(conn) as c:
            if artifact_type:
                rows = c.execute(
                    "SELECT * FROM artifacts WHERE root_task_id = ? AND artifact_type = ? ORDER BY created_at",
                    (root_task_id, artifact_type),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM artifacts WHERE root_task_id = ? ORDER BY created_at",
                    (root_task_id,),
                ).fetchall()
        return [_row_to_artifact(r) for r in rows]


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        artifact_id=row["artifact_id"],
        root_task_id=row["root_task_id"],
        producer_attempt_id=row["producer_attempt_id"],
        artifact_type=row["artifact_type"],
        source_version=row["source_version"],
        storage_ref=row["storage_ref"],
        hash=row["hash"],
        size=int(row["size"]),
        metadata=loads(row["metadata"], {}),
        created_at=parse_dt(row["created_at"]),
    )


# --------------------------------------------------------------------------- #
# patch applications
# --------------------------------------------------------------------------- #
class PatchApplicationRepository(BaseRepo):
    def insert(self, application: PatchApplication, conn: sqlite3.Connection | None = None) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO patch_applications (application_id, root_task_id, patch_id,
                    repair_attempt_id, base_version, result_version, status, idempotency_key,
                    error, prepared_at, committed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    application.application_id,
                    application.root_task_id,
                    application.patch_id,
                    application.repair_attempt_id,
                    application.base_version,
                    application.result_version,
                    application.status.value,
                    application.idempotency_key,
                    application.error,
                    iso(application.prepared_at),
                    iso(application.committed_at),
                ),
            )

    def get(self, application_id: str, conn: sqlite3.Connection | None = None) -> PatchApplication | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM patch_applications WHERE application_id = ?", (application_id,)
            ).fetchone()
        return _row_to_application(row) if row else None

    def get_by_key(self, key: str, conn: sqlite3.Connection | None = None) -> PatchApplication | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM patch_applications WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return _row_to_application(row) if row else None

    def update(
        self,
        application_id: str,
        *,
        status: PatchApplicationStatus | None = None,
        result_version: str | None = None,
        error: str | None = None,
        committed_at: datetime | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                UPDATE patch_applications
                SET status = COALESCE(?, status),
                    result_version = COALESCE(?, result_version),
                    error = COALESCE(?, error),
                    committed_at = COALESCE(?, committed_at)
                WHERE application_id = ?
                """,
                (
                    status.value if status else None,
                    result_version,
                    error,
                    iso(committed_at),
                    application_id,
                ),
            )

    def list_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[PatchApplication]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM patch_applications WHERE root_task_id = ? ORDER BY prepared_at",
                (root_task_id,),
            ).fetchall()
        return [_row_to_application(r) for r in rows]


def _row_to_application(row: sqlite3.Row) -> PatchApplication:
    return PatchApplication(
        application_id=row["application_id"],
        root_task_id=row["root_task_id"],
        patch_id=row["patch_id"],
        repair_attempt_id=row["repair_attempt_id"],
        base_version=row["base_version"],
        result_version=row["result_version"],
        status=PatchApplicationStatus(row["status"]),
        idempotency_key=row["idempotency_key"],
        error=row["error"],
        prepared_at=parse_dt(row["prepared_at"]),
        committed_at=parse_dt(row["committed_at"]),
    )


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
class EventRepository(BaseRepo):
    def append(self, event: ExecutionEvent, conn: sqlite3.Connection | None = None) -> ExecutionEvent:
        """Append an event, assigning the next sequence inside the transaction."""
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS n FROM execution_events WHERE root_task_id = ?",
                (event.root_task_id,),
            ).fetchone()
            sequence = int(row["n"]) + 1
            c.execute(
                """
                INSERT INTO execution_events (event_id, root_task_id, task_id, attempt_id,
                    dispatch_batch_id, source_version, sequence, timestamp, actor_id, event_type,
                    payload, artifact_refs, duration_ms)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_id,
                    event.root_task_id,
                    event.task_id,
                    event.attempt_id,
                    event.dispatch_batch_id,
                    event.source_version,
                    sequence,
                    iso(event.timestamp),
                    event.actor_id,
                    event.event_type.value,
                    dumps(event.payload),
                    dumps(event.artifact_refs),
                    event.duration_ms,
                ),
            )
        return event.model_copy(update={"sequence": sequence})

    def list_after(
        self,
        root_task_id: str,
        after_seq: int = 0,
        limit: int = 200,
        conn: sqlite3.Connection | None = None,
    ) -> list[ExecutionEvent]:
        with self._tx(conn) as c:
            rows = c.execute(
                """
                SELECT * FROM execution_events
                WHERE root_task_id = ? AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                (root_task_id, after_seq, limit),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def count(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> int:
        with self._tx(conn) as c:
            return int(
                c.execute(
                    "SELECT COUNT(*) AS n FROM execution_events WHERE root_task_id = ?",
                    (root_task_id,),
                ).fetchone()["n"]
            )

    def max_sequence(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> int:
        with self._tx(conn) as c:
            return int(
                c.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS n FROM execution_events WHERE root_task_id = ?",
                    (root_task_id,),
                ).fetchone()["n"]
            )


def _row_to_event(row: sqlite3.Row) -> ExecutionEvent:
    return ExecutionEvent(
        event_id=row["event_id"],
        root_task_id=row["root_task_id"],
        task_id=row["task_id"],
        attempt_id=row["attempt_id"],
        dispatch_batch_id=row["dispatch_batch_id"],
        source_version=row["source_version"],
        sequence=int(row["sequence"]),
        timestamp=parse_dt(row["timestamp"]),
        actor_id=row["actor_id"],
        event_type=row["event_type"],
        payload=loads(row["payload"], {}),
        artifact_refs=loads(row["artifact_refs"], []),
        duration_ms=row["duration_ms"],
    )


# --------------------------------------------------------------------------- #
# workflow configs / control / idempotency / uploads / contracts / checks
# --------------------------------------------------------------------------- #
class WorkflowConfigRepository(BaseRepo):
    def insert(self, config: WorkflowConfig, conn: sqlite3.Connection | None = None) -> None:
        assert config.workflow_version, "config must be bound before saving"
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO workflow_configs (workflow_version, schema_version, check_mode,
                    semantic_hash, content_digest, config_json, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    config.workflow_version,
                    config.schema_version,
                    config.check_mode.value,
                    config.semantic_hash or config.compute_semantic_hash(),
                    config.compute_semantic_hash(),
                    dumps(config.model_dump(mode="json")),
                    iso(datetime.now(timezone.utc)),
                ),
            )

    def get(self, workflow_version: str, conn: sqlite3.Connection | None = None) -> WorkflowConfig | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT config_json FROM workflow_configs WHERE workflow_version = ?",
                (workflow_version,),
            ).fetchone()
        return WorkflowConfig.model_validate(loads(row["config_json"])) if row else None

    def list(self, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT workflow_version, semantic_hash, check_mode, created_at "
                "FROM workflow_configs ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def exists(self, workflow_version: str, conn: sqlite3.Connection | None = None) -> bool:
        with self._tx(conn) as c:
            return (
                c.execute(
                    "SELECT 1 FROM workflow_configs WHERE workflow_version = ?", (workflow_version,)
                ).fetchone()
                is not None
            )


class ControlRepository(BaseRepo):
    def create(
        self,
        root_task_id: str,
        run_segment_id: str,
        graph_steps_granted: int,
        conn: sqlite3.Connection | None = None,
    ) -> TaskControl:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO task_controls (root_task_id, run_segment_id, graph_steps_granted, updated_at)
                VALUES (?,?,?,?)
                """,
                (root_task_id, run_segment_id, graph_steps_granted, iso(datetime.now(timezone.utc))),
            )
        return self.get(root_task_id, conn=conn)  # type: ignore[return-value]

    def get(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> TaskControl | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM task_controls WHERE root_task_id = ?", (root_task_id,)
            ).fetchone()
        return _row_to_control(row) if row else None

    def ensure(
        self, root_task_id: str, run_segment_id: str, graph_steps_granted: int
    ) -> TaskControl:
        control = self.get(root_task_id)
        if control is None:
            control = self.create(root_task_id, run_segment_id, graph_steps_granted)
        return control

    def acquire_lease(
        self,
        root_task_id: str,
        owner: str,
        expires_at: datetime,
        *,
        expected_revision: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> tuple[bool, TaskControl]:
        """Atomically take the lease and bump the fencing token (§11.1)."""
        now = datetime.now(timezone.utc)
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM task_controls WHERE root_task_id = ?", (root_task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown task control for {root_task_id}")
            current = _row_to_control(row)
            lease_free = (
                current.lease_expires_at is None or current.lease_expires_at <= now
            ) or current.lease_owner == owner
            if not lease_free:
                return False, current
            if expected_revision is not None:
                task_row = c.execute(
                    "SELECT revision FROM tasks WHERE task_id = ?", (root_task_id,)
                ).fetchone()
                if task_row is None or int(task_row["revision"]) != expected_revision:
                    return False, current
            c.execute(
                """
                UPDATE task_controls
                SET lease_owner = ?, lease_expires_at = ?, heartbeat_at = ?,
                    fencing_token = fencing_token + 1, updated_at = ?
                WHERE root_task_id = ?
                """,
                (owner, iso(expires_at), iso(now), iso(now), root_task_id),
            )
            updated = self.get(root_task_id, conn=c)
        return True, updated  # type: ignore[return-value]

    def update(
        self,
        root_task_id: str,
        *,
        heartbeat: bool = False,
        lease_expires_at: datetime | None = None,
        fencing_token: int | None = None,
        add_graph_steps: int = 0,
        graph_steps_used: int | None = None,
        run_segment_id: str | None = None,
        current_decision_id: str | None = None,
        parent_corrections: int | None = None,
        required_action: str | None = None,
        release_lease: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [iso(datetime.now(timezone.utc))]
        if heartbeat:
            sets.append("heartbeat_at = ?")
            params.append(iso(datetime.now(timezone.utc)))
        if lease_expires_at is not None:
            sets.append("lease_expires_at = ?")
            params.append(iso(lease_expires_at))
        if fencing_token is not None:
            sets.append("fencing_token = ?")
            params.append(fencing_token)
        if add_graph_steps:
            sets.append("graph_steps_granted = graph_steps_granted + ?")
            params.append(add_graph_steps)
        if graph_steps_used is not None:
            sets.append("graph_steps_used = ?")
            params.append(graph_steps_used)
        if run_segment_id is not None:
            sets.append("run_segment_id = ?")
            params.append(run_segment_id)
        if current_decision_id is not None:
            sets.append("current_decision_id = ?")
            params.append(current_decision_id)
        if parent_corrections is not None:
            sets.append("parent_corrections = ?")
            params.append(parent_corrections)
        if required_action is not None:
            sets.append("required_action = ?")
            params.append(required_action)
        if release_lease:
            sets.append("lease_owner = NULL")
            sets.append("lease_expires_at = NULL")
        params.append(root_task_id)
        with self._tx(conn) as c:
            c.execute(f"UPDATE task_controls SET {', '.join(sets)} WHERE root_task_id = ?", params)

    def increment_graph_steps(
        self, root_task_id: str, amount: int = 1, conn: sqlite3.Connection | None = None
    ) -> None:
        with self._tx(conn) as c:
            c.execute(
                "UPDATE task_controls SET graph_steps_used = graph_steps_used + ?, updated_at = ? "
                "WHERE root_task_id = ?",
                (amount, iso(datetime.now(timezone.utc)), root_task_id),
            )


def _row_to_control(row: sqlite3.Row) -> TaskControl:
    return TaskControl(
        root_task_id=row["root_task_id"],
        lease_owner=row["lease_owner"],
        lease_expires_at=parse_dt(row["lease_expires_at"]),
        heartbeat_at=parse_dt(row["heartbeat_at"]),
        fencing_token=int(row["fencing_token"]),
        run_segment_id=row["run_segment_id"],
        graph_steps_granted=int(row["graph_steps_granted"]),
        graph_steps_used=int(row["graph_steps_used"]),
        current_decision_id=row["current_decision_id"],
        parent_corrections=int(row["parent_corrections"]),
        required_action=row["required_action"],
    )


class IdempotencyRepository(BaseRepo):
    def get(self, operation_key: str, conn: sqlite3.Connection | None = None) -> dict | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM idempotency_records WHERE operation_key = ?", (operation_key,)
            ).fetchone()
        return dict(row) if row else None

    def put(
        self,
        *,
        operation_key: str,
        operation_kind: str,
        request_fingerprint: str,
        response_ref: str | None,
        root_task_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        """Insert if absent; returns True when newly recorded.

        An existing key with a different fingerprint is a conflict.
        """
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM idempotency_records WHERE operation_key = ?", (operation_key,)
            ).fetchone()
            if row is not None:
                if row["request_fingerprint"] != request_fingerprint:
                    raise ConflictError(
                        f"operation key {operation_key} was already used with different content"
                    )
                return False
            c.execute(
                """
                INSERT INTO idempotency_records (operation_key, operation_kind, root_task_id,
                    request_fingerprint, response_ref, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    operation_key,
                    operation_kind,
                    root_task_id,
                    request_fingerprint,
                    response_ref,
                    iso(datetime.now(timezone.utc)),
                ),
            )
            return True


class UploadRepository(BaseRepo):
    def insert(
        self,
        *,
        source_id: str,
        content_digest: str,
        files: list[dict],
        total_bytes: int,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO uploads (source_id, content_digest, files, total_bytes, created_at)
                VALUES (?,?,?,?,?)
                """,
                (source_id, content_digest, dumps(files), total_bytes, iso(datetime.now(timezone.utc))),
            )

    def get(self, source_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
        with self._tx(conn) as c:
            row = c.execute("SELECT * FROM uploads WHERE source_id = ?", (source_id,)).fetchone()
        if row is None:
            return None
        return {
            "source_id": row["source_id"],
            "content_digest": row["content_digest"],
            "files": loads(row["files"], []),
            "total_bytes": int(row["total_bytes"]),
            "root_task_id": row["root_task_id"],
            "created_at": row["created_at"],
        }

    def attach_root(self, source_id: str, root_task_id: str, conn: sqlite3.Connection | None = None) -> None:
        with self._tx(conn) as c:
            c.execute("UPDATE uploads SET root_task_id = ? WHERE source_id = ?", (root_task_id, source_id))


class ContractRepository(BaseRepo):
    def insert(
        self,
        *,
        contract_version: str,
        root_task_id: str,
        content_hash: str,
        artifact_id: str,
        supersedes: str | None,
        append_reason: str | None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO acceptance_contracts (contract_version, root_task_id, content_hash,
                    artifact_id, supersedes, append_reason, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    contract_version,
                    root_task_id,
                    content_hash,
                    artifact_id,
                    supersedes,
                    append_reason,
                    iso(datetime.now(timezone.utc)),
                ),
            )

    def get(
        self,
        contract_version: str,
        root_task_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict | None:
        with self._tx(conn) as c:
            if root_task_id is None:
                row = c.execute(
                    "SELECT * FROM acceptance_contracts WHERE contract_version = ? LIMIT 1",
                    (contract_version,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM acceptance_contracts WHERE contract_version = ? AND root_task_id = ?",
                    (contract_version, root_task_id),
                ).fetchone()
        return dict(row) if row else None

    def list_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM acceptance_contracts WHERE root_task_id = ? ORDER BY created_at",
                (root_task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
        rows = self.list_by_root(root_task_id, conn=conn)
        return rows[-1] if rows else None


class CheckResultRepository(BaseRepo):
    def insert(self, result: CheckResult, root_task_id: str, conn: sqlite3.Connection | None = None, *, refresh: bool = False) -> bool:
        with self._tx(conn) as c:
            if refresh:
                # The immutable check bundles retain each execution; this table
                # is the latest result of a check within the current attempt.
                c.execute("DELETE FROM check_results WHERE root_task_id=? AND check_id=? AND source_version=? AND producer_attempt_id=?",
                    (root_task_id, result.check_id, result.source_version, result.producer_attempt_id))
            cur = c.execute(
                """
                INSERT OR IGNORE INTO check_results (check_id, root_task_id, contract_version,
                    source_version, producer_attempt_id, method, status, evidence_refs, reason,
                    test_count, passed_count, failed_count, skipped_count, test_suite_hash,
                    executed, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    result.check_id,
                    root_task_id,
                    result.contract_version,
                    result.source_version,
                    result.producer_attempt_id,
                    result.method.value if isinstance(result.method, CheckMethod) else result.method,
                    result.status.value,
                    dumps(result.evidence_refs),
                    result.reason,
                    result.test_count,
                    result.passed_count,
                    result.failed_count,
                    result.skipped_count,
                    result.test_suite_hash,
                    int(result.executed),
                    iso(datetime.now(timezone.utc)),
                ),
            )
            return cur.rowcount == 1

    def list_for_version(
        self, root_task_id: str, source_version: str, conn: sqlite3.Connection | None = None
    ) -> list[CheckResult]:
        with self._tx(conn) as c:
            rows = c.execute(
                """
                SELECT * FROM check_results WHERE root_task_id = ? AND source_version = ?
                ORDER BY id
                """,
                (root_task_id, source_version),
            ).fetchall()
        return [_row_to_check(r) for r in rows]

    def latest_for_check(
        self, root_task_id: str, check_id: str, source_version: str, conn: sqlite3.Connection | None = None
    ) -> CheckResult | None:
        with self._tx(conn) as c:
            row = c.execute(
                """
                SELECT * FROM check_results
                WHERE root_task_id = ? AND check_id = ? AND source_version = ?
                ORDER BY id DESC LIMIT 1
                """,
                (root_task_id, check_id, source_version),
            ).fetchone()
        return _row_to_check(row) if row else None

    def list_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[CheckResult]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM check_results WHERE root_task_id = ? ORDER BY id", (root_task_id,)
            ).fetchall()
        return [_row_to_check(r) for r in rows]


def _row_to_check(row: sqlite3.Row) -> CheckResult:
    return CheckResult(
        check_id=row["check_id"],
        contract_version=row["contract_version"],
        source_version=row["source_version"],
        producer_attempt_id=row["producer_attempt_id"],
        method=CheckMethod(row["method"]) if row["method"] else None,
        status=CheckStatus(row["status"]),
        evidence_refs=loads(row["evidence_refs"], []),
        reason=row["reason"],
        test_count=row["test_count"],
        passed_count=row["passed_count"],
        failed_count=row["failed_count"],
        skipped_count=row["skipped_count"],
        test_suite_hash=row["test_suite_hash"],
        executed=bool(row["executed"]),
    )


class FindingRepository(BaseRepo):
    def upsert(self, finding: Finding, conn: sqlite3.Connection | None = None) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT INTO findings (finding_id, root_task_id, source_version, producer_attempt_id,
                    goal_ref, required_for_goal, check_id, file_path, line, symbol, rule, severity,
                    message, evidence_refs, status, resolution_evidence_refs, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(finding_id, source_version) DO UPDATE SET
                    status = excluded.status,
                    resolution_evidence_refs = excluded.resolution_evidence_refs,
                    updated_at = excluded.updated_at
                """,
                (
                    finding.finding_id,
                    finding.root_task_id,
                    finding.source_version,
                    finding.producer_attempt_id,
                    finding.goal_ref,
                    int(finding.required_for_goal),
                    finding.check_id,
                    finding.file_path,
                    finding.line,
                    finding.symbol,
                    finding.rule,
                    finding.severity.value,
                    finding.message,
                    dumps(finding.evidence_refs),
                    finding.status.value,
                    dumps(finding.resolution_evidence_refs),
                    iso(datetime.now(timezone.utc)),
                    iso(datetime.now(timezone.utc)),
                ),
            )

    def get(self, finding_id: str, source_version: str, conn: sqlite3.Connection | None = None) -> Finding | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM findings WHERE finding_id = ? AND source_version = ?",
                (finding_id, source_version),
            ).fetchone()
        return _row_to_finding(row) if row else None

    def list_by_root(
        self, root_task_id: str, status: str | None = None, conn: sqlite3.Connection | None = None
    ) -> list[Finding]:
        with self._tx(conn) as c:
            if status:
                rows = c.execute(
                    "SELECT * FROM findings WHERE root_task_id = ? AND status = ? ORDER BY created_at",
                    (root_task_id, status),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM findings WHERE root_task_id = ? ORDER BY created_at",
                    (root_task_id,),
                ).fetchall()
        return [_row_to_finding(r) for r in rows]

    def latest(self, root_task_id: str, finding_id: str, conn: sqlite3.Connection | None = None) -> Finding | None:
        with self._tx(conn) as c:
            row = c.execute(
                """
                SELECT * FROM findings WHERE root_task_id = ? AND finding_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (root_task_id, finding_id),
            ).fetchone()
        return _row_to_finding(row) if row else None

    def list_current_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[Finding]:
        """One authoritative row per finding_id: the newest version persisted for the root task."""
        with self._tx(conn) as c:
            rows = c.execute(
                """
                SELECT f.* FROM findings f
                JOIN (
                    SELECT finding_id, MAX(rowid) AS newest_rowid
                    FROM findings WHERE root_task_id = ? GROUP BY finding_id
                ) latest ON latest.newest_rowid = f.rowid
                ORDER BY f.created_at, f.finding_id
                """,
                (root_task_id,),
            ).fetchall()
        return [_row_to_finding(r) for r in rows]

    def resolve(
        self,
        finding_id: str,
        source_version: str,
        evidence_refs: list[str],
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        with self._tx(conn) as c:
            cur = c.execute(
                """
                UPDATE findings SET status = ?, resolution_evidence_refs = ?, updated_at = ?
                WHERE finding_id = ? AND source_version = ?
                """,
                (FindingStatus.RESOLVED.value, dumps(evidence_refs), iso(datetime.now(timezone.utc)), finding_id, source_version),
            )
            return cur.rowcount == 1


def _row_to_finding(row: sqlite3.Row) -> Finding:
    return Finding(
        finding_id=row["finding_id"],
        root_task_id=row["root_task_id"],
        source_version=row["source_version"],
        producer_attempt_id=row["producer_attempt_id"],
        goal_ref=row["goal_ref"],
        required_for_goal=bool(row["required_for_goal"]),
        check_id=row["check_id"],
        file_path=row["file_path"],
        line=row["line"],
        symbol=row["symbol"],
        rule=row["rule"],
        severity=row["severity"],
        message=row["message"],
        evidence_refs=loads(row["evidence_refs"], []),
        status=row["status"],
        resolution_evidence_refs=loads(row["resolution_evidence_refs"], []),
    )


class VerificationRepository(BaseRepo):
    def insert(
        self, report: VerificationReport, artifact_id: str | None = None, conn: sqlite3.Connection | None = None
    ) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT OR REPLACE INTO verifications (verification_id, root_task_id,
                    producer_attempt_id, source_version, contract_version, target_finding_ids,
                    coverage, not_run, passed, evidence_refs, notes, artifact_id, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    report.verification_id,
                    report.root_task_id,
                    report.producer_attempt_id,
                    report.source_version,
                    report.contract_version,
                    dumps(report.target_finding_ids),
                    dumps(report.coverage),
                    dumps(report.not_run),
                    b2i(report.passed),
                    dumps(report.evidence_refs),
                    report.notes,
                    artifact_id,
                    iso(datetime.now(timezone.utc)),
                ),
            )

    def list_by_root(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> list[dict]:
        with self._tx(conn) as c:
            rows = c.execute(
                "SELECT * FROM verifications WHERE root_task_id = ? ORDER BY created_at",
                (root_task_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest(self, root_task_id: str, conn: sqlite3.Connection | None = None) -> dict | None:
        rows = self.list_by_root(root_task_id, conn=conn)
        return rows[-1] if rows else None


class ReportRepository(BaseRepo):
    def insert(
        self,
        *,
        report_id: str,
        root_task_id: str,
        kind: str,
        artifact_id: str | None,
        final_status: str,
        passed: bool | None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        with self._tx(conn) as c:
            c.execute(
                """
                INSERT OR REPLACE INTO reports (report_id, root_task_id, kind, artifact_id,
                    final_status, passed, created_at)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    report_id,
                    root_task_id,
                    kind,
                    artifact_id,
                    final_status,
                    b2i(passed),
                    iso(datetime.now(timezone.utc)),
                ),
            )

    def latest(self, root_task_id: str, kind: str = "final", conn: sqlite3.Connection | None = None) -> dict | None:
        with self._tx(conn) as c:
            row = c.execute(
                "SELECT * FROM reports WHERE root_task_id = ? AND kind = ? ORDER BY created_at DESC LIMIT 1",
                (root_task_id, kind),
            ).fetchone()
        return dict(row) if row else None


class Repos:
    """Bundle of repositories sharing one :class:`Database`."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.tasks = TaskRepository(db)
        self.attempts = AttemptRepository(db)
        self.batches = BatchRepository(db)
        self.results = ResultRepository(db)
        self.terminals = TerminalRepository(db)
        self.budget = BudgetRepository(db)
        self.artifacts = ArtifactRepository(db)
        self.patch_applications = PatchApplicationRepository(db)
        self.events = EventRepository(db)
        self.workflows = WorkflowConfigRepository(db)
        self.controls = ControlRepository(db)
        self.idempotency = IdempotencyRepository(db)
        self.uploads = UploadRepository(db)
        self.contracts = ContractRepository(db)
        self.check_results = CheckResultRepository(db)
        self.findings = FindingRepository(db)
        self.verifications = VerificationRepository(db)
        self.reports = ReportRepository(db)
