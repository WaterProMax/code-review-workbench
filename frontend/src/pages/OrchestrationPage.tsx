/** Orchestration editor: templates in, a saved immutable version out (§14.1). */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  getWorkflow,
  listAgents,
  listTemplates,
  listWorkflows,
  saveWorkflow,
} from "../api/client";
import type {
  CapabilityListing,
  TemplateListing,
  WorkflowConfig,
  WorkflowListItem,
} from "../api/types";
import { WorkflowCanvas } from "../components/WorkflowCanvas";
import {
  BUDGET_FIELDS,
  defaultLayout,
  edgeKey,
  issuesForNode,
  nodeLabel,
  nodeTypeLabel,
  taskKindLabel,
  validateWorkflow,
} from "../workflow";
import type { Issue } from "../workflow";

export function OrchestrationPage() {
  const [templates, setTemplates] = useState<TemplateListing[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilityListing[]>([]);
  const [saved, setSaved] = useState<WorkflowListItem[]>([]);
  const [config, setConfig] = useState<WorkflowConfig | null>(null);
  const [templateName, setTemplateName] = useState<string>("");
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [versionDraft, setVersionDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const reloadSaved = useCallback(async () => {
    setSaved(await listWorkflows());
  }, []);

  useEffect(() => {
    let disposed = false;
    Promise.all([listTemplates(), listAgents(), listWorkflows()])
      .then(([nextTemplates, nextCapabilities, nextSaved]) => {
        if (disposed) return;
        setTemplates(nextTemplates);
        setCapabilities(nextCapabilities);
        setSaved(nextSaved);
        const first = nextTemplates[0];
        if (first) {
          setTemplateName(first.name);
          setConfig(withLayout(first.config));
        }
      })
      .catch((err: Error) => {
        if (!disposed) setLoadError(err.message);
      });
    return () => {
      disposed = true;
    };
  }, []);

  const issues = useMemo(
    () => (config ? validateWorkflow(config, capabilities) : []),
    [config, capabilities],
  );

  const selectedNode = useMemo(
    () => config?.nodes.find((node) => node.id === selectedNodeId) ?? null,
    [config, selectedNodeId],
  );

  function chooseTemplate(name: string) {
    const template = templates.find((item) => item.name === name);
    if (!template) return;
    setTemplateName(name);
    setConfig(withLayout(template.config));
    setSelectedNodeId(null);
    setNotice(null);
    setError(null);
  }

  async function loadSaved(version: string) {
    if (!version) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const loaded = await getWorkflow(version);
      setConfig(withLayout(loaded));
      setTemplateName("");
      setVersionDraft(loaded.workflow_version ?? "");
      setSelectedNodeId(null);
      setNotice(
        `已加载 ${version}（semantic_hash=${loaded.semantic_hash ?? "—"}）。该版本固定了角色版本与预算，修改后会保存为新版本。`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  function mutate(next: Partial<WorkflowConfig>) {
    setConfig((current) => (current ? { ...current, ...next } : current));
    setNotice(null);
  }

  function setNodeRole(nodeId: string, agentId: string) {
    if (!config) return;
    const capability = capabilities.find((item) => item.agent_id === agentId);
    const version = capability?.version ?? config.agents[agentId] ?? "1.0";
    mutate({
      nodes: config.nodes.map((item) =>
        item.id === nodeId ? { ...item, agent_id: agentId } : item,
      ),
      agents: { ...config.agents, [agentId]: version },
    });
  }

  function setBudget(key: string, value: number) {
    if (!config) return;
    mutate({ budgets: { ...config.budgets, [key]: value } });
  }

  async function save() {
    if (!config) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const payload: WorkflowConfig = {
        ...config,
        workflow_version: versionDraft.trim() || null,
      };
      const response = await saveWorkflow(payload);
      setNotice(
        `已保存 ${response.workflow_version}（semantic_hash=${response.semantic_hash}，模式 ${response.check_mode}）。运行会固定该版本。`,
      );
      const stored = { ...response.config };
      setConfig(withLayout(stored));
      setVersionDraft(response.workflow_version);
      await reloadSaved();
    } catch (err) {
      if (err instanceof ApiError) {
        const field = (err.details?.field ?? err.details?.node_id ?? "") as string;
        setError(`${err.code}: ${err.message}${field ? `（定位：${field}）` : ""}`);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setBusy(false);
    }
  }

  if (loadError) {
    return (
      <div className="panel">
        <h2>编排配置</h2>
        <div className="error-box">无法加载编排数据：{loadError}</div>
      </div>
    );
  }

  if (!config) {
    return (
      <div className="panel">
        <h2>编排配置</h2>
        <p className="muted">正在加载模板与角色目录…</p>
      </div>
    );
  }

  const layout = config.layout;
  const nodeIssues = selectedNodeId ? issuesForNode(issues, selectedNodeId) : [];

  return (
    <div className="orchestration">
      <section className="panel">
        <h2>编排配置</h2>
        <p className="muted">
          画布是受约束的编辑器：结构来自后端提供的模板，角色必须来自已注册目录。
          位置（layout）只影响展示，不参与语义哈希；模式、角色与预算的改变会影响实际执行。
        </p>
        <div className="filters">
          <label>
            <span>模板</span>
            <select
              aria-label="选择模板"
              value={templateName}
              onChange={(event) => chooseTemplate(event.target.value)}
            >
              <option value="">（自定义/已保存）</option>
              {templates.map((template) => (
                <option key={template.name} value={template.name}>
                  {template.name} — {template.description}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>加载已保存版本</span>
            <select
              aria-label="加载已保存版本"
              value=""
              onChange={(event) => void loadSaved(event.target.value)}
            >
              <option value="">（选择）</option>
              {saved.map((item) => (
                <option key={item.workflow_version} value={item.workflow_version}>
                  {item.workflow_version}（{item.check_mode}）
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>检查模式</span>
            <select
              aria-label="检查模式"
              value={config.check_mode}
              onChange={(event) => {
                const next = templates.find(
                  (item) => item.check_mode === event.target.value && item.name !== templateName,
                );
                if (next) {
                  chooseTemplate(next.name);
                } else {
                  setError("请先选择与该模式匹配的模板；模式本身不能在画布上直接改写。");
                }
              }}
            >
              {["sequential", "parallel"].map((mode) => (
                <option key={mode} value={mode}>
                  {mode}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>版本名（留空则使用语义哈希）</span>
            <input
              aria-label="版本名"
              value={versionDraft}
              onChange={(event) => setVersionDraft(event.target.value)}
            />
          </label>
        </div>
        <div className="actions">
          <button type="button" onClick={save} disabled={busy || issues.length > 0}>
            {busy ? "保存中…" : "保存为新版本"}
          </button>
        </div>
        {notice && <div className="ok-box">{notice}</div>}
        {error && <div className="error-box">{error}</div>}
      </section>

      <section className="panel">
        <h3>画布</h3>
        <WorkflowCanvas
          config={config}
          layout={layout}
          issues={issues}
          selectedNodeId={selectedNodeId}
          onSelect={setSelectedNodeId}
          onLayoutChange={(next) => mutate({ layout: next })}
        />
      </section>

      <section className="panel">
        <h3>节点（{config.nodes.length}）</h3>
        <table className="grid">
          <thead>
            <tr>
              <th>节点</th>
              <th>类型</th>
              <th>任务类型</th>
              <th>角色 / 版本</th>
              <th>入边</th>
              <th>出边</th>
            </tr>
          </thead>
          <tbody>
            {config.nodes.map((node) => (
              <tr
                key={node.id}
                className={node.id === selectedNodeId ? "row-selected" : undefined}
                onClick={() => setSelectedNodeId(node.id)}
              >
                <td className="mono">{node.id}</td>
                <td>{nodeTypeLabel(node.type)}</td>
                <td>{taskKindLabel(node.task_kind)}</td>
                <td className="mono">
                  {node.agent_id ? `${node.agent_id}@${config.agents[node.agent_id] ?? "?"}` : "—"}
                </td>
                <td className="mono">
                  {config.edges
                    .filter((edge) => edge.target === node.id)
                    .map((edge) => edge.source)
                    .join(", ") || "—"}
                </td>
                <td className="mono">
                  {config.edges
                    .filter((edge) => edge.source === node.id)
                    .map((edge) => edge.target)
                    .join(", ") || "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <h3>执行连接与输入依赖</h3>
        <p className="muted">
          实线是派发/回报执行边，虚线是输入依赖（数据来源）。子 Agent 之间不能直连，回报一律回到父 Agent。
        </p>
        <table className="grid">
          <thead>
            <tr>
              <th>连接</th>
              <th>方向</th>
              <th>判定</th>
            </tr>
          </thead>
          <tbody>
            {config.edges.map((edge) => {
              const illegal = issues.some(
                (issue) => issue.field === "edges" && issue.message.includes(`${edge.source} → ${edge.target}`),
              );
              return (
                <tr key={edgeKey(edge)}>
                  <td className="mono">
                    {edge.source} → {edge.target}
                  </td>
                  <td>{edge.source === "parent" ? "派发" : "回报"}</td>
                  <td>
                    {illegal ? (
                      <span className="badge failed">非法</span>
                    ) : (
                      <span className="badge ok">合法</span>
                    )}
                  </td>
                </tr>
              );
            })}
            {config.dependencies.map((dep) => (
              <tr key={`dep-${dep.source_node}-${dep.target_node}-${dep.artifact}`}>
                <td className="mono">
                  {dep.source_node} ⇢ {dep.target_node}
                </td>
                <td>输入依赖（{dep.artifact}）</td>
                <td>
                  <span className="badge partial">数据</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h3>节点参数{selectedNode ? `：${nodeLabel(selectedNode)}` : ""}</h3>
        {!selectedNode && <p className="muted">在画布或节点表中选择一个节点。</p>}
        {selectedNode && (
          <>
            {selectedNode.type === "agent" && (
              <label className="field">
                <span>角色（只能选择支持该任务类型的已注册角色）</span>
                <select
                  aria-label="节点角色"
                  value={selectedNode.agent_id ?? ""}
                  onChange={(event) => setNodeRole(selectedNode.id, event.target.value)}
                >
                  {capabilities
                    .filter((capability) =>
                      selectedNode.task_kind
                        ? capability.supported_task_kinds.includes(selectedNode.task_kind)
                        : false,
                    )
                    .map((capability) => (
                      <option key={capability.agent_id} value={capability.agent_id}>
                        {capability.agent_id}@{capability.version} — {capability.description}
                      </option>
                    ))}
                </select>
              </label>
            )}
            {selectedNode.type === "tool" && (
              <p className="mono">
                工具 {selectedNode.tool_name}
                {config.tool_policy_versions?.[selectedNode.tool_name ?? ""]
                  ? `（策略版本 ${config.tool_policy_versions[selectedNode.tool_name ?? ""]}）`
                  : ""}
              </p>
            )}
            {(selectedNode.type === "parent" || selectedNode.type === "end") && (
              <p className="muted">
                {selectedNode.type === "parent"
                  ? "父 Agent 内部固定执行动作校验、回报接收与完成检测，画布不能删除这些逻辑。"
                  : "结束节点只接受父 Agent 的连接，不是绕过完成检测的捷径。"}
              </p>
            )}
            {nodeIssues.length > 0 && (
              <div className="error-box">
                {nodeIssues.map((issue) => (
                  <div key={`${issue.field}-${issue.message}`}>
                    {issue.field}：{issue.message}
                  </div>
                ))}
              </div>
            )}
          </>
        )}

        <h3>预算与执行参数</h3>
        <p className="muted">超出服务端上限的配置会在保存时被拒绝；运行固定保存后的值。</p>
        <div className="filters">
          {BUDGET_FIELDS.map((field) => (
            <label key={field.key}>
              <span>{field.label}</span>
              <input
                type="number"
                aria-label={field.label}
                min={0}
                step={field.step ?? 1}
                value={String(config.budgets[field.key])}
                onChange={(event) => setBudget(field.key, Number(event.target.value))}
              />
            </label>
          ))}
        </div>
      </section>

      <section className="panel">
        <h3>校验结果</h3>
        {issues.length === 0 ? (
          <p className="ok-box">配置通过全部本地校验，可以保存。</p>
        ) : (
          <ul className="issue-list">
            {issues.map((issue: Issue) => (
              <li key={`${issue.field}-${issue.message}`}>
                <span className="mono">{issue.field}</span>
                {issue.nodeId ? `（节点 ${issue.nodeId}）` : ""}：{issue.message}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function withLayout(config: WorkflowConfig): WorkflowConfig {
  return { ...config, layout: defaultLayout(config) };
}
