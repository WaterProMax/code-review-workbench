import React from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useTaskData } from "../src/hooks/useTaskData";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

test("StrictMode keeps reading the task and leaves the stream connected", async () => {
  let revision = 0;
  let reads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/api/tasks/T-strict") {
        reads += 1;
        revision += 1;
        return json({ status: "running", revision });
      }
      if (url.includes("/events")) return json({ events: [], next_seq: 0 });
      return json([]);
    }),
  );

  const { result, unmount } = renderHook(() => useTaskData("T-strict"), {
    wrapper: React.StrictMode,
  });

  await waitFor(
    () => {
      expect(reads).toBeGreaterThanOrEqual(3);
      expect(["streaming", "polling"]).toContain(result.current.streamStatus);
    },
    { timeout: 6000 },
  );
  unmount();
}, 12000);
