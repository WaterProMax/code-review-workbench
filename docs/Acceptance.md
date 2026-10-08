# HW2 综合验收与测试矩阵（P11）

> 最新浏览器联调追加记录：修复布局、画布反馈和恢复后的旧拒绝反馈问题，后端全量 **216 passed**、前端 **13 passed**、生产构建通过；通过页面完成真实并行任务与恢复任务。详见 [前端浏览器联调记录](FrontendTest-2026-10-08.md)。下文 215 项为上一轮修复的历史统计。

本文记录 ImplementationPlan §15 的验收执行情况：每个必须通过的场景（A01–A37）的输入、
运行方式、实际结果与可定位证据。所有结论来自本仓库**真实组件**（真实 SQLite、真实工作区、
真实 LangGraph 检查点、真实 pytest 执行器、真实子进程中断）的自动化测试，不使用 mock 或
固定成功函数冒充。**修复后真实 DeepSeek 四场景同版回归已通过（4/4）**（见 §5），其结论在本文件中如实标注，
不以脚本模型结果替代。

## 1. 运行方式与复现命令

后端（`backend/`）：

```bash
cd backend
uv sync --dev
uv run pytest -q                      # 全量；本次 215 passed
uv run pytest tests/e2e -q            # HTTP 与验收场景
uv run pytest -q --collect-only       # 列出全部用例 id
```

前端（`frontend/`）：

```bash
cd frontend
npm ci
npx tsc --noEmit && npx vitest run && npm run build
```

运行模型配置探针（不读取 `.env`，走运行时接口）：

```bash
cd backend && uv run python -c "import asyncio,httpx,tempfile;from pathlib import Path;from httpx import ASGITransport;from app.main import create_app;from app.settings import Settings;
async def m():
 s=Settings(data_dir=Path(tempfile.mkdtemp())/'h');s.ensure_dirs();a=create_app(s)
 async with httpx.AsyncClient(transport=ASGITransport(app=a),base_url='http://t') as c:print((await c.get('/api/health')).json())
asyncio.run(m())"
# 本次输出：{'model_configured': False, 'model_provider': 'deepseek', ...}
```

## 2. 结果汇总

| 汇总 | 结果 |
|---|---|
| 后端用例 | **215 passed**（`uv run pytest -q`，约 25s） |
| 前端用例 | **13 passed**（workbench 5 + orchestration 7 + polling 1，`npx vitest run`） |
| 类型检查 / 构建 | `tsc --noEmit` 通过；`vite build` 通过 |
| 必须通过场景 | **A01–A37 通过**；A15 已取消 xfail，增加恢复后 completed/true 断言（见 §5.1） |
| 真实模型联调 | **4/4 completed / passed=true**，最终源码同版回归（见 §5.2） |

测试分层（§15.1）：协议与完成检测用单元测试；数据库、工作区、图与恢复用真实组件集成测试；
用户关键路径用 HTTP/浏览器端到端测试。模型输出由脚本模型控制不稳定因素，但真实
LangGraph、SQLite、文件工具与补丁应用**不替换**为固定成功函数。

## 3. 必须通过的场景（A01–A37）

证据列中的 `T(x)` 表示该场景由这些用例证明；路径均相对 `backend/`（前端为 `frontend/`）。

| 编号 | 场景 | 运行方式（用例） | 实际结果 |
|---|---|---|---|
| A01 | 首次审查通过 | T1 `tests/integration/test_sequential_workflow.py::test_first_review_pass_skips_fix_and_verify` | root completed/true；fix、verify 置 skipped 且带 skip_reason |
| A02 | 一轮修复成功 | T1 `test_review_fail_fix_apply_verify_pass`；T2 `tests/integration/test_workspace.py::test_patch_publishes_a_new_version_and_leaves_the_base_untouched` | 有真实 diff、新快照与绑定新版本的验证证据；基础版本未改 |
| A03 | 一轮失败二轮通过 | T3 `tests/integration/test_retry_and_budget.py::test_failed_verification_triggers_a_second_repair_round` | 同一 task_id，attempt 递增；第二轮输入含首轮失败证据 |
| A04 | 两轮修复耗尽 | T4 `test_exhausted_repair_budget_waits_and_keeps_the_verdict` | waiting_recovery；passed=false；保留失败项与追加建议 |
| A05 | 验证执行超时后恢复 | T5 `tests/integration/test_parallel_and_timeout.py::test_timed_out_attempt_gets_a_controller_terminal` | 分类 execution_fault；同版本新 attempt；不派发修复 |
| A06 | 必需证据缺失 | T6 `tests/integration/test_agents.py::test_reviewer_reports_insufficient_evidence_as_null`；T7 `tests/e2e/test_acceptance_scenarios.py::test_a25_required_check_that_never_runs_keeps_the_verdict_null` | 不返回 true；逐项指出缺失项，等待或补查 |
| A07 | 补丁不匹配/语法错误 | T8 `tests/unit/test_diff.py`（malformed/严格匹配/行数）；T9 `tests/integration/test_workspace.py::test_patch_with_syntax_error_is_never_published`、`::test_patch_for_a_stale_base_version_is_rejected`、`::test_patch_targeting_tests_is_rejected` | 不发布新版本；失败回到父 Agent |
| A08 | 无变化/重复失败 | T10 `tests/integration/test_retry_and_budget.py::test_repeated_identical_patch_is_detected_as_no_progress` | 检测 no_progress；有限停止，有限额度 |
| A09 | 重复回报 | T11 `tests/integration/test_persistence.py::test_result_receipt_is_idempotent_and_rejects_conflicting_content`；`::test_batch_receipts_require_expected_attempts_and_are_idempotent` | 相同内容幂等；不同内容冲突；每 attempt 一个已接受终态 |
| A10 | 迟到或错配回报 | T12 `tests/integration/test_agents.py::test_adapter_rejects_a_report_whose_identity_mismatches`；T13 `tests/e2e/test_acceptance_scenarios.py::test_a30_mismatched_and_late_reports_are_audited_only` | task/attempt/batch 任一不匹配：仅审计，不改判定 |
| A11 | 并行成功 | T14 `tests/integration/test_parallel_and_timeout.py::test_parallel_recheck_and_verify_on_one_frozen_snapshot` | 两分支真实重叠、同快照；全部回报后父 Agent 决策一次 |
| A12 | 并行一边故障 | T15 `tests/e2e/test_acceptance_scenarios.py::test_a12_parallel_branch_fault_is_collected_and_returns_to_the_parent` | 批次收齐终态（AGENT+CONTROLLER）；保留另一支证据；不死锁；故障回到父 Agent |
| A13 | 收到回报后进程退出 | T16 `tests/integration/test_process_interruption.py::test_killed_process_is_resumed_without_duplicating_work`；T17 `test_persistence.py::test_reopening_the_database_still_shows_tasks_and_events` | 复用持久化回报，不重复调用已完成子任务 |
| A14 | 补丁发布后进程退出 | T18 `tests/integration/test_recovery.py::test_published_but_uncommitted_patch_is_recovered_without_reapplying`；`::test_resume_after_interruption_does_not_duplicate_or_reapply` | 由快照确认提交，不重复应用补丁 |
| A15 | 并行中间进程退出 | `tests/e2e/test_acceptance_scenarios.py::test_a15_parallel_mid_batch_exit_keeps_the_finished_branch_evidence`；`::test_a15_unfinished_attempt_is_closed_with_a_controller_terminal` | 已完成分支保留单次 attempt 和报告；只重派中断分支；补丁应用一次；最终 completed/true |
| A16 | 重复/并发 resume | T19 `test_recovery.py::test_resume_grants_budget_atomically_and_is_idempotent`；`::test_resume_rejects_unknown_or_over_cap_grants_without_partial_writes`；T20 `tests/e2e/test_api.py::test_http_resume_after_exhausted_repair_budget` | 同操作幂等；竞争只一个有效；不重复追加额度 |
| A17 | 失效执行继续返回 | T21 `test_recovery.py::test_stale_executor_cannot_publish_results` | fencing/旧 attempt 写入被拒 |
| A18 | 恢复追加一轮 | T22 `test_recovery.py::test_resume_grants_budget_atomically_and_is_idempotent`；T20 `test_api.py::test_http_resume_after_exhausted_repair_budget` | 累计计数保留、最大额度增加、有追加记录 |
| A19 | 上传及产物边界 | T23 `test_api.py::test_upload_rejects_unknown_suffix_and_unsafe_path`；T24 `test_workspace.py::test_artifact_reads_are_confined_and_ownership_is_checked` | 路径穿越/超限/跨任务引用被拒 |
| A20 | 父模型非法决策 | T25 `test_sequential_workflow.py::test_illegal_parent_action_is_corrected`；T26 `tests/e2e/test_acceptance_scenarios.py::test_a37_parent_correction_limit_waits_with_a_reason` | 校验拒绝、有限纠正；不能绕过依赖或凭空 finish |
| A21 | 画布保存并运行 | T27 `frontend/tests/orchestration.test.tsx`（7 项）；T28 `test_api.py::test_workflow_templates_and_illegal_config_rejection` | 重载一致；合法变更改变真实执行路径；非法配置不能运行 |
| A22 | 新 Agent 接入 | T29 `tests/integration/test_extension_agent.py::test_extension_role_is_dispatched_reported_and_used_as_verifier_input`；T30 `test_api.py::test_extension_workflow_runs_over_http`；T31 `tests/unit/test_config_files.py::test_extension_template_is_registered_and_expands_from_the_plugin` | 无专用父派发分支；注册后统一执行/回报/日志/版本恢复 |
| A23 | 刷新与增量日志 | T32 `frontend/tests/workbench.test.tsx`（5 项）；T33 `test_api.py::test_full_flow_over_http`（增量 events）；T34 `test_persistence.py::test_event_sequence_is_unique_under_concurrency` | 同一任务不重复提交；事件无重复/遗漏；状态与产物仍可查 |
| A24 | 缺模型密钥/真实调用失败 | T35 `tests/integration/test_agents.py::test_llm_client_requires_real_credentials`、`::test_scripted_model_never_replaces_a_missing_real_model`；T36 `test_api.py::test_submit_without_model_configuration_returns_503` | 明确配置/故障状态；脚本结果不冒充真实通过 |
| A25 | 必需检查未运行/零测试/全部跳过 | T7 `test_a25_required_check_that_never_runs_keeps_the_verdict_null`；T37 `tests/unit/test_protocol_samples.py`（behavior not_run / 零测试 / 全跳过非法样例） | 不能 completed/true；逐项展示 not_run 及证据缺口 |
| A26 | 删除必需项或语法替代行为验证 | T38 `test_a26_contract_cannot_be_downgraded_or_proved_by_a_non_executable_method` | 合同校验拒绝降级；已有必需项不丢失 |
| A27 | 无现成测试的可复现缺陷 | T39 `test_agents.py::test_verifier_proves_a_defect_with_a_generated_test`；T29 `test_extension_agent.py`（基础版本失败证据） | 同测试在原版本失败、修复版本通过，有预期值来源 |
| A28 | 补丁删除/弱化验收测试 | T40 `tests/unit/test_diff.py::test_protected_test_paths`；T41 `test_workspace.py::test_patch_targeting_tests_is_rejected` | 拒绝发布，保留原因，不减少执行测试数 |
| A29 | 明确无法自动修复 | T42 `test_a29_explicitly_unfixable_run_completes_false` | completed/false；含缺陷、能力限制与收尾记录；不盲目派发 |
| A30 | 等待任务终止与恢复竞争 | T13 `test_a30_mismatched_and_late_reports_are_audited_only`；T43 `test_recovery.py::test_terminate_preserves_null_verdict_and_skips_children` | terminate/resume 仅一个生效；结论区分 false/null；终态拒绝 resume |
| A31 | 超时终态与正常回报竞争 | T44 `test_a31_final_state_refuses_resume_so_terminate_wins`；T45 `test_api.py::test_restart_keeps_tasks_queryable_and_rejects_terminate_on_running` | 唯一终态；控制层合法关闭等待项；迟到回报仅审计，不死锁 |
| A32 | 验证额度耗尽后恢复 | T46 `test_a32_verify_top_up_grants_only_verify_and_over_cap_writes_nothing`；T19 `test_recovery.py::test_resume_rejects_unknown_or_over_cap_grants_without_partial_writes` | 无追加时拒绝且不部分更新；追加 verify 后继续，repair 计数不变 |
| A33 | 修复故障重派与恢复重放 | T47 `test_a33_fix_fault_retry_consumes_both_declared_budgets`；T11 `test_persistence.py::test_budget_ledger_counts_once_per_operation`；T5 `test_parallel_and_timeout.py` | 新 fix attempt 同计 repair/fix 故障额度；重放不重复扣减 |
| A34 | 画布模式冲突与默认值冻结 | T48 `test_api.py::test_a34_mode_conflict_and_frozen_workflow_defaults`；T31 `tests/unit/test_config_files.py` | 不一致请求被拒；运行固定展开配置；旧任务不受新工作流影响 |
| A35 | 补充材料/目标的恢复请求 | T49 `test_api.py::test_a35_resume_cannot_change_the_task_inputs` | 原任务拒绝新 source/goal；旧证据仍可查 |
| A36 | 新版本/新测试集复用旧证据 | T50 `test_a36_old_version_evidence_cannot_close_a_new_version_finding` | 对源码版本核验；旧版本证据不能关闭新版本问题 |
| A37 | 图步数或父纠正次数耗尽 | T51 `test_a37_graph_step_limit_waits_with_a_reason`、T26 `test_a37_parent_correction_limit_waits_with_a_reason`；T25 `test_illegal_parent_action_is_corrected` | 进入等待并记录原因；仅显式 resume 授予新许可，重启不自动续额 |

## 4. 前端与浏览器验收（P09/P10）

- `frontend/tests/orchestration.test.tsx`（7）：模板加载与 9 条合法执行边 + 输入依赖；角色下拉
  仅列出能承担该节点任务类型的角色；扩展模板含 `test_artifact` 依赖；非法连线标红且禁用保存；
  后端字段级错误定位到 `nodes.ghost.task_kind`；预算编辑与快速保存的目标版本随请求发出。
- `frontend/tests/workbench.test.tsx`（5）：事件增量拉取与去重、问题状态取权威接口、三态展示等。
- 真实浏览器联调（P09/P10，见 `docs/Handoff.md`）：工作台与编排页连接真实后端，呈现三种交互类
  （首次通过 / 修复成功 / 待恢复）、刷新续拉同一任务、加载三个模板并真实保存 `wf-canvas-verify`
  （`curl /api/workflows/wf-canvas-verify` 回读确认）。**生产代码无 mock 分支**。
- 已知环境限制（非代码缺陷）：内置 Browser 面板未前台（`document.hidden`）时 React Flow 无法测量
  连线句柄，画布不绘制连线；结构与合法性的正确性由上述前端测试与后端模板/校验测试覆盖。

## 5. 修复后专项验收（2026-10-08）

### 5.1 A15「并行中间进程退出」已修复

逐分支返回时立即接收落库；以父决策 ID 派生派发幂等键，检查点重放复用原批次及已接受结果；仅为中断分支生成新的故障重试 attempt。真实 SIGKILL 回归同时验证：已完成验证分支不重跑、报告保留、补丁只应用一次、恢复后 completed/true。两个 A15 用例均通过，无 xfail。

```bash
cd backend
uv run pytest -q tests/e2e/test_acceptance_scenarios.py -k a15
uv run pytest -q tests/integration/test_review_regressions.py
```

### 5.2 真实 DeepSeek 模型联调

用户授权仅发送 clean、repairable、multi_file 三组课程示例及其检查目标。初测规划阶段 0/4 的历史记录保存在 [真实模型联调记录](RealModelTest-2026-10-08.md)。后续修复结构化输出、工具参数、测试绑定、证据复用、重复问题及并行规划问题；最新专项记录见 [修复与回归记录](RepairReport-2026-10-08.md)。

最终源码同版回归 **4/4 completed / passed=true**：clean T4595192d71、repairable Taf7d012c27、multi_file T2bc28f7f34、parallel Taca9453ca8。并行复审与验证同批次、同版本、执行时间真实重叠，两个报告均收齐。源码摘要、最终运行数据和去敏证据见 [修复与回归记录](RepairReport-2026-10-08.md)。

```bash
cd backend
.venv/bin/python scripts/smoke_real_model.py
# 按需补跑指定场景；密钥在隐藏输入中提供
.venv/bin/python scripts/smoke_real_model.py --cases parallel
```

## 6. 证据定位

- 用例与断言：`backend/tests/**`（本文件 §3 的 T 编号）。
- 中断驱动子进程：`backend/tests/e2e/interruption_driver.py`（顺序）、
  `backend/tests/e2e/interruption_driver_parallel.py`（并行，A15）。
- 设计取舍与已知限制：`docs/Handoff.md`「已知限制」「未完成项」。
- 接口与字段语义：`docs/API.md`、`Desgin.md`、`Architecture.md`。
