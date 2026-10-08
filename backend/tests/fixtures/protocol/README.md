# P01 协议样例（合法 / 非法）

这两个文件是 P01 的合同样例，由 `backend/tests/unit/test_protocol_samples.py` 直接驱动。
每个样例自带所属 schema 名称；`ParentAction` 是判别联合，用 `parse_parent_action()` 解析。

## legal.json

按 schema 分组，每个条目必须能被对应模型 `model_validate`，并能 `model_dump(mode="json")`
后再次解析得到相等的对象（往返一致）。

| schema | 覆盖的边界 |
|---|---|
| `Task` | 总任务（running）、子任务（queued）、首次通过后被跳过（skipped + skip_reason） |
| `Attempt` | 同一逻辑任务的第 2 次尝试，带 `retry_reason` 与输入引用 |
| `TaskEnvelope` | 完整派发身份、`contract_version`、`input_refs.acceptance_contract`、约束 |
| `TaskResult` | 审查 completed/true、验证 completed/false、修复 completed/null、执行故障 failed/null |
| `AcceptanceContract` | 冻结检查项映射（syntax / static_rule / behavior_test 各有必需项） |
| `CheckResult` | 语法 passed、行为测试 passed（含测试计数与摘要）、not_run | 
| `Finding` | 必需目标问题 open，以及带当前版本证据的 resolved |
| `Patch` | unified_diff 主格式与 structured_edits 备选格式 |
| `PatchApplication` | committed（含 result_version）与 failed（含错误） |
| `VerificationReport` | 修改后版本上全部必需检查 passed |
| `FinalReport` | 逐项列出必需检查、最新证据与结论范围 |
| `ParentAction` | dispatch_task / dispatch_batch / apply_patch / wait_for_recovery / finish |
| `TerminalRecord` | 控制层超时 failed 终态、Agent completed 终态 |
| `BudgetLedgerEntry` | 消耗（delta<0）与恢复时追加授予（delta>0） |
| `Artifact` | 证据产物的归属、版本与摘要 |
| `FileEntry` | 规范化相对路径 |
| `ExecutionEvent` | 带时区的工具结束事件与耗时 |
| `AgentSpec` | 基础角色的能力、工具授权与重试预算 |
| `TaskControl` | 租约、心跳、fencing token、run segment 与图步数 |
| `IdempotencyRecord` | 请求幂等键到结果的映射 |
| `WorkflowConfig` | 顺序模板与并行模板（含输入依赖 `dependencies`） |

## illegal.json

每个条目包含 `payload` 与 `reason`；解析必须抛出 `ValidationError` 或 `ValueError`。
覆盖的合同边界：身份错配（缺 `parent_task_id`）、非法状态组合（failed + passed=true、
completed + error、skipped 缺原因）、缺失版本（缺 `contract_version`、缺 `acceptance_contract`）、
不支持的动作（`dispatch_test_generator`）、零测试/全部跳过/含失败却判 passed、
必需问题无证据关闭、补丁双格式、控制层终态 completed、工作流非法连线
（review→fix、缺 verify→parent、并行缺 recheck、顺序含 recheck、预算超上限、多结束出口）等。

## 不在样例范围内的校验

“回报必须同时匹配 root/task/attempt/batch/agent 版本/源码版本/合同版本”属于**接收期**的身份核对，
需要与派发记录比对，因此不在 schema 层实现，由 P02 的接收账本与 P04 的 `collect_results` 校验，
对应验收场景 A09/A10/A13。schema 层只保证单对象内部自洽。
