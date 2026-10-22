import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, expect, test, vi } from "vitest";
import type { TaskDetailResponse } from "../src/api/types";
import { RecoveryPanel } from "../src/components/RecoveryPanel";
import { TaskDetailPage } from "../src/pages/TaskDetailPage";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const running = {
  root_task_id: "T-1",
  goal: "检查可变默认参数",
  status: "running",
  passed: null,
  revision: 4,
  workflow_version: "wf-1",
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
} as TaskDetailResponse;

afterEach(() => {
  vi.unstubAllGlobals();
});

test("recovery panel states which status resume and terminate require", () => {
  const { rerender } = render(
    <RecoveryPanel
      rootTaskId="T-1"
      status="running"
      revision={4}
      requiredAction={null}
      budgets={[]}
      onChanged={() => {}}
    />,
  );
  expect(screen.getByText("waiting_recovery")).toBeInTheDocument();
  expect(screen.getByText("interrupted")).toBeInTheDocument();
  expect(screen.getByText(/请先取消运行/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "请求恢复" })).not.toBeInTheDocument();

  rerender(
    <RecoveryPanel
      rootTaskId="T-1"
      status="completed"
      revision={4}
      requiredAction={null}
      budgets={[]}
      onChanged={() => {}}
    />,
  );
  expect(screen.getByText(/此状态下不能恢复或终止/)).toBeInTheDocument();
});

test("running detail page posts cancel with the current revision", async () => {
  const posts: Array<{ body: string | null; key: string | null }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
      const url = String(input);
      const method = (init.method ?? "GET").toUpperCase();
      if (method === "POST" && url.includes("/cancel")) {
        posts.push({
          body: typeof init.body === "string" ? init.body : null,
          key: new Headers(init.headers).get("Idempotency-Key"),
        });
        return json({
          root_task_id: "T-1",
          status: "waiting_recovery",
          revision: 5,
          reason: "用户取消运行中的任务",
          signalled: true,
        });
      }
      if (url === "/api/tasks/T-1") return json(running);
      if (url.includes("/events")) return json({ events: [], next_seq: 0 });
      if (url.includes("/report")) return json({ code: "NOT_FOUND", message: "no report" }, 404);
      return json([]);
    }),
  );

  render(
    <MemoryRouter initialEntries={["/tasks/T-1"]}>
      <Routes>
        <Route path="/tasks/:rootTaskId" element={<TaskDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );

  fireEvent.click(await screen.findByRole("button", { name: "取消" }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0].key).toBeTruthy();
  expect(JSON.parse(posts[0].body ?? "{}")).toMatchObject({
    expected_revision: 4,
    reason: "用户取消运行中的任务",
  });
  expect(screen.getByTestId("stream-status")).toHaveTextContent("轮询");
});
