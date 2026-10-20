import { useCallback, useEffect, useRef, useState } from "react";
import {
  getArtifact,
  getReport,
  getTask,
  listArtifacts,
  listAttempts,
  listEvents,
  listFindings,
  taskStreamUrl,
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

export type StreamMode = "idle" | "streaming" | "polling";

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
  updatedAt: number | null;
  streamStatus: StreamMode;
  refresh: () => void;
}

function parseStreamEvent(raw: unknown): ExecutionEvent | null {
  if (typeof raw !== "string") return null;
  try {
    const data = JSON.parse(raw) as ExecutionEvent;
    if (!data || typeof data.event_id !== "string" || typeof data.sequence !== "number") return null;
    return data;
  } catch {
    return null;
  }
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

const POLL_MS = 1500;
const REFRESH_DEBOUNCE_MS = 300;
const FULL_REFRESH_EVENTS = new Set([
  "patch_applied",
  "detection_completed",
  "task_completed",
  "waiting_recovery",
  "terminal_recorded",
]);

async function cachedArtifact<T>(cache: Map<string, unknown>, item: ArtifactView): Promise<T> {
  const hit = cache.get(item.artifact_id);
  if (hit !== undefined) return hit as T;
  const body = await getArtifact<T>(item.artifact_id);
  cache.set(item.artifact_id, body);
  return body;
}

async function loadArtifactBodies(artifacts: ArtifactView[], cache: Map<string, unknown>) {
  const pick = (type: string) => artifacts.filter((item) => item.artifact_type === type);
  const [patches, applications, verifications] = await Promise.all([
    Promise.all(pick("patch").map((item) => cachedArtifact<PatchArtifact>(cache, item))),
    Promise.all(
      pick("patch_application").map((item) => cachedArtifact<PatchApplicationArtifact>(cache, item)),
    ),
    Promise.all(
      pick("verification_report").map((item) => cachedArtifact<VerificationArtifact>(cache, item)),
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
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [streamStatus, setStreamStatus] = useState<StreamMode>("idle");
  const cursor = useRef(0);
  const timer = useRef<number | null>(null);
  const tickRef = useRef<(() => Promise<void>) | null>(null);
  const seen = useRef<Set<string>>(new Set());
  const backoff = useRef(1000);
  const artifactCache = useRef(new Map<string, unknown>());
  const activeRef = useRef(false);
  const lastOk = useRef(0);
  const streamRef = useRef<EventSource | null>(null);
  const streamWanted = useRef(true);

  const stop = useCallback(() => {
    if (timer.current !== null) {
      window.clearTimeout(timer.current);
      timer.current = null;
    }
  }, []);

  const load = useCallback(async (isCurrent: () => boolean): Promise<{ status: string; ok: boolean }> => {
    if (!rootTaskId) return { status: "queued", ok: false };
    try {
      const [nextDetail, nextAttempts, nextArtifacts, nextEvents, nextFindings] = await Promise.all([
        getTask(rootTaskId),
        listAttempts(rootTaskId),
        listArtifacts(rootTaskId),
        listEvents(rootTaskId, cursor.current),
        listFindings(rootTaskId),
      ]);
      if (!isCurrent()) return { status: nextDetail.status, ok: true };
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
        loadArtifactBodies(nextArtifacts, artifactCache.current),
      ]);
      if (!isCurrent()) return { status: nextDetail.status, ok: true };
      setBodies({ findings: nextFindings, findingsCoverage: coverage, ...loaded });
      if (TERMINAL.has(nextDetail.status)) {
        try {
          const nextReport = await getReport(rootTaskId);
          if (!isCurrent()) return { status: nextDetail.status, ok: true };
          setReport(nextReport);
        } catch {
          if (isCurrent()) setReport(null);
        }
      }
      if (!isCurrent()) return { status: nextDetail.status, ok: true };
      setError(null);
      backoff.current = 1000;
      setUpdatedAt(Date.now());
      return { status: nextDetail.status, ok: true };
    } catch (err) {
      if (!isCurrent()) return { status: "running", ok: false };
      setError(err instanceof Error ? err.message : String(err));
      backoff.current = Math.min(backoff.current * 2, 8000);
      return { status: "running", ok: false };
    }
  }, [rootTaskId]);

  useEffect(() => {
    if (!rootTaskId) return;
    cursor.current = 0;
    seen.current = new Set();
    artifactCache.current = new Map();
    backoff.current = 1000;
    lastOk.current = 0;
    activeRef.current = false;
    streamWanted.current = true;
    setEvents([]);
    setReport(null);
    setUpdatedAt(null);
    setStreamStatus("idle");
    setLoading(true);
    let disposed = false;
    let inFlight = false;
    let rerun = false;
    let refreshTimer: number | null = null;

    const closeStream = () => {
      const source = streamRef.current;
      streamRef.current = null;
      source?.close();
    };

    const dropStream = () => {
      streamWanted.current = false;
      closeStream();
      setStreamStatus("polling");
      if (disposed || timer.current !== null) return;
      timer.current = window.setTimeout(() => {
        void tickRef.current?.();
      }, 0);
    };

    const holdStream = (): boolean => {
      if (!streamWanted.current || typeof EventSource === "undefined" || !rootTaskId) return false;
      const current = streamRef.current;
      if (current && current.readyState !== 2 && current.readyState !== EventSource.CLOSED) return true;
      let source: EventSource;
      try {
        source = new EventSource(taskStreamUrl(rootTaskId, cursor.current));
      } catch {
        streamWanted.current = false;
        return false;
      }
      streamRef.current = source;
      source.onmessage = (message) => {
        const event = parseStreamEvent(message.data);
        if (!event) {
          dropStream();
          return;
        }
        if (!seen.current.has(event.event_id)) {
          seen.current.add(event.event_id);
          setEvents((currentEvents) =>
            [...currentEvents, event].sort((a, b) => a.sequence - b.sequence),
          );
        }
        cursor.current = Math.max(cursor.current, event.sequence);
        if (FULL_REFRESH_EVENTS.has(event.event_type)) {
          if (inFlight) rerun = true;
          else scheduleFullRefresh();
        }
      };
      source.onerror = () => {
        if (streamRef.current !== source) return;
        dropStream();
      };
      return true;
    };

    const scheduleFullRefresh = () => {
      if (disposed) return;
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        void tick();
      }, REFRESH_DEBOUNCE_MS);
    };

    const tick = async () => {
      if (disposed) return;
      if (inFlight) {
        rerun = true;
        return;
      }
      inFlight = true;
      stop();
      let active = false;
      let delay = POLL_MS;
      let outcomeOk = false;
      try {
        const outcome = await load(() => !disposed);
        if (disposed) return;
        setLoading(false);
        active = outcome.status === "running" || outcome.status === "queued";
        delay = outcome.ok ? POLL_MS : backoff.current;
        outcomeOk = outcome.ok;
        if (outcome.ok) lastOk.current = Date.now();
        activeRef.current = active;
      } catch {
        active = true;
        delay = backoff.current;
        activeRef.current = true;
      } finally {
        inFlight = false;
        if (disposed) return;
        if (rerun) {
          rerun = false;
          void tick();
          return;
        }
        const streaming = active && outcomeOk && holdStream();
        if (streaming) {
          setStreamStatus("streaming");
        } else if (active) {
          if (!streamWanted.current) closeStream();
          setStreamStatus("polling");
          if (timer.current === null) {
            timer.current = window.setTimeout(() => {
              void tick();
            }, delay);
          }
        } else {
          closeStream();
          setStreamStatus("idle");
        }
      }
    };
    tickRef.current = tick;
    void tick();
    const watchdog = window.setInterval(() => {
      if (disposed || !activeRef.current || inFlight || timer.current !== null) return;
      if (Date.now() - lastOk.current > POLL_MS * 3) void tick();
    }, POLL_MS);
    return () => {
      tickRef.current = null;
      disposed = true;
      streamWanted.current = false;
      if (refreshTimer !== null) {
        window.clearTimeout(refreshTimer);
        refreshTimer = null;
      }
      closeStream();
      window.clearInterval(watchdog);
      stop();
    };
  }, [rootTaskId, load, stop]);

  const refresh = useCallback(() => {
    streamWanted.current = true;
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
    updatedAt,
    streamStatus,
    refresh,
  };
}
