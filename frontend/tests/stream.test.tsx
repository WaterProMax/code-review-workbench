import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useTaskData } from "../src/hooks/useTaskData";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  readyState = 1;
  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  close() {
    this.readyState = 2;
  }
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  FakeEventSource.instances = [];
  vi.unstubAllGlobals();
});

test("missing EventSource keeps fetch polling", async () => {
  expect(typeof EventSource).toBe("undefined");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/tasks/T-1") return json({ status: "running", revision: 1 });
      if (url.includes("/events")) return json({ events: [], next_seq: 0 });
      return json([]);
    }),
  );
  const { result, unmount } = renderHook(() => useTaskData("T-1"));
  await waitFor(() => expect(result.current.streamStatus).toBe("polling"));
  expect(result.current.detail?.status).toBe("running");
  unmount();
});

test("prefers EventSource while running and falls back to polling on error", async () => {
  vi.stubGlobal("EventSource", FakeEventSource);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/tasks/T-1") return json({ status: "running", revision: 1 });
    if (url.includes("/events")) {
      return json({ events: [], next_seq: 0, status: "running", passed: null, revision: 1 });
    }
    if (url.includes("/report")) return json({ code: "NOT_FOUND", message: "no report" }, 404);
    return json([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  const { result, unmount } = renderHook(() => useTaskData("T-1"));
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
  expect(FakeEventSource.instances[0].url).toBe("/api/tasks/T-1/stream?after_seq=0");
  await waitFor(() => expect(result.current.streamStatus).toBe("streaming"));

  act(() => {
    FakeEventSource.instances[0].onmessage?.({
      data: JSON.stringify({
        event_id: "ev-4",
        root_task_id: "T-1",
        task_id: null,
        attempt_id: null,
        dispatch_batch_id: null,
        source_version: null,
        sequence: 4,
        timestamp: "2026-10-08T02:00:00Z",
        actor_id: "parent",
        event_type: "task_dispatched",
        payload: {},
        artifact_refs: [],
        duration_ms: null,
      }),
    } as MessageEvent);
  });
  await waitFor(() => expect(result.current.events.some((item) => item.event_id === "ev-4")).toBe(true));

  const before = fetchMock.mock.calls.length;
  act(() => {
    FakeEventSource.instances[0].onerror?.(new Event("error"));
  });
  await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(before));
  expect(result.current.streamStatus).toBe("polling");
  unmount();
});

test("only milestone events refresh, and unmount closes the stream", async () => {
  vi.stubGlobal("EventSource", FakeEventSource);
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url === "/api/tasks/T-1") return json({ status: "running", revision: 1 });
    if (url.includes("/events")) return json({ events: [], next_seq: 0, status: "running", passed: null, revision: 1 });
    return json([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  const { result, unmount } = renderHook(() => useTaskData("T-1"));
  await waitFor(() => expect(result.current.streamStatus).toBe("streaming"));
  const source = FakeEventSource.instances[0];
  const quiet = fetchMock.mock.calls.length;

  act(() => {
    source.onmessage?.({
      data: JSON.stringify({
        event_id: "ev-quiet",
        root_task_id: "T-1",
        task_id: null,
        attempt_id: null,
        dispatch_batch_id: null,
        source_version: null,
        sequence: 4,
        timestamp: "2026-10-08T02:00:00Z",
        actor_id: "parent",
        event_type: "task_dispatched",
        payload: { task_kind: "review" },
        artifact_refs: [],
        duration_ms: null,
      }),
    } as MessageEvent);
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 400));
  });
  expect(result.current.events.some((item) => item.event_id === "ev-quiet")).toBe(true);
  expect(fetchMock.mock.calls.length).toBe(quiet);

  act(() => {
    source.onmessage?.({
      data: JSON.stringify({
        event_id: "ev-patch",
        root_task_id: "T-1",
        task_id: null,
        attempt_id: null,
        dispatch_batch_id: null,
        source_version: null,
        sequence: 5,
        timestamp: "2026-10-08T02:00:01Z",
        actor_id: "tool",
        event_type: "patch_applied",
        payload: { result_version: "sv-2" },
        artifact_refs: [],
        duration_ms: null,
      }),
    } as MessageEvent);
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 120));
  });
  expect(fetchMock.mock.calls.length).toBe(quiet);
  await waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThan(quiet));

  const settled = fetchMock.mock.calls.length;
  act(() => {
    source.onmessage?.({
      data: JSON.stringify({
        event_id: "ev-late",
        root_task_id: "T-1",
        task_id: null,
        attempt_id: null,
        dispatch_batch_id: null,
        source_version: null,
        sequence: 6,
        timestamp: "2026-10-08T02:00:02Z",
        actor_id: "tool",
        event_type: "terminal_recorded",
        payload: {},
        artifact_refs: [],
        duration_ms: null,
      }),
    } as MessageEvent);
  });
  unmount();
  expect(source.readyState).toBe(2);
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 500));
  });
  expect(fetchMock.mock.calls.length).toBe(settled);
});
