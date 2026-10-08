import { useEffect, useState } from "react";
import { Link, NavLink, Route, Routes } from "react-router-dom";
import "@xyflow/react/dist/style.css";
import { getHealth } from "./api/client";
import type { HealthResponse } from "./api/types";
import { TaskDetailPage } from "./pages/TaskDetailPage";
import { TaskListPage } from "./pages/TaskListPage";
import { SubmitPage } from "./pages/SubmitPage";
import { OrchestrationPage } from "./pages/OrchestrationPage";

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch((err: Error) => setError(err.message));
  }, []);

  return (
    <div className="layout">
      <aside className="sidebar">
        <h1>代码审查与修复工作台</h1>
        <nav>
          <NavLink to="/" end className={({ isActive }) => (isActive ? "active" : "")}>
            提交任务
          </NavLink>
          <NavLink to="/tasks" className={({ isActive }) => (isActive ? "active" : "")}>
            历史任务
          </NavLink>
          <NavLink to="/workflows" className={({ isActive }) => (isActive ? "active" : "")}>
            编排配置
          </NavLink>
        </nav>
        <div className="backend-status">
          {error && <div className="error-box">后端不可用：{error}</div>}
          {!error && !health && <p className="muted">正在检查后端…</p>}
          {health && (
            <>
              <div>
                后端 <span className="badge ok">{health.status}</span>
              </div>
              <div className="muted mono">
                {health.model_provider} / {health.model_name}
              </div>
              <div>
                模型配置{" "}
                {health.model_configured ? (
                  <span className="badge ok">已配置</span>
                ) : (
                  <span className="badge null">未配置</span>
                )}
              </div>
              {!health.model_configured && (
                <p className="muted">
                  未配置模型凭据时仍可浏览已有任务；提交新任务会返回明确的配置错误，不会用模拟结果冒充真实执行。
                </p>
              )}
            </>
          )}
          <Link className="mono muted" to="/tasks">
            查看已有任务
          </Link>
        </div>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<SubmitPage />} />
          <Route path="/tasks" element={<TaskListPage />} />
          <Route path="/tasks/:rootTaskId" element={<TaskDetailPage />} />
          <Route path="/workflows" element={<OrchestrationPage />} />
          <Route path="*" element={<p className="error-box">未知页面</p>} />
        </Routes>
      </main>
    </div>
  );
}
