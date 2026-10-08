# DeepSeek 真实模型联调（2026-10-08）

> 以下为修复前初测记录。修复后的最新结果与证据见 [修复与回归记录](RepairReport-2026-10-08.md)。

北京时间 18:23–18:24，用户提供密钥并明确授权将 examples 下三组课程示例及检查目标发送至 DeepSeek。API 连通成功，但完整应用流程 **0/4 跑通**。

| 场景 | 模式 | 总任务 ID | 耗时 | 实际结果 |
|---|---|---|---|---|
| clean/stats.py | sequential | T2fb37afa88 | 10s | ParentPlan 缺少必填 reason，纠正两次后仍失败 |
| repairable/helpers.py | sequential | T5b71eaa7ae | 10s | 同上 |
| multi_file/calculator/*.py | sequential | T3c8d8cd867 | 15s | 同上 |
| repairable/helpers.py | parallel | Td1ebd3e0f1 | 15s | 初次规划经纠正后进入 replan，但最终检查合同非法，被控制层拒绝 |

四个任务均为 waiting_recovery、passed=null，没有创建子任务执行尝试或派发批次，没有补丁应用或验证报告。因此此次不能作为真实审查、修复、验证或并行验收已通过的证据。

## 模型与运行方式

- 官方接口：https://api.deepseek.com/chat/completions。
- 请求模型：deepseek-chat；实际成功响应模型：deepseek-flash。
- 流程累计 14 次真实模型调用，39,673 tokens（服务商 usage；不含此前独立连通性探针）。
- 通过 FastAPI 的真实 ASGI 接口完成上传、保存正式模板和提交；后台沿用正式 LangGraph、SQLite 和文件工作区装配。模型客户端包装仅额外记录响应模型名、耗时和 token 数，没有注入脚本结果。
- 输入仅为用户授权的课程示例。目标明确规定：单文件的默认列表隔离、空列表平均值抛 ValueError、端口范围 1..65535；多文件的 clamp 边界和依赖接口行为；并行场景限定可变默认参数修复。完整目标保存在隔离业务库中。

## 发现的阻断问题

`backend/app/agents/parent.py:32` 要求 ParentPlan.reason 必填，但同文件 `_PLAN_PROTOCOL` 字段说明未列出 reason。`backend/app/providers/base.py:117` 的结构化输出入口只追加一般 JSON 提示，没有提供 `model_cls.model_json_schema()`；纠错只给字段错误及类名。三组真实任务反复返回缺字段输出，而脚本模型事先提供 reason，因而未暴露该问题。

建议补齐完整 schema 和规划提示词，并以真实返回样例验证解析。本次只测试和记录，没有修改 Agent、模型适配器或编排业务实现。

并行场景最终错误为：必需检查 C5-parallel-review-verify 使用 model_review，却要求覆盖修改后阶段（both/post_patch）。控制层正确拒绝，但有限纠正未生成合法合同，因此仍未进入并行分支。应在修复后重跑。

## 复现与证据

可复用入口（从 backend 执行，会产生新的真实 API 调用）：

```bash
.venv/bin/python scripts/smoke_real_model.py --connect-only
.venv/bin/python scripts/smoke_real_model.py
```

密钥通过隐藏输入读取，不存入命令参数、环境文件或报告。当前机器同时配置 HTTP 与 SOCKS 代理，项目环境没有 socksio；测试进程临时移除冗余 ALL_PROXY/all_proxy，保留已有 HTTP(S) 代理，没有修改系统代理或项目依赖。

证据目录：`data/real-model-20261008-182341/`（忽略目录）。

- [汇总与模型调用元数据](../data/real-model-20261008-182341/real-model-results.json)。
- 各任务的 detail、attempts、events、artifacts JSON，以及 business.sqlite、checkpoints.sqlite 和上传快照。
- [并行合同拒绝原因](../data/real-model-20261008-182341/parallel-checkpoint-failure.json)。

扫描此次数据目录内 36 个文件，未发现 sk- 凭据格式内容；密钥未写入测试脚本、代码、配置或报告。

本次将“尚未执行真实模型联调”的历史状态更新为“已执行，但完整流程未通过”。原有 A15 并行中途崩溃恢复缺口也仍未解决。
