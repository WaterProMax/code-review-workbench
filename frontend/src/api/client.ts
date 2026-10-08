/** Thin fetch wrapper around the backend API with unified error handling. */

import type {
  ArtifactView,
  AttemptView,
  CapabilityListing,
  EventsResponse,
  Finding,
  HealthResponse,
  ReportResponse,
  ResumeRequest,
  ResumeResponse,
  SourceUploadResponse,
  SubmitTaskResponse,
  TaskDetailResponse,
  TaskListItem,
  TemplateListing,
  TerminateRequest,
  TerminateResponse,
  WorkflowConfig,
  WorkflowListItem,
  WorkflowSaveResponse,
} from "./types";

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Record<string, unknown>;
  request_id?: string | null;
}

export class ApiError extends Error {
  code: string;
  details: Record<string, unknown>;
  status: number;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.status = status;
    this.code = body.code;
    this.details = body.details ?? {};
  }
}

const BASE = "/api";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const body: ApiErrorBody =
      data && typeof data === "object" && "code" in data
        ? (data as ApiErrorBody)
        : { code: "HTTP_ERROR", message: `HTTP ${res.status}` };
    throw new ApiError(res.status, body);
  }
  return data as T;
}

export function newIdempotencyKey(): string {
  const globalCrypto = globalThis.crypto;
  if (globalCrypto && typeof globalCrypto.randomUUID === "function") {
    return globalCrypto.randomUUID();
  }
  return `key-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export function uploadSources(files: File[]): Promise<SourceUploadResponse> {
  const form = new FormData();
  for (const file of files) {
    form.append("files", file, file.name);
  }
  return request<SourceUploadResponse>("/sources", { method: "POST", body: form });
}

export function listAgents(): Promise<CapabilityListing[]> {
  return request<CapabilityListing[]>("/agents");
}

export function listWorkflows(): Promise<WorkflowListItem[]> {
  return request<WorkflowListItem[]>("/workflows");
}

export function listTemplates(): Promise<TemplateListing[]> {
  return request<TemplateListing[]>("/workflows/templates");
}

export function getWorkflow(workflowVersion: string): Promise<WorkflowConfig> {
  return request<WorkflowConfig>(`/workflows/${encodeURIComponent(workflowVersion)}`);
}

export function saveWorkflow(config: WorkflowConfig): Promise<WorkflowSaveResponse> {
  return request<WorkflowSaveResponse>("/workflows", {
    method: "POST",
    body: JSON.stringify(config),
  });
}

export function submitTask(input: {
  source_id: string;
  goal: string;
  workflow_version: string;
  check_mode?: string;
}): Promise<SubmitTaskResponse> {
  return request<SubmitTaskResponse>("/tasks", {
    method: "POST",
    body: JSON.stringify(input),
    headers: { "Idempotency-Key": newIdempotencyKey() },
  });
}

export function listTasks(limit = 50, offset = 0): Promise<TaskListItem[]> {
  return request<TaskListItem[]>(`/tasks?limit=${limit}&offset=${offset}`);
}

export function getTask(rootTaskId: string): Promise<TaskDetailResponse> {
  return request<TaskDetailResponse>(`/tasks/${encodeURIComponent(rootTaskId)}`);
}

export function listAttempts(rootTaskId: string, taskId?: string): Promise<AttemptView[]> {
  const suffix = taskId ? `?task_id=${encodeURIComponent(taskId)}` : "";
  return request<AttemptView[]>(`/tasks/${encodeURIComponent(rootTaskId)}/attempts${suffix}`);
}

export function listEvents(rootTaskId: string, afterSeq = 0, limit = 200): Promise<EventsResponse> {
  return request<EventsResponse>(
    `/tasks/${encodeURIComponent(rootTaskId)}/events?after_seq=${afterSeq}&limit=${limit}`,
  );
}

export function listFindings(rootTaskId: string, status?: string): Promise<Finding[]> {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : "";
  return request<Finding[]>(`/tasks/${encodeURIComponent(rootTaskId)}/findings${suffix}`);
}

export function listArtifacts(rootTaskId: string, artifactType?: string): Promise<ArtifactView[]> {
  const suffix = artifactType ? `?artifact_type=${encodeURIComponent(artifactType)}` : "";
  return request<ArtifactView[]>(
    `/tasks/${encodeURIComponent(rootTaskId)}/artifacts${suffix}`,
  );
}

export function getArtifact<T>(artifactId: string): Promise<T> {
  return request<T>(`/artifacts/${encodeURIComponent(artifactId)}`);
}

export function getReport(rootTaskId: string): Promise<ReportResponse> {
  return request<ReportResponse>(`/tasks/${encodeURIComponent(rootTaskId)}/report`);
}

export function resumeTask(rootTaskId: string, body: ResumeRequest): Promise<ResumeResponse> {
  return request<ResumeResponse>(`/tasks/${encodeURIComponent(rootTaskId)}/resume`, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": newIdempotencyKey() },
  });
}

export function terminateTask(rootTaskId: string, body: TerminateRequest): Promise<TerminateResponse> {
  return request<TerminateResponse>(`/tasks/${encodeURIComponent(rootTaskId)}/terminate`, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": newIdempotencyKey() },
  });
}

export const api = { request };
