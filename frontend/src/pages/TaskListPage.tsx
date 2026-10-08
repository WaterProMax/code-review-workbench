import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listTasks } from "../api/client";
import type { TaskListItem } from "../api/types";
import { TaskList } from "../components/TaskList";

export function TaskListPage() {
  const [items, setItems] = useState<TaskListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await listTasks());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const handle = window.setInterval(load, 5000);
    return () => window.clearInterval(handle);
  }, [load]);

  return (
    <div className="stack">
      <div className="panel">
        <div className="panel-head">
          <h2>历史任务</h2>
          <div className="row">
            <button type="button" onClick={load}>
              刷新
            </button>
            <Link className="button" to="/">
              新建任务
            </Link>
          </div>
        </div>
        {loading && items.length === 0 && <p className="muted">正在加载…</p>}
        {error && <div className="error-box">{error}</div>}
        <TaskList items={items} />
      </div>
    </div>
  );
}
