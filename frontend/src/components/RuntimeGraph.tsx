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
import { ApiError, getWorkflowGraph } from "../api/client";
import type { RuntimeGraphResponse } from "../api/types";
import { errorCodeLabel } from "../i18n/zh";

interface Props {
  workflowVersion: string | null | undefined;
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

function layoutGraph(graph: RuntimeGraphResponse): Map<string, { x: number; y: number }> {
  const depth = new Map<string, number>();
  const incoming = new Map<string, number>();
  for (const node of graph.nodes) incoming.set(node.id, 0);
  for (const edge of graph.edges) incoming.set(edge.target, (incoming.get(edge.target) ?? 0) + 1);
  const outgoing = new Map<string, string[]>();
  for (const edge of graph.edges) {
    const list = outgoing.get(edge.source) ?? [];
    list.push(edge.target);
    outgoing.set(edge.source, list);
  }
  const queue = graph.nodes.filter((node) => (incoming.get(node.id) ?? 0) === 0).map((node) => node.id);
  for (const id of queue) depth.set(id, 0);
  const indegree = new Map(incoming);
  while (queue.length > 0) {
    const id = queue.shift()!;
    const base = depth.get(id) ?? 0;
    for (const next of outgoing.get(id) ?? []) {
      depth.set(next, Math.max(depth.get(next) ?? 0, base + 1));
      indegree.set(next, (indegree.get(next) ?? 1) - 1);
      if (indegree.get(next) === 0) queue.push(next);
    }
  }
  const columns = new Map<number, string[]>();
  for (const node of graph.nodes) {
    const column = depth.get(node.id) ?? 0;
    const list = columns.get(column) ?? [];
    list.push(node.id);
    columns.set(column, list);
  }
  const positions = new Map<string, { x: number; y: number }>();
  for (const [column, ids] of columns) {
    ids.forEach((id, index) => {
      positions.set(id, { x: column * 230, y: index * 90 });
    });
  }
  return positions;
}

export function RuntimeGraph({ workflowVersion }: Props) {
  const [graph, setGraph] = useState<RuntimeGraphResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!workflowVersion) {
      setGraph(null);
      setError(null);
      setLoading(false);
      return;
    }
    let disposed = false;
    setLoading(true);
    setError(null);
    getWorkflowGraph(workflowVersion)
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
  }, [workflowVersion]);

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

  if (!workflowVersion) {
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
