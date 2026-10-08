import { Link } from "react-router-dom";
import type { TaskListItem } from "../api/types";
import { formatTime, rootStatusToken, verdictToken } from "../display";
import { StatusBadge } from "./Badges";

export function TaskList({ items }: { items: TaskListItem[] }) {
  if (items.length === 0) {
    return <p className="muted">还没有任务。提交一次代码审查后会显示在这里。</p>;
  }
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>总任务</th>
          <th>目标</th>
          <th>执行状态</th>
          <th>检查结论</th>
          <th>配置版本</th>
          <th>更新时间</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.root_task_id}>
            <td className="mono">{item.root_task_id}</td>
            <td>{item.goal}</td>
            <td>
              <StatusBadge token={rootStatusToken(item.status)} />
            </td>
            <td>
              <StatusBadge token={verdictToken(item.passed)} />
            </td>
            <td className="mono">{item.workflow_version}</td>
            <td className="mono">{formatTime(item.updated_at)}</td>
            <td>
              <Link to={`/tasks/${item.root_task_id}`}>查看</Link>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
