import type { ExecutionEvent, TaskDetailResponse } from "./api/types";

export const WORKFLOW_STEPS = ["规划", "审查", "修复", "打补丁", "验证", "结案"] as const;
export type WorkflowStep = (typeof WORKFLOW_STEPS)[number];

const PLAN_NODES = new Set(["plan", "validate_plan", "replan", "commit_plan", "initialize"]);
const BATCH_NODES = new Set(["dispatch", "run_attempt", "collect_results"]);
const HOLD_NODES = new Set(["parent_decide", "budget", "detect"]);
const WAIT_NODES = new Set(["await_human", "enter_wait", "wait"]);
const TERMINAL = new Set(["completed", "partial", "failed", "interrupted"]);

export interface StepInference {
  step: WorkflowStep;
  waiting: boolean;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

export function kindToStep(kind: string | null | undefined): WorkflowStep | null {
  if (kind === "review") return "审查";
  if (kind === "fix") return "修复";
  if (kind === "verify" || kind === "test_generator" || kind === "generate_tests") return "验证";
  return null;
}

function payloadKind(payload: Record<string, unknown> | undefined): string | null {
  if (!payload) return null;
  return text(payload.task_kind) ?? text(payload.kind) ?? text(payload.taskKind);
}

/** Events that move the highlighted step. Hold events leave the previous step in place. */
function eventStep(event: ExecutionEvent): WorkflowStep | null {
  const kind = payloadKind(event.payload);
  switch (event.event_type) {
    case "task_created":
      return "规划";
    case "task_dispatched":
    case "attempt_started":
    case "result_received":
    case "retry_scheduled":
      return kindToStep(kind);
    case "patch_applied":
    case "patch_application_failed":
      return "打补丁";
    case "check_recorded":
      return "验证";
    case "task_completed":
    case "terminal_recorded":
      if (!event.task_id || event.task_id === event.root_task_id) return "结案";
      return kindToStep(kind);
    default:
      return null;
  }
}

function walkedStep(events: ExecutionEvent[]): { step: WorkflowStep; started: boolean } {
  let step: WorkflowStep = "规划";
  let started = false;
  const ordered = [...events].sort((a, b) => a.sequence - b.sequence);
  for (const event of ordered) {
    const next = eventStep(event);
    if (next) {
      step = next;
      started = true;
    } else if (event.event_type === "task_created") {
      started = true;
    }
  }
  return { step, started };
}

function runningStep(detail: TaskDetailResponse): WorkflowStep | null {
  const active = detail.tasks.filter(
    (task) => task.task_level !== "root" && (task.status === "running" || task.status === "queued"),
  );
  for (const preferred of ["验证", "修复", "审查"] as const) {
    for (const task of active) {
      if (kindToStep(task.task_kind) === preferred) return preferred;
    }
  }
  return null;
}

function latestBatchStep(events: ExecutionEvent[]): WorkflowStep | null {
  const ordered = [...events].sort((a, b) => b.sequence - a.sequence);
  for (const event of ordered) {
    if (event.event_type !== "task_dispatched" && event.event_type !== "attempt_started") continue;
    const step = kindToStep(payloadKind(event.payload));
    if (step) return step;
  }
  return null;
}

export function inferWorkflowStep(
  detail: TaskDetailResponse,
  events: ExecutionEvent[],
  patchInProgress = false,
): StepInference {
  const nodes = detail.current_nodes ?? [];
  const waiting = detail.status === "waiting_recovery" || nodes.some((node) => WAIT_NODES.has(node));
  if (TERMINAL.has(detail.status)) return { step: "结案", waiting };

  const walked = walkedStep(events);
  const fromTasks = runningStep(detail);
  const previous: WorkflowStep = fromTasks ?? (patchInProgress ? "打补丁" : walked.started ? walked.step : "规划");

  if (nodes.length === 0) return { step: previous, waiting };

  if (nodes.some((node) => node === "apply_patch")) return { step: "打补丁", waiting };
  if (nodes.some((node) => BATCH_NODES.has(node))) {
    return { step: fromTasks ?? latestBatchStep(events) ?? previous, waiting };
  }
  if (nodes.some((node) => PLAN_NODES.has(node))) return { step: "规划", waiting };
  if (nodes.every((node) => HOLD_NODES.has(node) || WAIT_NODES.has(node))) {
    return { step: walked.started || fromTasks ? previous : "规划", waiting };
  }
  return { step: previous, waiting };
}
