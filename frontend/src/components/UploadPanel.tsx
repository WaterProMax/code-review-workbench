import { useRef, useState } from "react";
import { ApiError, uploadSources } from "../api/client";
import type { SourceUploadResponse } from "../api/types";
import { formatBytes } from "../display";

interface Props {
  onUploaded: (manifest: SourceUploadResponse) => void;
}

export function UploadPanel({ onUploaded }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [manifest, setManifest] = useState<SourceUploadResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function pick(selected: FileList | null) {
    setError(null);
    setManifest(null);
    setFiles(selected ? Array.from(selected) : []);
  }

  async function upload() {
    if (files.length === 0) {
      setError("请先选择要上传的代码文件");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await uploadSources(files);
      setManifest(result);
      onUploaded(result);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel">
      <h2>1. 上传代码</h2>
      <p className="muted">
        只接受相对路径的文本文件；上传内容会冻结为不可变的首版快照，后续修改只发布新版本。
      </p>
      <input
        ref={inputRef}
        type="file"
        multiple
        aria-label="选择代码文件"
        onChange={(event) => pick(event.target.files)}
      />
      {files.length > 0 && (
        <table className="grid">
          <thead>
            <tr>
              <th>文件</th>
              <th>大小</th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file.name}>
                <td className="mono">{file.name}</td>
                <td className="mono">{formatBytes(file.size)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="actions">
        <button type="button" onClick={upload} disabled={busy}>
          {busy ? "上传中…" : "上传并建立输入清单"}
        </button>
      </div>
      {error && <div className="error-box">{error}</div>}
      {manifest && (
        <div className="ok-box">
          <div>
            输入清单：<span className="mono">{manifest.source_id}</span>（
            {manifest.files.length} 个文件，{formatBytes(manifest.total_bytes)}）
          </div>
          <div className="mono muted">内容摘要 {manifest.content_digest.slice(0, 24)}…</div>
        </div>
      )}
    </section>
  );
}
