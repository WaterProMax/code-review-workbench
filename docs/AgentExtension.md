# 新增 Agent 接入说明（P10 §14.2）

本文件用一个真实扩展样例——**测试生成角色 `test_generator`**——说明新增角色需要改什么、
不需要改什么，以及如何验证接入是真实的。

样例本身已随仓库提供并默认注册（因此 `GET /api/agents` 能发现它），但**默认三个子 Agent 的
基础模式不变**：只有在保存的工作流配置引用了该角色时它才会被派发。

## 1. 接入清单（实际改动文件）

| 文件 | 作用 |
|---|---|
| `backend/app/agents/test_generator.py` | 角色实现（`TestGeneratorAgent`，遵循 `AgentProtocol`） |
| `backend/app/agents/prompts/test_generator.txt` | 角色提示词与版本（`prompt_version: test-generator-1.0`） |
| `backend/app/extensions/base.py` | 扩展契约：`TaskKindPlugin` / `FlowExtension` / `ExtensionRegistry` / 配置校验 |
| `backend/app/extensions/test_generator.py` | 样例的注册项：能力声明、插件（依赖、输入适配、重试预算） |
| `backend/app/extensions/__init__.py` | `build_default_extension_registry()`（注册样例） |
| `backend/app/services/tools/artifacts_tools.py` | 受控工具 `submit_generated_tests`（唯一产物出口） |
| `backend/app/services/tools/__init__.py` | 工具授权：读工具对该角色开放，`save_evidence`/`submit_generated_tests` 走 scratch 权限 |
| `backend/app/schemas/artifacts.py` | 产物载荷 `GeneratedTest` / `GeneratedTestSuite` |
| `backend/app/services/tools/checks.py` | 测试包展开：一个 `test_artifact` 可以是单个测试或测试包 |
| `backend/app/agents/verifier.py` | 消费 `input_refs.generated_tests`（输入适配，不是新分支） |
| `config/agents.yaml` | 声明该角色的版本、描述、工具与权限 |
| `config/workflow.extension.yaml` | 扩展模板（顺序模板 + `gen` 节点），依赖由插件提供 |

**没有改动**的地方（这是接入成本的关键）：

- `ParentController.validate_action` / `register_dispatch`：没有 `dispatch_test_generator` 之类专用动作，
  父 Agent 走通用 `dispatch_task`/`dispatch_batch`；
- 回报路径：仍走同一个接收适配器与检测入口；
- 数据库 Schema：没有为扩展角色加表或列；
- 基础三角色的行为与额度。

## 2. 新增角色需要声明的四件事

1. **能力与实现**：`AgentSpec`（`agent_id`、`version`、`supported_task_kinds`、`tool_names`、
   `produced_artifact_types`、`required_input_keys`、`retry_budget`）+ `AgentProtocol` 实现。
2. **工具授权**：角色只能调用被授权的工具；写操作必须经过受控工具（`submit_generated_tests`），
   不得直接写快照。`config/agents.yaml` 与代码注册项必须一致。
3. **输入依赖与输入适配**：`TaskKindPlugin.dependencies` 声明模板数据依赖；
   `consumes` 声明本角色读什么；`feeds` 声明**别的角色**从本角色产物获得什么输入
   （样例中 `verify` 获得 `generated_tests`），因此消费方不需要新增判断分支。
4. **重试预算**：`TaskKindPlugin.retry_budget_kind` 必填。未声明预算的任务类型会被
   `ExtensionRegistry.register` 拒绝（`EXTENSION_NO_RETRY_BUDGET`）；派发时若任务类型没有
   故障预算映射，`TaskService._budgets_for` 抛 `UNKNOWN_RETRY_BUDGET`，**不会落入无限重试**。

## 3. 如何验证接入是真实的（可复现命令）

```bash
cd backend
uv run pytest tests/integration/test_extension_agent.py -q
uv run pytest tests/e2e/test_api.py -q
uv run pytest tests/unit/test_config_files.py -q
```

`tests/integration/test_extension_agent.py` 断言（全部为真实执行，非 mock）：

- 扩展节点作为真实子任务被派发并回报（`generate_tests` 子任务 completed、`passed=null`）；
- 产物是真实测试包（`test_artifact`，`metadata.bundle = true`）；
- 该产物被作为 **verifier 输入**派发（`verify` attempt 的 `input_refs.generated_tests` 指向它）；
- 生成的测试被**真实执行**：在修复前版本上失败、在修复后版本上通过（检查结果 reason 记录基础版本结论）；
- 故障预算有界（`generate_retry`），未声明预算的任务类型会被拒绝；
- 带检查点的扩展流程中断后可恢复，扩展角色的结果在恢复后仍在。

`tests/e2e/test_api.py` 另证明：`GET /api/workflows/templates` 返回扩展模板，保存后可作为
真实工作流提交任务并跑到 `completed/true`，`GET /api/agents` 暴露该角色及其重试预算。

## 4. 保存配置时的校验

`POST /api/workflows` 会用**已注册**的角色与任务类型校验配置，错误带定位信息：

| 情况 | 返回 |
|---|---|
| 引用未注册的任务类型 | 400 `VALIDATION_ERROR`，`details.field = nodes.<id>.task_kind`、`details.node_id` |
| 引用未注册的角色版本 | 400 `VALIDATION_ERROR`，`details.field = agents.<agent_id>` |
| 角色不支持该任务类型 | 400 `VALIDATION_ERROR`，`details.field = nodes.<id>.task_kind` |
| 同版本名不同语义 | 409 `CONFIG_MODE_CONFLICT` |

因此“只往数据库写一条角色记录”无法完成接入：没有能力声明、工具授权、输入适配与预算声明，
配置保存就会被拒绝。
