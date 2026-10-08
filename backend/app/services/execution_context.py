"""Execution token bound to one graph invocation, inherited by child tasks."""
from contextvars import ContextVar
from datetime import datetime, timezone

active_execution: ContextVar[tuple[str, int] | None] = ContextVar("active_execution", default=None)

class ExecutionFenced(RuntimeError):
    pass

def check_execution(conn, root_task_id: str, token: int) -> None:
    row = conn.execute("SELECT fencing_token, lease_expires_at FROM task_controls WHERE root_task_id = ?", (root_task_id,)).fetchone()
    if row is None:
        if token:
            raise ExecutionFenced("执行权记录不存在")
        return
    if row["fencing_token"] != token:
        raise ExecutionFenced("STALE_FENCING_TOKEN: 执行权已经被接管")
    if token and (not row["lease_expires_at"] or datetime.fromisoformat(row["lease_expires_at"]) <= datetime.now(timezone.utc)):
        raise ExecutionFenced("LEASE_EXPIRED: 执行租约已过期")
