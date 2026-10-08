/** Types mirroring the backend response schemas (backend/app/schemas). */

export type CheckMode = "sequential" | "parallel";

export type RootStatus =
  | "queued"
  | "running"
  | "waiting_recovery"
  | "completed"
  | "partial"
  | "failed"
  | "interrupted";

export type TaskStatus = "blocked" | "queued" | "running" | "completed" | "failed" | "skipped";

export type AttemptStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "interrupted"
  | "invalidated";

export type ResultStatus = "completed" | "failed";

export type CheckStatus = "passed" | "failed" | "not_run" | "inconclusive";

export type DetectionCategory =
  | "pass"
  | "code_defect"
  | "insufficient_evidence"
  | "execution_fault"
  | "invalid_result"
  | "no_progress";

export interface HealthResponse {
  status: string;
  version: string;
  model_configured: boolean;
  model_provider: string;
  model_name: string;
  data_dir: string;
}

export interface SourceFileView {
  path: string;
  size: number;
  sha256: string;
}

export interface SourceUploadResponse {
  source_id: string;
  files: SourceFileView[];
  total_bytes: number;
  content_digest: string;
  created_at: string;
  artifact_id?: string | null;
}

export interface WorkflowListItem {
  workflow_version: string;
  semantic_hash: string;
  check_mode: CheckMode;
  created_at: string;
}

/** Orchestration config (§14.1.1): the canvas edits exactly these fields. */
export type WorkflowNodeType = "parent" | "agent" | "tool" | "end";

export interface WorkflowNode {
  id: string;
  type: WorkflowNodeType;
  agent_id?: string | null;
  task_kind?: string | null;
  tool_name?: string | null;
}

export interface WorkflowEdge {
  source: string;
  target: string;
}

export interface InputDependency {
  source_node: string;
  target_node: string;
  artifact: string;
}

export interface BudgetConfig {
  max_repair_rounds: number;
  max_verification_retries: number;
  max_review_retries: number;
  max_fix_retries: number;
  max_evidence_retries: number;
  max_generate_retries: number;
  max_parent_corrections: number;
  max_model_retries: number;
  max_tool_steps: number;
  model_timeout_seconds: number;
  tool_timeout_seconds: number;
  attempt_timeout_seconds: number;
  max_graph_steps: number;
}

export interface WorkflowLayoutItem {
  node_id: string;
  x: number;
  y: number;
}

export interface WorkflowConfig {
  schema_version: string;
  check_mode: CheckMode;
  agents: Record<string, string>;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  dependencies: InputDependency[];
  budgets: BudgetConfig;
  layout: WorkflowLayoutItem[];
  workflow_version?: string | null;
  semantic_hash?: string | null;
  tool_policy_versions?: Record<string, string>;
}

export interface WorkflowSaveResponse {
  workflow_version: string;
  semantic_hash: string;
  check_mode: CheckMode;
  config: WorkflowConfig;
}

export interface TemplateListing {
  name: string;
  description: string;
  check_mode: CheckMode;
  config: WorkflowConfig;
}

export interface CapabilityListing {
  agent_id: string;
  version: string;
  description: string;
  supported_task_kinds: string[];
  tool_names: string[];
  required_input_keys: string[];
  produced_artifact_types: string[];
  retry_budget: Record<string, string>;
}

export interface SubmitTaskResponse {
  root_task_id: string;
  status: RootStatus;
  revision: number;
  workflow_version: string;
  contract_version: string | null;
  created_at: string;
}

export interface TaskListItem {
  root_task_id: string;
  goal: string;
  status: RootStatus;
  passed: boolean | null;
  workflow_version: string;
  check_mode: CheckMode;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface TaskDep {
  task_id: string;
  attempt_id?: string | null;
  condition: string;
}

export interface Task {
  task_id: string;
  root_task_id: string;
  parent_task_id: string | null;
  task_level: "root" | "child";
  agent_id: string | null;
  agent_version: string | null;
  task_kind: string | null;
  goal: string;
  depends_on: TaskDep[];
  status: TaskStatus;
  passed: boolean | null;
  skip_reason: string | null;
  workflow_version: string;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface CheckResult {
  check_id: string;
  status: CheckStatus;
  reason?: string | null;
  evidence_refs?: string[];
  test_count?: number | null;
  passed_count?: number | null;
  failed_count?: number | null;
  skipped_count?: number | null;
  executed?: boolean;
}

export interface CheckSummary {
  check_id: string;
  goal_ref: string;
  method: string;
  required: boolean;
  status: CheckStatus;
  latest_evidence_ref: string | null;
  source_version: string | null;
  note: string | null;
}

export interface ProgressInfo {
  patch_fingerprint?: string | null;
  failure_signature?: string | null;
}

export interface DetectionResult {
  valid: boolean;
  category: DetectionCategory;
  facts: Record<string, unknown>;
  explanation: string | null;
  missing_items: string[];
  failed_check_ids: string[];
  related_finding_ids: string[];
  invalid_reason: string | null;
  progress: ProgressInfo;
  suggestion: string | null;
}

export interface BudgetItem {
  budget_kind: string;
  consumed: number;
  granted_max: number;
  remaining: number;
}

export interface BudgetStatus {
  items: BudgetItem[];
}

export interface ErrorInfo {
  code: string;
  category: string;
  message: string;
  recoverable: boolean;
}

export interface TaskDetailResponse {
  root_task_id: string;
  goal: string;
  status: RootStatus;
  passed: boolean | null;
  revision: number;
  workflow_version: string;
  check_mode: CheckMode;
  contract_version: string | null;
  source_version: string | null;
  tasks: Task[];
  detection: DetectionResult | null;
  next_action: string | null;
  required_action: string | null;
  error: ErrorInfo | null;
  report_ref: string | null;
  report_artifact_id: string | null;
  checks: CheckSummary[];
  budgets: BudgetStatus;
  artifact_refs: string[];
  created_at: string;
  updated_at: string;
}

export interface TaskResultView {
  result_id: string;
  task_id: string;
  attempt_id: string;
  status: ResultStatus;
  passed: boolean | null;
  summary: string;
  result_refs: Record<string, string>;
  evidence_refs: string[];
  error?: ErrorInfo | null;
  started_at: string;
  finished_at: string;
}

export interface AttemptView {
  attempt_id: string;
  task_id: string;
  root_task_id: string;
  task_kind: string | null;
  agent_id: string | null;
  agent_version: string | null;
  attempt_no: number;
  dispatch_batch_id: string | null;
  status: AttemptStatus;
  retry_reason: string | null;
  source_version: string;
  contract_version: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  input_refs: Record<string, string>;
  result: TaskResultView | null;
}

export interface ExecutionEvent {
  event_id: string;
  root_task_id: string;
  task_id: string | null;
  attempt_id: string | null;
  dispatch_batch_id: string | null;
  source_version: string | null;
  sequence: number;
  timestamp: string;
  actor_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  artifact_refs: string[];
  duration_ms: number | null;
}

export interface EventsResponse {
  root_task_id: string;
  events: ExecutionEvent[];
  next_seq: number;
  status: RootStatus;
  passed: boolean | null;
  revision: number;
}

export type ArtifactType =
  | "upload_manifest"
  | "source_snapshot"
  | "acceptance_contract"
  | "finding"
  | "patch"
  | "patch_application"
  | "verification_report"
  | "check_result"
  | "final_report"
  | "report_markdown"
  | "test_artifact"
  | "detection"
  | "evidence"
  | "diff"
  | "model_output";

export interface ArtifactView {
  artifact_id: string;
  root_task_id: string;
  producer_attempt_id: string | null;
  artifact_type: ArtifactType;
  source_version: string | null;
  hash: string;
  size: number;
  metadata: Record<string, unknown>;
  created_at: string;
  download_url: string;
}

export interface ReportResponse {
  root_task_id: string;
  final_status: RootStatus;
  passed: boolean | null;
  conclusion_scope: string;
  report: Record<string, unknown>;
  markdown: string | null;
  report_artifact_id: string | null;
  artifact_refs: string[];
}

export interface ResumeRequest {
  expected_revision: number;
  additional_repair_rounds?: number;
  additional_execution_retries?: Record<string, number>;
  additional_evidence_retries?: number;
  reason: string;
}

export interface ResumeResponse {
  root_task_id: string;
  status: RootStatus;
  revision: number;
  run_segment_id: string;
  granted: BudgetItem[];
  message: string;
}

export interface TerminateRequest {
  expected_revision: number;
  reason: string;
}

export interface TerminateResponse {
  root_task_id: string;
  status: RootStatus;
  passed: boolean | null;
  revision: number;
  reason: string;
  skipped_tasks: string[];
}

/** Artifact payload shapes the workbench renders. */

export interface Finding {
  finding_id: string;
  root_task_id: string;
  source_version: string;
  producer_attempt_id: string;
  goal_ref: string | null;
  required_for_goal: boolean;
  check_id: string | null;
  file_path: string;
  line: number | null;
  symbol: string | null;
  rule: string;
  severity: string;
  message: string;
  evidence_refs: string[];
  status: string;
  resolution_evidence_refs: string[];
}

export interface FindingsArtifact {
  source_version: string;
  findings: Finding[];
  coverage: string[];
  not_checked: string[];
}

export interface PatchArtifact {
  patch_id: string;
  root_task_id: string;
  producer_attempt_id: string;
  base_version: string;
  target_finding_ids: string[];
  format: string;
  diff_text: string | null;
  edits: Array<{ file_path: string; find: string; replace: string; occurrence: number }>;
  rationale?: string | null;
}

export interface PatchApplicationArtifact {
  application_id: string;
  root_task_id: string;
  patch_id: string;
  repair_attempt_id: string;
  base_version: string;
  result_version: string | null;
  status: string;
  error: string | null;
  prepared_at: string;
  committed_at: string | null;
}

export interface VerificationArtifact {
  verification_id: string;
  producer_attempt_id: string;
  source_version: string;
  target_finding_ids: string[];
  check_results: CheckResult[];
  coverage: string[];
  not_run: string[];
  passed: boolean | null;
  evidence_refs: string[];
  notes: string | null;
}

export interface FinalReportCheck {
  check_id: string;
  goal_ref: string;
  method: string;
  required: boolean;
  status: CheckStatus;
  latest_evidence_ref: string | null;
  source_version: string | null;
  note: string | null;
}

export interface FinalReportArtifact {
  report_id: string;
  root_task_id: string;
  final_status: string;
  passed: boolean | null;
  goal: string;
  conclusion_scope: string;
  source_version: string;
  workflow_version: string;
  contract_version: string;
  checks: FinalReportCheck[];
  unresolved_finding_ids: string[];
  not_run_items: string[];
  skipped_tasks: Array<{ task_id: string; skip_reason: string; detail: string }>;
  versions: Record<string, string>;
  execution_summary: Record<string, unknown>;
}
