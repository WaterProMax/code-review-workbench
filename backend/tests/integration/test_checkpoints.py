"""P02: the LangGraph checkpointer adapter works and is keyed by root task."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

import pytest

from app.storage.checkpoints import open_checkpointer, thread_id_for


class Proto(TypedDict):
    log: Annotated[list[str], operator.add]


def _build():
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(Proto)
    graph.add_node("only", lambda state: {"log": ["ran"]})
    graph.add_edge(START, "only")
    graph.add_edge("only", END)
    return graph


def test_thread_id_mapping_is_stable() -> None:
    assert thread_id_for("T100") == "hw2-task:T100"
    assert thread_id_for("T100") == thread_id_for("T100")
    assert thread_id_for("T100") != thread_id_for("T101")


async def test_checkpoint_survives_reopening_the_saver(settings) -> None:
    graph = _build()
    path = settings.checkpoints_db_path
    config = {"configurable": {"thread_id": thread_id_for("T100")}}

    async with open_checkpointer(path) as saver:
        app = graph.compile(checkpointer=saver)
        await app.ainvoke({}, config)

    async with open_checkpointer(path) as saver2:
        app2 = graph.compile(checkpointer=saver2)
        snapshot = await app2.aget_state(config)
        assert snapshot.values.get("log") == ["ran"]
