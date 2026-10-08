import type { AttemptView } from "../api/types";
import {
  RETRY_REASON_LABELS,
  attemptStatusToken,
  formatDuration,
  formatTime,
  verdictToken,
} from "../display";
import { StatusBadge } from "./Badges";

export function AttemptHistory({ attempts }: { attempts: AttemptView[] }) {
  if (attempts.length === 0) {
    return <p className="muted">该子任务还没有执行尝试。</p>;
  }
  return (
    <table className="grid attempt-history">
      <thead>
        <tr>
          <th>尝试</th>
          <th>重派原因</th>
          <th>执行状态</th>
          <th>结论</th>
          <th>代码版本</th>
          <th>开始 / 结束</th>
          <th>耗时</th>
          <th>回报摘要</th>
          <th>错误</th>
        </tr>
      </thead>
      <tbody>
        {attempts.map((attempt) => (
          <tr key={attempt.attempt_id}>
            <td className="mono">#{attempt.attempt_no}</td>
            <td>{attempt.retry_reason ? RETRY_REASON_LABELS[attempt.retry_reason] ?? attempt.retry_reason : "—"}</td>
            <td>
              <StatusBadge token={attemptStatusToken(attempt.status)} />
            </td>
            <td>
              <StatusBadge token={verdictToken(attempt.result ? attempt.result.passed : null)} />
            </td>
            <td className="mono">{attempt.source_version}</td>
            <td className="mono">
              {formatTime(attempt.started_at)}
              <br />
              {formatTime(attempt.finished_at)}
            </td>
            <td className="mono">{formatDuration(attempt.duration_ms)}</td>
            <td>{attempt.result?.summary ?? "—"}</td>
            <td className="mono">{attempt.result?.error ? `${attempt.result.error.code}: ${attempt.result.error.message}` : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
