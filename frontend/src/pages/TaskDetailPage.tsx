import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getArtifact } from "../api/client";
import { ArtifactViewer } from "../components/ArtifactViewer";
import { DiffViewer } from "../components/DiffViewer";
import { EventTimeline } from "../components/EventTimeline";
import { FindingList } from "../components/FindingList";
import { ParentDecisionPanel } from "../components/ParentDecisionPanel";
import { RecoveryPanel } from "../components/RecoveryPanel";
import { ReportPanel } from "../components/ReportPanel";
import { TaskSummary } from "../components/TaskSummary";
import { TaskTree } from "../components/TaskTree";
import { VerificationPanel } from "../components/VerificationPanel";
import { useTaskData } from "../hooks/useTaskData";

export function TaskDetailPage() {
  const { rootTaskId } = useParams<{ rootTaskId: string }>();
  const data = useTaskData(rootTaskId);
  const [inlineArtifact, setInlineArtifact] = useState<{ id: string; body: unknown } | null>(null);
  const [artifactError, setArtifactError] = useState<string | null>(null);

  const attemptsByTask = useMemo(() => {
    const map = new Map<string, typeof data.attempts>();
    for (const attempt of data.attempts) {
      const list = map.get(attempt.task_id) ?? [];
      list.push(attempt);
      map.set(attempt.task_id, list);
    }
    return map;
  }, [data.attempts]);

  const latestDecision = useMemo(() => {
    const decisions = data.events.filter((event) => event.event_type === "parent_decided");
    return decisions.length > 0 ? decisions[decisions.length - 1] : null;
  }, [data.events]);

  async function openArtifact(artifactId: string) {
    setArtifactError(null);
    try {
      setInlineArtifact({ id: artifactId, body: await getArtifact<unknown>(artifactId) });
    } catch (err) {
      setArtifactError(err instanceof Error ? err.message : String(err));
    }
  }

  if (!rootTaskId) {
    return <p className="error-box">URL 缺少总任务 ID。</p>;
  }

  return (
    <div className="stack">
      <div className="panel">
        <div className="panel-head">
          <Link to="/tasks">← 任务列表</Link>
          <span className="muted mono">URL 固化总任务：/tasks/{rootTaskId}</span>
        </div>
        {data.error && <div className="error-box">查询失败：{data.error}</div>}
        {data.loading && !data.detail && <p className="muted">正在加载任务…</p>}
      </div>

      {data.detail && (
        <>
          <TaskSummary detail={data.detail} />
          <section className="panel">
            <h2>任务树与尝试历史</h2>
            <TaskTree tasks={data.detail.tasks} attemptsByTask={attemptsByTask} />
          </section>
          <ParentDecisionPanel
            detection={data.detail.detection}
            requiredAction={data.detail.required_action}
            latestDecision={latestDecision}
          />
          <section className="panel">
            <h2>问题与差异</h2>
            <h3>问题列表</h3>
            <FindingList findings={data.findings} />
            <h3>补丁与差异</h3>
            <DiffViewer patches={data.patches} applications={data.applications} />
          </section>
          <section className="panel">
            <h2>验证</h2>
            <VerificationPanel verifications={data.verifications} checks={data.detail.checks} />
          </section>
          <RecoveryPanel
            rootTaskId={rootTaskId}
            status={data.detail.status}
            revision={data.detail.revision}
            requiredAction={data.detail.required_action}
            budgets={data.detail.budgets.items}
            onChanged={data.refresh}
          />
          <ReportPanel report={data.report} artifacts={data.artifacts} />
          <EventTimeline events={data.events} onOpenArtifact={openArtifact} />
          <section className="panel">
            <h2>产物</h2>
            {artifactError && <div className="error-box">{artifactError}</div>}
            <ArtifactViewer artifacts={data.artifacts} onOpen={openArtifact} />
            {inlineArtifact && (
              <div>
                <h3>
                  产物内容 <span className="mono">{inlineArtifact.id}</span>
                </h3>
                <pre className="artifact">{JSON.stringify(inlineArtifact.body, null, 2)}</pre>
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
