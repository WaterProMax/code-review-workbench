import type { CheckSummary, VerificationArtifact } from "../api/types";
import { checkStatusToken, verdictToken } from "../display";
import { StatusBadge } from "./Badges";

interface Props {
  verifications: VerificationArtifact[];
  checks: CheckSummary[];
}

export function VerificationPanel({ verifications, checks }: Props) {
  return (
    <div>
      <h3>合同检查项（当前版本）</h3>
      <table className="grid">
        <thead>
          <tr>
            <th>检查</th>
            <th>目标</th>
            <th>方法</th>
            <th>必需</th>
            <th>状态</th>
            <th>说明</th>
            <th>证据</th>
          </tr>
        </thead>
        <tbody>
          {checks.map((check) => (
            <tr key={check.check_id}>
              <td className="mono">{check.check_id}</td>
              <td>{check.goal_ref}</td>
              <td className="mono">{check.method}</td>
              <td>{check.required ? "必需" : "补充"}</td>
              <td>
                <StatusBadge token={checkStatusToken(check.status)} />
              </td>
              <td>{check.note ?? "—"}</td>
              <td className="mono">{check.latest_evidence_ref ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h3>验证报告</h3>
      {verifications.length === 0 ? (
        <p className="muted">还没有验证报告（首次审查通过时修复与验证会被跳过）。</p>
      ) : (
        verifications.map((report) => (
          <div key={report.verification_id} className="task-card">
            <div className="task-head">
              <div>
                <strong className="mono">{report.verification_id}</strong>{" "}
                <span className="mono muted">{report.source_version}</span>
              </div>
              <StatusBadge token={verdictToken(report.passed)} />
            </div>
            <table className="kv">
              <tbody>
                <tr>
                  <th>目标问题</th>
                  <td className="mono">{report.target_finding_ids.join(", ") || "—"}</td>
                </tr>
                <tr>
                  <th>覆盖检查</th>
                  <td className="mono">{report.coverage.join(", ") || "—"}</td>
                </tr>
                <tr>
                  <th>未执行项</th>
                  <td className="mono">{report.not_run.join(", ") || "—"}</td>
                </tr>
                <tr>
                  <th>说明</th>
                  <td>{report.notes ?? "—"}</td>
                </tr>
              </tbody>
            </table>
            {report.check_results.length > 0 && (
              <table className="grid">
                <thead>
                  <tr>
                    <th>检查</th>
                    <th>状态</th>
                    <th>用例数</th>
                    <th>通过 / 失败 / 跳过</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {report.check_results.map((result) => (
                    <tr key={result.check_id}>
                      <td className="mono">{result.check_id}</td>
                      <td>
                        <StatusBadge token={checkStatusToken(result.status)} />
                      </td>
                      <td className="mono">{result.test_count ?? "—"}</td>
                      <td className="mono">
                        {[result.passed_count, result.failed_count, result.skipped_count]
                          .map((value) => (value === null || value === undefined ? "—" : value))
                          .join(" / ")}
                      </td>
                      <td>{result.reason ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))
      )}
    </div>
  );
}
