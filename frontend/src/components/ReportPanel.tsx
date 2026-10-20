import type { ArtifactView, FinalReportArtifact, PatchArtifact, ReportResponse } from "../api/types";
import { reportMarkdownUrl, sourceZipUrl } from "../api/client";
import { checkStatusToken, formatTime, rootStatusToken, verdictToken } from "../display";
import { artifactTypeLabel, checkMethodLabel, skipReasonLabels } from "../i18n/zh";
import { StatusBadge } from "./Badges";

interface DownloadProps {
  rootTaskId: string;
  sourceVersion: string | null;
  report: ReportResponse | null;
  artifacts: ArtifactView[];
  patches: PatchArtifact[];
  variant?: "primary" | "all";
}

function artifactName(artifact: ArtifactView): string {
  const name = artifact.metadata?.name;
  if (typeof name === "string" && name.trim()) return name;
  return artifactTypeLabel(artifact.artifact_type);
}

function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: "text/x-diff" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function DownloadGroups({
  rootTaskId,
  sourceVersion,
  report,
  artifacts,
  patches,
  variant = "all",
}: DownloadProps) {
  const reportId = report?.report_artifact_id;
  const audit = artifacts.filter(
    (item) => item.artifact_id !== reportId && item.artifact_type !== "patch",
  );
  return (
    <div>
      <div className="download-group">
        <h3>主要</h3>
        <ul className="downloads">
          <li>
            <a href={sourceZipUrl(rootTaskId, sourceVersion)} download>
              修复后代码 zip
            </a>
          </li>
          <li>
            <a href={reportMarkdownUrl(rootTaskId)} download>
              审查报告 Markdown
            </a>
          </li>
          {patches.map((patch) => (
            <li key={patch.patch_id}>
              {patch.diff_text != null ? (
                <button type="button" className="link" onClick={() => downloadText(`${patch.patch_id}.diff`, patch.diff_text ?? "")}>
                  补丁 {patch.patch_id}.diff
                </button>
              ) : (
                <span className="muted">补丁 {patch.patch_id}（无差异文本）</span>
              )}
            </li>
          ))}
        </ul>
      </div>
      {variant === "all" && (
        <>
          <div className="download-group">
            <h3>报告数据</h3>
            <ul className="downloads">
              {reportId ? (
                <li>
                  <a href={`/api/artifacts/${reportId}`} download>
                    报告 JSON
                  </a>
                </li>
              ) : (
                <li className="muted">还没有报告 JSON</li>
              )}
            </ul>
          </div>
          <div className="download-group">
            <h3>审计证据</h3>
            <details>
              <summary>审计证据（{audit.length}）</summary>
              {audit.length === 0 ? (
                <p className="muted">没有审计证据。</p>
              ) : (
                <ul className="downloads">
                  {audit.map((artifact) => (
                    <li key={artifact.artifact_id}>
                      <a href={artifact.download_url} download title={artifact.artifact_id}>
                        {artifactName(artifact)}
                      </a>
                      <span className="muted">
                        {" "}
                        {artifact.source_version ?? "—"} · {formatTime(artifact.created_at)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </details>
          </div>
        </>
      )}
    </div>
  );
}

interface Props {
  rootTaskId: string;
  sourceVersion: string | null;
  report: ReportResponse | null;
  artifacts: ArtifactView[];
  patches: PatchArtifact[];
}

export function ReportPanel({ rootTaskId, sourceVersion, report, artifacts, patches }: Props) {
  if (!report) {
    return (
      <section className="panel">
        <h2>最终报告</h2>
        <p className="muted">任务尚未生成最终报告。</p>
        <DownloadGroups
          rootTaskId={rootTaskId}
          sourceVersion={sourceVersion}
          report={report}
          artifacts={artifacts}
          patches={patches}
        />
      </section>
    );
  }
  const body = report.report as unknown as FinalReportArtifact;
  return (
    <section className="panel">
      <h2>最终报告</h2>
      <table className="kv">
        <tbody>
          <tr>
            <th>最终状态</th>
            <td>
              <StatusBadge token={rootStatusToken(report.final_status)} />
            </td>
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
              <td>{checkMethodLabel(check.method)}</td>
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
                <td>{skipReasonLabels[task.skip_reason] ?? task.skip_reason}</td>
                <td>{task.detail}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>下载与导出</h3>
      <DownloadGroups
        rootTaskId={rootTaskId}
        sourceVersion={sourceVersion}
        report={report}
        artifacts={artifacts}
        patches={patches}
      />
    </section>
  );
}
