# DeepSeek 五次真实流程联调（2026-10-09）

用户授权推送修复并运行五次测试。仅发送此前已授权的 clean、repairable、multi_file 课程示例与对应目标。
先推送修复提交 `419ca1ef68cc24315f245bca3c008f851c6d3b51`，随后启动新进程加载该版代码。

后端源码摘要：`5fa755f08f8d478f6fa0ca2661e37ded91499d7eaaa49d17ac6b249fdc4c3406`。
联调前后源码摘要一致，后续文档提交不改变被测后端代码。

## 方法和结果

执行 `backend/scripts/smoke_real_model.py`；隐藏输入凭据，不保存到文件。
使用正式 DeepSeekClient、实际 FastAPI 路由（ASGITransport）、SQLite、LangGraph、文件工作区、补丁与 pytest，
未注入脚本模型，未替换图或执行器。每次新建任务，使用正式保存的顺序/并行模板，默认自动修复上限为两轮。
数据写到独立、被 Git 忽略的目录，不改日常运行数据。

| 次数 | 场景 | 模式 | 总任务 ID | 最终结果 | 耗时 | 实际补丁数 |
|---|---|---|---|---|---|---|
| 1 | clean | sequential | T582f60e476 | completed / true | 20s | 0 |
| 2 | repairable | sequential | T037652ed83 | completed / true | 55s | 1 |
| 3 | multi_file | sequential | T27730e2781 | completed / true | 105s | 2 |
| 4 | repairable（add_item 目标） | parallel | T5b4591b329 | completed / true | 45s | 1 |
| 5 | multi_file（clamp/describe_score 目标） | parallel | Tbd1c59003c | completed / true | 60s | 1 |

clean 首次审查通过，修复和验证明确 skipped。其他四次的验证绑定已提交补丁的新版本。
第三次第一轮验证失败，父 Agent 根据实际失败证据再次修复，第二轮才通过；没有人工追加额度或修改示例。
所有最终报告的必需检查均 passed，未解决必需问题列表为空。

请求模型为 `deepseek-chat`，供应商实际响应模型均为 `deepseek-flash`。
工作流共记录 128 次成功模型响应，869,073 tokens（另有连通性探针 50 tokens）；这是本次真实调用统计，不是费用估算。

## 额外核验

- 根据明确预期值在最终快照副本中运行额外 pytest：共 28 项通过。clean 验证统计函数与空输入异常；repairable 验证默认列表独立、显式列表原地追加、空 average 的 ValueError、端口合法/非法边界；两组多文件验证 clamp 正常/反向/相等边界、describe_score 分类，以及 safe_divide/format_ratio 的正确行为。第四次只核验声明范围内的 add_item，不要求修复范围外的缺陷。
- 逐一读取 254 个产物文件，SHA-256 均与数据库登记一致。
- 5 个原始示例 Python 文件的摘要前后不变。
- 两次并行的复审与验证同属一个批次、读取同一版本、开始结束区间重叠；预期集合等于接收集合，两个 result_received 都发生在最终完成事件之前。
- 凭据格式扫描覆盖本次数据目录，匹配数为 0；公开提交再次扫描，不含 API 密钥、数据库或上传运行数据。

| 并行场景 | 批次 | 同一源码版本 | verify 时间（UTC） | recheck 时间（UTC） |
|---|---|---|---|---|
| repairable | T5b4591b329-B3 | sv-52d0249d1b15143b2e33bdd3fdcbc944 | 05:13:51.204–05:13:57.556 | 05:13:51.213–05:14:05.900 |
| multi_file | Tbd1c59003c-B3 | sv-13ebbb2d8d41ee647a2fedfd2fd4b36e | 05:14:49.514–05:15:01.019 | 05:14:49.523–05:15:03.991 |

真实模型过程中出现两次 run_checks 非法参数错误，均被工具拒绝并由后续模型调用纠正。
部分规划也经过有限纠正，因此“5/5 最终通过”不表示模型每次首次输出均正确；控制层拒绝和反馈仍然必要。

## 证据和范围

公开去敏汇总见 [DeepSeekFiveRuns-2026-10-09.json](DeepSeekFiveRuns-2026-10-09.json)，包括任务 ID、检查、attempt 历史、版本、模型 usage、并行时间和额外核验结果。
本地详细证据位于 `data/real-model-20261009-131021/`，保存 detail/attempts/events/artifacts/report、业务库、检查点、不可变快照、real-model-results.json 和 verification-results.json。
原始运行目录保持 Git 忽略，密钥仅用于进程内鉴权。

本次验证覆盖上述五次受控示例的完整服务流程；没有重测前端浏览器，也没有将已有未提交的前端视觉改动纳入本次后端提交。
