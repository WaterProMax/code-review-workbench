import { expect, test } from "vitest";
import type { ExecutionEvent, TaskDetailResponse } from "../src/api/types";
import { inferWorkflowStep } from "../src/steps";

function detail(overrides: Partial<TaskDetailResponse> = {}): TaskDetailResponse {
  return {
    root_task_id: "T-1",
    goal: "检查",
    status: "running",
    passed: null,
    revision: 1,
    workflow_version: "wf",
    check_mode: "sequential",
    contract_version: null,
    source_version: null,
    tasks: [],
    detection: null,
    next_action: null,
    required_action: null,
    error: null,
    report_ref: null,
    report_artifact_id: null,
    checks: [],
    budgets: { items: [] },
    artifact_refs: [],
    created_at: "2026-10-08T02:00:00Z",
    updated_at: "2026-10-08T02:00:00Z",
    ...overrides,
  };
}

function event(sequence: number, eventType: string, payload: Record<string, unknown> = {}): ExecutionEvent {
  return {
    event_id: `ev-${sequence}`,
    root_task_id: "T-1",
    task_id: null,
    attempt_id: null,
    dispatch_batch_id: null,
    source_version: null,
    sequence,
    timestamp: "2026-10-08T02:00:00Z",
    actor_id: "parent",
    event_type: eventType,
    payload,
    artifact_refs: [],
    duration_ms: null,
  };
}

test("maps runtime nodes onto the step bar and keeps a waiting note", () => {
  expect(inferWorkflowStep(detail({ current_nodes: ["validate_plan"] }), []).step).toBe("规划");
  expect(
    inferWorkflowStep(
      detail({
        current_nodes: ["run_attempt"],
        tasks: [
          {
            task_id: "C1",
            root_task_id: "T-1",
            parent_task_id: "T-1",
            task_level: "child",
            agent_id: "reviewer",
            agent_version: "1",
            task_kind: "review",
            goal: "审查",
            depends_on: [],
            status: "running",
            passed: null,
            skip_reason: null,
            workflow_version: "wf",
            revision: 1,
            created_at: "2026-10-08T02:00:00Z",
            updated_at: "2026-10-08T02:00:00Z",
          },
        ],
      }),
      [],
    ).step,
  ).toBe("审查");
  const held = inferWorkflowStep(
    detail({ current_nodes: ["parent_decide", "await_human"] }),
    [event(1, "task_dispatched", { task_kind: "fix" })],
  );
  expect(held.step).toBe("修复");
  expect(held.waiting).toBe(true);
  expect(inferWorkflowStep(detail({ current_nodes: ["apply_patch"] }), [], true).step).toBe("打补丁");
  expect(inferWorkflowStep(detail({ status: "completed", current_nodes: ["detect"] }), []).step).toBe("结案");
  expect(inferWorkflowStep(detail({ current_nodes: ["budget"] }), []).step).toBe("规划");
});
