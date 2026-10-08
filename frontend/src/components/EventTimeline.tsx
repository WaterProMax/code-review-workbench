import { useMemo, useState } from "react";
import type { ExecutionEvent } from "../api/types";
import { formatDuration, formatTime, summarizePayload } from "../display";

const EVENT_LABELS: Record<string, string> = {
  task_created: "建立任务",
  task_dispatched: "派发子任务",
  attempt_started: "开始尝试",
  tool_started: "工具开始",
  tool_finished: "工具结束",
  result_received: "收到回报",
  detection_completed: "检测完成",
  retry_scheduled: "安排重试",
  task_skipped: "跳过子任务",
  waiting_recovery: "进入待恢复",
  task_completed: "任务完成",
  task_interrupted: "任务中断",
  task_resumed: "任务恢复",
  patch_applied: "补丁已应用",
  patch_application_failed: "补丁应用失败",
  budget_granted: "追加额度",
  budget_consumed: "消耗额度",
  parent_decided: "父 Agent 决策",
  action_rejected: "动作被拒",
  terminal_recorded: "记录终态",
  late_result_audit: "迟到回报审计",
  check_recorded: "记录检查结果",
};

const ACTORS = ["parent", "reviewer", "fixer", "verifier", "controller", "tool", "task_service", "system"];

interface Props {
  events: ExecutionEvent[];
  onOpenArtifact: (artifactId: string) => void;
}

export function EventTimeline({ events, onOpenArtifact }: Props) {
  const [actor, setActor] = useState<string>("");
  const [eventType, setEventType] = useState<string>("");
  const [tailOnly, setTailOnly] = useState(true);

  const types = useMemo(
    () => Array.from(new Set(events.map((event) => event.event_type))).sort(),
    [events],
  );

  const filtered = events.filter(
    (event) =>
      (!actor || event.actor_id === actor) && (!eventType || event.event_type === eventType),
  );
  const visible = tailOnly ? filtered.slice(-60).reverse() : filtered;

  return (
    <section className="panel">
      <h2>执行时间线</h2>
      <div className="filters">
        <label>
          <span>角色</span>
          <select aria-label="按角色筛选" value={actor} onChange={(event) => setActor(event.target.value)}>
            <option value="">全部</option>
            {ACTORS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>事件类型</span>
          <select
            aria-label="按事件类型筛选"
            value={eventType}
            onChange={(event) => setEventType(event.target.value)}
          >
            <option value="">全部</option>
            {types.map((item) => (
              <option key={item} value={item}>
                {EVENT_LABELS[item] ?? item}
              </option>
            ))}
          </select>
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={tailOnly}
            onChange={(event) => setTailOnly(event.target.checked)}
          />
          <span>只看最近 60 条</span>
        </label>
        <span className="muted">共 {events.length} 条事件</span>
      </div>
      <table className="grid">
        <thead>
          <tr>
            <th>序号</th>
            <th>时间</th>
            <th>角色</th>
            <th>事件</th>
            <th>任务 / 尝试 / 批次</th>
            <th>耗时</th>
            <th>摘要</th>
            <th>产物</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((event) => (
            <tr key={event.event_id}>
              <td className="mono">{event.sequence}</td>
              <td className="mono">{formatTime(event.timestamp)}</td>
              <td className="mono">{event.actor_id}</td>
              <td>{EVENT_LABELS[event.event_type] ?? event.event_type}</td>
              <td className="mono">
                {[event.task_id, event.attempt_id, event.dispatch_batch_id]
                  .filter(Boolean)
                  .join(" / ") || "—"}
              </td>
              <td className="mono">{formatDuration(event.duration_ms)}</td>
              <td>{summarizePayload(event.payload)}</td>
              <td>
                {event.artifact_refs.length === 0
                  ? "—"
                  : Array.from(new Set(event.artifact_refs)).map((ref) => (
                      <button key={ref} type="button" className="link" onClick={() => onOpenArtifact(ref)}>
                        {ref}
                      </button>
                    ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
