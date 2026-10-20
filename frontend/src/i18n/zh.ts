/** Chinese labels for roles, artifacts, checks, statuses, and error codes. */

export const roleLabels: Record<string, string> = {
  parent: "父 Agent",
  reviewer: "审查",
  fixer: "修复",
  verifier: "验证",
  test_generator: "测试生成",
  controller: "控制器",
  tool: "工具",
  task_service: "任务服务",
  system: "系统",
};

export const artifactTypeLabels: Record<string, string> = {
  upload_manifest: "上传清单",
  source_snapshot: "源码快照",
  acceptance_contract: "验收合同",
  finding: "问题清单",
  patch: "补丁",
  patch_application: "补丁应用记录",
  verification_report: "验证报告",
  check_result: "检查结果",
  final_report: "最终报告",
  report_markdown: "审查报告",
  test_artifact: "测试产物",
  detection: "检测记录",
  evidence: "审计证据",
  diff: "差异",
  model_output: "模型输出",
};

export const checkMethodLabels: Record<string, string> = {
  syntax: "语法检查",
  static_rule: "静态规则",
  behavior_test: "行为测试",
  model_review: "模型审查",
};

export const rootStatusLabels: Record<string, string> = {
  queued: "已排队",
  running: "执行中",
  waiting_recovery: "待恢复",
  completed: "已完成",
  partial: "部分完成",
  failed: "失败",
  interrupted: "已中断",
};

export const taskStatusLabels: Record<string, string> = {
  blocked: "未派发",
  queued: "已派发",
  running: "执行中",
  completed: "执行完成",
  failed: "执行失败",
  skipped: "已跳过",
  cancelled: "已取消",
};

export const attemptStatusLabels: Record<string, string> = {
  queued: "已登记",
  running: "执行中",
  completed: "执行完成",
  failed: "执行失败",
  interrupted: "被中断",
  invalidated: "已撤销",
};

export const checkStatusLabels: Record<string, string> = {
  passed: "通过",
  failed: "未通过",
  not_run: "未执行",
  inconclusive: "无法判定",
};

export const detectionLabels: Record<string, string> = {
  pass: "满足完成条件",
  code_defect: "存在代码缺陷",
  insufficient_evidence: "证据不足",
  execution_fault: "执行故障",
  invalid_result: "回报无效",
  no_progress: "重试无进展",
};

export const errorCodeLabels: Record<string, string> = {
  VALIDATION_ERROR: "校验失败",
  NOT_FOUND: "未找到",
  VERSION_CONFLICT: "版本冲突",
  CONFIG_MODE_CONFLICT: "配置模式冲突",
  CONFIG_DEFAULT_FROZEN: "默认配置已冻结",
  IDEMPOTENCY_CONFLICT: "幂等冲突",
  BUDGET_EXHAUSTED: "额度已用尽",
  CONFLICT: "状态冲突",
  NOT_RECOVERABLE: "不可恢复",
  NOT_RESUMABLE: "不可恢复执行",
  NOT_TERMINABLE: "不可终止",
  MODEL_NOT_CONFIGURED: "模型未配置",
  MODEL_REQUEST_FAILED: "模型请求失败",
  TEMPORARY_FAILURE: "暂时失败",
  INTERNAL_ERROR: "内部错误",
  TIMEOUT: "请求超时",
  HTTP_ERROR: "请求失败",
};

export const eventTypeLabels: Record<string, string> = {
  task_created: "建立任务",
  task_dispatched: "派发子任务",
  attempt_started: "开始尝试",
  tool_started: "工具开始",
  tool_finished: "工具结束",
  result_received: "收到回报",
  detection_completed: "检测完成",
  retry_scheduled: "安排重试",
  task_skipped: "跳过子任务",
  waiting_recovery: "进入待恢复",
  task_completed: "任务完成",
  task_interrupted: "任务中断",
  task_resumed: "任务恢复",
  patch_applied: "补丁已应用",
  patch_application_failed: "补丁应用失败",
  budget_granted: "追加额度",
  budget_consumed: "消耗额度",
  parent_decided: "父 Agent 决策",
  action_rejected: "动作被拒",
  terminal_recorded: "记录终态",
  late_result_audit: "迟到回报审计",
  check_recorded: "记录检查结果",
};

export const actionLabels: Record<string, string> = {
  dispatch_task: "派发子任务",
  dispatch_batch: "派发批次",
  apply_patch: "应用补丁",
  wait_for_recovery: "等待恢复",
  finish: "结束",
};

export const retryReasonLabels: Record<string, string> = {
  initial: "首次派发",
  business_repair: "业务修复重派",
  execution_fault: "执行故障重派",
  insufficient_evidence: "补充证据",
  new_version_reverify: "新版本重验",
  post_patch_first_check: "补丁后首次检查",
};

export const taskKindLabels: Record<string, string> = {
  review: "审查",
  fix: "修复",
  verify: "验证",
  generate_tests: "生成测试",
  test_generator: "测试生成",
};

export const skipReasonLabels: Record<string, string> = {
  first_review_passed: "首次审查通过，后续未执行",
  not_required: "任务结束时不再需要",
  prior_failure: "前序失败后跳过",
  terminated: "任务终止后跳过",
};

export const checkModeLabels: Record<string, string> = {
  sequential: "顺序",
  parallel: "并行",
};

export const errorCategoryLabels: Record<string, string> = {
  model_error: "模型错误",
  model_timeout: "模型超时",
  tool_error: "工具错误",
  tool_timeout: "工具超时",
  missing_dependency: "缺少依赖",
  invalid_patch: "补丁无效",
  patch_conflict: "补丁冲突",
  attempt_timeout: "尝试超时",
  cancelled: "已取消",
  interrupted: "已中断",
  cycle_limit: "循环上限",
  graph_step_limit: "图步数上限",
  invalid_action: "动作无效",
  invalid_result: "回报无效",
  unknown: "未知错误",
};

export function labelOf(map: Record<string, string>, value: string | null | undefined, fallback = "—"): string {
  if (!value) return fallback;
  return map[value] ?? value;
}

export function roleLabel(role: string | null | undefined): string {
  return labelOf(roleLabels, role);
}

export function artifactTypeLabel(type: string | null | undefined): string {
  return labelOf(artifactTypeLabels, type);
}

export function checkMethodLabel(method: string | null | undefined): string {
  return labelOf(checkMethodLabels, method);
}

export function errorCodeLabel(code: string | null | undefined): string {
  return labelOf(errorCodeLabels, code);
}

export function eventTypeLabel(eventType: string | null | undefined): string {
  return labelOf(eventTypeLabels, eventType);
}

export function actionLabel(action: string | null | undefined): string {
  return labelOf(actionLabels, action);
}

export function taskKindLabel(kind: string | null | undefined): string {
  return labelOf(taskKindLabels, kind, "");
}

export function checkModeLabel(mode: string | null | undefined): string {
  return labelOf(checkModeLabels, mode);
}

export function errorCategoryLabel(category: string | null | undefined): string {
  return labelOf(errorCategoryLabels, category);
}
