import { expect, test } from "vitest";
import { formatTime } from "../src/display";

test("formatTime labels Beijing time", () => {
  expect(formatTime("2026-10-19T13:04:12Z")).toContain("21:04:12");
  expect(formatTime("2026-10-19T13:04:12Z")).toContain("UTC+8");
});
