# Homework 2 — 父 Agent 管理的代码审查与修复工作台

在 `Test2` 中独立实现的代码审查与修复工作台：用户上传一个或多个 Python 文件并输入检查目标，
父 Agent 建立总任务与子任务，按需派发**审查 / 修复 / 验证**子 Agent，接收回报后做确定性检测并决定下一步；
全过程的派发、尝试、工具调用、回报与决策事件都可追踪、可查询、可恢复。

编排使用真实的 LangGraph（条件分支、循环、并行、状态归并、SQLite 检查点），
不依赖、不复制 Test1 的任何代码或运行目录。

## 文档索引

| 文件 | 内容 |
|---|---|
| [Architecture.md](Architecture.md) | 父 Agent 与子任务架构、流程图与结束规则 |
| [Desgin.md](Desgin.md) | 模块、字段、协议与接口设计 |
| [ImplementationPlan.md](ImplementationPlan.md) | P00–P12 详细实施与验收计划 |
| [docs/API.md](docs/API.md) | HTTP 接口说明 |
| [docs/AgentExtension.md](docs/AgentExtension.md) | 新增 Agent 的接入步骤与改动清单 |
| [docs/Acceptance.md](docs/Acceptance.md) | 验收场景 A01–A37 的实际结果与证据 |
| [docs/Handoff.md](docs/Handoff.md) | 完成情况、关键实现选择、已知限制与下一步 |

## 运行环境

本项目在以下版本上实际安装、启动并通过健康检查：

| 组件 | 版本 |
|---|---|
| Python | 3.12.13（由 `uv` 托管） |
| uv | 0.11.29 |
| Node.js | v24.9.0 |
| npm | 11.6.0 |
| FastAPI | 0.142.4 |
| Pydantic | 2.13.5 |
| LangGraph | 1.2.14 |
| langgraph-checkpoint-sqlite | 3.1.1 |
| Vite | 6.4.4 |

后端使用 `uv` 管理依赖（锁定在 `backend/uv.lock`），前端使用 `npm`（锁定在 `frontend/package-lock.json`）。

## 安装

```bash
# 后端
cd backend
uv sync                 # 创建 .venv 并安装锁定依赖

# 前端
cd ../frontend
npm install
```

## 配置

复制示例环境文件并按需修改；**只有后端**读取模型密钥。

```bash
cp .env.example .env            # 在 Test2 根目录（或放到 backend/.env）
```

关键变量（完整列表见 `.env.example`）：

| 变量 | 说明 |
|---|---|
| `HW2_MODEL_BASE_URL` | 模型服务地址，默认 `https://api.deepseek.com` |
| `HW2_MODEL_NAME` | 模型名，默认 `deepseek-chat` |
| `HW2_MODEL_API_KEY` | 模型密钥；**未配置时**可打开工作台、查看已有任务，但提交新的模型任务会返回明确的配置错误，不会伪造成功 |
| `HW2_DATA_DIR` | 运行数据目录，默认 `<Test2>/data` |

服务端对各项额度设有上限：配置只能收紧、不能突破上限；前端无法提高上限。

## 启动

```bash
# 终端 1：后端 API（http://127.0.0.1:8000）
./scripts/dev-backend.sh

# 终端 2：前端工作台（http://127.0.0.1:5173，/api 反向代理到后端）
./scripts/dev-frontend.sh
```

等价的直接命令：

```bash
cd backend  && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
cd frontend && npm run dev
```

## 健康检查

```bash
curl -s http://127.0.0.1:8000/api/health
# {"status":"ok","version":"0.1.0","model_configured":false,...}
```

`model_configured` 反映密钥是否已配置；工作台顶部会同时显示该状态。

## 测试

```bash
# 后端
cd backend && uv run pytest

# 前端
cd frontend && npm run test
```

## 数据目录

运行数据默认写入 `Test2/data`（已加入忽略规则），互相隔离：

```
data/
├── business.sqlite      业务库：任务树、尝试、回报、终态、额度账本、事件、配置
├── checkpoints.sqlite   LangGraph 流程检查点（由框架适配层管理）
├── uploads/             上传的原始输入（不可变）
├── snapshots/           按 root_task_id 划分的源码快照
├── workspaces/          任务工作副本
├── patches/             候选与已应用补丁
├── evidence/            工具输出、测试证据等长内容
└── reports/             最终报告
```

密钥、日志大文件、上传源码与数据库都不作为源代码提交。

## 使用流程

1. 打开工作台 `http://127.0.0.1:5173/tasks`，在提交表单里选择/拖入一个或多个 `.py` 文件，填写检查目标
   （例如“检查可变默认参数与异常处理，发现问题后修复并验证”），选择已保存的工作流版本后提交。
   提交后进入任务页；服务端会先冻结上传为不可变快照，再由父 Agent 建立检查合同并派发子任务。
2. **模式**：顺序模式依次“审查 → （如需）修复 → 应用 → 验证”；并行模式在补丁应用后于同一冻结快照上
   并行“复审 + 验证”，两边都回报后父 Agent 才决策一次。模式由所选工作流版本决定，请求里的 `check_mode`
   只用于核对，不一致会被拒绝。
3. **查看证据**：任务页按“执行状态”和“检查结论”两栏展示。每个子任务可看到 attempt 次数、每次的
   `retry_reason`、绑定到的 `source_version`；检查项逐个显示 `passed / failed / not_run / inconclusive`
   与证据；问题列表读权威接口，展示 `status` 与关闭证据；产物（补丁、应用、验证报告、检查包、最终报告）
   都可下载。事件增量刷新，只拉取新事件并去重。
4. **恢复**：进入 `waiting_recovery` 的任务在页面上显示 `required_action`（如“追加 repair_round 额度后恢复”）。
   恢复请求只能追加额度与原因；确认后任务重新取得执行权并从检查点续跑。`interrupted`（进程中断）同样
   可恢复。`completed`/`partial`/`failed` 为最终态，不再接受恢复。

### 结论语义

- 首次审查完全通过时任务即 `completed` 且 `passed=true`，修复与验证被标 `skipped` 并注明原因
  （“结论仅覆盖已执行的审查检查”）。
- 修复（fix）产出候选补丁时其 `passed` 保持 `null`——补丁不是证据；只有**修改后版本**上的验证/复审
  证据能改变结论。
- `passed` 三态：`true` 有当前版本通过证据；`false` 有当前版本明确失败的必需项（含“明确无法自动修复”
  时父 Agent 提交 `finish(false)` 并写明能力限制）；`null` 证据不足或尚无结论。`skipped` 单独表达，
  不显示为 `true`/`false`。
- 默认最多两轮自动修复；额度耗尽进入 `waiting_recovery`，通过 resume **追加**一轮（记录追加原因与
  累计次数，累计计数不重置）。无法恢复的等待项可终止：`completed/false`（已证实缺陷）或 `partial/null`
  （证据不足）。

### 常见情况处理

| 情况 | 现象 | 处理 |
|---|---|---|
| 未配置模型密钥 | `/api/health` 的 `model_configured=false`；提交返回 503 `MODEL_NOT_CONFIGURED` | 配置 `HW2_MODEL_API_KEY` 后重启后端；已有任务仍可浏览 |
| 缺少依赖 | 行为测试检查项 `not_run`，原因写明缺少的依赖 | 在 `backend` 的 `uv` 环境安装该依赖；不会因此判通过 |
| 补丁冲突/语法错误 | 补丁不发布新版本，失败回到父 Agent；必要项满足才可能继续 | 查看尝试的 `last_application_error` 与失败证据；必要时追加修复额度或终止 |
| 进程中断 | 重启后任务标 `interrupted`，`passed` 按已有证据保留 | 用 resume 重取执行权恢复；补丁不会重复应用 |
| 额度耗尽 | `waiting_recovery` + `required_action` | 用 resume 追加对应额度（不得超过服务器上限）或终止 |
| 画布配置非法 | 保存被拒，错误定位到节点/字段 | 按提示修正角色或任务类型后重存 |

## 演示数据

```bash
cd backend
uv run python -m scripts.seed_demo --data-dir ../data/demo   # 生成 3 个任务：首次通过 / 修复成功 / 待恢复
HW2_DATA_DIR=../data/demo ../scripts/dev-backend.sh          # 用演示数据目录启动后端
# 前端打开 http://127.0.0.1:5173/tasks 浏览
```

`seed_demo` 只用显式注入的脚本模型生成数据，不冒充真实模型执行；页面本身没有 mock 分支，全部读实际 API。

## 当前进度

阶段完成情况与证据见 `docs/Acceptance.md` 与 `docs/RepairReport-2026-10-08.md`。
真实模型四场景与 A15 已通过；最新浏览器联调又修复了画布、长表格和恢复决策问题，
后端 216 项、前端 13 项测试通过，生产构建成功，详见 `docs/FrontendTest-2026-10-08.md`。
