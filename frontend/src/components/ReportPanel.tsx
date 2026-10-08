import type { ArtifactView, FinalReportArtifact, ReportResponse } from "../api/types";
import { checkStatusToken, verdictToken } from "../display";
import { StatusBadge } from "./Badges";

interface Props {
  report: ReportResponse | null;
  artifacts: ArtifactView[];
}

export function ReportPanel({ report, artifacts }: Props) {
  if (!report) {
    return (
      <section className="panel">
        <h2>最终报告</h2>
        <p className="muted">任务尚未生成最终报告。</p>
      </section>
    );
  }
  const body = report.report as unknown as FinalReportArtifact;
  const patchArtifacts = artifacts.filter((item) => item.artifact_type === "patch");
  const logs = artifacts.filter((item) => item.artifact_type === "detection" || item.artifact_type === "evidence");
  return (
    <section className="panel">
      <h2>最终报告</h2>
      <table className="kv">
        <tbody>
          <tr>
            <th>最终状态</th>
            <td className="mono">{report.final_status}</td>
          </tr>
          <tr>
            <th>检查结论</th>
            <td>
              <StatusBadge token={verdictToken(report.passed)} />
            </td>
          </tr>
          <tr>
            <th>结论范围</th>
            <td>{report.conclusion_scope}</td>
          </tr>
          <tr>
            <th>版本</th>
            <td className="mono">
              {(body.versions && Object.entries(body.versions).map(([key, value]) => `${key}=${value}`).join("；")) ||
                "—"}
            </td>
          </tr>
        </tbody>
      </table>

      <h3>逐项检查</h3>
      <table className="grid">
        <thead>
          <tr>
            <th>检查</th>
            <th>目标</th>
            <th>方法</th>
            <th>必需</th>
            <th>状态</th>
            <th>版本</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          {(body.checks ?? []).map((check) => (
            <tr key={check.check_id}>
              <td className="mono">{check.check_id}</td>
              <td>{check.goal_ref}</td>
              <td className="mono">{check.method}</td>
              <td>{check.required ? "必需" : "补充"}</td>
              <td>
                <StatusBadge token={checkStatusToken(check.status)} />
              </td>
              <td className="mono">{check.source_version ?? "—"}</td>
              <td>{check.note ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {(body.unresolved_finding_ids?.length ?? 0) > 0 && (
        <div className="error-box">剩余未解决问题：{body.unresolved_finding_ids.join(", ")}</div>
      )}
      {(body.not_run_items?.length ?? 0) > 0 && (
        <div className="warn-box">未执行项：{body.not_run_items.join(", ")}</div>
      )}
      {(body.skipped_tasks?.length ?? 0) > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th>跳过子任务</th>
              <th>原因</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {body.skipped_tasks.map((task) => (
              <tr key={task.task_id}>
                <td className="mono">{task.task_id}</td>
                <td className="mono">{task.skip_reason}</td>
                <td>{task.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>下载与导出</h3>
      <ul className="downloads">
        {report.report_artifact_id && (
          <li>
            <a href={`/api/artifacts/${report.report_artifact_id}`} target="_blank" rel="noreferrer">
              报告 JSON
            </a>
          </li>
        )}
        {report.markdown && (
          <li>
            <a href={`/api/artifacts/${report.report_artifact_id}`} target="_blank" rel="noreferrer">
              Markdown 报告
            </a>
          </li>
        )}
        {patchArtifacts.map((artifact) => (
          <li key={artifact.artifact_id}>
            <a href={artifact.download_url} target="_blank" rel="noreferrer">
              补丁 {artifact.artifact_id}
            </a>
          </li>
        ))}
        {logs.map((artifact) => (
          <li key={artifact.artifact_id}>
            <a href={artifact.download_url} target="_blank" rel="noreferrer">
              {artifact.artifact_type} {artifact.artifact_id}
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}
