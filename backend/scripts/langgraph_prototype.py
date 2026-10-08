"""P00 LangGraph prototype.

Verifies, against the pinned dependency versions, that the real framework
features the workbench relies on actually work:

* a real SQLite checkpointer (persisted across graph instances)
* conditional routing (fan-out from a router)
* parallel state merge (reducer-annotated channels)
* pause / resume via ``interrupt`` + ``Command(resume=...)``

Run:  uv run python scripts/langgraph_prototype.py
The conclusion is recorded in docs/Handoff.md. This file is a verification
artefact only; the business graph lives in app/workflow/.
"""

from __future__ import annotations

import asyncio
import json
import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict


class ProtoState(TypedDict, total=False):
    route: str
    log: Annotated[list[str], operator.add]
    branches: Annotated[list[str], operator.add]
    approval_notes: Annotated[list[str], operator.add]


def start_node(state: ProtoState) -> dict:
    return {"log": ["start"], "route": state.get("route", "parallel")}


def router(state: ProtoState) -> list[str]:
    return ["branch_a", "branch_b"] if state.get("route") == "parallel" else ["merge"]


def branch_a(_: ProtoState) -> dict:
    return {"branches": ["a"], "log": ["branch_a"]}


def branch_b(_: ProtoState) -> dict:
    return {"branches": ["b"], "log": ["branch_b"]}


def merge_node(_: ProtoState) -> dict:
    return {"log": ["merge"]}


def pause_node(state: ProtoState) -> dict:
    from langgraph.types import interrupt

    note = interrupt({"question": "continue?", "so_far": state.get("log", [])})
    return {"approval_notes": [str(note)], "log": ["paused"]}


def finalize_node(_: ProtoState) -> dict:
    return {"log": ["finalize"]}


def build_graph() -> Any:
    from langgraph.graph import END, START, StateGraph

    graph = StateGraph(ProtoState)
    graph.add_node("start", start_node)
    graph.add_node("branch_a", branch_a)
    graph.add_node("branch_b", branch_b)
    graph.add_node("merge", merge_node)
    graph.add_node("pause", pause_node)
    graph.add_node("finalize", finalize_node)
    graph.add_edge(START, "start")
    graph.add_conditional_edges("start", router, ["branch_a", "branch_b", "merge"])
    graph.add_edge("branch_a", "merge")
    graph.add_edge("branch_b", "merge")
    graph.add_edge("merge", "pause")
    graph.add_edge("pause", "finalize")
    graph.add_edge("finalize", END)
    return graph


async def main() -> int:
    import langgraph
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command

    db_path = Path("/tmp/hw2_langgraph_prototype.sqlite")
    if db_path.exists():
        db_path.unlink()

    graph = build_graph()
    report: dict[str, Any] = {"langgraph_version": getattr(langgraph, "__version__", "unknown")}
    ok = True

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        app = graph.compile(checkpointer=saver)
        config = {"configurable": {"thread_id": "proto-1"}}

        first = await app.ainvoke({"route": "parallel"}, config)
        report["after_first_invoke"] = {"log": first.get("log"), "branches": first.get("branches")}
        if sorted(first.get("branches", [])) != ["a", "b"]:
            report["parallel_merge"] = "FAIL"
            ok = False
        else:
            report["parallel_merge"] = "PASS"

        snapshot = await app.aget_state(config)
        # LangGraph 1.x exposes pending interrupts on snapshot.tasks[*].interrupts
        task_interrupts = [i for t in snapshot.tasks for i in (getattr(t, "interrupts", ()) or ())]
        inline_interrupts = snapshot.values.get("__interrupt__") or []
        report["paused"] = bool(task_interrupts or inline_interrupts)
        report["interrupt_payload"] = [
            getattr(i, "value", None) for i in (task_interrupts or inline_interrupts)
        ]
        report["log_before_resume"] = snapshot.values.get("log")
        if not report["paused"] or "finalize" in (snapshot.values.get("log") or []):
            report["interrupt_pause"] = "FAIL"
            ok = False
        else:
            report["interrupt_pause"] = "PASS"
        report["pending_tasks"] = [t.name for t in snapshot.tasks]

        resumed = await app.ainvoke(Command(resume={"answer": "yes"}), config)
        report["after_resume"] = {
            "log": resumed.get("log"),
            "approval_notes": resumed.get("approval_notes"),
        }
        if "finalize" in (resumed.get("log") or []) and resumed.get("approval_notes"):
            report["resume"] = "PASS"
        else:
            report["resume"] = "FAIL"
            ok = False

        history = [s async for s in app.aget_state_history(config)]
        report["checkpoint_history_len"] = len(history)
        if len(history) < 2:
            report["checkpoint_history"] = "FAIL"
            ok = False
        else:
            report["checkpoint_history"] = "PASS"

        config2 = {"configurable": {"thread_id": "proto-2"}}
        second = await app.ainvoke({"route": "serial"}, config2)
        report["conditional_serial_branches"] = second.get("branches")
        if not second.get("branches"):
            report["conditional_routing"] = "PASS"
        else:
            report["conditional_routing"] = "FAIL"
            ok = False

    report["checkpoint_file_created"] = db_path.exists()
    report["checkpoint_file_bytes"] = db_path.stat().st_size if db_path.exists() else 0

    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver2:
        app2 = graph.compile(checkpointer=saver2)
        reopened = await app2.aget_state({"configurable": {"thread_id": "proto-1"}})
        report["state_survives_reopen"] = "finalize" in (reopened.values.get("log") or [])
        if not report["state_survives_reopen"]:
            ok = False

    report["result"] = "PASS" if ok else "FAIL"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
