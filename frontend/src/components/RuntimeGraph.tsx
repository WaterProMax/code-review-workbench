/** Read-only React Flow view of the compiled runtime graph. */

import { useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { ApiError, getWorkflowGraph, previewWorkflowGraph } from "../api/client";
import type { RuntimeGraphResponse, WorkflowConfig } from "../api/types";
import { errorCodeLabel } from "../i18n/zh";

interface Props {
  workflowVersion?: string | null;
  config?: WorkflowConfig | null;
}

const NODE_WIDTH = 180;
const NODE_HEIGHT = 52;

function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    const label = errorCodeLabel(err.code);
    const named = label === err.code ? err.message : `${label}：${err.message}`;
    return named;
  }
  return err instanceof Error ? err.message : String(err);
}

const RANK: Record<string, number> = {
  initialize: 0,
  plan: 1,
  validate_plan: 2,
  replan: 2,
  commit_plan: 3,
  budget: 4,
  parent_decide: 5,
  validate_action: 6,
  dispatch: 7,
  apply_patch: 7,
  check_finish: 7,
  enter_wait: 7,
  run_attempt: 8,
  await_human: 8,
  collect_results: 9,
  finalize_terminated: 9,
  detect: 10,
  finalize: 11,
};

function layoutGraph(graph: RuntimeGraphResponse): Map<string, { x: number; y: number }> {
  const columns = new Map<number, string[]>();
  graph.nodes.forEach((node, index) => {
    const column = RANK[node.id] ?? 12 + index;
    const list = columns.get(column) ?? [];
    list.push(node.id);
    columns.set(column, list);
  });
  const positions = new Map<string, { x: number; y: number }>();
  for (const [column, ids] of columns) {
    ids.forEach((id, index) => {
      positions.set(id, { x: column * 210, y: index * 90 });
    });
  }
  return positions;
}

export function RuntimeGraph({ workflowVersion, config }: Props) {
  const [graph, setGraph] = useState<RuntimeGraphResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!config && !workflowVersion) {
      setGraph(null);
      setError(null);
      setLoading(false);
      return;
    }
    let disposed = false;
    setLoading(true);
    setError(null);
    const pending = config ? previewWorkflowGraph(config) : getWorkflowGraph(workflowVersion as string);
    pending
      .then((next) => {
        if (disposed) return;
        setGraph(next);
      })
      .catch((err: unknown) => {
        if (disposed) return;
        setGraph(null);
        setError(`无法加载运行时图：${describeError(err)}`);
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [workflowVersion, config]);

  const positions = useMemo(() => (graph ? layoutGraph(graph) : new Map()), [graph]);
  const nodes: Node[] = useMemo(
    () =>
      (graph?.nodes ?? []).map((node) => {
        const position = positions.get(node.id) ?? { x: 0, y: 0 };
        return {
          id: node.id,
          position,
          data: { label: <div className="wf-node-title">{node.id}</div> },
          type: "default",
          className: "wf-node",
          width: NODE_WIDTH,
          height: NODE_HEIGHT,
          measured: { width: NODE_WIDTH, height: NODE_HEIGHT },
          sourcePosition: Position.Right,
          targetPosition: Position.Left,
          handles: [
            { type: "target", position: Position.Left, x: 0, y: NODE_HEIGHT / 2, width: 8, height: 8 },
            { type: "source", position: Position.Right, x: NODE_WIDTH, y: NODE_HEIGHT / 2, width: 8, height: 8 },
          ],
          draggable: false,
        } satisfies Node;
      }),
    [graph, positions],
  );
  const edges: Edge[] = useMemo(
    () =>
      (graph?.edges ?? []).map((edge) => ({
        id: `${edge.source}->${edge.target}${edge.conditional ? ":cond" : ""}`,
        source: edge.source,
        target: edge.target,
        type: "smoothstep",
        markerEnd: { type: MarkerType.ArrowClosed },
        className: edge.conditional ? "wf-edge wf-edge-conditional" : "wf-edge",
        style: edge.conditional ? { strokeDasharray: "6 4" } : undefined,
      })),
    [graph],
  );

  if (!workflowVersion && !config) {
    return <p className="muted">保存或加载工作流版本后才能读取运行时图。业务图仍可继续编辑。</p>;
  }
  if (loading && !graph) return <p className="muted">正在加载运行时图…</p>;
  if (error) return <div className="error-box">{error}</div>;
  if (!graph) return <p className="muted">没有运行时图数据。</p>;

  return (
    <div className="workflow-canvas" data-testid="runtime-graph">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
      >
        <Background color="rgba(148, 163, 184, 0.22)" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
