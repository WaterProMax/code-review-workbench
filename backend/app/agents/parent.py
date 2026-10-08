"""The parent agent: goal analysis, task planning and the next-step decision.

The parent role owns two model-backed responsibilities (Desgin §4.4): proposing a
frozen check contract/task plan up front, and choosing the next action from the
detection facts. Both outputs are strictly structured and are only *proposals* —
the deterministic control layer validates them and performs every side effect.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.providers.base import ChatMessage, LLMClient, LLMRequest
from app.registry.agents import AgentRegistry, load_prompt
from app.schemas.actions import ParentAction
from app.schemas.results import CheckSpec
from app.services.tools import static_rules
from app.settings import Settings


class ParentPlan(BaseModel):
    """The parent's proposal for scope, criteria and the check contract."""

    model_config = ConfigDict(extra="forbid")

    review_scope: list[str] = Field(min_length=1)
    acceptance_criteria: list[str] = Field(min_length=1)
    checks: list[CheckSpec] = Field(min_length=1)
    reason: str = Field(min_length=1)
    execution_requirements: list[str] = Field(default_factory=list, description="并行、派发、收齐分支、恢复等运行要求，只在此列出，不得放入源码 checks")


class ParentDecision(BaseModel):
    """One next-step proposal, expressed as exactly one supported action."""

    model_config = ConfigDict(extra="forbid")

    reasoning: str = Field(min_length=1, description="简短决策理由（不要输出私密思维链）")
    action: ParentAction


class ParentAgent:
    def __init__(self, *, llm: LLMClient, registry: AgentRegistry, settings: Settings) -> None:
        self.llm = llm
        self.registry = registry
        self.settings = settings
        prompt, version = load_prompt("parent")
        self.prompt = prompt
        self.prompt_version = version

    # ---- capability catalogue ---------------------------------------------
    def capabilities(self) -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in self.registry.list_capabilities()]

    def _registry_static_rules(self) -> list[str]:
        return list(static_rules.RULE_IDS)

    # ---- planning ----------------------------------------------------------
    async def plan(
        self,
        *,
        root_task_id: str,
        goal: str,
        files: list[str],
        operation_key: str,
    ) -> ParentPlan:
        system = "\n\n".join(
            [
                self.prompt,
                _PLAN_PROTOCOL,
                "可用静态规则（static_rule 检查只能引用这些 rule_ids）："
                + json.dumps(self._registry_static_rules(), ensure_ascii=False),
                "可用角色能力目录："
                + json.dumps(self.capabilities(), ensure_ascii=False, indent=2),
            ]
        )
        messages = [
            ChatMessage(
                role="user",
                content="任务目标：\n"
                + json.dumps(
                    {
                        "root_task_id": root_task_id,
                        "goal": goal,
                        "files": files,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        ]
        request = LLMRequest(
            system=system,
            messages=messages,
            timeout_seconds=self.settings.model_timeout_seconds,
            operation_key=f"{operation_key}:plan",
            script_key="parent:plan",
        )
        plan, _ = await self.llm.complete_json(
            request, ParentPlan, max_corrections=self.settings.max_model_retries
        )
        return plan

    async def replan(
        self,
        *,
        root_task_id: str,
        goal: str,
        files: list[str],
        problems: list[str],
        previous: ParentPlan,
        operation_key: str,
    ) -> ParentPlan:
        system = "\n\n".join(
            [
                self.prompt,
                _PLAN_PROTOCOL,
                "可用静态规则（static_rule 检查只能引用这些 rule_ids）："
                + json.dumps(self._registry_static_rules(), ensure_ascii=False),
            ]
        )
        messages = [
            ChatMessage(
                role="user",
                content="任务目标：\n"
                + json.dumps(
                    {"root_task_id": root_task_id, "goal": goal, "files": files},
                    ensure_ascii=False,
                    indent=2,
                ),
            ),
            ChatMessage(
                role="assistant",
                content=json.dumps(previous.model_dump(mode="json"), ensure_ascii=False),
            ),
            ChatMessage(
                role="user",
                content="上一版检查合同被控制层拒绝，原因：\n"
                + "\n".join(f"- {p}" for p in problems)
                + "\n请修正后重新输出完整的 plan。",
            ),
        ]
        request = LLMRequest(
            system=system,
            messages=messages,
            timeout_seconds=self.settings.model_timeout_seconds,
            operation_key=f"{operation_key}:replan",
            script_key="parent:plan",
        )
        plan, _ = await self.llm.complete_json(
            request, ParentPlan, max_corrections=self.settings.max_model_retries
        )
        return plan

    # ---- decision ----------------------------------------------------------
    async def decide(
        self,
        *,
        context: dict[str, Any],
        rejections: list[dict[str, Any]],
        operation_key: str,
    ) -> ParentDecision:
        system = "\n\n".join(
            [
                self.prompt,
                _DECIDE_PROTOCOL,
                "可用角色能力目录："
                + json.dumps(self.capabilities(), ensure_ascii=False, indent=2),
            ]
        )
        messages = [
            ChatMessage(
                role="user",
                content="当前运行上下文：\n"
                + json.dumps(context, ensure_ascii=False, indent=2, default=str),
            )
        ]
        if rejections:
            messages.append(
                ChatMessage(
                    role="user",
                    content="之前的动作被控制层拒绝：\n"
                    + json.dumps(rejections, ensure_ascii=False, indent=2)
                    + "\n请给出一个合法动作，不要重复被拒绝的动作。",
                )
            )
        request = LLMRequest(
            system=system,
            messages=messages,
            timeout_seconds=self.settings.model_timeout_seconds,
            operation_key=f"{operation_key}:decide{len(rejections)}",
            script_key="parent:decide",
        )
        decision, _ = await self.llm.complete_json(
            request, ParentDecision, max_corrections=self.settings.max_model_retries
        )
        return decision


_PLAN_PROTOCOL = """你现在要为本轮任务输出一份检查合同（ParentPlan）。

字段要求：
- reason：必填，简短说明检查计划依据。
- execution_requirements：并行复审/验证、同版本、收齐分支等运行要求只写这里，由控制层验收；checks 仅检查上传源码的语法、规则、函数行为。
- review_scope：本次审查覆盖的文件或符号范围。
- acceptance_criteria：本次任务局部的完成判据（不能放宽用户目标）。
- checks：检查项列表。每项必须包含 check_id、goal_ref、scope、required、method、
  pass_condition、applicable_phase。method 取值：syntax、static_rule、behavior_test、model_review。
  static_rule 必须提供 rule_ids，且只能引用上面列出的规则；其他 method 不得带 rule_ids。
  applicable_phase 取值：initial_review、post_patch、both。
- 至少一项 required 检查，且必须至少有一项 required 检查的 applicable_phase 为
  initial_review 或 both（用户必需目标不能只放在可跳过的 post_patch 阶段）。
- 顺序/并行、派发、恢复等运行方式由控制层执行，不要将它们创建为源码检查项。
- model_review 只能用于 initial_review；修改后必需检查必须为 syntax/static_rule/behavior_test。
- 有行为目标时应给出 behavior_test 检查；有明确测试要求时不能只用 model_review 替代。
只输出一个 JSON 对象。"""


_DECIDE_PROTOCOL = """你现在要以父 Agent 的身份给出下一步动作（ParentDecision）。

输出结构：{"reasoning": "...", "action": { ... }}，其中 action 是下列之一：
- dispatch_task: {"action":"dispatch_task","task":{"agent_id","task_kind","goal",
  "input_refs":{"source":"auto","acceptance_contract":"auto"},"acceptance_criteria":[..],
  "task_id":可选,"depends_on":可选,"reason"}}
- dispatch_batch: {"action":"dispatch_batch","tasks":[两项或以上独立分支],"reason"}
- apply_patch: {"action":"apply_patch","patch_ref":"auto","base_version":"<当前版本>",
  "repair_attempt_id":"<产出补丁的修复 attempt>","reason"}
- wait_for_recovery: {"action":"wait_for_recovery","reason","required_action","known_passed":true|false|null}
- finish: {"action":"finish","proposed_passed":true|false,"report_refs":[],"reason"}

规则：
1. 任务 ID 只能来自上下文给出的任务树；不要编造 ID。known 的角色可直接给 task_id，
   新建任务可省略 task_id 由控制层分配。
2. input_refs / patch_ref 的值由控制层按当前状态填充，写 "auto" 即可。
3. 补丁可用时必须先 apply_patch，成功后才能派发 verify；verify 必须绑定已应用版本。
4. finish 只是提议：控制层会用当前版本的证据重新检测完成条件。
5. 不得越权扩大工具或修改源码。
6. 以 budgets 中当前 remaining 与 repair_rounds_remaining 判断额度；显式恢复后的追加许可已经计入这些值。历史拒绝原因不能覆盖当前额度，不能因已解决的额度不足再次等待。
只输出一个 JSON 对象。"""
