/** Display semantics: execution status and check verdict are shown separately. */

import type { CheckStatus, DetectionCategory, RootStatus, TaskStatus, AttemptStatus } from "./api/types";
import {
  actionLabel,
  attemptStatusLabels,
  checkMethodLabel,
  checkStatusLabels,
  detectionLabels,
  errorCategoryLabel,
  errorCodeLabel,
  eventTypeLabel,
  retryReasonLabels,
  roleLabel,
  rootStatusLabels,
  skipReasonLabels,
  taskKindLabel,
  taskStatusLabels,
} from "./i18n/zh";

export interface Token {
  label: string;
  className: string;
  hint?: string;
}

export function rootStatusToken(status: RootStatus): Token {
  switch (status) {
    case "queued":
      return { label: rootStatusLabels.queued, className: "badge queued" };
    case "running":
      return { label: rootStatusLabels.running, className: "badge running" };
    case "waiting_recovery":
      return { label: rootStatusLabels.waiting_recovery, className: "badge waiting" };
    case "completed":
      return { label: rootStatusLabels.completed, className: "badge ok" };
    case "partial":
      return { label: rootStatusLabels.partial, className: "badge partial" };
    case "failed":
      return { label: rootStatusLabels.failed, className: "badge failed" };
    case "interrupted":
      return { label: rootStatusLabels.interrupted, className: "badge interrupted" };
    default:
      return { label: rootStatusLabels[status] ?? status, className: "badge" };
  }
}

export function taskStatusToken(status: TaskStatus): Token {
  switch (status) {
    case "blocked":
      return { label: taskStatusLabels.blocked, className: "badge blocked" };
    case "queued":
      return { label: taskStatusLabels.queued, className: "badge queued" };
    case "running":
      return { label: taskStatusLabels.running, className: "badge running" };
    case "completed":
      return { label: taskStatusLabels.completed, className: "badge ok" };
    case "failed":
      return { label: taskStatusLabels.failed, className: "badge failed" };
    case "skipped":
      return { label: taskStatusLabels.skipped, className: "badge skipped" };
    case "cancelled":
      return { label: taskStatusLabels.cancelled, className: "badge cancelled" };
    default:
      return { label: taskStatusLabels[status] ?? status, className: "badge" };
  }
}

export function attemptStatusToken(status: AttemptStatus): Token {
  switch (status) {
    case "queued":
      return { label: attemptStatusLabels.queued, className: "badge queued" };
    case "running":
      return { label: attemptStatusLabels.running, className: "badge running" };
    case "completed":
      return { label: attemptStatusLabels.completed, className: "badge ok" };
    case "failed":
      return { label: attemptStatusLabels.failed, className: "badge failed" };
    case "interrupted":
      return { label: attemptStatusLabels.interrupted, className: "badge interrupted" };
    case "invalidated":
      return { label: attemptStatusLabels.invalidated, className: "badge invalidated" };
    default:
      return { label: attemptStatusLabels[status] ?? status, className: "badge" };
  }
}

/** The business verdict is NEVER conflated with the execution status. */
export function verdictToken(passed: boolean | null): Token {
  if (passed === true) return { label: "检查通过", className: "badge ok" };
  if (passed === false) return { label: "检查未通过", className: "badge failed" };
  return { label: "尚无结论", className: "badge null", hint: "证据不足，未判定" };
}

export function checkStatusToken(status: CheckStatus): Token {
  switch (status) {
    case "passed":
      return { label: checkStatusLabels.passed, className: "badge ok" };
    case "failed":
      return { label: checkStatusLabels.failed, className: "badge failed" };
    case "not_run":
      return { label: checkStatusLabels.not_run, className: "badge null" };
    default:
      return { label: checkStatusLabels.inconclusive, className: "badge null" };
  }
}

export function detectionToken(category: DetectionCategory | undefined): Token {
  switch (category) {
    case "pass":
      return { label: detectionLabels.pass, className: "badge ok" };
    case "code_defect":
      return { label: detectionLabels.code_defect, className: "badge failed" };
    case "insufficient_evidence":
      return { label: detectionLabels.insufficient_evidence, className: "badge null" };
    case "execution_fault":
      return { label: detectionLabels.execution_fault, className: "badge waiting" };
    case "invalid_result":
      return { label: detectionLabels.invalid_result, className: "badge failed" };
    case "no_progress":
      return { label: detectionLabels.no_progress, className: "badge waiting" };
    default:
      return { label: "未知", className: "badge" };
  }
}

export const RETRY_REASON_LABELS: Record<string, string> = retryReasonLabels;

export const AGENT_ROLE_LABELS: Record<string, string> = {
  parent: "父 Agent",
  reviewer: "审查 Agent",
  fixer: "修复 Agent",
  verifier: "验证 Agent",
  test_generator: "测试生成 Agent",
};

export const TASK_KIND_LABELS: Record<string, string> = {
  review: "审查",
  fix: "修复",
  verify: "验证",
};

export const SKIP_REASON_LABELS: Record<string, string> = skipReasonLabels;

export function budgetLabel(kind: string): string {
  const map: Record<string, string> = {
    repair_round: "自动修复轮数",
    review_retry: "审查故障重试",
    fix_retry: "修复故障重试",
    verification_retry: "验证故障重试",
    evidence_retry: "补证据重试",
  };
  return map[kind] ?? kind;
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const text = date.toLocaleString("zh-CN", { hour12: false, timeZone: "Asia/Shanghai" });
  return `${text}（UTC+8）`;
}

export function formatAge(updatedAt: number, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - updatedAt) / 1000));
  if (seconds < 5) return "刚刚";
  return `${seconds} 秒前`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(2)} MB`;
}

function payloadText(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  return typeof value === "string" ? value : "";
}

function summaryForEvent(eventType: string, payload: Record<string, unknown>): string | null {
  const reason = payloadText(payload, "reason");
  const message = payloadText(payload, "message");
  const summary = payloadText(payload, "summary");
  const kind = taskKindLabel(payloadText(payload, "task_kind") || payloadText(payload, "kind"));
  switch (eventType) {
    case "task_created":
      return payloadText(payload, "goal") ? `已建立任务：${payloadText(payload, "goal")}` : "已建立任务";
    case "task_dispatched":
      return kind ? `已派发${kind}` : "已派发子任务";
    case "attempt_started":
      return payload.attempt_no != null ? `开始第 ${String(payload.attempt_no)} 次尝试` : "开始尝试";
    case "tool_started":
      return payloadText(payload, "tool_name") ? `工具 ${payloadText(payload, "tool_name")} 开始` : "工具开始";
    case "tool_finished":
      return payloadText(payload, "tool_name") ? `工具 ${payloadText(payload, "tool_name")} 结束` : "工具结束";
    case "result_received":
      return summary || "已收到回报";
    case "detection_completed":
      return `检测完成：${detectionLabels[payloadText(payload, "category")] ?? "未知"}`;
    case "retry_scheduled":
      return `安排重试：${retryReasonLabels[payloadText(payload, "retry_reason")] ?? (reason || "再次执行")}`;
    case "task_skipped":
      return `跳过子任务：${skipReasonLabels[payloadText(payload, "skip_reason")] ?? (reason || "不再执行")}`;
    case "waiting_recovery":
      return reason || "进入待恢复";
    case "task_completed":
      return "任务完成";
    case "task_interrupted":
      return reason || "任务中断";
    case "task_resumed":
      return "任务已恢复";
    case "patch_applied":
      return payloadText(payload, "result_version")
        ? `补丁已应用到版本 ${payloadText(payload, "result_version")}`
        : "补丁已应用";
    case "patch_application_failed":
      return payloadText(payload, "error") || reason || "补丁应用失败";
    case "budget_granted":
      return `追加额度 ${payloadText(payload, "budget_kind") || ""}`.trim();
    case "budget_consumed":
      return `消耗额度 ${payloadText(payload, "budget_kind") || ""}`.trim();
    case "parent_decided":
      return payloadText(payload, "reasoning") || summary || `决策：${actionLabel(payloadText(payload, "action"))}`;
    case "action_rejected":
      return reason || message || "动作被拒绝";
    case "terminal_recorded":
      return `记录终态：${rootStatusLabels[payloadText(payload, "status")] ?? eventTypeLabel(eventType)}`;
    case "late_result_audit":
      return "迟到回报已审计";
    case "check_recorded":
      return `记录检查 ${payloadText(payload, "check_id") || ""}：${checkMethodLabel(payloadText(payload, "method"))} ${checkStatusLabels[payloadText(payload, "status")] ?? ""}`.trim();
    default:
      return null;
  }
}

function preferredSummary(payload: Record<string, unknown>): string {
  const preferred = ["reason", "message", "summary", "suggestion", "category", "code", "action", "retry_reason"] as const;
  for (const key of preferred) {
    const value = payload[key];
    if (typeof value !== "string" || !value) continue;
    if (key === "category") return detectionLabels[value] ?? value;
    if (key === "code") return errorCodeLabel(value);
    if (key === "action") return actionLabel(value);
    if (key === "retry_reason") return retryReasonLabels[value] ?? value;
    return value;
  }
  return "详见开发者视图";
}

/** Chinese one-line summary. Raw JSON stays in the collapsed developer view. */
export function summarizePayload(payload: Record<string, unknown>, eventType?: string): string {
  if (!payload || Object.keys(payload).length === 0) return "";
  if (eventType) {
    const templated = summaryForEvent(eventType, payload);
    if (templated) return templated;
  }
  return preferredSummary(payload);
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "";
  if (id.length <= 8) return id;
  return `${id.slice(0, 1)}…${id.slice(-3)}`;
}

export { errorCategoryLabel, errorCodeLabel, roleLabel };
