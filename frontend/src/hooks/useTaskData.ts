import { useCallback, useEffect, useRef, useState } from "react";
import {
  getArtifact,
  getReport,
  getTask,
  listArtifacts,
  listAttempts,
  listEvents,
  listFindings,
} from "../api/client";
import type {
  ArtifactView,
  AttemptView,
  ExecutionEvent,
  FindingsArtifact,
  Finding,
  PatchApplicationArtifact,
  PatchArtifact,
  ReportResponse,
  TaskDetailResponse,
  VerificationArtifact,
} from "../api/types";

const TERMINAL = new Set(["completed", "partial", "failed"]);

export interface TaskData {
  detail: TaskDetailResponse | null;
  events: ExecutionEvent[];
  attempts: AttemptView[];
  artifacts: ArtifactView[];
  report: ReportResponse | null;
  findings: Finding[];
  findingsCoverage: string[];
  patches: PatchArtifact[];
  applications: PatchApplicationArtifact[];
  verifications: VerificationArtifact[];
  error: string | null;
  loading: boolean;
  refresh: () => void;
}

/** Only the reviewer's coverage notes live in the artifact; the findings themselves are DB rows. */
async function loadCoverage(artifacts: ArtifactView[]) {
  const bundles = await Promise.all(
    artifacts
      .filter((item) => item.artifact_type === "finding")
      .map((item) => getArtifact<FindingsArtifact>(item.artifact_id)),
  );
  const coverage: string[] = [];
  for (const bundle of bundles) {
    for (const item of bundle?.coverage ?? []) {
      if (!coverage.includes(item)) coverage.push(item);
    }
  }
  return coverage;
}

async function loadArtifactBodies(artifacts: ArtifactView[]) {
  const pick = (type: string) => artifacts.filter((item) => item.artifact_type === type);
  const [patches, applications, verifications] = await Promise.all([
    Promise.all(pick("patch").map((item) => getArtifact<PatchArtifact>(item.artifact_id))),
    Promise.all(
      pick("patch_application").map((item) => getArtifact<PatchApplicationArtifact>(item.artifact_id)),
    ),
    Promise.all(
      pick("verification_report").map((item) => getArtifact<VerificationArtifact>(item.artifact_id)),
    ),
  ]);
  return { patches, applications, verifications };
}

export function useTaskData(rootTaskId: string | undefined): TaskData {
  const [detail, setDetail] = useState<TaskDetailResponse | null>(null);
  const [events, setEvents] = useState<ExecutionEvent[]>([]);
  const [attempts, setAttempts] = useState<AttemptView[]>([]);
  const [artifacts, setArtifacts] = useState<ArtifactView[]>([]);
  const [report, setReport] = useState<ReportResponse | null>(null);
  const [bodies, setBodies] = useState<{
    findings: Finding[];
    findingsCoverage: string[];
    patches: PatchArtifact[];
    applications: PatchApplicationArtifact[];
    verifications: VerificationArtifact[];
  }>({ findings: [], findingsCoverage: [], patches: [], applications: [], verifications: [] });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const cursor = useRef(0);
  const timer = useRef<number | null>(null);
  const tickRef = useRef<(() => Promise<void>) | null>(null);
  const seen = useRef<Set<string>>(new Set());
  const backoff = useRef(1000);

  const stop = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const load = useCallback(async () => {
    if (!rootTaskId) return;
    try {
      const [nextDetail, nextAttempts, nextArtifacts, nextEvents, nextFindings] = await Promise.all([
        getTask(rootTaskId),
        listAttempts(rootTaskId),
        listArtifacts(rootTaskId),
        listEvents(rootTaskId, cursor.current),
        listFindings(rootTaskId),
      ]);
      setDetail(nextDetail);
      setAttempts(nextAttempts);
      setArtifacts(nextArtifacts);
      if (nextEvents.events.length > 0) {
        const fresh = nextEvents.events.filter((event) => !seen.current.has(event.event_id));
        fresh.forEach((event) => seen.current.add(event.event_id));
        if (fresh.length > 0) {
          setEvents((current) => [...current, ...fresh].sort((a, b) => a.sequence - b.sequence));
        }
      }
      cursor.current = Math.max(cursor.current, nextEvents.next_seq);
      const [coverage, loaded] = await Promise.all([
        loadCoverage(nextArtifacts),
        loadArtifactBodies(nextArtifacts),
      ]);
      setBodies({ findings: nextFindings, findingsCoverage: coverage, ...loaded });
      if (TERMINAL.has(nextDetail.status)) {
        try {
          setReport(await getReport(rootTaskId));
        } catch {
          setReport(null);
        }
      }
      setError(null);
      backoff.current = 1000;
      return nextDetail.status;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      backoff.current = Math.min(backoff.current * 2, 8000);
      return "running";
    }
  }, [rootTaskId]);

  useEffect(() => {
    if (!rootTaskId) return;
    cursor.current = 0;
    seen.current = new Set();
    setEvents([]);
    setReport(null);
    setLoading(true);
    let disposed = false;

    const tick = async () => {
      if (disposed) return;
      const status = await load();
      if (disposed) return;
      setLoading(false);
      const active = status === "running" || status === "queued";
      if (active) {
        // poll only while the task can still change; a finished task stops here
        timer.current = window.setTimeout(tick, 1500);
      }
    };
    tickRef.current = tick;
    void tick();
    return () => {
      tickRef.current = null;
      disposed = true;
      stop();
    };
  }, [rootTaskId, load, stop]);

  const refresh = useCallback(() => {
    stop();
    void tickRef.current?.();
  }, [stop]);

  return {
    detail,
    events,
    attempts,
    artifacts,
    report,
    ...bodies,
    error,
    loading,
    refresh,
  };
}
