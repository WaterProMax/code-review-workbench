import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import type { ExecutionEvent, TaskDetailResponse } from "../src/api/types";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface Route {
  method: string;
  match: (url: string) => boolean;
  respond: (init?: RequestInit) => Response;
}

let routes: Route[] = [];
let calls: Array<{ url: string; method: string; headers: Headers; body: string | null }> = [];

function on(method: string, match: RegExp | string, respond: (init?: RequestInit) => Response) {
  routes.push({
    method,
    match: (url) => (typeof match === "string" ? url.startsWith(match) : match.test(url)),
    respond,
  });
}

beforeEach(() => {
  routes = [];
  calls = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init.method ?? "GET").toUpperCase();
    calls.push({
      url,
      method,
      headers: new Headers(init.headers),
      body: typeof init.body === "string" ? init.body : null,
    });
    const route = routes.find((item) => item.method === method && item.match(url));
    if (!route) return Promise.resolve(json({ code: "NOT_FOUND", message: `no route ${method} ${url}` }, 404));
    return Promise.resolve(route.respond(init));
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function health(modelConfigured = true) {
  on("GET", "/api/health", () =>
    json({
      status: "ok",
      version: "0.1.0",
      model_configured: modelConfigured,
      model_provider: "deepseek",
      model_name: "deepseek-chat",
      data_dir: "/tmp/data",
    }),
  );
}

function event(sequence: number, eventType: string, actor = "parent", payload = {}): ExecutionEvent {
  return {
    event_id: `ev-${sequence}`,
    root_task_id: "T-1",
    task_id: null,
    attempt_id: null,
    dispatch_batch_id: null,
    source_version: null,
    sequence,
    timestamp: "2026-10-08T02:00:00Z",
    actor_id: actor,
    event_type: eventType,
    payload,
    artifact_refs: [],
    duration_ms: null,
  };
}

function detail(overrides: Partial<TaskDetailResponse> = {}): TaskDetailResponse {
  return {
    root_task_id: "T-1",
    goal: "检查可变默认参数",
    status: "completed",
    passed: true,
    revision: 12,
    workflow_version: "wf-1",
    check_mode: "sequential",
    contract_version: "contract-1",
    source_version: "sv-base",
    tasks: [
      {
        task_id: "T-1-C001",
        root_task_id: "T-1",
        parent_task_id: "T-1",
        task_level: "child",
        agent_id: "reviewer",
        agent_version: "1.0",
        task_kind: "review",
        goal: "审查",
        depends_on: [],
        status: "completed",
        passed: true,
        skip_reason: null,
        workflow_version: "wf-1",
        revision: 3,
        created_at: "2026-10-08T02:00:00Z",
        updated_at: "2026-10-08T02:00:01Z",
      },
    ],
    detection: {
      valid: true,
      category: "pass",
      facts: {},
      explanation: null,
      missing_items: [],
      failed_check_ids: [],
      related_finding_ids: [],
      invalid_reason: null,
      progress: {},
      suggestion: null,
    },
    next_action: null,
    required_action: null,
    error: null,
    report_ref: "art-report",
    report_artifact_id: "art-report",
    checks: [
      {
        check_id: "CHK-MUTABLE",
        goal_ref: "避免共享可变默认参数",
        method: "static_rule",
        required: true,
        status: "passed",
        latest_evidence_ref: "art-check",
        source_version: "sv-base",
        note: "无共享可变默认参数",
      },
    ],
    budgets: {
      items: [{ budget_kind: "repair_round", consumed: 0, granted_max: 2, remaining: 2 }],
    },
    artifact_refs: [],
    created_at: "2026-10-08T02:00:00Z",
    updated_at: "2026-10-08T02:00:02Z",
    ...overrides,
  };
}

function serveTask(task: TaskDetailResponse, options: {
  attempts?: unknown[];
  artifacts?: unknown[];
  events?: ExecutionEvent[];
  report?: unknown;
  findings?: unknown[];
  artifactBodies?: Record<string, unknown>;
} = {}) {
  const artifacts = options.artifacts ?? [];
  on("GET", /\/api\/tasks\/T-1\/attempts/, () => json(options.attempts ?? []));
  on("GET", /\/api\/tasks\/T-1\/artifacts/, () => json(artifacts));
  on("GET", /\/api\/tasks\/T-1\/findings/, () => json(options.findings ?? []));
  on("GET", /\/api\/tasks\/T-1\/events/, () =>
    json({
      root_task_id: "T-1",
      events: options.events ?? [],
      next_seq: (options.events ?? []).length + 1,
      status: task.status,
      passed: task.passed,
      revision: task.revision,
    }),
  );
  on("GET", /\/api\/tasks\/T-1\/report/, () =>
    options.report ? json(options.report) : json({ code: "NOT_FOUND", message: "no report" }, 404),
  );
  on("GET", /\/api\/tasks\/T-1$/, () => json(task));
  for (const [id, body] of Object.entries(options.artifactBodies ?? {})) {
    on("GET", `/api/artifacts/${id}`, () => json(body));
  }
}

describe("workbench", () => {
  it("shows a first-review pass with fix/verify skipped and no verification report", async () => {
    health();
    const task = detail({
      tasks: [
        ...detail().tasks,
        { ...detail().tasks[0], task_id: "T-1-C002", agent_id: "fixer", task_kind: "fix", status: "skipped", passed: null, skip_reason: "first_review_passed" },
        { ...detail().tasks[0], task_id: "T-1-C003", agent_id: "verifier", task_kind: "verify", status: "skipped", passed: null, skip_reason: "first_review_passed" },
      ],
    });
    serveTask(task, {
      events: [event(1, "task_created"), event(2, "parent_decided", "parent", { action: "finish", reasoning: "必需检查全部通过" })],
      report: {
        root_task_id: "T-1",
        final_status: "completed",
        passed: true,
        conclusion_scope: "首次审查完整通过，修复与验证未执行",
        report: { checks: [], versions: { source_version: "sv-base" } },
        markdown: null,
        report_artifact_id: "art-report",
        artifact_refs: [],
      },
    });

    render(
      <MemoryRouter initialEntries={["/tasks/T-1"]}>
        <App />
      </MemoryRouter>,
    );

    await screen.findByText("任务总览");
    expect(await screen.findByText("已完成")).toBeInTheDocument();
    expect(screen.getAllByText("检查通过").length).toBeGreaterThan(0);
    // both skipped children carry the same reason
    expect(screen.getAllByText("首次审查通过，后续未执行").length).toBeGreaterThanOrEqual(2);
    // execution status and check verdict are separate facts
    expect(screen.getByText("必需检查全部通过")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("首次审查完整通过，修复与验证未执行")).toBeInTheDocument());
    expect(screen.getByText("还没有验证报告（首次审查通过时修复与验证会被跳过）。")).toBeInTheDocument();
  });

  it("shows a repaired-and-verified task with diff, application and verification evidence", async () => {
    health();
    const artifacts = [
      { artifact_id: "art-patch", root_task_id: "T-1", producer_attempt_id: "T-1-C002-A1", artifact_type: "patch", source_version: "sv-base", hash: "h", size: 10, metadata: {}, created_at: "2026-10-08T02:00:00Z", download_url: "/api/artifacts/art-patch" },
      { artifact_id: "art-app", root_task_id: "T-1", producer_attempt_id: null, artifact_type: "patch_application", source_version: "sv-fixed", hash: "h", size: 10, metadata: {}, created_at: "2026-10-08T02:00:00Z", download_url: "/api/artifacts/art-app" },
      { artifact_id: "art-ver", root_task_id: "T-1", producer_attempt_id: "T-1-C003-A1", artifact_type: "verification_report", source_version: "sv-fixed", hash: "h", size: 10, metadata: {}, created_at: "2026-10-08T02:00:00Z", download_url: "/api/artifacts/art-ver" },
      { artifact_id: "art-findings", root_task_id: "T-1", producer_attempt_id: "T-1-C001-A1", artifact_type: "finding", source_version: "sv-base", hash: "h", size: 10, metadata: {}, created_at: "2026-10-08T02:00:00Z", download_url: "/api/artifacts/art-findings" },
    ];
    serveTask(detail({ source_version: "sv-fixed", tasks: [
      { ...detail().tasks[0], status: "completed", passed: false },
      { ...detail().tasks[0], task_id: "T-1-C002", agent_id: "fixer", task_kind: "fix", status: "completed", passed: null },
      { ...detail().tasks[0], task_id: "T-1-C003", agent_id: "verifier", task_kind: "verify", status: "completed", passed: true },
    ] }), {
      artifacts,
      events: [event(1, "task_created"), event(2, "patch_applied", "tool", { result_version: "sv-fixed" }), event(3, "result_received", "controller", { summary: "验证完成" })],
      report: {
        root_task_id: "T-1",
        final_status: "completed",
        passed: true,
        conclusion_scope: "修改后的必需检查已在当前版本重新执行并通过",
        report: { checks: [], versions: { source_version: "sv-fixed" }, unresolved_finding_ids: [], not_run_items: [], skipped_tasks: [] },
        markdown: null,
        report_artifact_id: "art-report",
        artifact_refs: [],
      },
      findings: [
        { finding_id: "F-abc", root_task_id: "T-1", source_version: "sv-base", producer_attempt_id: "T-1-C001-A1", goal_ref: "避免共享可变默认参数", required_for_goal: true, check_id: "CHK-MUTABLE", file_path: "helpers.py", line: 1, symbol: null, rule: "B006-mutable-default", severity: "error", message: "共享可变默认参数 items=[]", evidence_refs: [], status: "resolved", resolution_evidence_refs: ["art-ver"] },
      ],
      artifactBodies: {
        "art-patch": { patch_id: "P1", base_version: "sv-base", format: "unified_diff", diff_text: "--- a/helpers.py\n+++ b/helpers.py\n@@ -1,1 +1,1 @@\n-old\n+new\n", edits: [], target_finding_ids: [] },
        "art-app": { application_id: "pa-1", patch_id: "P1", base_version: "sv-base", result_version: "sv-fixed", status: "committed", error: null, prepared_at: "2026-10-08T02:00:00Z", committed_at: "2026-10-08T02:00:01Z" },
        // the review snapshot is immutable: it still claims the finding is open
        "art-findings": { source_version: "sv-base", findings: [{ finding_id: "F-abc", root_task_id: "T-1", source_version: "sv-base", producer_attempt_id: "T-1-C001-A1", goal_ref: "避免共享可变默认参数", required_for_goal: true, check_id: "CHK-MUTABLE", file_path: "helpers.py", line: 1, symbol: null, rule: "B006-mutable-default", severity: "error", message: "共享可变默认参数 items=[]", evidence_refs: [], status: "open", resolution_evidence_refs: [] }], coverage: ["CHK-MUTABLE"], not_checked: [] },
        "art-ver": { verification_id: "v-1", source_version: "sv-fixed", target_finding_ids: ["f-1"], check_results: [{ check_id: "CHK-MUTABLE", status: "passed", test_count: 3, passed_count: 3, failed_count: 0, skipped_count: 0, executed: true }], coverage: ["CHK-MUTABLE"], not_run: [], passed: true, evidence_refs: ["art-check"], notes: "复用确定性检查结果" },
      },
    });

    render(
      <MemoryRouter initialEntries={["/tasks/T-1"]}>
        <App />
      </MemoryRouter>,
    );

    await screen.findByText("补丁与差异");
    await waitFor(() => expect(screen.getByText("已提交")).toBeInTheDocument());
    expect(screen.getByText(/\+new/)).toBeInTheDocument();
    expect(screen.getByText(/判断依据|复用确定性检查结果/)).toBeInTheDocument();
    // the check appears in both the contract table and the verification evidence
    expect(screen.getAllByText("CHK-MUTABLE").length).toBeGreaterThanOrEqual(2);
    // the finding row shows the persisted (closed) state, not the immutable review
    // snapshot, which still claims the finding is open
    expect(await screen.findByText("共享可变默认参数 items=[]")).toBeInTheDocument();
    expect(screen.getByText("已解决")).toBeInTheDocument();
    expect(screen.queryByText("未解决")).not.toBeInTheDocument();
    expect(calls.some((call) => call.url === "/api/tasks/T-1/findings")).toBe(true);
    const timelineBody = screen.getByText("执行时间线").closest("section")!.querySelector("tbody")!;
    expect(within(timelineBody as HTMLElement).getByText("补丁已应用")).toBeInTheDocument();
  });

  it("shows a waiting task and posts a resume with the current revision", async () => {
    health();
    const waiting = detail({
      status: "waiting_recovery",
      passed: false,
      revision: 42,
      required_action: "追加 repair_round 额度后恢复任务",
      detection: {
        valid: true,
        category: "code_defect",
        facts: {},
        explanation: null,
        missing_items: [],
        failed_check_ids: ["CHK-MUTABLE"],
        related_finding_ids: [],
        invalid_reason: null,
        progress: {},
        suggestion: "重新派发修复",
      },
    });
    serveTask(waiting, {
      events: [event(1, "waiting_recovery", "parent", { reason: "修复额度耗尽", required_action: "追加 repair_round 额度后恢复任务" })],
    });
    on("POST", /\/api\/tasks\/T-1\/resume/, () =>
      json({
        root_task_id: "T-1",
        status: "running",
        revision: 43,
        run_segment_id: "seg-2",
        granted: [{ budget_kind: "repair_round", consumed: 1, granted_max: 2, remaining: 1 }],
        message: "已受理恢复请求",
      }),
    );

    render(
      <MemoryRouter initialEntries={["/tasks/T-1"]}>
        <App />
      </MemoryRouter>,
    );

    await screen.findByText("恢复与终止");
    expect((await screen.findAllByText("追加 repair_round 额度后恢复任务")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("检查未通过")).toBeInTheDocument();
    expect(screen.getByText("存在代码缺陷")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "请求恢复" }));
    await waitFor(() => {
      const post = calls.find((call) => call.method === "POST" && call.url.includes("/resume"));
      expect(post).toBeTruthy();
      expect(post!.headers.get("Idempotency-Key")).toBeTruthy();
      expect(JSON.parse(post!.body!)).toMatchObject({ expected_revision: 42, additional_repair_rounds: 1 });
    });
    await waitFor(() => expect(screen.getByText(/已受理恢复请求/)).toBeInTheDocument());
  });

  it("merges incremental events without duplicating them", async () => {
    health();
    serveTask(detail(), {
      events: [event(1, "task_created"), event(2, "task_dispatched"), event(3, "result_received", "controller")],
    });
    render(
      <MemoryRouter initialEntries={["/tasks/T-1"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByText("执行时间线");
    const table = screen.getByText("执行时间线").closest("section")!;
    // the filter dropdown also lists event labels, so query inside the table body
    const body = table.querySelector("tbody")!;
    await waitFor(() => expect(within(body as HTMLElement).getByText("建立任务")).toBeInTheDocument());
    expect(within(body as HTMLElement).getAllByText("建立任务")).toHaveLength(1);
    expect(within(body as HTMLElement).getAllByText("收到回报")).toHaveLength(1);
    expect(within(body as HTMLElement).getAllByText("派发子任务")).toHaveLength(1);
  });

  it("surfaces the real model-not-configured error on submit instead of faking a run", async () => {
    health(false);
    on("GET", "/api/workflows", () =>
      json([{ workflow_version: "wf-1", semantic_hash: "h", check_mode: "sequential", created_at: "2026-10-08T02:00:00Z" }]),
    );
    on("POST", "/api/tasks", () =>
      json({ code: "MODEL_NOT_CONFIGURED", message: "模型凭据未配置：请设置 HW2_MODEL_API_KEY 后再提交新的模型任务。" }, 503),
    );
    render(
      <MemoryRouter initialEntries={["/"]}>
        <App />
      </MemoryRouter>,
    );
    await screen.findByText("2. 描述目标并提交");
    await waitFor(() => expect(screen.getByText("wf-1（顺序）")).toBeInTheDocument());

    // upload requires a file: use the file input
    const file = new File(["def f(items=[]):\n    return items\n"], "helpers.py", { type: "text/x-python" });
    on("POST", "/api/sources", () => json({ source_id: "s-1", files: [{ path: "helpers.py", size: 30, sha256: "a" }], total_bytes: 30, content_digest: "sha256:abc" }, 201));
    fireEvent.change(screen.getByLabelText("选择代码文件"), { target: { files: [file] } });
    fireEvent.click(screen.getByRole("button", { name: "上传并建立输入清单" }));
    await waitFor(() => expect(screen.getByText(/s-1/)).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "提交任务" }));
    await waitFor(() =>
      expect(screen.getByText(/MODEL_NOT_CONFIGURED/)).toBeInTheDocument(),
    );
  });
});
