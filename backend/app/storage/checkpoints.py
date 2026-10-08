"""LangGraph checkpointer adapter.

The framework's checkpoint database is owned by this adapter only — the business
schema never assumes the framework's internal tables (§6.1). Checkpoints are
addressed by a stable thread id derived from the root task so every checkpoint
read and every resume uses the same mapping (§11.1).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

THREAD_PREFIX = "hw2-task:"


def thread_id_for(root_task_id: str) -> str:
    """Stable mapping from a business root task to a framework thread id."""
    return f"{THREAD_PREFIX}{root_task_id}"


@asynccontextmanager
async def open_checkpointer(path: Path | str) -> AsyncIterator["object"]:
    """Open the SQLite checkpointer, creating the file if needed."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        yield saver
