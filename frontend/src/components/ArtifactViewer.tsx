import type { ArtifactView } from "../api/types";
import { formatBytes, formatTime } from "../display";

interface Props {
  artifacts: ArtifactView[];
  onOpen: (artifactId: string) => void;
}

export function ArtifactViewer({ artifacts, onOpen }: Props) {
  if (artifacts.length === 0) {
    return <p className="muted">还没有产物。</p>;
  }
  return (
    <table className="grid">
      <thead>
        <tr>
          <th>产物</th>
          <th>类型</th>
          <th>代码版本</th>
          <th>生产者尝试</th>
          <th>大小</th>
          <th>创建时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        {artifacts.map((artifact) => (
          <tr key={artifact.artifact_id}>
            <td className="mono">{artifact.artifact_id}</td>
            <td className="mono">{artifact.artifact_type}</td>
            <td className="mono">{artifact.source_version ?? "—"}</td>
            <td className="mono">{artifact.producer_attempt_id ?? "—"}</td>
            <td className="mono">{formatBytes(artifact.size)}</td>
            <td className="mono">{formatTime(artifact.created_at)}</td>
            <td>
              <button type="button" className="link" onClick={() => onOpen(artifact.artifact_id)}>
                查看
              </button>{" "}
              <a href={artifact.download_url} target="_blank" rel="noreferrer">
                下载
              </a>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
