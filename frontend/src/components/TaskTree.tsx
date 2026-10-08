import { useState } from "react";
import type { AttemptView, Task } from "../api/types";
import {
  AGENT_ROLE_LABELS,
  SKIP_REASON_LABELS,
  TASK_KIND_LABELS,
  taskStatusToken,
  verdictToken,
} from "../display";
import { StatusBadge } from "./Badges";
import { AttemptHistory } from "./AttemptHistory";

interface Props {
  tasks: Task[];
  attemptsByTask: Map<string, AttemptView[]>;
}

export function TaskTree({ tasks, attemptsByTask }: Props) {
  const [expanded, setExpanded] = useState<string | null>(null);
  if (tasks.length === 0) {
    return <p className="muted">子任务尚未建立（父 Agent 尚未完成初始化）。</p>;
  }
  return (
    <div className="task-tree">
      {tasks.map((task) => {
        const attempts = attemptsByTask.get(task.task_id) ?? [];
        const open = expanded === task.task_id;
        return (
          <div key={task.task_id} className="task-card">
            <div className="task-head">
              <div>
                <strong>{TASK_KIND_LABELS[task.task_kind ?? ""] ?? task.task_kind ?? "任务"}</strong>{" "}
                <span className="mono muted">{task.task_id}</span>
              </div>
              <div className="row">
                <StatusBadge token={taskStatusToken(task.status)} />
                <StatusBadge token={verdictToken(task.passed)} />
              </div>
            </div>
            <table className="kv">
              <tbody>
                <tr>
                  <th>角色 / 版本</th>
                  <td className="mono">
                    {AGENT_ROLE_LABELS[task.agent_id ?? ""] ?? task.agent_id ?? "—"}@{task.agent_version ?? "—"}
                  </td>
                </tr>
                <tr>
                  <th>依赖</th>
                  <td className="mono">
                    {task.depends_on.length === 0
                      ? "—"
                      : task.depends_on
                          .map((dep) => `${dep.task_id}（${dep.condition}）`)
                          .join("；")}
                  </td>
                </tr>
                {task.skip_reason && (
                  <tr>
                    <th>跳过原因</th>
                    <td>{SKIP_REASON_LABELS[task.skip_reason] ?? task.skip_reason}</td>
                  </tr>
                )}
                <tr>
                  <th>执行尝试</th>
                  <td>
                    <button type="button" className="link" onClick={() => setExpanded(open ? null : task.task_id)}>
                      {open ? "收起" : `展开全部（${attempts.length}）`}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
            {open && <AttemptHistory attempts={attempts} />}
          </div>
        );
      })}
    </div>
  );
}
