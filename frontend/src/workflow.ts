/** Orchestration config helpers: labels, layout and the legality rules (§14.1).
 *
 * The rules here mirror the backend validators so the canvas can point at a node
 * before a request is even sent. The backend remains authoritative: it re-validates
 * on save and the errors are located the same way.
 */

import type {
  BudgetConfig,
  CapabilityListing,
  WorkflowConfig,
  WorkflowEdge,
  WorkflowLayoutItem,
  WorkflowNode,
} from "./api/types";

export const REQUIRED_TASK_KINDS = ["review", "fix", "verify"] as const;

const TASK_KIND_LABELS: Record<string, string> = {
  review: "审查",
  fix: "修复",
  verify: "验证",
  generate_tests: "生成测试",
};

const NODE_TYPE_LABELS: Record<string, string> = {
  parent: "父 Agent",
  agent: "子 Agent",
  tool: "工具",
  end: "结束",
};

export function taskKindLabel(kind: string | null | undefined): string {
  if (!kind) return "—";
  return TASK_KIND_LABELS[kind] ?? kind;
}

export function nodeTypeLabel(type: string): string {
  return NODE_TYPE_LABELS[type] ?? type;
}

export function nodeLabel(node: WorkflowNode): string {
  if (node.type === "agent") return `${node.id}（${taskKindLabel(node.task_kind)}）`;
  if (node.type === "tool") return `${node.id}（工具 ${node.tool_name ?? "?"}）`;
  return `${node.id}（${nodeTypeLabel(node.type)}）`;
}

export interface Issue {
  field: string;
  nodeId?: string;
  message: string;
}

function isWorking(node: WorkflowNode): boolean {
  return node.type === "agent" || node.type === "tool";
}

/** Mirror of WorkflowConfig._validate_structure plus the registry checks. */
export function validateWorkflow(
  config: WorkflowConfig,
  capabilities: CapabilityListing[],
): Issue[] {
  const issues: Issue[] = [];
  const byId = new Map(config.nodes.map((node) => [node.id, node]));
  const ids = config.nodes.map((node) => node.id);
  if (new Set(ids).size !== ids.length) {
    issues.push({ field: "nodes", message: "节点 id 不能重复" });
  }

  const parents = config.nodes.filter((node) => node.type === "parent");
  const ends = config.nodes.filter((node) => node.type === "end");
  if (parents.length !== 1) {
    issues.push({ field: "nodes", message: "工作流必须且只能有一个父 Agent 入口" });
  }
  if (ends.length !== 1) {
    issues.push({ field: "nodes", message: "工作流必须且只能有一个结束节点" });
  }
  const parentId = parents[0]?.id;
  const endId = ends[0]?.id;
  if (parentId && config.nodes.some((n) => n.agent_id === "parent" && n.id !== parentId)) {
    issues.push({ field: "nodes", message: "父 Agent 只能出现在唯一入口节点" });
  }
  if (endId && config.edges.some((edge) => edge.source === endId)) {
    issues.push({ field: `nodes.${endId}`, message: "结束节点不能有出边" });
  }

  for (const node of config.nodes) {
    if (node.type === "agent" && (!node.agent_id || !node.task_kind)) {
      issues.push({
        field: `nodes.${node.id}`,
        nodeId: node.id,
        message: "子 Agent 节点必须同时指定角色与任务类型",
      });
      continue;
    }
    if (node.type === "tool" && !node.tool_name) {
      issues.push({ field: `nodes.${node.id}`, nodeId: node.id, message: "工具节点必须指定工具名" });
      continue;
    }
    if (node.type === "parent" && node.agent_id !== "parent") {
      issues.push({
        field: `nodes.${node.id}`,
        nodeId: node.id,
        message: "父节点必须引用 agent_id=parent",
      });
    }
    if (node.type === "agent" && node.agent_id) {
      const version = config.agents[node.agent_id];
      const cap = capabilities.find((item) => item.agent_id === node.agent_id);
      if (version === undefined) {
        issues.push({
          field: `nodes.${node.id}.agent_id`,
          nodeId: node.id,
          message: `工作流未声明角色 ${node.agent_id} 的版本`,
        });
      } else if (!cap) {
        issues.push({
          field: `nodes.${node.id}.agent_id`,
          nodeId: node.id,
          message: `角色 ${node.agent_id} 未注册，不能保存为可运行节点`,
        });
      } else if (node.task_kind && !cap.supported_task_kinds.includes(node.task_kind)) {
        issues.push({
          field: `nodes.${node.id}.task_kind`,
          nodeId: node.id,
          message: `角色 ${node.agent_id} 不支持任务类型 ${node.task_kind}`,
        });
      }
    }
  }

  for (const edge of config.edges) {
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (!source || !target) {
      issues.push({
        field: "edges",
        message: `连接 ${edge.source} → ${edge.target} 引用了不存在的节点`,
      });
      continue;
    }
    const legal =
      (source.type === "parent" && target.type !== "parent" && target.type !== "end") ||
      (source.type === "parent" && target.type === "end") ||
      (isWorking(source) && target.type === "parent");
    if (!legal) {
      issues.push({
        field: "edges",
        message:
          `非法连接 ${edge.source} → ${edge.target}：只允许 父→子/工具、子/工具→父、父→结束` +
          "（子 Agent 之间不能直连）",
      });
    }
  }

  if (parentId) {
    for (const node of config.nodes) {
      if (!isWorking(node)) continue;
      if (!config.edges.some((edge) => edge.source === parentId && edge.target === node.id)) {
        issues.push({
          field: `nodes.${node.id}`,
          nodeId: node.id,
          message: `节点 ${node.id} 无法从父 Agent 派发`,
        });
      }
      if (!config.edges.some((edge) => edge.source === node.id && edge.target === parentId)) {
        issues.push({
          field: `nodes.${node.id}`,
          nodeId: node.id,
          message: `节点 ${node.id} 缺少回报父 Agent 的连接`,
        });
      }
    }
  }
  if (endId) {
    const intoEnd = config.edges.filter((edge) => edge.target === endId);
    if (intoEnd.length !== 1 || intoEnd[0].source !== parentId) {
      issues.push({
        field: `nodes.${endId}`,
        message: "只有父 Agent 可以连接到结束节点（不能绕过完成检测）",
      });
    }
  }

  const recheck = byId.get("recheck");
  if (config.check_mode === "parallel") {
    if (!recheck || recheck.agent_id !== "reviewer") {
      issues.push({
        field: "nodes",
        message: "并行模板必须包含复用 reviewer 的 recheck 节点",
      });
    }
  } else if (recheck) {
    issues.push({ field: "nodes.recheck", message: "顺序模板不应声明 recheck 节点" });
  }

  const kinds = new Set(config.nodes.map((node) => node.task_kind).filter(Boolean) as string[]);
  const missing = REQUIRED_TASK_KINDS.filter((kind) => !kinds.has(kind));
  if (missing.length > 0) {
    issues.push({ field: "nodes", message: `工作流缺少必需任务类型 ${missing.join("、")}` });
  }

  for (const dep of config.dependencies) {
    if (!byId.has(dep.source_node) || !byId.has(dep.target_node)) {
      issues.push({
        field: "dependencies",
        message: `输入依赖 ${dep.source_node} → ${dep.target_node} 引用了不存在的节点`,
      });
    }
    if (dep.source_node === parentId || dep.target_node === parentId) {
      issues.push({
        field: "dependencies",
        message: "输入依赖连接工作节点，不连接父 Agent（它不是执行边）",
      });
    }
  }
  return issues;
}

export function issuesForNode(issues: Issue[], nodeId: string): Issue[] {
  return issues.filter(
    (issue) => issue.nodeId === nodeId || issue.field === `nodes.${nodeId}`,
  );
}

export function edgeKey(edge: WorkflowEdge): string {
  return `${edge.source}->${edge.target}`;
}

/** Column positions by template order; a saved layout always wins. */
export function defaultLayout(config: WorkflowConfig): WorkflowLayoutItem[] {
  const saved = new Map(config.layout.map((item) => [item.node_id, item]));
  const order = ["parent", "review", "gen", "fix", "apply", "verify", "recheck", "end"];
  const ranked = [...config.nodes].sort((a, b) => {
    const left = order.indexOf(a.id);
    const right = order.indexOf(b.id);
    return (left === -1 ? 99 : left) - (right === -1 ? 99 : right);
  });
  return ranked.map((node) => {
    const existing = saved.get(node.id);
    const column = node.type === "parent" ? 0 : node.type === "end" ? 2 : 1;
    // Agents and tools share the middle column, so they must share its rows.
    const row = ranked.filter((item) => item.type === "agent" || item.type === "tool").indexOf(node);
    return {
      node_id: node.id,
      x: existing?.x ?? column * 260,
      y: existing?.y ?? (node.type === "parent" ? 120 : node.type === "end" ? 120 : 60 + row * 110),
    };
  });
}

export const BUDGET_FIELDS: Array<{ key: keyof BudgetConfig; label: string; step?: number }> = [
  { key: "max_repair_rounds", label: "自动修复轮数（repair_round）" },
  { key: "max_verification_retries", label: "验证故障重试（verification_retry）" },
  { key: "max_review_retries", label: "审查故障重试（review_retry）" },
  { key: "max_fix_retries", label: "修复故障重试（fix_retry）" },
  { key: "max_evidence_retries", label: "补证据重试（evidence_retry）" },
  { key: "max_generate_retries", label: "测试生成故障重试（generate_retry）" },
  { key: "max_parent_corrections", label: "父 Agent 纠正次数" },
  { key: "max_model_retries", label: "模型错误重试" },
  { key: "max_tool_steps", label: "单次尝试工具步数" },
  { key: "model_timeout_seconds", label: "模型超时（秒）", step: 1 },
  { key: "tool_timeout_seconds", label: "工具超时（秒）", step: 1 },
  { key: "attempt_timeout_seconds", label: "尝试超时（秒）", step: 1 },
  { key: "max_graph_steps", label: "图步数许可" },
];
