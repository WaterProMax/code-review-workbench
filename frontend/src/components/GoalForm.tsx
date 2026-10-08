import { useEffect, useState } from "react";
import { ApiError, listWorkflows, submitTask } from "../api/client";
import type { SubmitTaskResponse, WorkflowListItem } from "../api/types";

interface Props {
  sourceId: string | null;
  onSubmitted: (task: SubmitTaskResponse) => void;
}

export function GoalForm({ sourceId, onSubmitted }: Props) {
  const [goal, setGoal] = useState("检查可变默认参数与异常处理，发现问题后修复并验证");
  const [workflows, setWorkflows] = useState<WorkflowListItem[]>([]);
  const [version, setVersion] = useState<string>("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listWorkflows()
      .then((items) => {
        setWorkflows(items);
        if (items.length > 0) setVersion((current) => current || items[0].workflow_version);
      })
      .catch(() => setWorkflows([]));
  }, []);

  const selected = workflows.find((item) => item.workflow_version === version);

  async function submit() {
    if (!sourceId) {
      setError("请先上传代码并建立输入清单");
      return;
    }
    if (!version) {
      setError("请选择一个已保存的工作流配置版本");
      return;
    }
    if (!goal.trim()) {
      setError("请填写检查目标");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const task = await submitTask({ source_id: sourceId, goal: goal.trim(), workflow_version: version });
      onSubmitted(task);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <h2>2. 描述目标并提交</h2>
      <label className="field">
        <span>检查目标</span>
        <textarea
          aria-label="检查目标"
          value={goal}
          rows={3}
          onChange={(event) => setGoal(event.target.value)}
        />
      </label>
      <label className="field">
        <span>工作流配置版本</span>
        <select aria-label="工作流配置版本" value={version} onChange={(event) => setVersion(event.target.value)}>
          {workflows.length === 0 && <option value="">（尚无已保存配置）</option>}
          {workflows.map((item) => (
            <option key={item.workflow_version} value={item.workflow_version}>
              {item.workflow_version}（{item.check_mode === "parallel" ? "并行" : "顺序"}）
            </option>
          ))}
        </select>
      </label>
      <div className="field">
        <span>检查模式</span>
        <div className="mono">{selected ? (selected.check_mode === "parallel" ? "sequential / parallel（并行模板）" : "sequential（顺序模板）") : "—"}</div>
      </div>
      <div className="actions">
        <button type="button" onClick={submit} disabled={busy}>
          {busy ? "提交中…" : "提交任务"}
        </button>
      </div>
      {error && <div className="error-box">{error}</div>}
    </section>
  );
}
