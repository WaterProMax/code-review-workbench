import { render } from "@testing-library/react";
import { expect, test } from "vitest";
import { DiffViewer } from "../src/components/DiffViewer";
import type { PatchArtifact } from "../src/api/types";

const added = [
  "+def add_item(item, items=None):",
  "+    if items is None:",
  "+        items = []",
  "+    try:",
  "+        items.append(item)",
  "+    except ValueError:",
  "+        raise",
  "+    return items",
  "+",
  "+def helper():",
  "+    return None",
  "+# end",
  "+# trailing",
];

test("added diff lines stay on their own rows", () => {
  expect(added.filter((line) => line.startsWith("+") && !line.startsWith("+++"))).toHaveLength(13);
  const diff = ["--- a/helpers.py", "+++ b/helpers.py", "@@ -1,2 +1,13 @@", " context", "-old", ...added].join("\n");
  const patch: PatchArtifact = {
    patch_id: "P-05b79bca04",
    root_task_id: "T-1",
    producer_attempt_id: "A1",
    base_version: "sv",
    target_finding_ids: [],
    format: "unified_diff",
    diff_text: diff,
    edits: [],
  };
  const { container } = render(<DiffViewer patches={[patch]} applications={[]} />);
  const nodes = container.querySelectorAll(".diff-add");
  expect(nodes).toHaveLength(13);
  nodes.forEach((node, index) => {
    expect(node.textContent).toBe(added[index]);
    expect(node.getAttribute("aria-label")).toContain("新增");
  });
  expect(container.textContent).toContain("+13 / -1");
});
