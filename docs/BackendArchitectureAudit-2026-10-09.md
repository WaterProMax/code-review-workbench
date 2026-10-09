# 后端架构对照与修复记录（2026-10-09）

## 范围与结论

依据 ImplementationPlan.md 的 §1、§5–§12、§14，检查当前后端实际装配、调用路径、存储和测试。
文档中的历史“已完成”记录不作为本次审查通过的替代证据。架构核查及最初回归阶段没有重跑真实 DeepSeek 联调；
下列回归使用临时数据库、文件副本、真实 LangGraph/SQLite/pytest，以及显式注入的脚本模型。

主干架构符合约定：父模型提出计划与动作，父控制层核验，统一注册表与适配器执行子任务，
子任务回报归并到父层；补丁应用是程序节点；完成结论由合同、当前版本证据和运行状态共同决定。
不需要重建工作流架构，但修复前有一处入口归属偏差和七处边界缺陷。

## 实际实现与计划的对应

| 计划要求 | 实际代码与执行路径 | 本次判断 |
|---|---|---|
| §1.2 独立项目 | backend/app 无 Test1 导入；项目独立依赖与配置 | 符合 |
| §8.1、§12.1 父入口调用受控建任务工具 | 修复前 api/tasks.py 直接生成 root ID；现调用 ParentController.initialize_task → TaskCreationService.create_root | 本轮修正；模型不编造 ID，API 不再直接生成 ID |
| §1.2 父模型真实参与 | agents/parent.py 的 plan/replan/decide 调用 LLMClient；providers/factory.py 正式装配 DeepSeek，测试显式注入脚本客户端 | 符合实现要求；本轮未重新验证真实服务商 |
| §7.4 统一协议与权限 | AgentRegistry、AgentProtocol、AgentNodeAdapter、ToolRegistry；独立角色提示词 | 符合 |
| §8.2 条件分支与反馈闭环 | workflow/builder.py 的真实 StateGraph；parent_decide → validate_action → dispatch → collect_results → detect → parent_decide | 符合；子任务不直接完成根任务 |
| §10 同版本并行、收齐后决策 | nodes.py 在图的 dispatch 节点内 asyncio.gather；每个分支先持久化回报，批次归并后返回父决策 | 符合；不是另建互相调用的子 Agent |
| §6.2–6.3 不可变输入、补丁程序节点 | WorkspaceService 的上传/快照/候选副本、prepared/committed 应用账本；apply_patch 独立节点 | 符合 |
| §5.3、§8.4 检查合同与完成检测 | AcceptanceContract、CheckSpec/CheckResult、Detector、TaskService.detect_completion；n_finalize 重新校验 | 主干符合；本轮补齐规则与通配符校验 |
| §6.1、§6.4 持久化与自动日志 | SQLite 迁移、事务仓库、EventSink；AsyncSqliteSaver 独立检查点库，root 映射稳定 thread_id | 符合 |
| §9、§11 额度、执行权与恢复 | 预算账本、DB 租约、fencing token、WorkflowRunner、RecoveryCoordinator；恢复不清零累计额度 | 主干符合；本轮补齐快速重启和请求幂等边界 |
| §5.4、§5.3.6 最终状态与报告 | 正常 finalize 有报告；修复前人工 terminate 缺报告 | 本轮修正，包括尚无合同的 partial/null 终止 |
| §7.2 固定测试命令与有界输出 | 固定 pytest 参数、独立执行目录、超时/取消杀进程组 | 本轮改为实时读取管道并保留有限输出尾部 |
| §14 保存配置影响执行、扩展注册 | WorkflowConfig 的结构校验/标准依赖展开；ExtensionRegistry 插件提供角色、依赖、输入与重试策略 | 符合首版范围；不是任意 DAG 编排 |

这里的“符合”指本次检查过的结构与关键路径，不等同于任意输入下都没有缺陷。
完整回归同时覆盖顺序、并行、扩展、补丁保护、真实子进程中断与额度恢复等既有验收场景。

## 本轮修复

1. 父合同校验拒绝未注册的静态规则；执行器对旧合同中的非法规则返回 not_run/executed=false，不能误报 passed。
2. 校验与执行共用文件 scope 解析。支持 fnmatch 风格的 `*`、`?`、`[]`，以及 `file.py:symbol` 文件前缀；逐项要求匹配文件。未匹配范围不回退到全项目，语法/静态范围没有 Python 文件也不能通过。
3. lifespan 启动扫描后，每 5 秒复查失效执行。租约、当前状态、孤立尝试关闭与中断事件在同一事务中重查/更新；释放旧租约并递增 fencing token。周期扫描跳过本服务运行句柄与尚未取得租约的新 queued 任务；其他服务有效租约也受保护。仅标记可恢复状态，不自动续跑或追加额度。
4. resume/terminate 重放必须同时匹配请求摘要、root_task_id 和 operation_kind，跨任务或跨操作复用返回幂等冲突。
5. 提交经父初始化入口，在 SQLite 写事务中复查幂等记录、创建根任务、登记输入产物和创建事件。竞争请求复用获胜任务，只由首次创建方启动后台运行。
6. pytest 使用 `-s` 关闭自身文件捕获；stdout/stderr 通过管道实时读取，每路仅保留末尾 20,000 字节，解码后的返回值也有字符上限。最终摘要仍用于测试数量与结论分类。
7. terminate 在同一业务事务中生成并登记 FinalReport、收尾子任务和完成事件。无合同/无证据时报告列明缺失项，保留 partial/null；报告存储失败则回滚终态，允许重试。重复终止不重复生成报告。

## 验证

新增 `backend/tests/integration/test_backend_review_fixes.py`，17 个回归用例，包括：

- 未注册规则、混合/单独通配符、未匹配范围、无 Python 文件、通配符内语法失败。
- 快速重启时先保留有效租约，随后通过实际 lifespan 监控自动标记 interrupted；旧执行权失效。
- 保护正常任务和新 queued 任务；跨任务/跨操作幂等键无副作用冲突。
- 两个独立事件循环并发进入真实提交处理函数，SQLite/工作区实际创建一个根任务，并仅调度一次。
- 真正 pytest 子进程输出 stdout/stderr 共 8 MiB，尾部有界且正确识别失败摘要。
- 真实 HTTP 工作流等待 → 人工终止 → 查询与下载报告；重复终止只有一份报告。
- 无合同终止保留证据不足；报告失败不留下无报告的最终任务。

最终命令（backend 目录）：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider`。
结果：**233 passed in 24.98s**，无失败或 xfail。相比审查前 216 项，增加 17 项针对性回归。
`git diff --check` 也通过。与详细记录同步见 [Acceptance.md](Acceptance.md)。

## 使用边界

架构核查及最初回归阶段只修复本地代码并同步文档，没有提交或推送 GitHub，也没有改变前端已有修改。
当前运行中的旧后端进程需重启后才能加载本轮代码。原有历史误报不会被自动改写，验证新行为应新建任务。
计划 §7.2 的本地可信课程示例边界保持不变；此次修复不增加公网部署或不可信代码隔离能力。

后续用户授权推送与五次真实联调已完成：修复提交 `419ca1e` 已推送，DeepSeek 五次完整流程 5/5 通过。
测试新进程加载该版代码；详细证据与范围见 [五次真实联调记录](DeepSeekFiveRuns-2026-10-09.md)。
