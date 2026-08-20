"""Deterministic travel planning before the generic bounded LLM planner."""
from __future__ import annotations

from collections.abc import Callable

from larkflow.workflow.draft_validation import GeneratedDraftValidator

from .contracts import PlannerRequest, PlannerResult, PlannerRuntime


class TravelTemplatePlannerRuntime:
    """Build the high-risk travel DAG locally and delegate other requests."""

    NAME = "travel_template"

    def __init__(
        self,
        fallback: PlannerRuntime,
        *,
        allow_web_search: bool,
    ) -> None:
        self.fallback = fallback
        self.validator = GeneratedDraftValidator(
            allow_web_search=allow_web_search,
        )

    def plan(
        self,
        request: PlannerRequest,
        *,
        on_repair: Callable[[], None] | None = None,
    ) -> PlannerResult:
        policy = self.validator.validate_request(
            brief=request.brief,
            context=request.context,
            context_bundle=request.context_bundle,
        )
        if not (
            policy.travel_intent
            and not policy.no_web
            and policy.web_search_available
        ):
            return self.fallback.plan(request, on_repair=on_repair)
        return PlannerResult(
            candidate=_travel_definition(),
            planning_evidence={
                "policy": "deterministic_travel_v1",
                "web_search_required": True,
            },
            runtime_metadata={
                "runtime": self.NAME,
                "adapter": "deterministic_travel_v1",
                "adapter_version": "1",
                "model_calls": 0,
            },
        )


def _travel_definition() -> dict:
    """Return the bounded five-node travel research and review skeleton."""

    return {
        "schema_version": "0.2",
        "goal": "形成有来源、可复核的旅行规划",
        "inputs": {},
        "nodes": [
            {
                "id": "confirm_travel_requirements",
                "title": "确认出行需求与资料边界",
                "owner_role": "requester",
                "executor": "human",
                "deps": [],
                "work": {
                    "objective": "补全并确认旅行规划所需的关键输入和资料边界",
                    "inputs": [
                        "instance_inputs.brief",
                        "instance_inputs.context",
                    ],
                    "outputs": [
                        {
                            "id": "destination",
                            "type": "text",
                            "label": "目的地",
                            "required": True,
                        },
                        {
                            "id": "origin",
                            "type": "text",
                            "label": "出发地",
                            "required": True,
                        },
                        {
                            "id": "travel_start_date",
                            "type": "date",
                            "label": "旅行开始日期",
                            "required": True,
                        },
                        {
                            "id": "travel_end_date",
                            "type": "date",
                            "label": "旅行结束日期",
                            "required": True,
                        },
                        {
                            "id": "travelers",
                            "type": "integer",
                            "label": "出行人数",
                            "required": True,
                        },
                        {
                            "id": "total_budget",
                            "type": "money",
                            "label": "旅行总预算",
                            "required": True,
                        },
                        {
                            "id": "constraints",
                            "type": "long_text",
                            "label": "限制条件与偏好",
                            "required": True,
                        },
                    ],
                    "acceptance": [
                        "目的地、出发地、起止日期、人数和总预算均已明确",
                        "限制条件与资料使用边界已确认",
                    ],
                },
            },
            _search_node(
                node_id="research_attractions",
                title="调研景点攻略与开放信息",
                objective="检索目的地景点、开放安排和游玩限制的公开信息",
                instructions=(
                    "根据已确认的目的地、日期和限制条件，检索景点开放安排、"
                    "预约要求、游玩时长和风险提示，保留可核验来源 URL。"
                ),
                evidence_label="景点攻略与来源证据",
            ),
            _search_node(
                node_id="research_transport",
                title="调研交通攻略与路线信息",
                objective="检索出发地到目的地及目的地内部交通的公开信息",
                instructions=(
                    "根据已确认的出发地、目的地、日期、人数和预算，检索往返交通、"
                    "区域内交通、衔接时间和限制条件，保留可核验来源 URL。"
                ),
                evidence_label="交通攻略与来源证据",
            ),
            {
                "id": "synthesize_travel_plan",
                "title": "生成旅行规划方案",
                "owner_role": "requester",
                "executor": "agent",
                "deps": [
                    "confirm_travel_requirements",
                    "research_attractions",
                    "research_transport",
                ],
                "work": {
                    "objective": "综合确认需求、企业资料和公开研究形成可复核方案",
                    "inputs": [
                        "dependencies.confirm_travel_requirements",
                        "dependencies.research_attractions",
                        "dependencies.research_transport",
                    ],
                    "outputs": [
                        {
                            "id": "content",
                            "type": "text",
                            "label": "旅行规划方案",
                            "required": True,
                        }
                    ],
                    "acceptance": [
                        "逐日行程与起止日期一致",
                        "景点和交通结论保留对应来源",
                        "预算覆盖主要费用并标明估算边界",
                        "包含预订责任、风险和备选安排",
                    ],
                    "agent": {
                        "kind": "llm.generate",
                        "model_role": "default",
                        "instructions": (
                            "只基于已确认需求、服务器授权资料和两个搜索节点的来源证据，"
                            "形成逐日行程、交通衔接、预算、预订责任、风险和备选方案。"
                            "每项时效性结论必须标注来源，无法独立验证的内容必须明确说明。"
                        ),
                    },
                },
            },
            {
                "id": "review_travel_plan",
                "title": "复核并决定旅行规划",
                "owner_role": "requester",
                "executor": "human",
                "deps": ["synthesize_travel_plan"],
                "work": {
                    "objective": "复核方案的可执行性、来源、预算和风险",
                    "inputs": ["dependencies.synthesize_travel_plan"],
                    "outputs": [
                        {
                            "id": "decision",
                            "type": "decision",
                            "label": "复核决定",
                            "required": True,
                        }
                    ],
                    "acceptance": [
                        "由人类明确接受或退回方案",
                        "退回意见能够指导生成节点返工",
                    ],
                    "decision": {
                        "kind": "accept_reject",
                        "reject_target": "synthesize_travel_plan",
                    },
                },
            },
        ],
    }


def _search_node(
    *,
    node_id: str,
    title: str,
    objective: str,
    instructions: str,
    evidence_label: str,
) -> dict:
    return {
        "id": node_id,
        "title": title,
        "owner_role": "requester",
        "executor": "tool",
        "deps": ["confirm_travel_requirements"],
        "work": {
            "objective": objective,
            "inputs": ["dependencies.confirm_travel_requirements"],
            "outputs": [
                {
                    "id": "content",
                    "type": "text",
                    "label": evidence_label,
                    "required": True,
                },
                {
                    "id": "sources",
                    "type": "string_list",
                    "label": "来源链接",
                    "required": True,
                },
            ],
            "acceptance": [
                "研究结果包含可核验来源 URL",
                "明确发布时间未知和仍需人工复核的边界",
            ],
            "tool": {
                "kind": "web.search",
                "args": {
                    "model_role": "default",
                    "instructions": instructions,
                    "freshness_max_age_days": 90,
                },
            },
        },
    }


__all__ = ["TravelTemplatePlannerRuntime"]
