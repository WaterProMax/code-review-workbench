import { useMemo, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { ApiError, cancelTask, getArtifact, getTask } from "../api/client";
import type { StreamMode } from "../hooks/useTaskData";
import { ArtifactViewer } from "../components/ArtifactViewer";
import { DiffViewer } from "../components/DiffViewer";
import { DownloadGroups } from "../components/ReportPanel";
import { EventTimeline } from "../components/EventTimeline";
import { FindingList } from "../components/FindingList";
import { ParentDecisionPanel } from "../components/ParentDecisionPanel";
import { RecoveryPanel } from "../components/RecoveryPanel";
import { ReportPanel } from "../components/ReportPanel";
import { StepBar } from "../components/StepBar";
import { TaskSummary } from "../components/TaskSummary";
import { TaskTree } from "../components/TaskTree";
import { VerificationPanel } from "../components/VerificationPanel";
import { useTaskData } from "../hooks/useTaskData";
import { inferWorkflowStep } from "../steps";

const TABS = [
  { id: "overview", label: "概览" },
  { id: "findings", label: "问题与补丁" },
  { id: "verification", label: "验证" },
  { id: "timeline", label: "时间线" },
  { id: "artifacts", label: "产物与下载" },
  { id: "recovery", label: "恢复" },
] as const;

type TabId = (typeof TABS)[number]["id"];

function streamChannelLabel(status: string, mode: StreamMode): string {
  if (mode === "streaming") return "实时连接";
  if (mode === "polling") return "轮询";
  if (status === "running" || status === "queued") return "连接中";
  return "已停止";
}

function activeTab(value: string | null): TabId {
  return TABS.some((tab) => tab.id === value) ? (value as TabId) : "overview";
}

export function TaskDetailPage() {
  const { rootTaskId } = useParams<{ rootTaskId: string }>();
  const [params] = useSearchParams();
  const tab = activeTab(params.get("tab"));
  const data = useTaskData(rootTaskId);
  const [inlineArtifact, setInlineArtifact] = useState<{ id: string; body: unknown } | null>(null);
  const [artifactError, setArtifactError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);

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

  const step = useMemo(() => {
    if (!data.detail) return null;
    return inferWorkflowStep(
      data.detail,
      data.events,
      data.applications.some((item) => item.status === "prepared"),
    );
  }, [data.detail, data.events, data.applications]);

  async function onCancel() {
    if (!rootTaskId || !data.detail) return;
    setCancelling(true);
    setCancelError(null);
    const reason = "用户取消运行中的任务";
    try {
      try {
        await cancelTask(rootTaskId, { expected_revision: data.detail.revision, reason });
      } catch (err) {
        if (!(err instanceof ApiError) || err.code !== "VERSION_CONFLICT") throw err;
        const fresh = await getTask(rootTaskId);
        if (fresh.status === "running" || fresh.status === "queued") {
          await cancelTask(rootTaskId, { expected_revision: fresh.revision, reason });
        }
      }
      data.refresh();
    } catch (err) {
      setCancelError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
      data.refresh();
    } finally {
      setCancelling(false);
    }
  }

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
          <span className="muted mono">这个任务的地址：/tasks/{rootTaskId}</span>
        </div>
        {data.error && <div className="error-box">查询失败：{data.error}</div>}
        {data.loading && !data.detail && <p className="muted">正在加载任务…</p>}
        {data.detail && (
          <div className="actions">
            <span className="muted" data-testid="stream-status">
              事件通道：{streamChannelLabel(data.detail.status, data.streamStatus)}
            </span>
            {(data.detail.status === "running" || data.detail.status === "queued") && (
              <button type="button" onClick={() => void onCancel()} disabled={cancelling}>
                {cancelling ? "取消中…" : "取消"}
              </button>
            )}
          </div>
        )}
        {cancelError && <div className="error-box">{cancelError}</div>}
      </div>

      {data.detail && (
        <>
          <nav className="tabs" role="tablist" aria-label="任务详情">
            {TABS.map((item) => (
              <Link
                key={item.id}
                role="tab"
                aria-selected={tab === item.id}
                className={tab === item.id ? "active" : undefined}
                to={`/tasks/${rootTaskId}?tab=${item.id}`}
              >
                {item.label}
              </Link>
            ))}
          </nav>

          {tab === "overview" && (
            <>
              <TaskSummary detail={data.detail} updatedAt={data.updatedAt} />
              {step && <StepBar step={step.step} waiting={step.waiting} />}
              <ParentDecisionPanel
                detection={data.detail.detection}
                requiredAction={data.detail.required_action}
                latestDecision={latestDecision}
              />
              <section className="panel">
                <h2>任务树与尝试历史</h2>
                <TaskTree tasks={data.detail.tasks} attemptsByTask={attemptsByTask} />
              </section>
              {data.report && (
                <section className="panel">
                  <h2>结论</h2>
                  <p>{data.report.conclusion_scope}</p>
                </section>
              )}
              <section className="panel">
                <h2>主要下载</h2>
                <DownloadGroups
                  rootTaskId={rootTaskId}
                  sourceVersion={data.detail.source_version}
                  report={data.report}
                  artifacts={data.artifacts}
                  patches={data.patches}
                  variant="primary"
                />
              </section>
            </>
          )}

          {tab === "findings" && (
            <section className="panel">
              <h2>问题与差异</h2>
              <h3>问题列表</h3>
              <FindingList findings={data.findings} />
              <h3>补丁与差异</h3>
              <DiffViewer patches={data.patches} applications={data.applications} />
            </section>
          )}

          {tab === "verification" && (
            <section className="panel">
              <h2>验证</h2>
              <VerificationPanel verifications={data.verifications} checks={data.detail.checks} />
            </section>
          )}

          {tab === "timeline" && <EventTimeline events={data.events} onOpenArtifact={openArtifact} />}

          {tab === "artifacts" && (
            <>
              <ReportPanel
                rootTaskId={rootTaskId}
                sourceVersion={data.detail.source_version}
                report={data.report}
                artifacts={data.artifacts}
                patches={data.patches}
              />
              <section className="panel">
                <h2>产物</h2>
                {artifactError && <div className="error-box">{artifactError}</div>}
                <ArtifactViewer artifacts={data.artifacts} onOpen={openArtifact} />
                {inlineArtifact && (
                  <div>
                    <h3>
                      产物内容 <span className="mono">{inlineArtifact.id}</span>
                    </h3>
                    <details className="developer-view">
                      <summary>开发者视图</summary>
                      <pre className="artifact">{JSON.stringify(inlineArtifact.body, null, 2)}</pre>
                    </details>
                  </div>
                )}
              </section>
            </>
          )}

          {tab === "recovery" && (
            <RecoveryPanel
              rootTaskId={rootTaskId}
              status={data.detail.status}
              revision={data.detail.revision}
              requiredAction={data.detail.required_action}
              budgets={data.detail.budgets.items}
              onChanged={data.refresh}
            />
          )}
        </>
      )}
    </div>
  );
}
