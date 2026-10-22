"""SQLite access layer: connections, PRAGMAs, transactions and migrations.

Design notes (ImplementationPlan §6.1):
* connections are opened per operation and closed again, so the async layer can
  hand each call to a worker thread without sharing a handle across threads;
* every write goes through a short ``transaction`` block — model and tool calls
  never run while a write transaction is open;
* WAL + ``busy_timeout`` + a bounded retry give sane local concurrent behaviour;
* foreign keys are enabled on every connection.
"""

from __future__ import annotations

import contextvars
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger("hw2.storage")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_LOCK_MESSAGES = ("database is locked", "database table is locked")
_MAX_RETRIES = 6
_BASE_DELAY = 0.02

# The active transaction of the *current context* (one per asyncio task). Using a
# ContextVar rather than a thread-local means two concurrently running attempts
# never share a connection, while nested calls inside one attempt join it.
_active: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "hw2_active_tx", default=None
)


class MigrationError(RuntimeError):
    pass


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")


class Database:
    """Thin, explicit SQLite wrapper used by the repositories.

    A transaction is re-entrant *within one execution context*: a repository call
    made from inside another transaction (a nested read or bookkeeping write)
    joins the outer transaction instead of opening a second connection, which
    would otherwise deadlock against the outer writer's lock.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    # ---- connections -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        _apply_pragmas(conn)
        return conn

    @contextmanager
    def transaction(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        """Run one short transaction, retrying briefly when *acquiring* the lock.

        Contention is only retried before the body runs: a generator-based context
        manager cannot yield twice, and re-running an arbitrary body would repeat
        whatever side effects it already performed.
        """
        active = _active.get()
        if active is not None:
            active["depth"] += 1
            try:
                yield active["conn"]
            finally:
                active["depth"] -= 1
            return

        conn = None
        for attempt in range(_MAX_RETRIES + 1):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                break
            except sqlite3.OperationalError as exc:
                conn.close()
                conn = None
                if any(msg in str(exc) for msg in _LOCK_MESSAGES) and attempt < _MAX_RETRIES:
                    time.sleep(_BASE_DELAY * (2**attempt))
                    continue
                raise

        assert conn is not None
        holder = {"conn": conn, "depth": 1}
        token = _active.set(holder)
        try:
            if write:
                from app.services.execution_context import active_execution, check_execution
                execution = active_execution.get()
                if execution is not None:
                    check_execution(conn, *execution)
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            conn.execute("COMMIT")
        finally:
            _active.reset(token)
            conn.close()

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        with self.transaction(write=False) as conn:
            yield conn

    # ---- migrations --------------------------------------------------------
    def initialize(self) -> list[str]:
        """Apply pending migrations; returns the versions applied this call."""
        applied: list[str] = []
        with self.transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            done = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, sql in self._pending_migrations(done):
                for statement in _split_statements(sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))",
                    (version,),
                )
                applied.append(version)
        if applied:
            logger.info("applied migrations: %s", ", ".join(applied))
        return applied

    @staticmethod
    def _pending_migrations(done: set[str]) -> list[tuple[str, str]]:
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        pending: list[tuple[str, str]] = []
        for path in files:
            version = path.stem
            if version in done:
                continue
            pending.append((version, path.read_text(encoding="utf-8")))
        return pending

    def schema_versions(self) -> list[str]:
        with self.reader() as conn:
            return [row["version"] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]


def _split_statements(sql: str) -> list[str]:
    """Split a migration file into statements, ignoring comment-only chunks.

    The files contain no semicolons or ``--`` markers inside string literals, so
    a simple split on ``;`` at line ends is sufficient and predictable.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(buffer).rstrip(";").strip())
            buffer = []
    if buffer:
        statements.append("\n".join(buffer).strip())
    return [s for s in statements if s]
