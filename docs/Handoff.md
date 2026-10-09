# Handoff — 交接说明

> **2026-10-09 后端更新**：主干架构经实施计划对照；总任务提交改经父控制层初始化入口，7 项后端问题均已修复。新增 17 项回归，后端全量 **233 passed**，详见 [后端架构核查与修复记录](BackendArchitectureAudit-2026-10-09.md)。需重启后端加载更新；本轮未重新调用 DeepSeek、未修改或验收前端、未提交或推送 GitHub。下列真实模型与浏览器记录保留其原日期范围。

> 最新浏览器联调：后端 216 passed、前端 13 passed、生产构建通过；画布节点重叠、保存提示消失、长表格溢出及恢复时旧拒绝反馈已修复。页面真实提交的并行任务和额度恢复任务均 completed/true，见 [前端浏览器联调记录](FrontendTest-2026-10-08.md)。

> **2026-10-08 修复更新**：此前审核问题和真实联调阻断已修复。后端 215 passed、无 xfail；前端 13 passed、生产构建通过；A15 已实现逐分支持久化与恢复重放。最终源码同版 DeepSeek 四场景 4/4 completed / passed=true，包括真实并行复审与验证。当前状态与证据以 [修复与回归记录](RepairReport-2026-10-08.md) 和 [验收记录](Acceptance.md) 为准。

## 修复前交接记录（历史）

以下保留原始交付过程；其中“无密钥、真实联调未执行、A15 未达标”等描述已被上述修复记录取代。

本文件记录已完成功能、启动入口、关键实现选择、运行版本、未完成项与已知限制。
随阶段推进持续更新。

## 运行版本（P00 实测）

| 组件 | 版本 |
|---|---|
| Python | 3.12.13（uv 托管 `cpython-3.12.13-macos-aarch64-none`） |
| uv | 0.11.29 |
| Node.js | v24.9.0 |
| npm | 11.6.0 |
| FastAPI | 0.142.4 |
| Pydantic | 2.13.5 |
| LangGraph | 1.2.14 |
| langgraph-checkpoint-sqlite | 3.1.1 |
| httpx | 0.28.1 |
| uvicorn | 0.54.0 |
| pytest | 9.1.1 |
| Vite | 6.4.4 |
| vitest | 3.2.4 |

## 启动入口

- 后端：`./scripts/dev-backend.sh` → `uv run uvicorn app.main:app`（默认 127.0.0.1:8000）
- 前端：`./scripts/dev-frontend.sh` → `npm run dev`（默认 127.0.0.1:5173，`/api` 代理到后端）
- 健康检查：`GET /api/health`

## 关键实现选择

### 依赖管理
- 后端用 `uv`（`backend/uv.lock`），前端用 `npm`（`frontend/package-lock.json`）。
- `pyproject.toml` 的 `requires-python = ">=3.11"`；解析出兼容版本用 uv 托管的 Python 3.12.13。

### 前端测试工具链
- vitest 2.x 会为 vitest 单独安装一份嵌套的 Vite 5，与顶层 Vite 6 的类型冲突，导致 `tsc --noEmit` 失败。
  解决方式：升级到 vitest 3.2.4（与 Vite 6 共用同一份 Vite），锁定后类型检查与构建均通过。

### LangGraph 原型结论（P00 第 6 步）
原型脚本：`backend/scripts/langgraph_prototype.py`，用真实 `AsyncSqliteSaver` 运行，结论 **PASS**：

| 能力 | 结论 | 证据 |
|---|---|---|
| 真实 SQLite 检查点 | PASS | 检查点文件生成（36864 bytes）；用新的 saver 重新打开后仍能读回已完成状态 |
| 条件路由 | PASS | 路由函数返回不同目标列表，`parallel` 走两分支、`serial` 跳过分支 |
| 并行状态归并 | PASS | 两条分支写入 `Annotated[list, operator.add]` 通道，归并结果为 `["a","b"]` |
| 暂停 / 恢复 | PASS | `interrupt` 后 `aget_state` 显示 `pending_tasks=["pause"]` 且 `finalize` 未执行；`Command(resume=...)` 后继续执行到 `finalize` |
| 检查点历史 | PASS | `aget_state_history` 返回 7 条快照 |

版本相关 API 备注（供业务图实现使用）：

1. LangGraph 1.x 中，待处理的中断挂在 `snapshot.tasks[*].interrupts`（`Interrupt.value` 为中断载荷），
   **不再**通过 `state.values["__interrupt__"]` 暴露；恢复用 `langgraph.types.Command(resume=<value>)`。
2. `StateGraph` 的状态类型如果定义在函数内部，`add_conditional_edges` 的路由函数做 `get_type_hints`
   时会因局部作用域解析失败（`NameError`）。状态类型与节点函数需定义在**模块级**作用域。
3. 异步检查点用 `langgraph.checkpoint.sqlite.aio.AsyncSqliteSaver.from_conn_string(path)`（异步上下文管理器）。

原型仅用于验证框架能力，不保留为第二套业务运行器；业务图在 `app/workflow/`。

## 未完成项

- P11 已完成（确定性验收全绿；`uv run pytest -q` → 195 passed, 1 xfailed）；P12 见本文件其余章节。
- **A15「并行中间进程退出」未达标**（`docs/Acceptance.md` §5.1）。真实子进程在补丁后并行批次
  （recheck+verify）中途被 SIGKILL 后，已完成分支的回报在恢复时丢失：根因是子 Agent 回报只在批次
  整体返回后的 `n_collect_results` 才批量落库，崩溃中途时业务账本里没有“已接受回报”，恢复无法按
  设计 §11“从接收账本重放”。已以 `@pytest.mark.xfail(strict=True)` 保留复现
  （`backend/tests/e2e/test_acceptance_scenarios.py::test_a15_parallel_mid_batch_exit_keeps_the_finished_branch_evidence`）。
  **未完成的改动**：将子 Agent 回报落库前移到各分支回报到达时（`run_one`/adapter 内 `receive_result`），
  并为 `n_dispatch` 增加按父决策 id 派生的稳定操作键——重放时复用同一批次、只执行缺少合法终态的分支、
  复用已接受回报且不重复扣减额度。该改动触及核心派发/恢复路径，需同步补批次重放与额度重放回归用例。
- **真实模型联调未执行**。运行时探针 `GET /api/health` 返回 `model_configured=false`，未配置
  `HW2_MODEL_API_KEY`，故 §15.3 的真实 DeepSeek 联调（clean/repairable/multi_file 三组示例、并行模式
  真实运行、去敏事件/报告/版本引用）未产出。复现命令与所需配置见 `docs/Acceptance.md` §5.2。
- P03–P11 的测试与浏览器联调都使用显式注入的 `ScriptedLLMClient`（需要观察并发时可设 `latency_seconds`），
  **不能视为真实联调通过**。没有密钥时 `POST /api/tasks`、`/resume` 返回 503 `MODEL_NOT_CONFIGURED`，
  不会静默改用脚本模型；已有任务仍可浏览。

## A15 修复方向（供接手）

1. 子 Agent 回报一到就打账（`run_one` 内 `self.task_service.receive_result(...)`），让“已完成分支”在批次
   结束前即持久化；`n_collect_results` 退化为幂等对账。
2. 派发幂等：`n_parent_decide` 的决策 id（`state["decision_ids"][-1]`）派生稳定操作键，`register_dispatch`
   命中该键时返回原批次/attempts，不再新建、不再扣减额度。
3. `n_dispatch` 重放时对每个预期 attempt 只执行缺少合法终态者；已接受回报按 §7.5 复用。
4. 回归：新增“批次中途崩溃后恢复，已完成分支不重跑且证据保留”的确定性用例，并把 A15 的 xfail 转为通过。

## 启动入口（P08 追加）

- API 文档：`GET /docs`（OpenAPI）；业务接口见 `docs/API.md`（P12 补齐）。
- 服务启动即扫描失去有效租约的运行并标记 `interrupted`（lifespan），退出时取消后台运行并释放租约。

## 启动入口（P09 追加）

- 前端工作台：`./scripts/dev-frontend.sh`（Vite，127.0.0.1:5173，`/api` 代理到后端）。
- 前端检查：`cd frontend && npx tsc --noEmit && npx vitest run && npm run build`。
- 浏览真实数据：先 `cd backend && uv run python -m scripts.seed_demo --data-dir ../data/demo` 生成三个任务
  （首次通过 `T-demo-pass`、修复成功 `T-demo-repair`、待恢复 `T-demo-wait`），
  再 `HW2_DATA_DIR=../data/demo ./scripts/dev-backend.sh` 启动后端，打开 `http://127.0.0.1:5173/tasks`。
  该脚本只用显式注入的脚本模型生成数据，不冒充真实模型执行；页面本身没有任何 mock 分支，全部读实际 API。
- 编排配置页：`http://127.0.0.1:5173/workflows`，模板与角色目录都来自后端。
- 扩展角色（P10）：接入方式、改动文件与复现命令见 `docs/AgentExtension.md`
  （`uv run pytest tests/integration/test_extension_agent.py -q`）。

## 已知限制（P10 画布）

- 画布使用 React Flow 绘制，边的路由依赖节点/连接点的尺寸测量（`ResizeObserver`）。当内置 Browser 面板
  未前台（`document.hidden === true`）时该回调不触发，因此**在该面板里看不到连线**；节点、节点表、连接与环境依赖表
  仍照常渲染（已在面板中核对：6 个节点文案与 9 条合法执行边 + 2/4 条输入依赖）。把面板置前后连线会正常绘制。
  画布的正确性由 `frontend/tests/orchestration.test.tsx`（7 项）与后端模板/校验测试覆盖。
- 首版画布不支持任意连线/节点重排：结构来自模板（默认顺序、并行、扩展三种），可编辑的是角色选择、预算与位置；
  扩展角色通过选择扩展模板接入。

## 关键实现选择（P10）

- **扩展只走一套机制**：`app/extensions/base.py` 的 `TaskKindPlugin` 声明任务类型、角色、输入依赖
  (`dependencies`)、输入适配 (`consumes`/`feeds`)、重试预算 (`retry_budget_kind`) 与 `verdict_bearing`。
  注册项进 `AgentRegistry`，工具授权进 `ToolRegistry.allowed_roles`，配置校验进 `validate_workflow_config`。
  父 Agent 的 `validate_action`/`register_dispatch` **没有**任何针对扩展的分支。
- **输入引用由适配器表解析**：`ParentController` 不再按 task_kind 写 if/elif，而是遍历
  `default_input_adapters()` + 插件声明的适配器（`SCOPE_LATEST` 取最新产物，`SCOPE_COMMITTED_APPLICATION`
  取已提交应用对应的版本绑定产物），因此 verifier 获得 `generated_tests` 不需要新增分支。
- **非结论型回报**：`Detector(verdict_kinds=...)` 过滤掉扩展任务类型的成功回报，使可选角色跑完不会被误判为阶段结论；
  其失败仍按执行故障处理并消耗各自预算。
- **禁止无限重试**：未声明预算的任务类型在注册（`EXTENSION_NO_RETRY_BUDGET`）、配置保存
  （`UNKNOWN_TASK_KIND`）与派发（`UNKNOWN_RETRY_BUDGET`）三处被拒。新增预算种类 `generate_retry`
  与上限 `CAP_MAX_GENERATE_RETRIES` 同步落入 `execution_locks.BUDGET_CONFIG_FIELD/BUDGET_CAPS`、
  `TASK_KIND_FAULT_BUDGET` 与 `recovery.EXECUTION_RETRY_BUDGET`。
- **测试包是一个真实的输入依赖**：`submit_generated_tests` 保存一个 JSON 测试包产物；`run_checks`
  在 `_expand_generated` 中识别测试包并按 check_id 展开为测试文件，因此扩展产物被固定执行器真实运行
  （检查结果 reason 里带有基础版本结论），而不是只被记录。
- **模板是源文件的投影**：`config/workflow.*.yaml` 经 `GET /api/workflows/templates` 提供给画布；
  扩展模板**不**手写输入依赖，依赖由插件在保存时展开，因此“换模板 = 换真实执行结构”。
- **画布的边界**：结构来自模板、角色来自已注册目录、位置只进 `layout`（不参与 semantic_hash）。
  前端 `src/workflow.ts` 镜像后端结构规则用于即时反馈，后端保存时仍重新校验并返回字段级定位。

## 关键实现选择（P09）

- **问题状态取权威接口而非产物**：审查产物是不可变快照，其中的 Finding 永远是提交时的 `open`。
  新增 `GET /api/tasks/{id}/findings`（`FindingRepository.list_current_by_root`，按 `finding_id` 取最新版本行）
  提供 `status` 与 `resolution_evidence_refs`；前端问题列表读该接口，`finding` 产物仅用于审查覆盖说明与取证。
  关闭证据写入时按顺序去重（`Detector.resolve_required_findings`）。
- **轮询与去重**：`useTaskData` 用 `after_seq` 增量拉事件，按 `event_id` 去重后按 `sequence` 排序；
  仅在 `running`/`queued` 时轮询，终态停止；失败指数退避。切换任务重置游标与去重集合。
- **展示语义分离**：`src/display.ts` 分别给出执行状态（root/task/attempt）与检查结论（`true`→检查通过、
  `false`→检查未通过、`null`→尚无结论）的文案，`skipped` 单独表达；不把第二次尝试覆盖第一次，
  不把 Agent 名当任务编号。检测面板注明“结论由控制层按持久化证据重算，模型说明只作解释”。
- **真实浏览器联调发现并修复的缺陷**：同一事件重复引用产物导致 React key 冲突（`Array.from(new Set(...))`）、
  `finding` 产物是 `{source_version, findings, coverage, not_checked}` 包体而列表按单条 Finding 渲染导致崩溃（新增
  `FindingsArtifact` 类型与扁平化）、以及上述问题状态误用快照的问题。这三处只有真实连后端渲染才能暴露。

## 关键实现选择（P08）

- **提交返回的 ID 来自受控建任务服务**：`POST /api/tasks` 用 `TaskCreationService.new_root_task_id()` 铸造
  root_task_id（不经前端），冻结上传为不可变首版快照后立即返回 202；规划与子任务由后台运行继续，接口不阻塞。
- **应用级后台服务**：`services/background.py` 每个 root 持一个 asyncio 句柄，独立于请求生命周期，
  前端断线/关页不取消任务；`shutdown()` 取消句柄，运行器的 `finally` 释放租约。异常被记录并把任务置为
  `waiting_recovery`（`required_action` 写明内部错误），不静默吞掉。
- **生产与测试同源装配**：`workflow/assembly.py` 的 `assemble_workflow` 是唯一装配入口，测试夹具改为调用它，
  避免测试驱动与生产接线不一致。
- **幂等键强制**：`/tasks`、`/resume`、`/terminate` 需要 `Idempotency-Key`；同键同请求复用同一结果，
  同键不同内容 409 `IDEMPOTENCY_CONFLICT`，缺失键 400。恢复请求另按请求指纹在 `idempotency_records` 去重。
- **恢复的租约所有者与后台运行一致**：API 层 `recovery.resume` 以 `runner.owner` 取得执行权，
  否则后台续跑会因“另一所有者持锁”被拒（第一版曾出现）。
- **检测结论落产物**：图节点把 `DetectionResult` 按版本存为 `detection` 产物，任务详情页无需重放图即可解释
  “为什么通过/等待”；`required_action` 在 `n_wait` 持久化到 `task_controls`、`n_finalize` 清空。
- **`null` 与 `false` 分列**：`SubmitTaskResponse`/`TaskDetailResponse` 的 `passed` 直接来自任务行，
  未判定即 `null`，不会与 `false` 混同；`GET /events` 返回有界增量与 `next_seq`，无新事件时返回空数组。

## 关键实现选择（P07）

- **执行权是数据库租约**：`workflow/runner.py` 在驱动图之前用 `RecoveryCoordinator.acquire_run_right` 取得租约并
  在运行期间心跳；fencing token 随接管递增，旧持有者用旧 token 写终态会被 `record_controller_terminal` 拒为 `STALE_FENCING_TOKEN`。
  释放租约时同时清空 `lease_owner` 与 `lease_expires_at`（否则已释放的租约仍被判定占用、无法再次取得）。
- **检查点必须同步落盘**：`graph.ainvoke(..., durability="sync")`。默认的异步检查点会被 `SIGKILL` 丢在半路，
  进程中断恢复会退回到很早的位置（实测只留下 initialize 一步）。仅在存在 checkpointer 时传该参数（无 checkpointer 会报错）。
- **补丁应用意图先落库再发布**：`WorkspaceService.apply_patch` 先写 `patch_applications` 的 prepared 记录
  （`result_version` 由候选内容预先算出，内容寻址），再发布快照，最后 `commit_application` 提交并落产物；
  崩溃在“已发布未提交”时由 `verify_published_application` 校验快照元数据后补记，绝不重放补丁（测试断言快照 mtime 不变）。
- **恢复校正当前版本投影**：`WorkflowRunner.resume` 先读检查点与补丁账本，用 `RecoveryCoordinator.reconcile`
  以已提交补丁为准修正 `source_version`/`patched`，再用 `aupdate_state(..., as_node="detect")` 只覆盖标量通道后 `ainvoke(None)` 继续，
  因此不会重复追加 `dispatch_history`/`errors` 等累积通道，也不会重建子任务或批次。
- **启动扫描与孤儿终态**：`recover_incomplete_runs` 找出失去有效租约的 running/queued 任务，把未结束 attempt 写为
  controller/failed 终态（origin=controller）并标 `interrupted`，从而可被 resume 或 terminate 处理。
- **resume 原子追加且幂等**：一个事务内校验 revision、租约与追加上限（`GRANT_OVER_CAP`），追加 `repair_round` /
  `additional_execution_retries`（仅已注册 task_kind，未知键拒绝）/ `additional_evidence_retries`，登记新 `run_segment_id` 与图步数许可；
  任一不满足整体拒绝、不部分生效。重复请求按幂等键返回 `reused=True`，不重复记账。重启本身不追加额度。
- **terminate 只处理可恢复态**：仅 `waiting_recovery`/`interrupted` 且无有效租约、无未结束 attempt 时可终止；
  结论由完成检测重算——已证实必需失败为 `completed/false`，证据不足保留 `null` 并投影 `partial`，同时把不再需要的子任务标 `skipped` 并注明原因。

## 关键实现选择（P06）

- **并行批次真的并发**：`n_dispatch` 对一批 attempts 用 `asyncio.gather` 同时执行，因此两条分支的时间区间在数据库中真实重叠
  （测试断言 `second.started_at < first.finished_at`）。
- **事务按执行上下文隔离**：`Database.transaction` 的重入状态放在 `contextvars.ContextVar` 而不是线程本地，
  这样两个并发 attempt（同一事件循环线程）不会共享连接，而单个 attempt 内的嵌套调用仍加入同一事务。
- **子任务按 workflow 节点建**：`create_standard_children` 为每个 Agent 节点建一个子任务（按节点 id 幂等），
  所以并行模板的 `recheck` 有自己的子任务但复用 reviewer 角色——角色数不变、任务数增加。
  子任务与节点的对应关系按节点顺序确定，并在决策上下文的 `task_tree[].node_id` / `latest_reports[].node_id` 中暴露给父模型。
- **超时是控制层终态**：每个 attempt 用 `asyncio.wait_for(constraints.timeout_seconds)` 包裹；
  超时/身份错配/未预期异常都由控制层写 `attempt_terminals`（origin=controller、outcome=failed、带 error_ref）并把 attempt 置为 invalidated，
  批次接收集合记 controller 来源。检测把控制层故障判为 `execution_fault`（可恢复则可重派）。
- **取消不能被吞成回报**：`BaseAgent._execute` 直接向上抛 `asyncio.CancelledError`，
  否则被撤销的尝试会伪造一份 failed 回报、绕过控制层终态。
- **阶段由 `patched` 决定**：`Detector.detect(patched=...)` 决定用 initial_review 还是 post_patch 的必需检查，
  不再按批次里出现了哪种 task_kind 猜测（并行批次同时含 review 与 verify）。

## 关键实现选择（P05）

- **重试原因由证据推导**（`ParentController.retry_reason`）：新建 attempt 时按“上一轮该 task 的最后一次回报”判定
  `initial` / `execution_fault` / `insufficient_evidence` / `business_repair`，而不是听模型叙述。
  原因决定本次消耗哪些额度（fix 恒消耗一轮 repair_round，执行故障另加 fix_retry；
  review/verify 故障重派消耗各自 fault 额度；证据补查消耗 evidence_retry；修改后首次 verify 不消耗故障额度）。
- **额度检查在业务事务内**：`register_dispatch` 在同一事务里做 `ensure_available` + 建 attempt + 扣减；
  任一额度不足即抛 `BudgetExhausted(required_additions)`，不产生 attempt、不部分追加。
- **阻塞原因不丢失**：非法动作的 details 会被带到最终 `waiting_reason` / `required_action`（额度耗尽时写明追加项），
  避免把“停止自动重试”显示成成功。
- **等待时的结论由控制层重算**：`n_wait` 不采用模型给的 `known_passed`，而是重新跑完成检测；
  仍有已证实必需失败则保留 `passed=false`，证据不足则 `null`（Desgin.md §2.1）。
- **不重复的进展判定**：检测记录失败签名（失败 check + 未关闭必需 Finding 的规则/位置）与补丁指纹；
  同一版本重复相同失败、或修复提交相同补丁指纹时判为 `no_progress`，不再无限重试。
- **辅助判定字段**（`_decision_context` 新增，供父模型与前端）：`unapplied_fix_attempt_id`（产出补丁但未应用的最近修复 attempt）、
  `last_verified_source_version`、`repair_rounds_remaining`、`budgets`。

## 关键实现选择（P04）

- **图结构**（`app/workflow/builder.py`）：`initialize → parent_decide → validate_action →（dispatch → collect_results → detect → parent_decide
  循环 | apply_patch → detect → parent_decide | wait → END | finalize →（END | parent_decide））`。
  节点函数与状态类型都在模块级定义（P00 原型结论第 2 条）；条件边用映射字典显式命名目标。
- **父模型只提议**：`agents/parent.py` 的 `plan/replan` 产出检查合同，`decide` 产出 `ParentAction`（discriminated union）。
  控制层 `workflow/controller.py` 逐项校验（角色已注册且支持该 task_kind、任务存在且角色一致、依赖满足、额度足够、
  verify 必须已有一个结果版本等于当前版本的 committed 补丁应用、apply_patch 的 base_version 必须等于当前版本），
  非法动作有限纠正（`max_parent_corrections`），纠正耗尽转 waiting_recovery。
- **输入引用不由模型编造**：`DispatchItem.input_refs` / `apply_patch.patch_ref` 允许写 `"auto"`，
  控制层按 task_kind 从当前状态填充 source / acceptance_contract / findings / previous_patch / verification / patch_application
  的真实产物 id；`task_id` 只能引用任务树里已有的子任务。
- **建任务由控制层代父 Agent 执行**：`create_standard_children` 在 initialize 阶段登记 review/fix/verify 三个子任务
  （带 `review_completed` / `patch_applied` 依赖），用 `operation_key` 幂等，重放不会重复建树；登记不等于执行。
- **检测确定性**（`app/workflow/detection.py`）：类别只由持久化证据得出——批次/尝试是否收齐、报告 status、
  当前版本必需 CheckResult（passed/failed/not_run/inconclusive）、未关闭的必需 Finding、补丁应用失败、失败签名与补丁指纹是否重复。
  检测到的 category 决定建议动作，但动作仍由父模型提出、控制层校验。
- **完成检测独立于模型**：`TaskService.detect_completion` 重新从数据库计算；`finish` 只是提议，
  控制层拒绝不满足条件的 finish，并把原因作为反馈回到父模型。
- **必需 Finding 的关闭**：只有“当前版本存在 passed 验证报告且目标该 Finding，并且该 Finding 的 check_id 在当前版本有 passed 结果”
  时，控制层才写入 resolution_evidence_refs 并置 resolved（Desgin.md §5.1）。
- **事务可重入**：`Database.transaction` 在线程内可重入，嵌套仓储调用加入外层事务；派发登记（批次 + 尝试 + 额度扣减 + 建子任务）
  是一个业务事务（Desgin.md §6.2.1）。此前另开内层写事务会与外层 `BEGIN IMMEDIATE` 互锁，实测 76s 后抛
  `database is locked` 并触发 contextmanager 的 `generator didn't stop after throw()`。
- **首版合同规则**（确定性校验）：required 的 `model_review` 只能是 initial_review；仅 initial_review 的 required 检查
  必须另有同 goal_ref 的 both/post_patch required 检查覆盖；追加合同不得降级已有必需项。

## 关键实现选择（P03）

- **模型接口**：`LLMClient.complete/complete_json` 统一普通生成与结构化输出；DeepSeek 用 httpx 直连
  OpenAI 兼容接口，封装鉴权、超时、可重试错误与有限重试；密钥只由后端读取且不写日志。
  脚本模型 `ScriptedLLMClient` 只在测试中显式注入，`build_llm_client` 无密钥会抛 `ConfigurationError`，
  绝不静默降级（已有测试断言）。
- **结构化输出兜底**：显式 JSON 提示 + 解析（容忍代码围栏）+ Pydantic 校验 + 有限次纠正，
  超限抛 `LLMParseError`，不用“默认通过”兜底。
- **工具纪律**：`ToolRegistry.execute` 是唯一入口，负责角色权限、工具步数预算、期限、取消、
  错误归一化与 `tool_started/tool_finished` 事件；`run_checks`/`submit_*` 等写产物的工具靠 `allowed_roles` 授权，
  审查/验证角色没有任何修改快照的工具。
- **角色输出落产物**：审查问题（Finding）、验证报告、检查结果、证据都经受控工具写入不可变产物，
  TaskResult 只引用 artifact id，专用数据不进入共享 State。
- **静态规则判定确定性**：`CheckSpec.rule_ids`（新增字段，已同步 Desgin.md §5.1）声明要评估的规则集，
  规则实现见 `services/tools/static_rules.py`（B006 可变默认参数 / E722 裸 except / E711 与 None 比较）。
- **固定测试执行器**：只使用后端定义的 pytest 参数，独立执行目录，硬超时后终止进程组，输出限量；
  明确区分断言失败、收集失败、零测试、全部跳过、依赖缺失、超时与崩溃，零收集/全跳过/未运行一律不算通过。
- **未确定测试**：verifier 可让模型产出最小复现测试，保存为 `test_artifact` 产物；测试先跑基础版本证明缺陷、
  再跑当前版本，两个版本都记录证据与测试集摘要。

## 关键实现选择（P02）

- **迁移即 DDL 真源**：`app/storage/migrations/0001_init.sql` 建 20 张表；`Database.initialize()` 幂等，按文件名顺序应用，`schema_migrations` 记录版本。
- **连接策略**：每次操作新建连接、结束即关；`PRAGMA foreign_keys=ON`（每连接）、WAL、`busy_timeout=5000`、有限指数退避重试，写事务用 `BEGIN IMMEDIATE`，短小且不跨越模型/工具调用。
- **仓储层**：所有方法接受可选 `conn`，便于把批次、尝试、额度扣减放进同一业务事务；`Repos` 汇总全部仓储供服务注入。
- **事件序号**：`EventRepository.append` 在事务内取 `MAX(sequence)+1`，并发分支不会各自在内存递增；已实测 12 线程并发写入得到连续唯一的 1..12。
- **唯一终态**：`attempt_terminals.attempt_id` 主键 + `INSERT OR IGNORE`，Agent 回报与控制层超时竞争同一约束；控制层只能写 failed。
- **严格补丁**：`workspace.apply_hunks` 按 hunk 声明的行列数精确消费、逐行等值匹配，不匹配即失败；结构化片段要求 `find` 原文命中；候选文件语法检查通过才发布；发布用临时目录 + `os.replace` 原子落位，重复发布校验内容一致。
- **受保护测试路径**：补丁命中 `tests/`、`test_*.py`、`*_test.py`、`conftest.py`、`pytest.ini`、`tox.ini`、`setup.cfg`、`pyproject.toml` 一律拒绝（A28）。
- **检查点与业务库分离**：`storage/checkpoints.py` 只暴露 `open_checkpointer` 与 `thread_id_for`，业务 Schema 不假设框架内部表。

## 关键实现选择（P01）

- **协议样例即可执行合同**：`backend/tests/fixtures/protocol/{legal,illegal}.json` 自述所属 schema 与拒绝原因，
  由 `tests/unit/test_protocol_samples.py` 驱动；合法样例要求往返序列化后对象相等，非法样例要求被拒绝。
- **schema 只保证单对象自洽**：跨对象身份核对（root/task/attempt/batch/agent 版本/源码版本/合同版本）
  属于接收期校验，放在 P02 接收账本与 P04 `collect_results`，见 fixtures README 说明。
- **控制字段单独建档**：租约 owner、心跳、租约截止、fencing token、run segment、图步数许可与父纠正计数
  放入独立的 `task_controls` 记录（`TaskControl`），不放总任务行、也不用 Python 内存锁；
  请求与操作幂等用 `IdempotencyRecord`（P02 迁移中落表）。
- **配置不可变 + 布局不参与语义**：`WorkflowConfig` 的 `semantic_hash` 只覆盖 check_mode/agents/nodes/edges/
  dependencies/budgets/工具策略版本，画布坐标 `layout` 不参与；`with_standard_expansion()` 补齐标准输入依赖
  与工具策略版本，保存时固定，运行不再读取变化后的全局默认值。
- **预算上限写死在服务端**：`settings.py` 定义 cap，`BudgetConfig` 用 `le` 校验，配置只能收紧。

## 已知限制

- 首版仅面向 Python 文件与小型 Python 项目。
- 本地可信示例执行，不自动安装上传代码声明的依赖，不向模型开放任意 shell。
- 恢复重入点固定在“父决策前”（`aupdate_state(..., as_node="detect")`）：中断若发生在 `collect_results` 中途，
  未归并的回报由 `detect` 从业务表读取参与判定，但不补写 collect 阶段的事件；未结束 attempt 由启动扫描写为
  controller 终态后按证据重派（保留历史）。§11.2 表中更细的逐分支重放（如“候选补丁尚未发布”）留待需要时补充。
- 进程中断验收覆盖“补丁已提交、验证未开始”这一断点（A13/A14，通过）；**并行批次中途退出（A15）尚未达标**：
  已完成分支的回报在恢复时丢失，因回报在批次结束后才批量落库（详见「未完成项」与 `docs/Acceptance.md` §5.1）。

## 启动入口（P11/P12 追加）

- 后端：`./scripts/dev-backend.sh`（`uv run uvicorn app.main:app`，`HW2_HOST`/`HW2_PORT` 可覆盖，默认 127.0.0.1:8000）。
- 前端：`./scripts/dev-frontend.sh`（Vite，127.0.0.1:5173，`/api` 代理到后端）。
- 演示数据：`cd backend && uv run python -m scripts.seed_demo --data-dir ../data/demo` → `T-demo-pass` /
  `T-demo-repair` / `T-demo-wait`；仅用显式注入的脚本模型生成，不冒充真实模型。
- 接口文档：`docs/API.md` 与 `GET /docs`（OpenAPI）。验收矩阵：`docs/Acceptance.md`。

## 最终交接声明（P12）

- **可运行入口**：前后端启动脚本、演示数据脚本、`docs/API.md`；清洁启动实测通过（`/api/health`、
  `/api/workflows/templates`、未知版本 404）。
- **真实验证结果**：后端 `uv run pytest -q` → **195 passed, 1 xfailed**；前端 `tsc --noEmit`、`vitest run`
  → **12 passed**、`vite build` 通过；A01–A14、A16–A37 场景通过，逐条证据见 `docs/Acceptance.md`。
- **未完成项**：① 真实 DeepSeek 模型联调（未配置 `HW2_MODEL_API_KEY`，`model_configured=false`）；
  ② A15「并行中间进程退出」未达标（已完成分支回报在批次中途崩溃后丢失，strict xfail 复现，修复方向见上）。
  两者均在 `docs/Acceptance.md` §5 与「未完成项」中如实标注，未以脚本模型结果冒充真实通过。
- **已知限制**：见上「已知限制」；其中并行批次中途中断的逐分支重放（A15）尚未实现。

## 关键实现选择（P11）

- **`completed/false` 可达且受控**：`finish(false)` 由 `TaskService.confirmed_required_failure` 判定——当前版本
  必需检查全部有定论且至少一项 `FAILED`、且 `has_in_flight_work` 为假时才受理；父模型的不可修复原因写入
  报告 `conclusion_scope` 与 `execution_summary.unfixable_reason`。额度耗尽仍走 `waiting_recovery`（`workflow/nodes.py`）。
- **孤儿 attempt 关闭必须带证据**：`RecoveryCoordinator._close_orphan_attempts` 先保存中断证据产物再以之为
  controller 终态的 `error_ref`，修除了并行批次存在在飞 attempt 时恢复抛 `ValidationError` 的崩溃。
- **验收缺口可复现**：A15 以 `@pytest.mark.xfail(strict=True)` 固定；一旦实现修复会变为 XPASS 并因 strict 报错，
  提醒同步更新 `docs/Acceptance.md`。
