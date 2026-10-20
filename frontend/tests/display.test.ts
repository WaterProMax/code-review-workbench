import { expect, test } from "vitest";
import { attemptStatusToken, formatTime, taskStatusToken } from "../src/display";

test("formatTime labels Beijing time", () => {
  expect(formatTime("2026-10-19T13:04:12Z")).toContain("21:04:12");
  expect(formatTime("2026-10-19T13:04:12Z")).toContain("UTC+8");
});

test("cancelled tasks use a grey 已取消 badge and invalidated attempts stay 已撤销", () => {
  const cancelled = taskStatusToken("cancelled");
  expect(cancelled.label).toBe("已取消");
  expect(cancelled.className).toContain("cancelled");
  expect(cancelled.label).not.toBe("执行失败");
  expect(attemptStatusToken("invalidated").label).toBe("已撤销");
});
