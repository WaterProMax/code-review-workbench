import { useState } from "react";
import { useNavigate } from "react-router-dom";
import type { SourceUploadResponse, SubmitTaskResponse } from "../api/types";
import { GoalForm } from "../components/GoalForm";
import { UploadPanel } from "../components/UploadPanel";

export function SubmitPage() {
  const [source, setSource] = useState<SourceUploadResponse | null>(null);
  const [task, setTask] = useState<SubmitTaskResponse | null>(null);
  const navigate = useNavigate();

  function onSubmitted(submitted: SubmitTaskResponse) {
    setTask(submitted);
    navigate(`/tasks/${submitted.root_task_id}`);
  }

  return (
    <div className="stack">
      <UploadPanel onUploaded={setSource} />
      <GoalForm sourceId={source?.source_id ?? null} onSubmitted={onSubmitted} />
      {task && (
        <div className="ok-box">
          已提交：<span className="mono">{task.root_task_id}</span>，正在跳转到任务详情…
        </div>
      )}
    </div>
  );
}
