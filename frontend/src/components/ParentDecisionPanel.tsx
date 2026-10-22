import type { DetectionResult, ExecutionEvent } from "../api/types";
import { detectionToken, summarizePayload } from "../display";
import { StatusBadge } from "./Badges";

interface Props {
  detection: DetectionResult | null;
  requiredAction: string | null;
  latestDecision: ExecutionEvent | null;
}

export function ParentDecisionPanel({ detection, requiredAction, latestDecision }: Props) {
  const reasoning = latestDecision
    ? String(latestDecision.payload.reasoning ?? summarizePayload(latestDecision.payload))
    : null;
  const action = latestDecision ? String(latestDecision.payload.action ?? "—") : "—";
  return (
    <section className="panel">
      <h2>父 Agent 检测与决策</h2>
      <table className="kv">
        <tbody>
          <tr>
            <th>检测结论</th>
            <td>
              <StatusBadge token={detectionToken(detection?.category)} />
            </td>
          </tr>
          <tr>
            <th>下一步建议</th>
            <td>{detection?.suggestion ?? "—"}</td>
          </tr>
          <tr>
            <th>未满足的必需检查</th>
            <td className="mono">
              {detection && detection.failed_check_ids.length > 0
                ? detection.failed_check_ids.join(", ")
                : "—"}
            </td>
          </tr>
          <tr>
            <th>缺失证据</th>
            <td className="mono">
              {detection && detection.missing_items.length > 0 ? detection.missing_items.join("；") : "—"}
            </td>
          </tr>
          <tr>
            <th>最近父决策</th>
            <td>
              <span className="mono">{action}</span>
            </td>
          </tr>
          <tr>
            <th>决策理由</th>
            <td>{reasoning ?? "—"}</td>
          </tr>
          <tr>
            <th>恢复条件</th>
            <td>{requiredAction ?? "—"}</td>
          </tr>
        </tbody>
      </table>
      <p className="muted">
        检测结论由控制层按持久化证据重算，模型的说明只作解释。执行状态与检查结论分开显示。
      </p>
    </section>
  );
}
