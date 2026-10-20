import type { PatchApplicationArtifact, PatchArtifact } from "../api/types";
import { formatTime } from "../display";

interface Props {
  patches: PatchArtifact[];
  applications: PatchApplicationArtifact[];
}

function diffCountLabel(diffText: string): string {
  let added = 0;
  let removed = 0;
  for (const line of diffText.split("\n")) {
    if (line.startsWith("+") && !line.startsWith("+++")) added += 1;
    else if (line.startsWith("-") && !line.startsWith("---")) removed += 1;
  }
  return `+${added} / -${removed}`;
}

function lineKind(line: string): "新增" | "删除" | "差异块" | "上下文" {
  if (line.startsWith("+") && !line.startsWith("+++")) return "新增";
  if (line.startsWith("-") && !line.startsWith("---")) return "删除";
  if (line.startsWith("@@")) return "差异块";
  return "上下文";
}

function lineClass(kind: ReturnType<typeof lineKind>): string {
  if (kind === "新增") return "diff-add";
  if (kind === "删除") return "diff-del";
  if (kind === "差异块") return "diff-hunk";
  return "diff-ctx";
}

export function DiffViewer({ patches, applications }: Props) {
  if (patches.length === 0 && applications.length === 0) {
    return <p className="muted">没有补丁：当前任务不需要修改代码，或修复尚未产出补丁。</p>;
  }
  const appliedIds = new Set(
    applications.filter((app) => app.status === "committed").map((app) => app.patch_id),
  );
  return (
    <div>
      {applications.length > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th>应用记录</th>
              <th>状态</th>
              <th>基础版本</th>
              <th>结果版本</th>
              <th>提交时间</th>
            </tr>
          </thead>
          <tbody>
            {applications.map((app) => (
              <tr key={app.application_id}>
                <td className="mono">{app.application_id}</td>
                <td>
                  {app.status === "committed" ? (
                    <span className="badge ok">已提交</span>
                  ) : app.status === "prepared" ? (
                    <span className="badge waiting">已准备（待提交）</span>
                  ) : (
                    <span className="badge failed">失败</span>
                  )}
                </td>
                <td className="mono">{app.base_version}</td>
                <td className="mono">{app.result_version ?? "—"}</td>
                <td className="mono">{formatTime(app.committed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {patches.map((patch) => (
        <div key={patch.patch_id} className="patch-block">
          <div className="row">
            <strong className="mono">{patch.patch_id}</strong>
            {appliedIds.has(patch.patch_id) ? (
              <span className="badge ok">已应用</span>
            ) : (
              <span className="badge null">未使用</span>
            )}
            <span className="muted mono">基础版本 {patch.base_version}</span>
            {patch.rationale && <span className="muted">{patch.rationale}</span>}
            {patch.diff_text && (
              <span className="diff-counts" aria-label={diffCountLabel(patch.diff_text)}>
                {diffCountLabel(patch.diff_text)}
              </span>
            )}
          </div>
          {patch.diff_text ? (
            <pre className="diff" aria-label={`补丁 ${patch.patch_id}`}>
              {patch.diff_text.split("\n").map((line, index) => {
                const kind = lineKind(line);
                return (
                  <span key={index} className={lineClass(kind)} aria-label={`${kind} ${line}`}>
                    {line || " "}
                  </span>
                );
              })}
            </pre>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>文件</th>
                  <th>替换前</th>
                  <th>替换后</th>
                </tr>
              </thead>
              <tbody>
                {patch.edits.map((edit, index) => (
                  <tr key={index}>
                    <td className="mono">{edit.file_path}</td>
                    <td className="mono">{edit.find}</td>
                    <td className="mono">{edit.replace}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
      <p className="muted">
        “修复完成”只说明补丁已生成；补丁应用成功单独显示，验证通过再说明当前版本满足约定检查。
      </p>
    </div>
  );
}
