import { useMemo, useState } from "react";
import type { ExecutionEvent } from "../api/types";
import { formatDuration, formatTime, shortId, summarizePayload } from "../display";
import { eventTypeLabel, roleLabel, roleLabels } from "../i18n/zh";

const ACTORS = ["parent", "reviewer", "fixer", "verifier", "test_generator", "controller", "tool", "task_service", "system"];

interface Props {
  events: ExecutionEvent[];
  onOpenArtifact: (artifactId: string) => void;
}

export function EventTimeline({ events, onOpenArtifact }: Props) {
  const [actor, setActor] = useState<string>("");
  const [eventType, setEventType] = useState<string>("");
  const [tailOnly, setTailOnly] = useState(true);
  const [newestFirst, setNewestFirst] = useState(true);

  const types = useMemo(
    () => Array.from(new Set(events.map((event) => event.event_type))).sort(),
    [events],
  );

  const filtered = events.filter(
    (event) =>
      (!actor || event.actor_id === actor) && (!eventType || event.event_type === eventType),
  );
  const ordered = [...filtered].sort((a, b) => (newestFirst ? b.sequence - a.sequence : a.sequence - b.sequence));
  const visible = tailOnly ? ordered.slice(0, 60) : ordered;

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
                {roleLabels[item] ?? roleLabel(item)}
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
                {eventTypeLabel(item)}
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
        <label className="checkbox">
          <input
            type="checkbox"
            checked={newestFirst}
            onChange={(event) => setNewestFirst(event.target.checked)}
          />
          <span>最新在前</span>
        </label>
        <span className="muted">共 {events.length} 条事件</span>
      </div>
      <div className="timeline-scroll">
        <table className="grid event-timeline">
          <colgroup>
            <col className="col-seq" />
            <col className="col-time" />
            <col className="col-role" />
            <col className="col-event" />
            <col className="col-ids" />
            <col className="col-duration" />
            <col className="col-summary" />
            <col className="col-artifacts" />
          </colgroup>
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
            {visible.map((event) => {
              const ids = [event.task_id, event.attempt_id, event.dispatch_batch_id].filter(
                (id): id is string => Boolean(id),
              );
              return (
                <tr key={event.event_id}>
                  <td className="mono">{event.sequence}</td>
                  <td className="mono">{formatTime(event.timestamp)}</td>
                  <td title={event.actor_id}>{roleLabel(event.actor_id)}</td>
                  <td>{eventTypeLabel(event.event_type)}</td>
                  <td className="mono">
                    {ids.length === 0
                      ? "—"
                      : ids.map((id, index) => (
                          <span key={id} title={id}>
                            {index > 0 ? " / " : ""}
                            {shortId(id)}
                          </span>
                        ))}
                  </td>
                  <td className="mono">{formatDuration(event.duration_ms)}</td>
                  <td className="summary-cell">
                    <div>{summarizePayload(event.payload, event.event_type)}</div>
                    <details className="developer-view">
                      <summary>开发者视图</summary>
                      <pre className="artifact">{JSON.stringify(event.payload, null, 2)}</pre>
                    </details>
                  </td>
                  <td>
                    {event.artifact_refs.length === 0
                      ? "—"
                      : Array.from(new Set(event.artifact_refs)).map((ref) => (
                          <button key={ref} type="button" className="link" onClick={() => onOpenArtifact(ref)}>
                            {shortId(ref)}
                          </button>
                        ))}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
