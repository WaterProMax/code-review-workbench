# Homework 2 项目说明与技术设计

本项目构建一个由父 Agent 管理的代码审查与修复工作台。用户上传代码并说明检查目标，父 Agent 建立总任务和子任务，选择审查、修复、验证 Agent 执行，并检测回报是否满足完成条件。系统提供任务树、执行日志、修改差异、检查证据和恢复入口。

本文规定开发框架、模块职责、数据字段、接口和使用方式。整体编排与流程图见 [Architecture.md](Architecture.md)，本文侧重实现这些流程需要的数据结构和调用约定。项目在 Test2 中独立开发。

详细阶段、预算默认值、完整配置样例与验收场景见 [ImplementationPlan.md](ImplementationPlan.md)。其中 §5.3、§5.4、§7.5、§9.4、§10.1、§11.3 和 §14.1.1 分别细化检查合同、状态、验证、预算、超时终态、恢复和配置规则，与本文共同作为实现约定。

## 1. 项目能力与技术分工

### 1.1 项目能力

| 能力 | 含义 |
|---|---|
| 代码审查 | 对约定范围的代码执行分析，输出问题、位置和证据 |
| 代码修复 | 根据问题和失败反馈生成补丁，修改工作副本 |
| 修复验证 | 检查修改后代码，执行约定检查或测试，返回实际证据 |
| 父 Agent 管理 | 建立任务、派发执行、接收回报、检测结果、安排下一步 |
| 执行可追踪 | 查看每个任务、每次尝试及工具调用发生了什么 |
| 异常可恢复 | 暂时性错误有限重试，中断或无法继续时保存任务等待处理 |
| Agent 可扩展 | 通过统一协议、注册描述和配置接入新 Agent |

第一版面向 Python 文件和小型 Python 项目。上传文件组成一个输入项目，源码保存为快照；补丁作用于本任务的工作副本。支持其他语言时增加语言对应的工具与验证实现，统一任务协议继续使用。

### 1.2 技术分工

| 技术或模块 | 在项目中的职责 |
|---|---|
| Python | Agent、调度控制、工具、数据模型和服务端实现 |
| LangGraph | 执行节点、状态更新、条件路由、循环、并行及检查点恢复 |
| LLMClient | 统一调用模型，默认接入 DeepSeek，保留其他兼容模型的适配接口 |
| FastAPI | 文件输入、任务提交、状态查询、配置保存、报告与恢复接口 |
| React + TypeScript | 工作台、任务树、产物查看和执行时间线 |
| React Flow | 编排配置画布，展示与编辑受支持的节点关系 |
| SQLite | 业务任务记录、事件查询和持久化检查点，各自使用明确的表或存储边界 |
| 本地文件存储 | 源码快照、工作副本、补丁、检查输出和报告 |

LangGraph 是执行框架。父 Agent 的职责、任务 ID、父子关系、任务协议和完成判定由本项目定义。

## 2. 代码组织与模块职责

以下目录是开发结构约定；安装与启动命令在入口实现后补充，本文中的目录和接口示例用于指导开发。

```text
Test2/
├── Architecture.md
├── Desgin.md
├── backend/
│   ├── app/
│   │   ├── api/             文件、任务、配置、报告与恢复接口
│   │   ├── schemas/         任务、回报、事件、产物与配置的数据模型
│   │   ├── agents/          父 Agent、审查、修复与验证 Agent
│   │   ├── workflow/        图构建、节点适配、回报归并、检测与恢复
│   │   ├── registry/        Agent 和工具注册表
│   │   ├── services/        任务、ID、工作区、产物和事件服务
│   │   ├── providers/       模型客户端及服务商适配
│   │   └── storage/         业务记录与检查点适配
│   └── tests/               协议、检测、恢复和端到端验收
├── frontend/
│   └── src/                 上传、编排、任务树、差异、日志和报告页面
├── config/                  默认流程和 Agent 注册配置
└── examples/                无缺陷、可修复、验证失败等演示输入
```

运行数据保存在单独配置的数据目录，包含业务数据库、检查点数据库和按总任务划分的产物目录，不与上传源码混放。每个任务拥有独立工作区。

| 模块 | 输入与输出 | 职责边界 |
|---|---|---|
| 父 Agent | 用户目标、能力目录和执行回报；输出任务计划与调度决策 | 建立任务与 ID、解释结果、选择下一步 |
| 父控制层 | 结构化决策与回报；输出合法派发、状态更新和检测结论 | 实现依赖、版本、额度、回报去重与完成条件的确定性校验 |
| 子 Agent | TaskEnvelope + AgentContext；返回 TaskResult | 执行本角色任务，结果统一返回父 Agent |
| 图构建器 | WorkflowConfig + 固定版本的 Agent 注册项 | 构建 StateGraph，校验派发与回报路径 |
| AgentNodeAdapter | 标准任务与执行实现；输出可合并的状态更新 | 适配 LangGraph，校验数据、记录事件、归一化错误 |
| WorkspaceService | 源码版本、读取或补丁请求；输出快照或应用结果 | 管理版本和文件，提供可重复执行的补丁应用 |
| EventSink | 执行事件；输出持久化记录 | 追踪执行过程，不替父 Agent 作调度决策 |
| 任务服务 | 提交或恢复请求；输出任务状态与报告 | 后台运行、执行锁、心跳和恢复入口 |

## 3. 核心对象与字段关系

### 3.1 总任务、子任务、Agent 和执行尝试

这四个概念分别表示用户目标、工作单元、执行角色和实际执行次数。

| 对象 | 示例 | 与其他对象的关系 |
|---|---|---|
| 总任务 | T100：检查上传项目 | 包含多个子任务、执行批次、版本和报告 |
| 子任务 | T102：修复审查发现的问题 | 属于 T100，指定修复 Agent，可执行多次 |
| Agent | fixer，版本 1.0 | 可服务不同总任务，不等同于某个 task_id |
| 执行尝试 | T102-A1、T102-A2 | 属于同一子任务，每次有独立输入、版本、回报和状态 |
| 派发批次 | B2 | 归属于总任务，包含一项或多项执行尝试 |
| 回报 | R-T102-A1 | 关联具体执行尝试，由父 Agent 接收与检测 |

总任务和子任务 ID 由父 Agent 执行生成：父 Agent 调用受控的 ID 生成与任务建立工具，工具完成唯一性校验和持久化。它属于父 Agent 的执行能力，不由子 Agent 自行编号。进程恢复时相同建任务操作返回已建立的 ID，避免重复生成任务树。

```text
T100 总任务
├── T101 审查任务 → reviewer → T101-A1
├── T102 修复任务 → fixer    → T102-A1、T102-A2
└── T103 验证任务 → verifier → T103-A1、T103-A2
```

T103-A2 必须验证 T102-A2 实际应用后的版本，不能只因为 T102 曾完成过就使用第一轮代码。并行扩展模式可再建立复审任务 T104，由同一个 reviewer 执行；任务数增加不等于 Agent 角色增加。

### 3.2 身份、关联与版本字段

| 字段 | 类型 | 约束与使用方式 |
|---|---|---|
| root_task_id | string | 总任务 ID；总任务自身的值等于自己的 task_id |
| task_id | string | 逻辑任务唯一 ID，重试时保持不变 |
| parent_task_id | string 或 null | 总任务为 null，基础模式下子任务指向总任务 ID |
| agent_id | string | 注册表中的执行角色标识，如 reviewer |
| agent_version | string | 本次运行固定的角色实现版本 |
| task_kind | string | review、fix、verify 等已注册任务类型 |
| attempt_id | string | 一次执行尝试的唯一 ID；新一轮执行生成新值 |
| attempt_no | integer | 同一子任务内递增，从 1 开始 |
| dispatch_batch_id | string | 父 Agent 本次派发批次 ID；并行任务可共享同一批次 |
| workflow_version | string | 总任务固定使用的编排配置版本 |
| schema_version | string | 协议版本，用于输入输出兼容性校验 |
| source_version | string | 项目快照的内容版本，不用可变路径代替版本 |
| contract_version | string | 本次派发绑定的检查合同版本；追加合同保留历史，不放宽原必需项 |
| result_id | string | 回报唯一 ID，用于接收去重 |
| artifact_id | string | 产物 ID，通过产物服务解析到实际存储 |

同一回报必须同时匹配总任务、子任务、执行尝试、派发批次、Agent 版本、源码版本和派发时的合同版本。即使 task_id 正确，过期 attempt 的结果也不能覆盖当前尝试。旧合同下有效回报可以保存，但不能直接作为新增必需检查已完成的证据。

### 3.3 依赖字段

depends_on 表示任务的业务输入依赖，parent_task_id 表示从属关系，两者不能互相替代。

验证任务的依赖至少包含：对应修复尝试已返回、候选补丁已经应用成功、修改后快照已经冻结。父 Agent 先接收修复回报，再调用补丁工具，工具成功后才允许派发验证。

依赖记录使用 task_id、attempt_id、条件和必要的产物引用。例如验证依赖某次补丁应用的结果，而不只是依赖“修复任务 status=completed”。第一轮验证失败时，新验证尝试重新绑定第二轮应用结果。

### 3.4 状态与检查结论

status 描述执行生命周期，passed 描述业务检查结论。前端同时展示两者。

| 对象 | 状态取值 |
|---|---|
| 总任务 | queued、running、waiting_recovery、completed、partial、failed、interrupted |
| 子任务 | blocked、queued、running、completed、failed、skipped |
| 执行尝试 | queued、running、completed、failed、interrupted、invalidated |
| TaskResult.status | completed 或 failed；回报代表一次尝试的执行结果 |

invalidated 表示旧尝试已失效，例如超时后被撤销；其迟到回报只作审计，不参与当前检测。单次模型请求的内部重试记录为事件，父 Agent 重新派发才建立新 attempt。

| 情况 | 执行状态与 passed |
|---|---|
| 审查完整执行，满足约定目标 | completed、true |
| 验证完整执行，仍有失败测试 | completed、false |
| 检查正常返回，但缺少必要覆盖或证据 | completed、null，父 Agent 安排补充检查 |
| 工具无法运行或请求失败 | failed、null |
| 修复 Agent 成功产出候选补丁 | completed、null；补丁尚未证明修复有效 |
| 审查通过后不需要修复与验证 | 子任务 skipped、passed=null，并填写 skip_reason |

总任务 completed、passed=true 必须通过父控制层完成条件检测。审查提前通过时允许跳过后续任务，报告明确结论范围；修改过代码时必须检查修改后版本。总任务 waiting_recovery 保留已知的 false 或 null，不显示为成功。

总任务 completed/false 用于已证实但明确不能自动修复的问题，或用户在等待/中断状态明确终止且仍有有效缺陷证据；partial/null 用于用户终止时证据不足。可恢复的系统故障进入 waiting_recovery，确认不可恢复的执行故障才使用 failed。completed、partial、failed 为首版最终态，不能 resume；waiting_recovery、interrupted 可以恢复。额度耗尽不等同于明确不可修复。

终止入口仅处理 waiting_recovery/interrupted，不处理 running；与恢复竞争同一个 revision 和执行权。结束时将无有效 attempt 且不再需要的 blocked/queued 子任务标记 skipped 并注明原因，已完成或失败的事实及所有 attempt 历史保留。完整转移表见 ImplementationPlan.md §5.4。

## 4. 统一协议与接口设计

### 4.1 TaskEnvelope：派发输入

| 字段组 | 字段 | 作用 |
|---|---|---|
| 身份 | schema_version、root_task_id、parent_task_id、task_id、attempt_id、dispatch_batch_id | 确定本次派发对应的工作单元 |
| 执行目标 | agent_id、agent_version、task_kind、goal | 选择已注册执行者并明确本次目标 |
| 输入 | input_refs、source_version、acceptance_criteria、contract_version | 指定代码、证据、上轮反馈和完成判据；input_refs.acceptance_contract 指向冻结检查合同 |
| 约束 | constraints | 工具权限、执行期限、允许的文件范围和调用额度 |

示例为第二轮修复派发，ID 采用便于阅读的形式；产物 ID 为示例数据：

```json
{
  "schema_version": "1.0",
  "root_task_id": "T100",
  "parent_task_id": "T100",
  "task_id": "T102",
  "attempt_id": "T102-A2",
  "dispatch_batch_id": "B3",
  "agent_id": "fixer",
  "agent_version": "1.0",
  "task_kind": "fix",
  "goal": "根据第一轮验证失败证据修复剩余问题",
  "source_version": "version-1",
  "contract_version": "contract-1",
  "input_refs": {
    "source": "artifact-source-v1",
    "acceptance_contract": "artifact-contract-1",
    "findings": "artifact-findings-1",
    "previous_patch": "artifact-patch-1",
    "verification": "artifact-verification-1"
  },
  "acceptance_criteria": ["为目标问题生成可匹配当前版本的候选补丁"],
  "constraints": {"max_tool_steps": 8, "timeout_seconds": 60}
}
```

示例中的工具步数和超时是可配置值，不表示模型必须在固定时长内完成全部项目。

### 4.2 TaskResult：子任务回报

回报保留派发身份字段与 contract_version，并增加 result_id、status、passed、summary、result_refs、evidence_refs 和 error。时间信息记录 started_at、finished_at，使用带时区的时间；耗时由程序计算。控制层超时/中断终态不是 Agent TaskResult，使用 §7 的 TerminalRecord。

```json
{
  "schema_version": "1.0",
  "result_id": "R-T103-A1",
  "root_task_id": "T100",
  "parent_task_id": "T100",
  "task_id": "T103",
  "attempt_id": "T103-A1",
  "dispatch_batch_id": "B2",
  "agent_id": "verifier",
  "agent_version": "1.0",
  "task_kind": "verify",
  "source_version": "version-1",
  "contract_version": "contract-1",
  "status": "completed",
  "passed": false,
  "summary": "约定测试已执行，仍有一项失败",
  "result_refs": {"verification": "artifact-verification-1"},
  "evidence_refs": ["artifact-test-output-1"],
  "error": null,
  "started_at": "2026-10-08T02:00:00Z",
  "finished_at": "2026-10-08T02:00:03Z"
}
```

此例表示验证执行正常，业务结论未通过。测试失败详情在验证产物中，不冒充系统 error。执行故障的 error 则包含 code、category、message、recoverable 和 detail_ref，由父控制层核验后决定如何恢复。

### 4.3 AgentProtocol、AgentSpec 与 AgentContext

统一执行签名为异步 execute(task: TaskEnvelope, context: AgentContext) → TaskResult。新 Agent 使用相同调用入口，各自实现内部模型与工具逻辑。

| 对象 | 字段或能力 | 关系 |
|---|---|---|
| AgentSpec | agent_id、version、description、supported_task_kinds | 父 Agent 可发现的角色能力目录 |
| AgentSpec | input_schema、output_schema、tool_names、permissions | 适配器和控制层核验可执行性 |
| AgentContext | llm_client、tool_access、workspace、event_sink | 运行依赖，按角色授权 |
| AgentContext | deadline、cancellation、budgets | 所有 Agent 共用的执行约束 |

AgentContext 中的客户端和工具实例不序列化进 State。恢复时根据固定配置重建上下文。可持久化的版本、额度和输入引用留在任务记录中。

### 4.4 父 Agent 的统一决策

父 Agent 从注册表读取能力目录，输出统一决策，不针对每个 Agent 增加一套调度方法。

| action | 关键参数 | 校验重点 |
|---|---|---|
| dispatch_task | agent_id、task_kind、task_id、input_refs、reason | 角色存在、版本固定、依赖满足、输入合法 |
| dispatch_batch | 多个任务派发项、reason | 任务独立性、同一快照、并行结果归并 |
| apply_patch | patch_ref、base_version、reason | 修改目标与当前版本一致，补丁有效 |
| wait_for_recovery | reason、required_action | 保存待处理原因、恢复条件和已有结果 |
| finish | proposed_passed、report_refs、reason | 必须经过完成条件检测，不能直接采用模型结论。`finish(true)` 需要当前版本必需项全部通过；`finish(false)` 仅在控制层确认“当前版本存在明确失败的必需检查、且没有未结束的尝试或未收齐的批次”时受理，并把父模型给出的不可修复原因写入报告——额度耗尽不等于不可修复，仍进 waiting_recovery（§5.4、P11） |

AgentRegistry 提供 register、list_capabilities 和 resolve；AgentNodeAdapter 将统一协议接入图节点。父 Agent 选择执行者，控制层验证动作，适配器执行并形成回报，父 Agent 接收后继续检测。

## 5. 业务产物与源码版本

| 产物 | 主要字段 | 关联对象 |
|---|---|---|
| SourceSnapshot | source_version、文件清单、内容摘要、存储引用 | 总任务和所有读取该版本的尝试 |
| Finding | finding_id、文件、位置、证据引用、规则、严重程度、状态 | 审查结果、源码版本、后续修复与验证 |
| Patch | patch_id、base_version、修改片段或 diff、target_finding_ids | 修复尝试与被修改问题 |
| PatchApplication | application_id、patch_id、base_version、result_version、status | 补丁是否真正应用及生成哪个新版本 |
| VerificationReport | source_version、目标问题、检查项、执行证据、覆盖范围、passed | 对应验证尝试和被验证的问题 |
| AcceptanceContract / CheckSpec | contract_version、check_id、goal_ref、scope、required、method、pass_condition、evidence_requirements、applicable_phase、checker_version | 冻结用户目标到检查项的映射，检查方法须已注册 |
| CheckResult | check_id、contract_version、source_version、producer_attempt_id、status、evidence_refs、reason、测试数量和摘要 | status 为 passed/failed/not_run/inconclusive，按版本保存 |
| FinalReport | root_task_id、版本、检查结论、差异、剩余问题、执行摘要 | 用户查看和导出的总任务结果 |

source_version 通过规范化文件清单和文件内容摘要生成。修改应用成功后建立新快照，发布新版本；历史快照保持不可变。行号、问题证据、补丁和验证结果都绑定版本。

补丁应用分为读取基础快照、匹配修改、构建候选副本、语法校验和发布新版本。候选修改失败时不发布新版本；应用结果返回父 Agent。语法校验只证明可解析，业务正确性由后续验证检查。

并行复审和验证读取同一冻结版本。两边结果分别保存，父 Agent 校验批次、尝试和版本后汇总；任何一边使用旧版本时不得直接判定通过。

### 5.1 检查合同和基础验证

初始化时父模型提出检查合同，控制层确认覆盖用户目标、至少一项必需检查后持久化。首版不允许在同一任务中删除、降级或修改已有必需项；补充遗漏检查时追加合同版本并保留历史，减少范围或更换目标则新建任务。acceptance_criteria 是任务局部说明，不能覆盖或放宽根任务合同。

首次通过需要当前版本 initial_review/both 的全部必需检查通过；用户必需目标不能仅放在可跳过的 post_patch 阶段。修改后执行 post_patch/both，并重新验证初始必需目标或合同预先指定的等价后置检查。存在当前有效必需失败则 false，未发现明确失败但证据不足则 null。零测试、全部跳过或只有语法检查不能证明行为目标通过。模型审查标明证据和方法，不作为确定性正确性证明。

实现补充字段：`CheckSpec.rule_ids`。method 为 `static_rule` 的检查必须显式声明要评估的已注册规则 id（如 `B006-mutable-default`），
否则判定会退化为对 `pass_condition` 自由文本的猜测。其他 method 不得设置该字段。规则集与版本见 `app/services/tools/static_rules.py`，
检查结果记录 `rule_version`。该补充只收紧了确定性判定的输入，未放宽任何通过条件。

Finding 另有 required_for_goal、status、resolution_evidence_refs；必需目标问题必须由当前版本复查/验证证据关闭，修复声明不能单独关闭问题。最终报告逐项展示范围、方法、未执行项和剩余问题。

实现补充约定（控制层对合同的确定性校验）：除“至少一项必需检查”外，父模型提出的合同还必须满足：

1. method 为 `model_review` 的 required 检查只能是 `initial_review`。模型审查不是可执行证据，不能作为修改后（both/post_patch）的必需证据；
   语义类用户目标由 Finding 的 `required_for_goal` 承载并门控结论。
2. 仅 `initial_review` 的 required 检查，其 `goal_ref` 必须另有 `both`/`post_patch` 的 required 检查覆盖，
   否则该目标在修改后无法重新证明。不满足时控制层拒绝并交由父模型重出合同；模型也可以把该检查设为 `required=false`（补充性检查）。
3. 追加合同版本时不得删除或降级任何已有必需检查（`AcceptanceContract.diff_required_checks`）。
4. 必需 Finding 的关闭由控制层判定：需要“当前版本存在 passed 验证报告且其 `target_finding_ids` 命中该问题”，
   并且“该问题的 `check_id` 在当前版本有 passed 结果”，才写入 `resolution_evidence_refs` 并置为 resolved。
   fixer 声明、问题从摘要消失或严重程度下降都不算关闭。

基础 verifier 可以生成临时测试产物，不依赖扩展 Agent。可复现行为缺陷优先使用同一测试证明原版本失败、修复版本通过，并执行已有相关回归测试；预期值必须有用户目标或接口合同等依据。不能删除或弱化已有验收测试来通过，生成测试写独立目录并记录摘要。无法确定预期时报告证据不足。证据复用同时核对源码、合同、检查器版本和测试摘要。

## 6. WorkflowState、数据库与文件存储

### 6.1 WorkflowState

| 字段组 | 内容 | 更新责任 |
|---|---|---|
| 总任务 | root_task_id、goal、review_scope、acceptance_criteria、contract_version、acceptance_contract_ref | 父 Agent 建任务阶段，经控制层持久化；后续追加合同须留历史 |
| 固定配置 | workflow_version、agent_versions、schema_version | 初始化确定，运行期间固定 |
| 任务与尝试 | tasks、attempts | 父控制层建立、派发、接收后更新 |
| 派发批次 | dispatch_batch_id、pending_attempt_ids | 控制层管理，子 Agent 不自行关闭待完成集合 |
| 回报收集 | received_results、processed_result_ids | 适配器提供回报，控制层校验和去重 |
| 产物与版本 | workspace、source_versions、artifact_refs | 工作区与产物服务提供，控制层合并引用 |
| 检测与反馈 | detection_result、feedback、next_action | 程序检测加父 Agent 分析 |
| 额度 | repair_round、各类 retry_count/max_retries、budget_ledger_ref、run_segment_id | 控制层原子记账；包含验证、审查、修复故障与补证据预算 |
| 总体状态 | status、passed、errors、report_ref | 控制层统一更新 |

审查问题、补丁和验证等业务数据可通过产物引用访问，不将完整源码和全部工具输出反复塞入共享 State。新增 Agent 通过标准回报和产物引用接入，无需持续扩充顶层专用字段。

并行结果以 attempt_id 为键合并，不能让两个节点同时覆盖整个 tasks 或全局 status。父 Agent 只在本批 required attempts 都有有效终态结果后再次决策；终态可来自 Agent 或控制层确认的故障。批次内失败也需分类，不能永远等待“全部成功”或已失效 Agent 自己回报。

实现约定：并行批次中每个 Agent 节点对应一个独立子任务（子任务按节点 id 幂等建立），并行模板的 `recheck` 复用 reviewer 角色但有自己的子任务，角色数不变、任务数增加。检测所用的检查阶段由“当前版本是否已应用补丁”决定，而不是由批次里出现了哪种 task_kind 推断。

实现约定：检测结果（`DetectionResult`）的事实字段只由持久化证据计算——批次是否收齐、回报 status、当前版本的逐项 CheckResult、仍未关闭的必需 Finding、补丁应用失败、失败签名与补丁指纹是否重复。父模型的 `finish` 与 `wait_for_recovery.known_passed` 都只是提议：进入 waiting_recovery 时由控制层重跑完成检测决定保留 false 还是 null，不采用模型给的布尔值。

### 6.2 业务表的关系

| 数据表 | 主键与关联 | 保存内容 |
|---|---|---|
| tasks | task_id；root_task_id 和 parent_task_id 关联 tasks | 总任务和子任务，task_level 区分 root / child，各自校验状态集合 |
| attempts | attempt_id；task_id 关联 tasks | 每次执行的批次、版本、状态和计数 |
| dispatch_batches | dispatch_batch_id；root_task_id 关联 tasks | 本批预期执行尝试和接收进度 |
| task_results | result_id；attempt_id 关联 attempts | 子任务回报，单次尝试只接受一个最终回报 |
| attempt_terminals | attempt_id 唯一；关联 result_id 或 error_ref | 每次尝试唯一终态及 agent/controller 来源，控制层故障不伪造 Agent 回报 |
| budget_ledger | root_task_id；operation_key+budget_kind 唯一 | 额度授予、消耗、作用范围和原因，避免恢复重放重复记账 |
| artifacts | artifact_id；关联总任务、生产者与版本 | 文件位置、类型、摘要、大小和产物关系 |
| patch_applications | application_id；关联补丁产物和执行尝试 | 应用前后版本与幂等处理记录 |
| execution_events | event_id；关联总任务、任务与尝试 | 可增量查询的执行事件 |
| workflow_configs | workflow_version | 固定的编排、角色版本、额度和工具配置 |

总任务在 tasks 中也是一条记录，parent_task_id=null，root_task_id 指向自身。基础模式子任务的 root_task_id 与 parent_task_id 都指向总任务；若以后增加更深层子任务，parent_task_id 可指向直接父任务，root_task_id 仍指向最上层目标。

同一 task_id 下 attempt_no 唯一；同一 attempt_id 的最终回报不能被另一个内容不同的回报覆盖。事件在总任务内有单调递增的 sequence，用于前端游标查询。业务表写入通过服务层处理，子 Agent 不直接写 SQL。

#### 6.2.1 运行控制字段的落点（实现选择）

计划要求在迁移与本文中写清控制字段的选择。首版选择如下：

| 字段组 | 落点 | 原因 |
|---|---|---|
| 租约 owner、租约截止、心跳、fencing token、run_segment_id、图步数许可/已用、当前 decision_id、父纠正计数、required_action | 独立表 `task_controls`（主键 root_task_id） | 恢复互斥必须由数据库约束保证，不能用 Python 内存锁；控制字段频繁更新，与任务行分离避免影响任务 revision |
| 请求与操作幂等键 | 独立表 `idempotency_records`（主键 operation_key），保存 operation_kind、请求指纹与结果引用 | 提交、恢复、补丁应用、派发重放都靠稳定操作键去重；同键不同内容判为冲突 |
| 上传清单 | `uploads`（source_id、content_digest、files、total_bytes） | 上传阶段只产生 source_id 与不可变清单，不冒充总任务 ID |
| 冻结检查合同 | `acceptance_contracts`（contract_version、root_task_id、content_hash、artifact_id、supersedes、append_reason） | 合同不可变、可追加版本，内容存产物、索引存表 |
| 检查结果、问题、验证、报告 | `check_results`（按 check_id+source_version+producer 唯一）、`findings`（主键 finding_id+source_version）、`verifications`、`reports` | 逐版本保存，便于按版本查询必需项状态与最新证据 |

于是总任务自身的状态与 passed 仍在 `tasks`，执行权、额度追加与恢复许可在 `task_controls` 与 `budget_ledger`，三者按 §11.2 的恢复顺序协调。

事务边界（实现约定）：`Database.transaction` 在**同一执行上下文内可重入**——嵌套的仓储调用复用外层连接并加入同一事务，
不会另开连接。否则内层写事务会与外层 `BEGIN IMMEDIATE` 持有的写锁互锁（实测表现为 `database is locked`）。
可重入状态放在 `contextvars.ContextVar`（每个异步任务独立），因此并行批次里的两个 attempt 不会共享连接。
派发登记（批次 + 尝试 + 额度扣减 + 建子任务）因此是**一个**业务事务：任一额度不足都不产生新 attempt。
补丁文件发布与数据库提交仍不假设同一事务，按 §6.3 与 P07 的恢复规则处理。

实现约定：并行批次的每个 attempt 由控制层用执行期限包裹（`asyncio.wait_for`）。超时、回报身份错配或未预期异常都不产生子 Agent 回报，
而是由控制层写入 `attempt_terminals`（origin=controller、outcome=failed、带 error_ref）并把该 attempt 置为 invalidated，
批次接收集合记录 controller 来源。因此 Agent 实现必须让取消信号向上传播，不能把取消转写为一份 failed 回报；
请求取消或超时的尝试不参与当前检测，迟到写入只作审计（§7）。

### 6.3 检查点与展示数据

LangGraph 检查点决定图从哪里继续执行；业务表和事件提供任务查询与执行解释；文件目录保存实际产物。报告和日志不能替代图检查点。

图检查点与业务表不假设天然处于同一数据库事务。回报和产物保存使用幂等标识，任务展示记录可以按已恢复的图状态校正。已保存但尚未归并到父状态的回报可重新提交父控制层；不能只因页面显示“已完成”就跳过执行恢复校验。

实现约定（P07）：`workflow/runner.py` 是唯一驱动图的入口，运行期间持有 `task_controls` 租约并周期心跳；`graph.ainvoke` 传 `durability="sync"`，使硬终止（SIGKILL）不会丢失已完成步骤的检查点，恢复位置才可信。释放租约时同时清空 `lease_owner` 与 `lease_expires_at`，否则已释放的租约会被误判为占用。补丁应用先写一条 `patch_applications` 的 prepared 意图（含内容寻址的 `result_version`）再发布快照，最后提交；`workflow/recovery.py` 在启动/恢复时对 prepared 记录用 `verify_published_application` 校验快照存在且属于该补丁与基础版本，确认后补记提交，不重放补丁。恢复顺序为：取得租约 → 读检查点与业务账本校正当前版本投影 → 幂等重放未消费回报 → 继续图；`WorkflowRunner.resume` 用 `aupdate_state(..., as_node="detect")` 只覆盖标量通道（status/next_action/patched/source_version 等）后 `ainvoke(None)`，避免把累积型通道重复追加。`RecoveryCoordinator.recover_incomplete_runs` 扫描失去有效租约的 running/queued 任务，关闭孤儿 attempt（controller/failed 终态）并标 interrupted；`resume`/`terminate` 在校验 revision、租约与追加上限后于一个事务内完成（否则整体拒绝，不部分生效），并以幂等键去重。

## 7. 父 Agent 检测、重试与恢复

父 Agent 接收结果后，先进行身份与版本校验，再检查执行状态、实际证据和目标是否满足。模型分析负责解释问题与提出策略，确定性控制逻辑负责禁止错误回报、超额执行和无证据完成。

| 检测结果 | 处理方式 |
|---|---|
| 首次审查完整通过 | 标记修复和验证 skipped，总任务通过，记录检查范围 |
| 验证正常执行但失败 | 反馈失败检查、错误位置、上轮补丁和当前版本，重新派发修复 |
| 执行超时或工具不可用 | 检测可恢复原因，重试原任务或等待处理，不能把故障当成代码缺陷 |
| 证据不足 | 明确缺失检查项，安排补充审查或验证 |
| 批次尚未收齐回报 | 等待有效终态回报，不提前调用父模型判断通过 |
| 回报重复、过期或版本错误 | 去重或拒绝，记录审计事件，不污染当前结果 |
| 次数耗尽或重复失败无进展 | waiting_recovery，保存证据与所需处理，不标记成功 |

代码修复轮数与执行故障重试分开计数。默认自动修复额度为两轮；每个新 fix attempt 均消耗一轮，故障重派同时消耗 fix 故障额度。验证/审查故障重派消耗各自额度，正常返回但缺证据的补查消耗 evidence 额度；修改后首次验证/复审不消耗故障重试额度。以上业务计数按 root 累计，不随版本变化重置；同一已登记 queued attempt 的恢复不重复扣减。默认值、上限及模型/工具/图限制见 ImplementationPlan.md §9.4。

恢复前核对 workflow_version、Agent 和协议版本、源码快照、补丁应用记录、未处理回报与执行锁。中断节点可能从头执行，因此建任务、补丁应用和回报接收都需要幂等处理。

同一补丁若已经应用，返回原应用记录；版本既不匹配基础版本也不匹配已发布结果时报告冲突。超时旧尝试先失效，确认其不能继续写入或发布产物后再重派；迟到结果不参与新尝试判定。进程中断由任务服务识别，不能等待已停止的 Agent 主动报告。

TerminalRecord 至少包含 attempt_id、dispatch_batch_id、origin、outcome、result_ref/error_ref、fencing_token、operation_key、created_at。origin 为 agent/controller，outcome 为 completed/failed；controller 只能写 failed。正常回报与超时判定竞争同一个唯一终态：正常回报先提交则保留；控制层先提交则在同一事务中失效 attempt、写故障终态、关闭等待项和记录事件。控制层使用核验执行权的内部入口，Agent 不能伪造 origin；迟到回报仅审计。received_attempt_ids 表示合法终态集合，包含控制层终态，另存来源。

重试批次只登记需重跑分支；已完成分支可以复用仍符合当前合同和输入的证据，但不得改写其生产者、原批次或版本。仅显式 resume 才建立新的 run_segment 并登记图步数许可；重启本身不追加额度。

## 8. 日志与前端状态

| 事件字段 | 说明 |
|---|---|
| event_id、sequence、timestamp | 唯一事件、总任务内查询顺序和带时区时间 |
| root_task_id、task_id、attempt_id | 精确定位总任务、逻辑任务与执行尝试 |
| dispatch_batch_id、source_version | 区分派发批次和代码版本 |
| actor_id、event_type | 记录父 Agent、子 Agent、工具或任务服务产生的事件 |
| payload、artifact_refs、duration_ms | 保存结构化摘要、详细产物引用与耗时 |

事件类型包括 task_created、task_dispatched、attempt_started、tool_started、tool_finished、result_received、detection_completed、retry_scheduled、task_skipped、waiting_recovery、task_completed、task_interrupted 和 task_resumed。

EventSink 自动关联执行上下文，不要求每个 Agent 手工拼装所有 ID。记录输入输出摘要、实际工具证据、错误和派发理由；长内容通过产物引用查看。密钥不写入事件或报告。

任务树显示逻辑任务的当前状态，展开后显示历史尝试；时间线按 sequence 增量加载，时间戳和耗时用于观察并行重叠。前端刷新只恢复查看，不自动重复提交任务。

## 9. 使用方式与 HTTP 接口约定

本节描述工作台和接口的预期使用方式。以下路径为开发接口约定；代码入口、依赖文件和页面实现后，再补充可实际执行的安装启动命令。

### 9.1 用户操作

1. 在本机配置模型服务、数据目录、工具权限和执行额度，启动后端与工作台。
2. 上传 Python 文件或选择一组项目文件，输入审查目标；预览文件清单后提交。
3. 选择顺序模式，或在配置中启用修复后并行复审与验证。
4. 在任务树查看父 Agent 建立的总任务与子任务，在时间线查看派发、执行、回报与检测记录。
5. 查看问题证据、补丁差异和验证结果。审查直接通过时，后续任务显示跳过及原因。
6. 任务进入待处理状态时，按提示处理故障、追加所需额度，再请求继续执行；进程重启后也通过原任务恢复。需补充文件、更换目标或改变测试输入时新建任务，首版不在 resume 中更换原输入。等待/中断任务也可明确终止。
7. 下载报告、日志或修复补丁，根据报告中的检查范围理解结论。

修复结果保存在工作副本，用户可以查看和导出。将结果应用到原项目属于单独操作，不与提交检查混为一个动作。

### 9.2 输入、模型和任务配置

| 配置 | 内容与作用 |
|---|---|
| 模型连接 | 服务商、base URL、模型名与本机密钥；密钥仅由后端读取 |
| 工作区 | 数据目录、上传文件限制、可处理的文件类型与工具访问范围 |
| 工作流 | workflow_version、启用角色、角色版本、输入输出依赖 |
| 检查模式 | 顺序检查或修复后并行复审与验证 |
| 业务额度 | max_repair_rounds、max_verification_retries |
| 执行约束 | 模型超时、工具超时、工具步数、图执行步数 |

全局默认配置与单任务配置合并后生成固定运行配置，不能让前端提交字段任意扩大后端权限。恢复使用原配置版本，额外执行额度单独记录。

WorkflowConfig 必需字段为 schema_version、check_mode、agents、nodes、edges、budgets，可选 layout。保存时展开预算默认值、标准依赖和受控工具策略并固定版本；运行保存 effective_config。check_mode 仅为 sequential/parallel，以已保存 workflow_version 为准；提交时可省略，提供不同值则 409 CONFIG_MODE_CONFLICT。布局不参与 semantic_hash。

首版允许唯一 parent、已注册 agent、apply_patch 工具和唯一 end 节点；派发为 parent→agent/tool，回报仅 agent/tool→parent，结束仅 parent→end 且须通过完成检测。支持顺序/并行模板切换，以及已注册依赖插件支持的扩展角色连接，不支持任意图。并行模板必须含 recheck（复用 reviewer），补丁应用后与 verify 同批派发。输入依赖与执行连线分别保存和展示。完整可保存示例与非法配置见 ImplementationPlan.md §14.1.1。

实现约定（P10）：画布是受约束的编辑器——结构来自 `GET /workflows/templates`（`config/*.yaml` + 已注册插件提供的依赖），
角色只能从 `GET /agents` 的已注册目录中选择，位置写入 `layout`（不参与 semantic_hash）。
保存时后端用注册表重新校验，错误定位到 `nodes.<id>.task_kind` / `agents.<id>` 等字段（400 `VALIDATION_ERROR`）。
扩展角色的接入方式与改动文件清单见 `docs/AgentExtension.md`；新任务类型必须声明重试预算，
未声明的任务类型既不能保存进配置，也不能被派发（不会落入无限重试）。

### 9.3 接口清单

| 方法与路径 | 输入或用途 | 返回 |
|---|---|---|
| POST /api/sources | 上传文件集合，建立输入清单 | source_id、文件清单与快照引用 |
| GET /api/agents | 查询已注册角色能力 | agent_id、version、支持任务类型与输入输出描述 |
| POST /api/workflows | 保存受支持的编排与参数 | workflow_version 与校验结果 |
| GET /api/workflows/templates | 读取随仓库提供的顺序/并行/扩展模板（保存前的完整配置） | 模板名、说明、模式与展开后的配置 |
| POST /api/tasks | source_id、goal、workflow_version、检查模式 | 202、父 Agent 初始化生成的 root_task_id、初始状态 |
| GET /api/tasks/{root_task_id} | 查询任务详情 | 总任务状态、passed、任务树、当前检测与报告引用 |
| GET /api/tasks/{root_task_id}/attempts | 查看执行尝试，可按 task_id 筛选 | 每轮输入、结果、版本与错误摘要 |
| GET /api/tasks/{root_task_id}/events?after_seq=N | 查询后续事件 | events、next_seq 与当前状态摘要 |
| POST /api/tasks/{root_task_id}/resume | 恢复原任务，携带预期修订号和必要的追加额度 | 恢复状态或冲突原因 |
| POST /api/tasks/{root_task_id}/terminate | 仅 waiting_recovery/interrupted；expected_revision、reason、Idempotency-Key | 终止后的状态与保留结论；running/竞争失败返回 409 |
| GET /api/tasks/{root_task_id}/artifacts | 查看本任务产物清单，可按类型筛选 | artifact_id、类型、版本、生产者、大小与下载地址 |
| GET /api/tasks/{root_task_id}/findings | 查看问题列表，可按状态筛选 | 每个 finding_id 的最新持久化行（含 status 与 resolution_evidence_refs） |
| GET /api/artifacts/{artifact_id} | 读取本任务授权访问的产物 | 结构化内容或文件下载 |
| GET /api/tasks/{root_task_id}/report | 查看或导出报告 | 报告内容或下载数据 |

提交接口在父 Agent 初始化工具分配总任务 ID 后返回；后续规划与子任务在后台运行。接口响应中的 ID 取自父 Agent 的任务建立记录，不由前端自编。恢复时用 revision 做并发校验，重复点击通过执行锁和幂等请求处理。

实现约定（P08）：实际实现另加 `GET /api/workflows`（配置列表）与 `GET /api/workflows/{version}`（按版本取全部字段），
以及 `GET /api/tasks`（分页任务列表，`limit`/`offset`）。`POST /api/tasks`、`/resume`、`/terminate` 要求 `Idempotency-Key` 请求头
（缺失返回 400 `VALIDATION_ERROR`）：同键同内容复用同一结果（提交返回原 root_task_id），同键不同内容返回 409 `IDEMPOTENCY_CONFLICT`。
后台运行由应用级任务服务（`services/background.py`）管理：每个 root 一个 asyncio 句柄，独立于请求生命周期；
服务退出时取消句柄并释放租约，启动时扫描失去有效租约的运行并标记 `interrupted`。
统一错误体为 `{code, message, details, request_id}`；`passed` 未判定时为 `null`，与 `false` 分列。检测结论按版本另存为 `detection` 产物，
详情接口据此展示“为什么通过/等待”，`required_action` 来自 `task_controls`（`n_wait` 写入、`n_finalize` 清空）。

实现约定（P09）：问题状态由 `GET /api/tasks/{id}/findings` 提供权威值，前端不解析 `finding` 产物推断状态——
审查产物是不可变快照，其中的问题始终为提交时的 `open`，只有库行会带上 `resolved` 与关闭证据。
工作台据此把“执行状态”（root/task/attempt）与“检查结论”（`true/false/null`）分开渲染，`skipped` 单独表达；
差异、事件、问题、验证与报告均取自上述接口，不存在生产 mock 分支或前端伪造进度。

### 9.4 提交与恢复请求示例

下例表示选择已上传的代码输入和已保存的工作流配置，均为接口示例数据：

```json
{
  "source_id": "source-demo",
  "goal": "检查异常处理和可变默认参数，发现问题后修复并验证",
  "workflow_version": "workflow-1",
  "check_mode": "sequential"
}
```

达到自动修复额度后，用户决定增加一轮执行时，恢复请求可以为：

```json
{
  "expected_revision": 12,
  "additional_repair_rounds": 1,
  "reason": "查看失败证据后继续修复"
}
```

执行控制层记录追加额度，不将 repair_round 清零。仅处理模型网络故障而没有增加业务修复轮数时，不需要追加 repair 额度。

执行故障重试额度已耗尽时，可提交以下恢复请求（Idempotency-Key 通过请求头传递）：

```json
{
  "expected_revision": 12,
  "additional_repair_rounds": 0,
  "additional_execution_retries": {"verify": 1},
  "additional_evidence_retries": 0,
  "reason": "验证服务已恢复，追加一次验证故障重试许可"
}
```

additional_execution_retries 的键仅限有已注册预算策略的 task_kind，基础为 review/fix/verify，扩展角色注册时一并声明（样例为 generate_tests → generate_retry，见 `app/extensions/base.py` 的 `BASE_TASK_KIND_FAULT_BUDGETS` 与扩展注册项）；追加字段省略时为零。服务端在一个事务中核验所有所需额度和上限、追加许可、取得执行权；不足返回 409 BUDGET_EXHAUSTED 及 required_additions，不部分追加。若需重派 fixer 且修复额度也耗尽，应同时追加 repair 和 fix 额度。resume 不接受新 source_id、goal 或测试文件，使用当前已登记合同版本与原配置；可修复凭据或连通性，不能借恢复更换固定模型/角色/工具策略或扩大权限。

## 10. 新 Agent 的开发接入

新增 Agent 的最小工作包括：实现 AgentProtocol，提供 AgentSpec，注册工具与权限，配置输入依赖和完成条件。父 Agent 的通用派发动作、日志协议和回报接口保持一致。

以测试生成 Agent 为例：输入是指定版本的源码和测试目标，输出是测试文件产物引用。父 Agent 从能力目录发现该角色，建立测试生成子任务并派发；收到测试产物后，将其作为验证任务的输入。验证 Agent 依照自己的输入 schema 执行测试。

增加角色后需检查：新能力是否出现在注册目录、输入缺失是否被拒绝、结果是否回到父 Agent、日志是否带正确身份、执行额度是否生效，以及恢复时是否使用原版本。涉及新业务语义时增加专用校验器和工具适配，不把任意连接视为有效流程。

实现约定（P10）：上述要求由 `app/extensions/base.py` 的 `ExtensionRegistry` 与 `TaskKindPlugin` 落实——
能力与实现走 `AgentRegistry`，工具授权走 `ToolRegistry` 的 `allowed_roles`，输入依赖/输入适配由插件声明
（`dependencies` / `consumes` / `feeds`），重试预算在注册时强制声明，完成检测通过 `verdict_bearing`
决定该任务类型的成功是否算作阶段结论。样例（test_generator）的改动文件与复现命令见 `docs/AgentExtension.md`。

## 11. 开发验收重点

| 场景 | 应验证的实际行为 |
|---|---|
| 首次审查通过 | 总任务通过，修复和验证跳过，有明确范围和原因 |
| 修复后通过 | 补丁已应用到新版本，验证证据指向同一版本 |
| 验证失败后再修复 | 第二轮输入包含第一轮失败证据，attempt 历史完整 |
| 验证工具故障 | passed=null，进入执行恢复而非盲目修改代码 |
| 并行复审与验证 | 同一快照、时间区间重叠、收齐回报后父 Agent 才决策 |
| 重复或迟到回报 | 不重复更新状态，不覆盖当前尝试 |
| 进程中断恢复 | 原任务 ID、配置版本和历史保留，补丁不重复应用 |
| 新增标准 Agent | 注册后可统一派发、回报、记录日志与执行额度 |

技术参考：[LangGraph Orchestrator-worker](https://docs.langchain.com/oss/python/langgraph/workflows-agents#orchestrator-worker)、[Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Streaming](https://docs.langchain.com/oss/python/langgraph/streaming)。
