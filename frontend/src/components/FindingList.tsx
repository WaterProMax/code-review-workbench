import type { Finding } from "../api/types";

export function FindingList({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) {
    return <p className="muted">当前版本没有记录到问题。</p>;
  }
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>问题</th>
          <th>规则</th>
          <th>位置</th>
          <th>严重度</th>
          <th>是否必需</th>
          <th>状态</th>
          <th>关闭证据</th>
        </tr>
      </thead>
      <tbody>
        {findings.map((finding) => (
          <tr key={`${finding.finding_id}-${finding.source_version}`}>
            <td>
              <div>{finding.message}</div>
              <div className="mono muted">{finding.finding_id}</div>
            </td>
            <td className="mono">{finding.rule}</td>
            <td className="mono">
              {finding.file_path}
              {finding.line ? `:${finding.line}` : ""}
            </td>
            <td>{finding.severity}</td>
            <td>{finding.required_for_goal ? "必需" : "补充"}</td>
            <td>
              {finding.status === "resolved" ? (
                <span className="badge ok">已解决</span>
              ) : finding.status === "accepted" ? (
                <span className="badge partial">已接受</span>
              ) : (
                <span className="badge failed">未解决</span>
              )}
            </td>
            <td className="mono">
              {finding.resolution_evidence_refs.length > 0
                ? finding.resolution_evidence_refs.join(", ")
                : "—"}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
