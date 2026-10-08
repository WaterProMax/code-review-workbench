# 代码修复与回归记录（2026-10-08）

按用户“修复问题”的请求，修复此前代码审核中确认的 7 项问题，并处理真实 DeepSeek 联调暴露的阻断。ImplementationPlan.md 作为验收依据，文末实施指令未作为额外用户请求执行。

## 审核问题

| 问题 | 修复 | 回归证据 |
|---|---|---|
| 生成测试在缺陷基础版本也通过，仍判定修复成功 | 修复证明必须在基础版本失败；明确属于修复目标的检查必须失败；其他检查只有真实通过基础版才能作为回归覆盖，未声明目标范围时要求既有通过证据与测试内容一致 | test_baseline_proof_and_unchanged_regression：无基础证据、基础失败、替换测试均不能冒充通过；原有通过测试可复用 |
| 过期执行器能提交结果或继续写入 | Attempt/Envelope 保存派发令牌；写事务及快照发布核验 ContextVar 中的执行令牌和租约；服务实例使用独立 owner | test_stale_agent_result_rejected、test_stale_execution_cannot_publish_snapshot、test_fenced_background_error_does_not_overwrite_new_run |
| 追加图步数后仍不能恢复 | 父决策读取持久化 graph_steps_granted；恢复追加冻结工作流预算；耗尽不额外消费一步 | test_resume_grants_a_new_graph_segment |
| 增量事件漏掉边界事件 | next_seq 返回最后一条已交付事件序号，继续使用 sequence > after_seq | test_incremental_events_keeps_boundary；API 文档同步 |
| 超时/取消后测试子进程存活 | Popen 独立进程组，超时/取消均终止整个组并回收；后台线程结束后再清理运行目录 | test_timeout_reaps_descendants、test_cancelled_check_reaps_descendants |
| 恢复后界面不再轮询 | refresh 重启现有 tick，沿用事件游标，直到任务结束 | frontend/tests/polling.test.tsx |
| A15 并行中途崩溃丢失已完成报告 | 分支返回立即落库；父决策派发具有幂等键；恢复只重派中断分支，复用已完成报告 | A15 真实 SIGKILL 用例不再 xfail；断言单次验证、报告保留、补丁只应用一次、最终 completed/true |

## 真实模型联调修复

- 结构化输出提供完整 JSON Schema，规划协议补齐 reason 和检查阶段约束；工具参数 Schema 从实际 Pydantic 参数模型生成。
- reviewer 对生成测试进行逐项 check_id 覆盖校验，输出不完整时要求模型纠正；可在独立产物中生成行为测试，执行器在模型循环结束后按冻结合同执行；verifier 复用测试包，收集问题账本中尚未解决的必需问题。
- 同一检查同一尝试补测后更新检查结果投影，保留每次执行的不可变检查产物；证据文件名包含唯一产物 ID，重复名称不会覆盖旧内容。
- 同一尝试同一问题位置使用稳定 Finding ID，重复提交或仅调整描述不会生成重复未解决问题。
- 规划将并行/收齐分支等要求放入 execution_requirements，控制层拒绝把它们变成源码检查；修改后复审与验证共同复用原有测试包，并明确测试的根目录与导入方式。
- 子 Agent 输出上限提高为 8192 tokens，避免较长测试代码或结构化结果被 2048 tokens 截断。
- smoke_real_model.py 从隐藏输入读取密钥，记录源码摘要、模型 usage、事件、尝试、版本、报告；不保存密钥。

## 验证

- 后端全量：215 passed，0 xfail（约 25s）。
- 前端：13 passed；TypeScript 和 Vite 生产构建通过。
- 真实 DeepSeek 四场景：**4/4 完成，全部 completed / passed=true**；最终两次分组回归使用同一源码摘要。

新增持久化迁移 0002_execution_tokens.sql 由启动流程自动应用；旧尝试令牌默认 0。未改变 examples 原始课程源码，也未将 API 密钥写入代码或配置。

## 最终真实模型证据

北京时间 18:51–18:54，最终源码摘要：`13244d513a3705623f5cb95227cc9302ee7477b8fae65f4b477bc2aafc24617e`。两次分组运行均与当前后端源码一致；使用正式模板、真实 FastAPI/SQLite/LangGraph/工作区/pytest，没有脚本模型注入。

| 场景 | 总任务 ID | 结果 | 耗时 |
|---|---|---|---|
| clean | T4595192d71 | completed / true | 20s |
| repairable | Taf7d012c27 | completed / true | 65s |
| multi_file | T2bc28f7f34 | completed / true | 60s |
| parallel | Taca9453ca8 | completed / true | 50s |

- [三组顺序场景汇总](../data/real-model-20261008-185217/real-model-results.json)；[最终并行场景汇总](../data/real-model-20261008-185140/real-model-results.json)。目录还保存 detail、attempts、events、artifacts、报告、业务库、检查点和源码快照。
- 请求模型 deepseek-chat，供应商实际返回 deepseek-flash。最终四场景共 79 次调用、535,684 tokens；统计不含连通性探针及此前定位问题的调试运行。
- 并行验证 Taca9453ca8-C003-A1 与复审 Taca9453ca8-C004-A1 同属批次 Taca9453ca8-B3，同一版本 sv-52d0249d1b15143b2e33bdd3fdcbc944；执行时间分别为 18:52:12.707–19.647、18:52:12.714–25.417，确实重叠；两个报告收齐后父 Agent 才结束。
- 已逐一核验最终两组数据内 196 个产物文件的 SHA-256，与业务库登记一致。此前扫描 1,568 个项目/证据文件，未检出 API 凭据格式；密钥仅用于隐藏输入和进程内调用。
- 本次为这四个受控示例的真实通过证据；模型仍可能提出非法工具参数，控制层会拒绝并要求纠正，不能据此保证所有任意项目均一次通过。
