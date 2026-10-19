/** React Flow canvas for the constrained orchestration config (§14.1).
 *
 * The canvas renders the configured nodes/edges and lets the user drag them
 * (layout only). It is a *view* of the config: legality, role selection and
 * parameters live in the panels, and the structural templates come from the API,
 * so the drawing cannot describe a graph the backend will not run.
 */

import { useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  MarkerType,
  Position,
  ReactFlow,
  applyNodeChanges,
  useReactFlow,
  type Edge,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import type { WorkflowConfig, WorkflowLayoutItem } from "../api/types";
import { taskKindLabel } from "../workflow";
import type { Issue } from "../workflow";

interface Props {
  config: WorkflowConfig;
  layout: WorkflowLayoutItem[];
  issues: Issue[];
  selectedNodeId: string | null;
  onSelect: (nodeId: string | null) => void;
  onLayoutChange: (layout: WorkflowLayoutItem[]) => void;
}

const NODE_WIDTH = 170;
const NODE_HEIGHT = 52;

export function WorkflowCanvas({
  config,
  layout,
  issues,
  selectedNodeId,
  onSelect,
  onLayoutChange,
}: Props) {
  const problemNodes = useMemo(() => {
    const ids = new Set<string>();
    for (const issue of issues) if (issue.nodeId) ids.add(issue.nodeId);
    return ids;
  }, [issues]);

  const positions = useMemo(() => {
    const map = new Map(layout.map((item) => [item.node_id, item]));
    return { map, items: layout };
  }, [layout]);

  const nodes: Node[] = useMemo(
    () =>
      config.nodes.map((node) => {
        const position = positions.map.get(node.id) ?? { node_id: node.id, x: 0, y: 0 };
        const classes = ["wf-node", `wf-node-${node.type}`];
        if (node.id === selectedNodeId) classes.push("wf-node-selected");
        if (problemNodes.has(node.id)) classes.push("wf-node-invalid");
        return {
          id: node.id,
          position: { x: position.x, y: position.y },
          data: {
            label: (
              <div>
                <div className="wf-node-title">{node.id}</div>
                <div className="wf-node-sub">
                  {node.type === "agent"
                    ? `${taskKindLabel(node.task_kind)} · ${node.agent_id}`
                    : node.type === "tool"
                      ? `工具 ${node.tool_name}`
                      : node.type === "parent"
                        ? "父 Agent"
                        : "结束"}
                </div>
              </div>
            ),
          },
          type: "default",
          className: classes.join(" "),
          // explicit initial sizes so the first paint (and edge routing) does not
          // depend on an async measurement pass
          initialWidth: NODE_WIDTH,
          initialHeight: NODE_HEIGHT,
          sourcePosition: node.type === "parent" ? Position.Right : undefined,
          targetPosition: node.type === "parent" ? Position.Left : undefined,
        } satisfies Node;
      }),
    [config.nodes, positions, problemNodes, selectedNodeId],
  );

  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const edges: Edge[] = useMemo(
    () =>
      config.edges.map((edge) => {
        const id = `${edge.source}->${edge.target}`;
        const outbound = edge.source === "parent";
        return {
          id,
          source: edge.source,
          target: edge.target,
          type: "smoothstep",
          pathOptions: { offset: outbound ? 16 : 36, borderRadius: 8 },
          animated: false,
          markerEnd: { type: MarkerType.ArrowClosed },
          label: outbound ? "派发" : "回报",
          className: selectedEdgeId === id ? "wf-edge selected" : "wf-edge",
        };
      }),
    [config.edges, selectedEdgeId],
  );

  const dependencyEdges: Edge[] = useMemo(
    () =>
      config.dependencies.map((dep) => ({
        id: `dep:${dep.source_node}->${dep.target_node}:${dep.artifact}`,
        source: dep.source_node,
        target: dep.target_node,
        label: `输入：${dep.artifact}`,
        className: "wf-edge-dependency",
        style: { strokeDasharray: "4 4" },
      })),
    [config.dependencies],
  );

  function handleChanges(changes: NodeChange[]) {
    // Measurement and selection events do not edit the saved layout. Treating
    // them as edits also clears save/load confirmations immediately after paint.
    const moves = changes.filter((change) => change.type === "position" && change.position);
    if (moves.length === 0) return;
    const next = applyNodeChanges(moves, nodes);
    if (next.every((node, index) =>
      node.position.x === nodes[index].position.x && node.position.y === nodes[index].position.y
    )) return;
    onLayoutChange(
      next.map((node) => ({
        node_id: node.id,
        x: Math.round(node.position.x),
        y: Math.round(node.position.y),
      })),
    );
  }

  return (
    <div className="workflow-canvas" data-testid="workflow-canvas">
      <ReactFlow
        nodes={nodes}
        edges={[...edges, ...dependencyEdges]}
        onNodesChange={handleChanges}
        onNodeClick={(_, node) => {
          setSelectedEdgeId(null);
          onSelect(node.id);
        }}
        onEdgeClick={(_, edge) => {
          setSelectedEdgeId(edge.id);
          onSelect(null);
        }}
        onPaneClick={() => {
          setSelectedEdgeId(null);
          onSelect(null);
        }}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable
        nodesConnectable={false}
        elementsSelectable
      >
        <FitView signature={config.nodes.map((node) => node.id).join(",") + config.edges.map((edge) => edge.source + edge.target).join(",")} />
        <Background color="rgba(148, 163, 184, 0.22)" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}

function FitView({ signature }: { signature: string }) {
  const { fitView } = useReactFlow();
  useEffect(() => {
    const id = window.requestAnimationFrame(() => {
      void fitView({ padding: 0.2 });
    });
    return () => window.cancelAnimationFrame(id);
  }, [signature, fitView]);
  return null;
}
