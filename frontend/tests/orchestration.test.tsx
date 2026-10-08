/** P10: the orchestration canvas edits only what the backend will really run. */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../src/App";
import type { CapabilityListing, WorkflowConfig } from "../src/api/types";

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
let calls: Array<{ url: string; method: string; body: string | null }> = [];
let savedList: Array<{ workflow_version: string; semantic_hash: string; check_mode: string; created_at: string }> = [];

function on(method: string, match: RegExp | string, respond: (init?: RequestInit) => Response) {
  routes.push({
    method,
    match: (url) => (typeof match === "string" ? url.startsWith(match) : match.test(url)),
    respond,
  });
}

const CAPABILITIES: CapabilityListing[] = [
  {
    agent_id: "reviewer",
    version: "1.0",
    description: "审查 Agent",
    supported_task_kinds: ["review"],
    tool_names: ["read_file"],
    required_input_keys: ["source"],
    produced_artifact_types: ["finding"],
    retry_budget: { review: "review_retry" },
  },
  {
    agent_id: "fixer",
    version: "1.0",
    description: "修复 Agent",
    supported_task_kinds: ["fix"],
    tool_names: ["submit_patch"],
    required_input_keys: ["source"],
    produced_artifact_types: ["patch"],
    retry_budget: { fix: "fix_retry" },
  },
  {
    agent_id: "verifier",
    version: "1.0",
    description: "验证 Agent",
    supported_task_kinds: ["verify"],
    tool_names: ["run_checks"],
    required_input_keys: ["source"],
    produced_artifact_types: ["verification_report"],
    retry_budget: { verify: "verification_retry" },
  },
  {
    agent_id: "test_generator",
    version: "1.0",
    description: "测试生成 Agent",
    supported_task_kinds: ["generate_tests"],
    tool_names: ["submit_generated_tests"],
    required_input_keys: ["source"],
    produced_artifact_types: ["test_artifact"],
    retry_budget: { generate_tests: "generate_retry" },
  },
];

const BUDGETS = {
  max_repair_rounds: 2,
  max_verification_retries: 2,
  max_review_retries: 2,
  max_fix_retries: 2,
  max_evidence_retries: 2,
  max_generate_retries: 2,
  max_parent_corrections: 2,
  max_model_retries: 2,
  max_tool_steps: 12,
  model_timeout_seconds: 60,
  tool_timeout_seconds: 30,
  attempt_timeout_seconds: 180,
  max_graph_steps: 256,
};

function sequentialConfig(): WorkflowConfig {
  return {
    schema_version: "1.0",
    check_mode: "sequential",
    agents: { parent: "1.0", reviewer: "1.0", fixer: "1.0", verifier: "1.0" },
    nodes: [
      { id: "parent", type: "parent", agent_id: "parent" },
      { id: "review", type: "agent", agent_id: "reviewer", task_kind: "review" },
      { id: "fix", type: "agent", agent_id: "fixer", task_kind: "fix" },
      { id: "apply", type: "tool", tool_name: "apply_patch" },
      { id: "verify", type: "agent", agent_id: "verifier", task_kind: "verify" },
      { id: "end", type: "end" },
    ],
    edges: [
      { source: "parent", target: "review" },
      { source: "review", target: "parent" },
      { source: "parent", target: "fix" },
      { source: "fix", target: "parent" },
      { source: "parent", target: "apply" },
      { source: "apply", target: "parent" },
      { source: "parent", target: "verify" },
      { source: "verify", target: "parent" },
      { source: "parent", target: "end" },
    ],
    dependencies: [
      { source_node: "review", target_node: "fix", artifact: "finding" },
      { source_node: "apply", target_node: "verify", artifact: "patch_application" },
    ],
    budgets: { ...BUDGETS },
    layout: [],
    workflow_version: null,
    semantic_hash: null,
    tool_policy_versions: { apply_patch: "1.0" },
  };
}

function extensionConfig(): WorkflowConfig {
  const base = sequentialConfig();
  return {
    ...base,
    agents: { ...base.agents, test_generator: "1.0" },
    nodes: [
      base.nodes[0],
      base.nodes[1],
      { id: "gen", type: "agent", agent_id: "test_generator", task_kind: "generate_tests" },
      ...base.nodes.slice(2),
    ],
    edges: [
      ...base.edges,
      { source: "parent", target: "gen" },
      { source: "gen", target: "parent" },
    ],
    dependencies: [
      ...base.dependencies,
      { source_node: "review", target_node: "gen", artifact: "finding" },
      { source_node: "gen", target_node: "verify", artifact: "test_artifact" },
    ],
  };
}

beforeEach(() => {
  routes = [];
  calls = [];
  savedList = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init.method ?? "GET").toUpperCase();
    calls.push({
      url,
      method,
      body: typeof init.body === "string" ? init.body : null,
    });
    const route = routes.find((item) => item.method === method && item.match(url));
    if (!route) return Promise.resolve(json({ code: "NOT_FOUND", message: `no route ${method} ${url}` }, 404));
    return Promise.resolve(route.respond(init));
  });
  if (!("ResizeObserver" in globalThis)) {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  }
  on("GET", "/api/health", () =>
    json({
      status: "ok",
      version: "0.1.0",
      model_configured: true,
      model_provider: "deepseek",
      model_name: "deepseek-chat",
      data_dir: "/tmp/data",
    }),
  );
  on("GET", "/api/agents", () => json(CAPABILITIES));
  on("GET", "/api/workflows/templates", () =>
    json([
      {
        name: "workflow.sequential.yaml",
        description: "顺序模板",
        check_mode: "sequential",
        config: sequentialConfig(),
      },
      {
        name: "workflow.extension.yaml",
        description: "扩展模板",
        check_mode: "sequential",
        config: extensionConfig(),
      },
    ]),
  );
  on("GET", /\/api\/workflows$/, () => json(savedList));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/workflows"]}>
      <App />
    </MemoryRouter>,
  );
}

describe("orchestration canvas", () => {
  it("loads a template with nodes, legal edges and input dependencies", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "编排配置" });
    // the template came from the API, expanded by the backend
    await waitFor(() => expect(screen.getAllByText("review").length).toBeGreaterThan(0));
    expect(screen.getByText("配置通过全部本地校验，可以保存。")).toBeInTheDocument();
    expect(screen.getAllByText("合法").length).toBe(9);
    // input dependencies are shown separately from execution edges
    expect(screen.getByText("输入依赖（finding）")).toBeInTheDocument();
    expect(screen.getByText("输入依赖（patch_application）")).toBeInTheDocument();
  });

  it("offers only registered roles for a node's task kind", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "编排配置" });
    fireEvent.click(screen.getByText("审查").closest("tr")!);
    const select = await screen.findByLabelText("节点角色");
    const options = Array.from(select.querySelectorAll("option")).map((item) => item.value);
    // only the reviewer supports "review"; the ext role is not offered here
    expect(options).toEqual(["reviewer"]);
  });

  it("loads the extension template and shows the plugin-provided dependency", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "编排配置" });
    fireEvent.change(screen.getByLabelText("选择模板"), {
      target: { value: "workflow.extension.yaml" },
    });
    expect(await screen.findByText("输入依赖（test_artifact）")).toBeInTheDocument();
    expect(screen.getAllByText("生成测试").length).toBeGreaterThan(0);
    expect(screen.getByText("配置通过全部本地校验，可以保存。")).toBeInTheDocument();
  });

  it("offers only the role that can run each node's task kind", async () => {
    renderPage();
    await screen.findByRole("heading", { name: "编排配置" });
    fireEvent.click(screen.getByText("修复").closest("tr")!);
    const select = await screen.findByLabelText("节点角色");
    const options = Array.from(select.querySelectorAll("option")).map((item) => item.value);
    expect(options).toEqual(["fixer"]);
  });

  it("rejects an illegal connection from a loaded version and disables saving", async () => {
    const broken = sequentialConfig();
    // a child -> child edge is never legal; only the parent may dispatch or receive
    broken.edges = [...broken.edges, { source: "review", target: "fix" }];
    savedList = [
      {
        workflow_version: "wf-broken",
        semantic_hash: "wf-x",
        check_mode: "sequential",
        created_at: "2026-10-08T02:00:00Z",
      },
    ];
    on("GET", "/api/workflows/wf-broken", () => json({ ...broken, workflow_version: "wf-broken" }));
    renderPage();
    await screen.findByRole("heading", { name: "编排配置" });
    fireEvent.change(screen.getByLabelText("加载已保存版本"), {
      target: { value: "wf-broken" },
    });
    expect(await screen.findByText(/非法连接 review → fix/)).toBeInTheDocument();
    expect(screen.getByText("非法")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /保存为新版本/ })).toBeDisabled();
    expect(calls.some((call) => call.method === "POST")).toBe(false);
  });

  it("surfaces a backend rejection located to the offending node", async () => {
    on("POST", "/api/workflows", () =>
      json(
        {
          code: "VALIDATION_ERROR",
          message: "节点 ghost 的任务类型 'ghost_kind' 未注册",
          details: { field: "nodes.ghost.task_kind", node_id: "ghost" },
        },
        400,
      ),
    );
    renderPage();
    await screen.findByRole("heading", { name: "编排配置" });
    fireEvent.change(screen.getByLabelText("版本名"), { target: { value: "wf-canvas-1" } });
    fireEvent.click(screen.getByRole("button", { name: /保存为新版本/ }));
    const error = await screen.findByText(/VALIDATION_ERROR/);
    expect(error.textContent).toContain("nodes.ghost.task_kind");
    const posted = calls.find((call) => call.method === "POST");
    expect(posted?.body).toContain("wf-canvas-1");
  });

  it("saves the edited budgets and shows the frozen version", async () => {
    on("POST", "/api/workflows", (init) => {
      const posted = JSON.parse(String(init?.body)) as WorkflowConfig;
      return json(
        {
          workflow_version: posted.workflow_version ?? "wf-hash",
          semantic_hash: "wf-abc123",
          check_mode: posted.check_mode,
          config: { ...posted, workflow_version: posted.workflow_version ?? "wf-hash", semantic_hash: "wf-abc123" },
        },
        201,
      );
    });
    renderPage();
    await screen.findByRole("heading", { name: "编排配置" });
    fireEvent.change(screen.getByLabelText("自动修复轮数（repair_round）"), {
      target: { value: "3" },
    });
    fireEvent.change(screen.getByLabelText("版本名"), { target: { value: "wf-canvas-budget" } });
    fireEvent.click(screen.getByRole("button", { name: /保存为新版本/ }));
    expect(await screen.findByText(/已保存 wf-canvas-budget/)).toBeInTheDocument();
    const posted = JSON.parse(String(calls.find((call) => call.method === "POST")?.body));
    expect(posted.budgets.max_repair_rounds).toBe(3);
    // layout is carried in the payload but does not change execution semantics
    expect(Array.isArray(posted.layout)).toBe(true);
    expect(posted.layout.length).toBe(posted.nodes.length);
  });
});
