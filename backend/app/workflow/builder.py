"""Graph construction: wires the parent/child nodes into a real StateGraph (§8.2).

The graph never lets a child node finish the task: every path back to the parent
goes through ``collect_results``/``detect``, and only ``finalize`` (after the
control layer re-derives the completion condition) may end a run.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, StateGraph

from app.workflow.nodes import TERMINAL_ROOT_STATUSES, WorkflowRuntime
from app.workflow.state import WorkflowState

_ACTION_TARGET = {
    "dispatch_task": "dispatch",
    "dispatch_batch": "dispatch",
    "apply_patch": "apply_patch",
    "wait_for_recovery": "wait",
    "finish": "finalize",
}


def route_after_initialize(state: dict) -> str:
    return "wait" if state.get("fatal_error") else "parent_decide"


def route_after_validate(state: dict) -> str:
    raw = state.get("next_action")
    if not raw:
        return "parent_decide"
    return _ACTION_TARGET.get(raw.get("action", ""), "parent_decide")


def route_after_dispatch(state: dict) -> str:
    return "collect_results" if state.get("dispatch_ok") else "parent_decide"


def route_after_finalize(state: dict) -> str:
    return "end" if state.get("status") in TERMINAL_ROOT_STATUSES else "parent_decide"


def build_graph(runtime: WorkflowRuntime, *, checkpointer: Any = None):
    graph = StateGraph(WorkflowState)
    graph.add_node("initialize", runtime.n_initialize)
    graph.add_node("parent_decide", runtime.n_parent_decide)
    graph.add_node("validate_action", runtime.n_validate_action)
    graph.add_node("dispatch", runtime.n_dispatch)
    graph.add_node("collect_results", runtime.n_collect_results)
    graph.add_node("detect", runtime.n_detect)
    graph.add_node("apply_patch", runtime.n_apply_patch)
    graph.add_node("wait", runtime.n_wait)
    graph.add_node("finalize", runtime.n_finalize)

    graph.set_entry_point("initialize")
    graph.add_conditional_edges(
        "initialize", route_after_initialize, {"parent_decide": "parent_decide", "wait": "wait"}
    )
    graph.add_edge("parent_decide", "validate_action")
    graph.add_conditional_edges(
        "validate_action",
        route_after_validate,
        {
            "dispatch": "dispatch",
            "apply_patch": "apply_patch",
            "finalize": "finalize",
            "wait": "wait",
            "parent_decide": "parent_decide",
        },
    )
    graph.add_conditional_edges(
        "dispatch", route_after_dispatch, {"collect_results": "collect_results", "parent_decide": "parent_decide"}
    )
    graph.add_edge("collect_results", "detect")
    graph.add_edge("detect", "parent_decide")
    graph.add_edge("apply_patch", "detect")
    graph.add_conditional_edges(
        "finalize", route_after_finalize, {"end": END, "parent_decide": "parent_decide"}
    )
    graph.add_edge("wait", END)
    return graph.compile(checkpointer=checkpointer)
