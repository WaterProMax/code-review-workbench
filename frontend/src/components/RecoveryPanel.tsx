import { useState } from "react";
import { ApiError, resumeTask, terminateTask } from "../api/client";
import type { BudgetItem, RootStatus, TerminateResponse } from "../api/types";
import { BudgetTable } from "./Badges";

interface Props {
  rootTaskId: string;
  status: RootStatus;
  revision: number;
  requiredAction: string | null;
  budgets: BudgetItem[];
  onChanged: () => void;
}

const RESUMABLE: RootStatus[] = ["waiting_recovery", "interrupted"];

export function RecoveryPanel({
  rootTaskId,
  status,
  revision,
  requiredAction,
  budgets,
  onChanged,
}: Props) {
  const resumable = RESUMABLE.includes(status);
  const [reason, setReason] = useState("追加执行许可后恢复原任务");
  const [repairRounds, setRepairRounds] = useState(1);
  const [retries, setRetries] = useState<Record<string, number>>({ review: 0, fix: 0, verify: 0 });
  const [evidenceRetries, setEvidenceRetries] = useState(0);
  const [terminateReason, setTerminateReason] = useState("用户确认终止该任务");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [terminated, setTerminated] = useState<TerminateResponse | null>(null);

  async function resume() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const outcome = await resumeTask(rootTaskId, {
        expected_revision: revision,
        additional_repair_rounds: repairRounds,
        additional_execution_retries: retries,
        additional_evidence_retries: evidenceRetries,
        reason: reason.trim() || "恢复原任务",
      });
      setMessage(`${outcome.message}（run_segment=${outcome.run_segment_id}）`);
      onChanged();
    } catch (err) {
      setError(
        err instanceof ApiError
          ? `${err.code}: ${err.message}${err.status === 409 ? "（请刷新后重试）" : ""}`
          : String(err),
      );
    } finally {
      setBusy(false);
    }
  }

  async function terminate() {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const outcome = await terminateTask(rootTaskId, {
        expected_revision: revision,
        reason: terminateReason.trim() || "用户终止",
      });
      setTerminated(outcome);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <h2>恢复与终止</h2>
      <h3>额度</h3>
      <BudgetTable items={budgets} />
      {!resumable && (
        <p className="muted">
          当前状态为 <span className="mono">{status}</span>，只有等待恢复或已中断的任务可以恢复或终止。
        </p>
      )}
      {resumable && (
        <>
          <div className="warn-box">
            <strong>等待原因：</strong> {requiredAction ?? "存在需要人工处理的问题"}
            <div className="muted">恢复不会替换输入或验收目标；需要补充材料或更换目标时请新建任务。</div>
          </div>
          <div className="filters">
            <label>
              <span>追加自动修复轮数</span>
              <input
                type="number"
                min={0}
                aria-label="追加自动修复轮数"
                value={repairRounds}
                onChange={(event) => setRepairRounds(Number(event.target.value))}
              />
            </label>
            {(["review", "fix", "verify"] as const).map((kind) => (
              <label key={kind}>
                <span>追加 {kind} 故障重试</span>
                <input
                  type="number"
                  min={0}
                  aria-label={`追加 ${kind} 故障重试`}
                  value={retries[kind] ?? 0}
                  onChange={(event) =>
                    setRetries((current) => ({ ...current, [kind]: Number(event.target.value) }))
                  }
                />
              </label>
            ))}
            <label>
              <span>追加补证据重试</span>
              <input
                type="number"
                min={0}
                aria-label="追加补证据重试"
                value={evidenceRetries}
                onChange={(event) => setEvidenceRetries(Number(event.target.value))}
              />
            </label>
          </div>
          <label className="field">
            <span>恢复原因</span>
            <input
              aria-label="恢复原因"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
          <div className="actions">
            <button type="button" onClick={resume} disabled={busy}>
              {busy ? "处理中…" : "请求恢复"}
            </button>
          </div>
          <label className="field">
            <span>终止原因</span>
            <input
              aria-label="终止原因"
              value={terminateReason}
              onChange={(event) => setTerminateReason(event.target.value)}
            />
          </label>
          <div className="actions">
            <button type="button" className="danger" onClick={terminate} disabled={busy}>
              确认终止（保留已有结论）
            </button>
          </div>
        </>
      )}
      {message && <div className="ok-box">{message}</div>}
      {error && <div className="error-box">{error}</div>}
      {terminated && (
        <div className="ok-box">
          已终止：最终状态 <span className="mono">{terminated.status}</span>，
          结论 {terminated.passed === null ? "尚无结论" : terminated.passed ? "通过" : "不通过"}；
          跳过 {terminated.skipped_tasks.length} 个子任务。
        </div>
      )}
    </section>
  );
}
