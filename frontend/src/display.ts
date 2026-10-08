/** Display semantics: execution status and check verdict are shown separately. */

import type { CheckStatus, DetectionCategory, RootStatus, TaskStatus, AttemptStatus } from "./api/types";

export interface Token {
  label: string;
  className: string;
  hint?: string;
}

export function rootStatusToken(status: RootStatus): Token {
  switch (status) {
    case "queued":
      return { label: "已排队", className: "badge queued" };
    case "running":
      return { label: "执行中", className: "badge running" };
    case "waiting_recovery":
      return { label: "待恢复", className: "badge waiting" };
    case "completed":
      return { label: "已完成", className: "badge ok" };
    case "partial":
      return { label: "部分完成", className: "badge partial" };
    case "failed":
      return { label: "失败", className: "badge failed" };
    case "interrupted":
      return { label: "已中断", className: "badge interrupted" };
    default:
      return { label: status, className: "badge" };
  }
}

export function taskStatusToken(status: TaskStatus): Token {
  switch (status) {
    case "blocked":
      return { label: "未派发", className: "badge blocked" };
    case "queued":
      return { label: "已派发", className: "badge queued" };
    case "running":
      return { label: "执行中", className: "badge running" };
    case "completed":
      return { label: "执行完成", className: "badge ok" };
    case "failed":
      return { label: "执行失败", className: "badge failed" };
    case "skipped":
      return { label: "已跳过", className: "badge skipped" };
    default:
      return { label: status, className: "badge" };
  }
}

export function attemptStatusToken(status: AttemptStatus): Token {
  switch (status) {
    case "queued":
      return { label: "已登记", className: "badge queued" };
    case "running":
      return { label: "执行中", className: "badge running" };
    case "completed":
      return { label: "执行完成", className: "badge ok" };
    case "failed":
      return { label: "执行失败", className: "badge failed" };
    case "interrupted":
      return { label: "被中断", className: "badge interrupted" };
    case "invalidated":
      return { label: "已撤销", className: "badge invalidated" };
    default:
      return { label: status, className: "badge" };
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
      return { label: "通过", className: "badge ok" };
    case "failed":
      return { label: "未通过", className: "badge failed" };
    case "not_run":
      return { label: "未执行", className: "badge null" };
    default:
      return { label: "无法判定", className: "badge null" };
  }
}

export function detectionToken(category: DetectionCategory | undefined): Token {
  switch (category) {
    case "pass":
      return { label: "满足完成条件", className: "badge ok" };
    case "code_defect":
      return { label: "存在代码缺陷", className: "badge failed" };
    case "insufficient_evidence":
      return { label: "证据不足", className: "badge null" };
    case "execution_fault":
      return { label: "执行故障", className: "badge waiting" };
    case "invalid_result":
      return { label: "回报无效", className: "badge failed" };
    case "no_progress":
      return { label: "重试无进展", className: "badge waiting" };
    default:
      return { label: "未知", className: "badge" };
  }
}

export const RETRY_REASON_LABELS: Record<string, string> = {
  initial: "首次派发",
  business_repair: "业务修复重派",
  execution_fault: "执行故障重派",
  insufficient_evidence: "补充证据",
};

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

export const SKIP_REASON_LABELS: Record<string, string> = {
  first_review_passed: "首次审查通过，后续未执行",
  not_required: "任务结束时不再需要",
};

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
  return date.toLocaleString("zh-CN", { hour12: false });
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

export function summarizePayload(payload: Record<string, unknown>): string {
  if (!payload || Object.keys(payload).length === 0) return "";
  const preferred = ["reason", "message", "summary", "suggestion", "category", "code", "action", "retry_reason"];
  for (const key of preferred) {
    const value = payload[key];
    if (typeof value === "string" && value) return value;
  }
  const json = JSON.stringify(payload);
  return json.length > 160 ? `${json.slice(0, 160)}…` : json;
}
