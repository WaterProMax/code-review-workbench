import { useEffect, useState } from "react";
import type { TaskDetailResponse } from "../api/types";
import { errorCategoryLabel, errorCodeLabel, formatAge, formatTime, rootStatusToken, verdictToken } from "../display";
import { checkModeLabel } from "../i18n/zh";
import { StatusBadge } from "./Badges";

export function TaskSummary({
  detail,
  updatedAt,
}: {
  detail: TaskDetailResponse;
  updatedAt?: number | null;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!updatedAt) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [updatedAt]);
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
              {updatedAt ? <span className="muted"> 最后更新于 {formatAge(updatedAt, now)}</span> : null}
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
              {detail.workflow_version} / {checkModeLabel(detail.check_mode)}
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
          {errorCodeLabel(detail.error.code)}（{errorCategoryLabel(detail.error.category)}）：{detail.error.message}
          {detail.error.recoverable ? "（可恢复）" : "（不可自动恢复）"}
        </div>
      )}
    </section>
  );
}
