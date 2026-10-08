# HW2 HTTP 接口（P12）

本文件对应实际运行的 OpenAPI 规范（`GET /docs` / `GET /openapi.json`），由 FastAPI 从路由与
Pydantic 模型生成，示例取自真实响应。所有业务接口挂在 `/api` 前缀下。

统一约定：

- **错误体**：`{"code": <ErrorCode>, "message": str, "details": {...}, "request_id": str|null}`；
  `code` 稳定可编程，`details` 携带字段级定位（如画布校验的 `node_id` / `field`）。
- **幂等键**：`POST /api/tasks`、`/resume`、`/terminate` 需要 `Idempotency-Key` 头；同键同内容
  复用同一结果，同键异内容返回 409 `IDEMPOTENCY_CONFLICT`。
- **JSON 解析错误**（字段类型/多余字段等）返回 400 `VALIDATION_ERROR`。
- **错误码 → HTTP**：`VALIDATION_ERROR`/`NOT_FOUND`→400/404；`VERSION_CONFLICT`、`CONFIG_MODE_CONFLICT`、
  `IDEMPOTENCY_CONFLICT`、`BUDGET_EXHAUSTED`、`CONFLICT`、`NOT_RESUMABLE`、`NOT_TERMINABLE`→409；
  `MODEL_NOT_CONFIGURED`、`MODEL_REQUEST_FAILED`→503；其余→500。

## 1. 系统

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康与模型配置状态 |
| GET | `/api/agents` | 已注册角色目录（只暴露能力与版本，**不含密钥**） |

```bash
curl -s http://127.0.0.1:8000/api/health
# {"status":"ok","version":"0.1.0","model_configured":false,
#  "model_provider":"deepseek","model_name":"deepseek-chat","data_dir":"..."}

curl -s http://127.0.0.1:8000/api/agents
# [{"agent_id":"reviewer","version":"1.0","supported_task_kinds":["review"],
#   "tools":[...],"retry_budget":{"review":"review_retry"}}, ...]
```

## 2. 上传源码

`POST /api/sources`（`multipart/form-data`，字段名 `files`，可多文件）。只接受 `.py` 等已知后缀，
拒绝路径穿越与超限；内容寻址，同一内容稳定得到同一 `content_digest`。

```bash
curl -s -X POST http://127.0.0.1:8000/api/sources \
  -F "files=@example/clean/src/helpers.py;type=text/x-python"
# 201 {"source_id":"src-…","files":[{"path":"helpers.py","size":123,"sha256":"…"}],
#      "total_bytes":123,"content_digest":"…","created_at":"…","artifact_id":null}
```

## 3. 提交任务

`POST /api/tasks`，202 Accepted；`root_task_id` 由父控制层的受控建任务服务生成（前端/模型不得自造）。
`check_mode` 仅用于**核对**：与所选工作流模式不一致时返回 409 `CONFIG_MODE_CONFLICT`。
未配置模型密钥时返回 503 `MODEL_NOT_CONFIGURED`，且不创建任何任务。

```bash
curl -s -X POST http://127.0.0.1:8000/api/tasks \
  -H 'Idempotency-Key: submit-demo-1' -H 'Content-Type: application/json' \
  -d '{"source_id":"src-…","goal":"检查可变默认参数与异常处理，发现问题后修复并验证",
       "workflow_version":"wf-1"}'
# 202 {"root_task_id":"T-…","status":"queued","revision":0,
#      "workflow_version":"wf-1","contract_version":null,"created_at":"…"}
```

## 4. 查询任务

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/tasks` | 总任务列表（`limit`/`offset`） |
| GET | `/api/tasks/{root_task_id}` | 详情：状态、`passed`、子任务树、检测结论、`checks`、`budgets`、`required_action` |
| GET | `/api/tasks/{root_task_id}/attempts` | 每次尝试（含 `retry_reason`、`source_version` 与回报） |
| GET | `/api/tasks/{root_task_id}/events?after_seq=&limit=` | 增量事件流；`next_seq` 为最后已返回的序号，原样作为下次 after_seq |
| GET | `/api/tasks/{root_task_id}/findings?status=` | **当前**问题状态（每个 finding_id 取最新版本行） |
| GET | `/api/tasks/{root_task_id}/artifacts?artifact_type=` | 产物清单（含 `download_url`） |
| GET | `/api/tasks/{root_task_id}/report` | 最终报告（含 `conclusion_scope` 与逐项 `checks`） |
| GET | `/api/artifacts/{artifact_id}` | 下载单个产物内容 |

```bash
curl -s http://127.0.0.1:8000/api/tasks/T-…
# {"root_task_id":"T-…","status":"completed","passed":true,"revision":7,
#  "contract_version":"contract-1","source_version":"sv-…",
#  "checks":[{"check_id":"CHK-MUTABLE","required":true,"status":"passed",
#             "source_version":"sv-…","note":"…"}],
#  "budgets":{"items":[{"budget_kind":"repair_round","consumed":1,"granted_max":2,"remaining":1}, …]},
#  "required_action":null, "tasks":[…]}

# 增量日志：无新事件时 events 为空、next_seq 不变
curl -s "http://127.0.0.1:8000/api/tasks/T-…/events?after_seq=42"
# {"root_task_id":"T-…","events":[],"next_seq":42,"status":"completed","passed":true,"revision":7}

curl -s "http://127.0.0.1:8000/api/tasks/T-…/findings"
# [{"finding_id":"…","check_id":"CHK-MUTABLE","required_for_goal":true,
#   "status":"resolved","resolution_evidence_refs":["art-…"]}]

curl -s "http://127.0.0.1:8000/api/artifacts/art-…" -o report.json
```

语义要点：`status`（执行状态）与 `passed`（检查结论）分离；`passed` 可为 `true`/`false`/`null`，
`skipped` 单独表达；`null` 表示尚无结论或证据不足，绝不与 `false` 混用。

## 5. 恢复与终止

`POST /api/tasks/{root_task_id}/resume`：仅 `waiting_recovery` / `interrupted` 可恢复。请求**只能**
追加额度与给出原因，**不能**更换 `source_id`/`goal`/测试文件（额外字段被拒绝，400 `VALIDATION_ERROR`）。

```bash
curl -s -X POST http://127.0.0.1:8000/api/tasks/T-…/resume \
  -H 'Idempotency-Key: resume-1' -H 'Content-Type: application/json' \
  -d '{"expected_revision":7,"additional_repair_rounds":1,
       "additional_execution_retries":{"verify":1},"reason":"追加一轮修复与一次验证许可"}'
# 200 {"root_task_id":"T-…","status":"running","revision":8,"run_segment_id":"seg-2",
#      "granted":[{"budget_kind":"repair_round","consumed":2,"granted_max":3,"remaining":1}, …],
#      "message":"已受理恢复请求"}

# 重复同键同内容 → 同一结果；同键异内容或超出服务器上限 → 409
#  {"code":"GRANT_OVER_CAP"…} 映射为 409 BUDGET_EXHAUSTED（details 含 required_additions）
```

`POST /api/tasks/{root_task_id}/terminate`：仅 `waiting_recovery`/`interrupted` 可终止；证据足以
判定不通过时结果为 `completed`/`false`，证据不足为 `partial`/`null`。与 resume 竞争只有一个生效，
终态任务再次 resume/terminate 返回 409 `NOT_RESUMABLE`。

```bash
curl -s -X POST http://127.0.0.1:8000/api/tasks/T-…/terminate \
  -H 'Idempotency-Key: term-1' -H 'Content-Type: application/json' \
  -d '{"expected_revision":8,"reason":"用户确认终止"}'
# 200 {"root_task_id":"T-…","status":"partial","passed":null,"revision":9,
#      "reason":"用户确认终止","skipped_tasks":["T-…-C002"]}
```

## 6. 工作流配置（画布）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/workflows/templates` | 内置模板（顺序 / 并行 / 扩展），已展开标准与插件输入依赖 |
| GET | `/api/workflows` | 已保存版本列表 |
| GET | `/api/workflows/{workflow_version}` | 取回某个不可变版本 |
| POST | `/api/workflows` | 校验并保存新版本（201） |

保存时后端按**已注册**角色与任务类型重新校验并展开，失败在 `details` 定位到字段/节点。

```bash
curl -s http://127.0.0.1:8000/api/workflows/templates | head -c 400

# 取某个模板的 config 字段作为请求体，改 workflow_version 后保存
curl -s http://127.0.0.1:8000/api/workflows/templates \
  | python3 -c "import json,sys;d=json.load(sys.stdin);c=next(t['config'] for t in d if t['name']=='workflow.sequential.yaml');c['workflow_version']='wf-my-1';print(json.dumps(c))" \
  | curl -s -X POST http://127.0.0.1:8000/api/workflows -H 'Content-Type: application/json' -d @-
# 201 {"workflow_version":"wf-my-1","semantic_hash":"wf-…","check_mode":"sequential","config":{…}}

# 未注册任务类型：400，details 定位节点
# {"code":"VALIDATION_ERROR","message":"未注册的任务类型 …",
#  "details":{"field":"nodes.ghost.task_kind","node_id":"ghost", …}}
# 未注册角色版本：400，details.field = "agents.reviewer"
# 版本名复用但语义不同：409 CONFIG_MODE_CONFLICT（配置不可变）
```

## 7. 冲突与恢复速查

| 情形 | 响应 |
|---|---|
| 提交重试同键同内容 | 202，同一 `root_task_id` |
| 提交/恢复同键异内容 | 409 `IDEMPOTENCY_CONFLICT` |
| `check_mode` 与工作流不一致 | 409 `CONFIG_MODE_CONFLICT` |
| 画布保存非法配置（未注册类型/角色） | 400 `VALIDATION_ERROR`，`details` 带 `node_id`/`field` |
| 恢复额度超服务器上限 / 未声明重试种类 | 409 `BUDGET_EXHAUSTED`，不产生任何部分写入 |
| 终态任务再次 resume/terminate | 409 `NOT_RESUMABLE` |
| 未配置模型密钥 | 503 `MODEL_NOT_CONFIGURED`（不创建任务、不静默降级） |
| 中断恢复 | 服务启动扫描失去有效租约的运行标 `interrupted`；`POST /resume` 重新取得执行权后从检查点续跑 |
