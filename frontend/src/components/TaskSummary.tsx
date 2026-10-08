import type { TaskDetailResponse } from "../api/types";
import { formatTime, rootStatusToken, verdictToken } from "../display";
import { StatusBadge } from "./Badges";

export function TaskSummary({ detail }: { detail: TaskDetailResponse }) {
  return (
    <section className="panel">
      <h2>任务总览</h2>
      <table className="kv">
        <tbody>
          <tr>
            <th>总任务 ID</th>
            <td className="mono">{detail.root_task_id}</td>
          </tr>
          <tr>
            <th>目标</th>
            <td>{detail.goal}</td>
          </tr>
          <tr>
            <th>执行状态</th>
            <td>
              <StatusBadge token={rootStatusToken(detail.status)} />
            </td>
          </tr>
          <tr>
            <th>检查结论</th>
            <td>
              <StatusBadge token={verdictToken(detail.passed)} />
            </td>
          </tr>
          <tr>
            <th>当前代码版本</th>
            <td className="mono">{detail.source_version ?? "—"}</td>
          </tr>
          <tr>
            <th>检查合同版本</th>
            <td className="mono">{detail.contract_version ?? "—"}</td>
          </tr>
          <tr>
            <th>工作流 / 模式</th>
            <td className="mono">
              {detail.workflow_version} / {detail.check_mode}
            </td>
          </tr>
          <tr>
            <th>修订号</th>
            <td className="mono">{detail.revision}</td>
          </tr>
          <tr>
            <th>更新时间</th>
            <td className="mono">{formatTime(detail.updated_at)}</td>
          </tr>
        </tbody>
      </table>
      {detail.required_action && (
        <div className="warn-box">
          <strong>需要处理：</strong> {detail.required_action}
        </div>
      )}
      {detail.error && (
        <div className="error-box">
          {detail.error.code}（{detail.error.category}）：{detail.error.message}
          {detail.error.recoverable ? "（可恢复）" : "（不可自动恢复）"}
        </div>
      )}
    </section>
  );
}
