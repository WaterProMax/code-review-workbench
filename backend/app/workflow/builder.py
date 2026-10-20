"""Graph construction: wires the parent/child nodes into a real StateGraph (§8.2).

Child nodes never finish the root task. Paths back to the parent go through
``budget`` (the business step-budget gate) and only ``finalize`` — after
``check_finish`` has accepted the proposal — has an edge to END.
``enter_wait`` records the pause, then ``await_human`` interrupts until a
resume command. The step budget is the ledger in ``task_controls``; it is not
``recursion_limit``.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.providers.base import LLMError
from app.workflow.nodes import AttemptPayload, WorkflowRuntime
from app.workflow.state import WorkflowState

# Framework superstep cap. A full repair is many nodes per business step, and
# the granted step ledger can grow on resume up to the configured cap, so this
# stays above that ledger. It does not replace ``graph_steps``.
GRAPH_RECURSION_LIMIT = 16384


def _retry_parent_model(exc: Exception) -> bool:
    """Retry only a transient model failure of the parent decision call.

    A rejected action is a normal return value and starts a new decision; it
    must not be replayed as the same node attempt.
    """
    return isinstance(exc, LLMError) and bool(exc.recoverable)


PARENT_DECIDE_RETRY = RetryPolicy(
    initial_interval=0.0,
    backoff_factor=1.0,
    max_interval=0.0,
    max_attempts=2,
    jitter=False,
    retry_on=_retry_parent_model,
)


def build_graph(runtime: WorkflowRuntime, *, checkpointer: Any = None):
    graph = StateGraph(WorkflowState)
    graph.add_node("initialize", runtime.n_initialize)
    graph.add_node("plan", runtime.n_plan)
    graph.add_node("validate_plan", runtime.n_validate_plan)
    graph.add_node("replan", runtime.n_replan)
    graph.add_node("commit_plan", runtime.n_commit_plan)
    graph.add_node("budget", runtime.n_budget)
    graph.add_node("parent_decide", runtime.n_parent_decide, retry_policy=PARENT_DECIDE_RETRY)
    graph.add_node("validate_action", runtime.n_validate_action)
    graph.add_node(
        "dispatch",
        runtime.n_dispatch,
        destinations=("run_attempt", "budget", "collect_results"),
    )
    graph.add_node(
        "run_attempt",
        runtime.n_run_attempt,
        retry_policy=PARENT_DECIDE_RETRY,
        input_schema=AttemptPayload,
    )
    graph.add_node("collect_results", runtime.n_collect_results)
    graph.add_node("detect", runtime.n_detect)
    graph.add_node("apply_patch", runtime.n_apply_patch)
    graph.add_node("check_finish", runtime.n_check_finish)
    graph.add_node("enter_wait", runtime.n_enter_wait)
    graph.add_node(
        "await_human",
        runtime.n_await_human,
        destinations=("budget", "finalize_terminated"),
    )
    graph.add_node("finalize_terminated", runtime.n_finalize_terminated)
    graph.add_node("finalize", runtime.n_finalize)

    graph.add_edge(START, "initialize")
    graph.add_conditional_edges(
        "initialize",
        runtime.route_after_initialize,
        {"plan": "plan", "budget": "budget", "enter_wait": "enter_wait"},
    )
    graph.add_edge("plan", "validate_plan")
    graph.add_conditional_edges(
        "validate_plan",
        runtime.route_after_validate_plan,
        {"replan": "replan", "commit_plan": "commit_plan", "enter_wait": "enter_wait"},
    )
    graph.add_edge("replan", "validate_plan")
    graph.add_edge("commit_plan", "budget")
    graph.add_conditional_edges(
        "budget",
        runtime.route_after_budget,
        {"enter_wait": "enter_wait", "parent_decide": "parent_decide"},
    )
    # parent_decide, validate_action, dispatch, await_human and check_finish
    # route with Command. run_attempt is the Send target for one attempt.
    graph.add_edge("run_attempt", "collect_results")
    graph.add_edge("collect_results", "detect")
    graph.add_edge("detect", "budget")
    graph.add_edge("apply_patch", "detect")
    graph.add_edge("enter_wait", "await_human")
    graph.add_edge("finalize", END)
    graph.add_edge("finalize_terminated", END)
    return graph.compile(checkpointer=checkpointer)
