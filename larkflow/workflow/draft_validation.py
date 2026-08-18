"""Deterministic validation for untrusted generated workflow definitions."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import re
from typing import Any

from larkflow.planning.context import ContextBundle

from .decision import human_decision_config
from .template_service import (
    TemplateValidationError,
    inline_owner_roles,
    instantiate_inline_definition,
)


MAX_GENERATED_NODES = 8

_TRAVEL_INTENT_TERMS = (
    "旅游",
    "旅行",
    "出游",
    "行程规划",
    "itinerary",
    "trip plan",
    "travel plan",
    "行程",
)
_TRAVEL_REQUIREMENT_TERMS = {
    "出发地": ("出发", "起点", "origin", "departure"),
    "出行日期": ("日期", "时间", "start_date", "travel_date", "date"),
    "出行人数": ("人数", "同行", "travelers", "travellers", "people"),
    "预算": ("预算", "budget"),
}
_TRAVEL_RESEARCH_TERMS = {
    "景点攻略": ("景点", "游玩", "attraction", "sightseeing"),
    "交通攻略": ("交通", "路线", "transport", "transit"),
}
_NO_WEB_TERMS = (
    "不要联网",
    "禁止联网",
    "无需联网",
    "不查询外部",
    "无需外部查询",
    "附件为唯一来源",
    "附件中的资料为唯一来源",
    "no web",
    "offline only",
)
_EXTERNAL_RESEARCH_TERMS = (
    "联网搜索",
    "联网查询",
    "搜索公开",
    "调研公开",
    "最新公开信息",
    "web search",
)
_SOURCE_HANDOFF_TERMS = (
    "资料",
    "材料",
    "附件",
    "来源",
    "source",
    "evidence",
)
_NEGATIVE_SOURCE_TERMS = (
    "未知",
    "待定",
    "未定",
    "待确认",
    "未确认",
    "尚未确认",
    "冲突",
    "不一致",
    "待核对",
    "没有",
    "无资料",
    "暂无",
    "缺少",
    "缺失",
    "未提供",
    "未给出",
    "不详",
    "空白",
    "unknown",
    "pending",
    "unconfirmed",
    "missing",
    "not provided",
)
_SOURCE_SEGMENT_SEPARATOR = re.compile(r"[\n\r,，。；;！？!?]+")
_ORIGIN_PATTERNS = (
    re.compile(
        r"(?:出发地|出发城市|起点|origin|departure)\s*[:：=]?\s*"
        r"([^\s,，。；;]{1,40})",
        re.IGNORECASE,
    ),
    re.compile(r"从\s*([^\s,，。；;]{1,40})\s*出发"),
)
_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*[-年/.]\s*(?P<month>\d{1,2})"
    r"\s*[-月/.]\s*(?P<day>\d{1,2})\s*日?"
)
_FIELD_PREFIX = r"(?:^|[\n\r,，。；;！？!?])\s*(?:[-*#]+\s*)?"
_FIELD_VALUE = r"(?P<value>[^\n\r,，。；;！？!?]{0,120})"
_TRAVEL_DATE_LABEL = (
    r"(?:出行|旅行|旅游|行程)(?:开始|结束)?日期|"
    r"出发日期|返程日期|开始日期|结束日期|"
    r"travel_date|start_date|end_date|departure_date|return_date"
)
_TRAVEL_DATE_FIELD_PATTERN = re.compile(
    _FIELD_PREFIX
    + r"(?P<label>(?:"
    + _TRAVEL_DATE_LABEL
    + r"))"
    r"(?:\s*(?:[:：=]|为)\s*|\s*(?=20\d{2}\s*[-年/.]))"
    + _FIELD_VALUE,
    re.IGNORECASE,
)
_TRAVEL_DATE_NEGATIVE_FIELD_PATTERN = re.compile(
    _FIELD_PREFIX
    + r"(?:"
    + _TRAVEL_DATE_LABEL
    + r")\s*(?:(?:[:：=]|为)\s*)?"
    r"(?:未知|待定|未定|待确认|未确认|尚未确认|缺少|缺失|未提供|未给出|不详|空白|"
    r"unknown|pending|unconfirmed|missing|not\s+provided)"
    r"(?=$|[\s,，。；;！？!?])",
    re.IGNORECASE,
)
_DATE_RANGE_SEPARATOR_PATTERN = re.compile(
    r"^\s*(?:至|到|to|through|until|[-~～–])\s*",
    re.IGNORECASE,
)
_TRAVELER_FIELD_PATTERN = re.compile(
    _FIELD_PREFIX
    + r"(?P<label>"
    r"出行人数|同行人数|旅客人数|旅行人数|旅游人数|"
    r"travelers?|travellers?|travel_party_size"
    r")\s*[:：=]?\s*"
    + _FIELD_VALUE,
    re.IGNORECASE,
)
_TRAVELER_VALUE_PATTERN = re.compile(r"(?<!\d)(\d+)\s*(?:人|位)?")
_BUDGET_FIELD_PATTERN = re.compile(
    _FIELD_PREFIX
    + r"(?P<label>"
    r"(?:旅行|旅游|行程|出行)总预算|请求预算|总预算|"
    r"travel_total_budget|trip_budget|request_budget|total_budget"
    r")\s*[:：=]?\s*"
    + _FIELD_VALUE,
    re.IGNORECASE,
)
_BUDGET_VALUE_PATTERN = re.compile(
    r"(?:为|总计|上限|约|[:：=])*\s*(\d+(?:\.\d+)?)\s*"
    r"(万元|人民币|元|rmb|cny|[¥￥])",
    re.IGNORECASE,
)


class DraftGenerationRejected(ValueError):
    """The model response is not a safe, executable inline definition."""


class DraftCapabilityUnavailable(DraftGenerationRejected):
    """The requested evidence path has no configured executable capability."""


@dataclass(frozen=True)
class DraftEvidencePolicy:
    """Server-derived evidence decision for one untrusted planning request."""

    travel_intent: bool
    no_web: bool
    attachment_sources: bool
    web_search_available: bool


class GeneratedDraftValidator:
    """Enforce larkflow-owned invariants on every generated candidate DAG."""

    def __init__(self, *, allow_web_search: bool = False) -> None:
        self.allow_web_search = allow_web_search

    def validate(
        self,
        definition: Mapping[str, Any],
        *,
        context_bundle: ContextBundle | None = None,
    ) -> None:
        nodes = definition.get("nodes")
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= MAX_GENERATED_NODES:
            raise DraftGenerationRejected(
                f"生成流程必须包含 1 到 {MAX_GENERATED_NODES} 个节点"
            )
        try:
            roles = set(inline_owner_roles(definition))
        except TemplateValidationError as exc:
            raise DraftGenerationRejected(f"生成流程未通过校验：{exc}") from exc
        if not roles <= {"requester", "collaborator"}:
            raise DraftGenerationRejected("生成流程包含未授权的 Owner 角色")
        has_agent = False
        has_human_after_agent = False
        agent_ids: set[str] = set()
        node_by_id: dict[str, Mapping[str, Any]] = {}
        depended_on_ids: set[str] = set()
        for raw_node in nodes:
            if not isinstance(raw_node, Mapping):
                raise DraftGenerationRejected("生成流程节点必须是对象")
            executor = raw_node.get("executor")
            allowed_executors = (
                {"human", "agent", "tool"}
                if self.allow_web_search
                else {"human", "agent"}
            )
            if executor not in allowed_executors:
                raise DraftGenerationRejected("生成流程包含未启用的执行器")
            node_id = raw_node.get("id")
            if isinstance(node_id, str):
                node_by_id[node_id] = raw_node
            deps = raw_node.get("deps") or []
            if isinstance(deps, list):
                depended_on_ids.update(
                    dep for dep in deps if isinstance(dep, str)
                )
            work = raw_node.get("work") or {}
            if not isinstance(work, Mapping):
                raise DraftGenerationRejected("生成流程节点 work 必须是对象")
            outputs = work.get("outputs")
            if not isinstance(outputs, list) or not outputs:
                raise DraftGenerationRejected("每个节点必须声明非空交付物")
            output_ids: set[str] = set()
            output_types: dict[str, object] = {}
            for output in outputs:
                if not isinstance(output, Mapping):
                    raise DraftGenerationRejected("节点交付物必须是对象")
                output_id = output.get("id")
                if not isinstance(output_id, str) or not output_id:
                    raise DraftGenerationRejected("节点交付物必须声明 id")
                if output_id in output_ids:
                    raise DraftGenerationRejected("节点交付物 id 不能重复")
                output_ids.add(output_id)
                output_types[output_id] = output.get("type")
                if output.get("required") is not True:
                    raise DraftGenerationRejected("生成流程的每项交付物都必须是必填")
                label = output.get("label")
                if not isinstance(label, str) or not label.strip():
                    raise DraftGenerationRejected("节点交付物必须有用户可读名称")
            inputs = work.get("inputs") or []
            if not isinstance(inputs, list):
                raise DraftGenerationRejected("节点 inputs 必须是数组")
            dependency_inputs = {
                item.removeprefix("dependencies.")
                for item in inputs
                if isinstance(item, str) and item.startswith("dependencies.")
            }
            if set(deps) != dependency_inputs:
                raise DraftGenerationRejected(
                    "每个上游依赖都必须在 inputs 中被明确消费"
                )
            if executor == "agent" and isinstance(node_id, str):
                has_agent = True
                agent_ids.add(node_id)
                if not deps:
                    raise DraftGenerationRejected("Agent 节点必须消费至少一个上游交付物")
                if "content" not in output_ids:
                    raise DraftGenerationRejected("Agent 节点必须声明 content 交付物")
                agent = work.get("agent")
                if not isinstance(agent, Mapping) or agent.get("model_role") != "default":
                    raise DraftGenerationRejected("生成流程的 Agent 必须使用 default 模型角色")
                if (
                    context_bundle is not None
                    and "instance_inputs.project_attachments" not in inputs
                ):
                    raise DraftGenerationRejected(
                        "附件支持的 Agent 节点必须显式消费项目附件"
                    )
            if executor == "tool":
                if not deps:
                    raise DraftGenerationRejected("联网研究 Tool 必须消费已确认的上游输入")
                if output_ids != {"content", "sources"}:
                    raise DraftGenerationRejected(
                        "联网研究 Tool 必须声明 content 和 sources 交付物"
                    )
                if output_types != {"content": "text", "sources": "string_list"}:
                    raise DraftGenerationRejected(
                        "联网研究 Tool 的 content 必须是 text，sources 必须是 string_list"
                    )
                tool = work.get("tool")
                if not isinstance(tool, Mapping) or set(tool) != {"kind", "args"}:
                    raise DraftGenerationRejected("联网研究 Tool 定义不完整")
                if tool.get("kind") != "web.search":
                    raise DraftGenerationRejected("生成流程只允许 web.search Tool")
                args = tool.get("args")
                if not isinstance(args, Mapping) or set(args) != {
                    "model_role",
                    "instructions",
                }:
                    raise DraftGenerationRejected("web.search args 定义不完整")
                if args.get("model_role") != "default":
                    raise DraftGenerationRejected("web.search 必须使用 default 模型角色")
                instructions = args.get("instructions")
                if not isinstance(instructions, str) or not instructions.strip():
                    raise DraftGenerationRejected("web.search 必须声明具体研究指令")
            if executor == "human":
                if isinstance(deps, list) and any(dep in agent_ids for dep in deps):
                    has_human_after_agent = True
                decision = human_decision_config(work)
                if decision is not None:
                    if "decision" not in output_ids:
                        raise DraftGenerationRejected(
                            "Human 决策节点必须声明 decision 交付物"
                        )
                    reject_target = decision.get("reject_target")
                    target = node_by_id.get(str(reject_target))
                    if target is None or target.get("executor") != "agent":
                        raise DraftGenerationRejected(
                            "Human 决策节点的 reject_target 必须是直接上游 Agent 节点"
                        )
        if has_agent and not has_human_after_agent:
            raise DraftGenerationRejected("Agent 产出后必须安排 Human 节点直接复核")
        if has_agent:
            roots = [raw_node for raw_node in nodes if not (raw_node.get("deps") or [])]
            if not roots or any(
                root.get("executor") != "human"
                or human_decision_config(root.get("work") or {}) is not None
                for root in roots
            ):
                raise DraftGenerationRejected(
                    "包含 Agent 的流程必须先由无上游依赖的普通 Human 节点补全输入"
                )
            terminal_nodes = tuple(
                raw_node
                for raw_node in nodes
                if raw_node.get("id") not in depended_on_ids
            )
            for terminal in terminal_nodes:
                if terminal.get("executor") != "human" or human_decision_config(
                    terminal.get("work") or {}
                ) is None:
                    raise DraftGenerationRejected(
                        "包含 Agent 的生成流程必须以可接受或退回的 Human 决策节点结束"
                    )
        self._validate_domain_shape(
            definition,
            context_bundle=context_bundle,
        )
        owner_bindings = {role: f"person_{role}" for role in roles}
        try:
            instantiate_inline_definition(
                definition,
                owner_bindings=owner_bindings,
            )
        except TemplateValidationError as exc:
            raise DraftGenerationRejected(f"生成流程未通过校验：{exc}") from exc

    def validate_request(
        self,
        *,
        brief: str,
        context: str,
        context_bundle: ContextBundle | None,
    ) -> DraftEvidencePolicy:
        """Fail before model work when the requested evidence path is impossible."""

        definition = {"inputs": {"brief": brief, "context": context}}
        policy = self._evidence_policy(definition, context_bundle=context_bundle)
        request_text = f"{brief} {context}".casefold()
        needs_external = policy.travel_intent or any(
            term in request_text for term in _EXTERNAL_RESEARCH_TERMS
        )
        if policy.no_web:
            if context_bundle is None:
                raise DraftGenerationRejected(
                    "已明确禁止联网，但没有提供可授权读取的附件资料"
                )
            if policy.travel_intent:
                missing = self._missing_travel_source_evidence(context_bundle)
                if missing:
                    raise DraftGenerationRejected(
                        "附件资料不足，缺少可用证据：" + "、".join(missing)
                    )
        elif needs_external and not self.allow_web_search:
            raise DraftCapabilityUnavailable(
                "当前没有支持 URL 引用的联网搜索后端。请上传完整资料并明确要求不联网，"
                "或由管理员配置已验证的搜索后端后重试"
            )
        return policy

    def _validate_domain_shape(
        self,
        definition: Mapping[str, Any],
        *,
        context_bundle: ContextBundle | None,
    ) -> None:
        """Reject known high-risk shallow plans before they reach a user."""

        inputs = definition.get("inputs") or {}
        if not isinstance(inputs, Mapping):
            return
        policy = self._evidence_policy(
            definition,
            context_bundle=context_bundle,
        )
        if not policy.travel_intent:
            return

        nodes = definition.get("nodes") or []
        node_by_id = {
            str(node.get("id") or ""): node
            for node in nodes
            if isinstance(node, Mapping)
        }
        if policy.no_web and any(node.get("executor") == "tool" for node in nodes):
            raise DraftGenerationRejected("no-web 资料流程不能包含联网研究 Tool")
        roots = [node for node in nodes if not (node.get("deps") or [])]
        root_outputs = " ".join(
            " ".join(
                str(output.get(field) or "")
                for field in ("id", "label")
            )
            for root in roots
            for output in ((root.get("work") or {}).get("outputs") or [])
            if isinstance(output, Mapping)
        ).casefold()
        missing_requirements = [
            label
            for label, terms in _TRAVEL_REQUIREMENT_TERMS.items()
            if not any(term in root_outputs for term in terms)
        ]
        if missing_requirements:
            raise DraftGenerationRejected(
                "旅游规划必须先收集必填需求：" + "、".join(missing_requirements)
            )

        if policy.no_web:
            if not policy.attachment_sources:
                raise DraftGenerationRejected(
                    "no-web 旅游流程缺少已授权附件来源"
                )
            source_root_ids = {
                str(root.get("id") or "")
                for root in roots
                if any(
                    isinstance(output, Mapping)
                    and output.get("type") in {"text", "long_text", "document"}
                    and any(
                        term
                        in f"{output.get('id', '')} {output.get('label', '')}".casefold()
                        for term in _SOURCE_HANDOFF_TERMS
                    )
                    for output in ((root.get("work") or {}).get("outputs") or [])
                )
            }
            if not source_root_ids:
                raise DraftGenerationRejected(
                    "no-web 旅游流程的首个 Human 节点必须交付完整的已确认来源资料，"
                    "供下游 Agent 显式消费"
                )
            reviewed_agent_ids = {
                str(decision.get("reject_target") or "")
                for node in nodes
                if node.get("executor") == "human"
                for decision in [human_decision_config(node.get("work") or {})]
                if decision is not None
            }
            unbound_agents = [
                agent_id
                for agent_id in reviewed_agent_ids
                if not (
                    set(node_by_id[agent_id].get("deps") or [])
                    & source_root_ids
                )
            ]
            if unbound_agents:
                raise DraftGenerationRejected(
                    "no-web 旅游流程的生成 Agent 必须直接依赖并消费包含完整已确认来源资料的 Human 根节点"
                )
            return
        if not self.allow_web_search:
            raise DraftCapabilityUnavailable(
                "当前没有支持 URL 引用的联网搜索后端。请上传完整资料并明确要求不联网，"
                "或由管理员配置已验证的搜索后端后重试"
            )

        research_nodes: dict[str, set[str]] = {
            label: set() for label in _TRAVEL_RESEARCH_TERMS
        }
        for node in nodes:
            if node.get("executor") != "tool" or not (node.get("deps") or []):
                continue
            work = node.get("work") or {}
            tool = work.get("tool")
            if not isinstance(tool, Mapping) or tool.get("kind") != "web.search":
                continue
            node_text = " ".join(
                (
                    str(node.get("id") or ""),
                    str(node.get("title") or ""),
                    str(work.get("objective") or ""),
                    *(
                        str(output.get("label") or "")
                        for output in work.get("outputs") or []
                        if isinstance(output, Mapping)
                    ),
                )
            ).casefold()
            for label, terms in _TRAVEL_RESEARCH_TERMS.items():
                if any(term in node_text for term in terms):
                    research_nodes[label].add(str(node.get("id") or ""))

        missing_research = [
            label for label, node_ids in research_nodes.items() if not node_ids
        ]
        if missing_research:
            raise DraftGenerationRejected(
                "旅游规划必须拆分独立研究交付物：" + "、".join(missing_research)
            )
        agent_dependencies = {
            str(dependency)
            for node in nodes
            if node.get("executor") == "agent"
            for dependency in (node.get("deps") or [])
        }
        if any(
            not (node_ids & agent_dependencies)
            for node_ids in research_nodes.values()
        ):
            raise DraftGenerationRejected(
                "旅游规划 Agent 必须同时消费景点和交通研究交付物"
            )

    def _evidence_policy(
        self,
        definition: Mapping[str, Any],
        *,
        context_bundle: ContextBundle | None,
    ) -> DraftEvidencePolicy:
        inputs = definition.get("inputs") or {}
        request_text = (
            " ".join(str(inputs.get(key) or "") for key in ("brief", "context"))
            if isinstance(inputs, Mapping)
            else ""
        ).casefold()
        source_text = self._context_text(context_bundle).casefold()
        return DraftEvidencePolicy(
            travel_intent=any(
                term in request_text or term in source_text
                for term in _TRAVEL_INTENT_TERMS
            ),
            no_web=any(term in request_text for term in _NO_WEB_TERMS),
            attachment_sources=context_bundle is not None,
            web_search_available=self.allow_web_search,
        )

    @staticmethod
    def _context_text(context_bundle: ContextBundle | None) -> str:
        if context_bundle is None:
            return ""
        return "\n".join(chunk.text for chunk in context_bundle.chunks)

    @classmethod
    def _missing_travel_source_evidence(
        cls,
        context_bundle: ContextBundle,
    ) -> list[str]:
        source_text = cls._context_text(context_bundle).casefold()
        checks = (
            ("出发地", cls._has_positive_origin(source_text)),
            ("出行日期", cls._has_valid_date(source_text)),
            ("出行人数", cls._has_positive_travelers(source_text)),
            ("预算", cls._has_positive_budget(source_text)),
            (
                "景点资料",
                cls._has_positive_topic_evidence(
                    source_text,
                    _TRAVEL_RESEARCH_TERMS["景点攻略"],
                ),
            ),
            (
                "交通资料",
                cls._has_positive_topic_evidence(
                    source_text,
                    _TRAVEL_RESEARCH_TERMS["交通攻略"],
                ),
            ),
        )
        return [label for label, present in checks if not present]

    @staticmethod
    def _is_negative_source_value(value: str) -> bool:
        normalized = value.casefold().strip()
        return not normalized or any(term in normalized for term in _NEGATIVE_SOURCE_TERMS)

    @classmethod
    def _has_positive_origin(cls, source_text: str) -> bool:
        for pattern in _ORIGIN_PATTERNS:
            for match in pattern.finditer(source_text):
                if not cls._is_negative_source_value(match.group(1)):
                    return True
        return False

    @classmethod
    def _has_valid_date(cls, source_text: str) -> bool:
        if _TRAVEL_DATE_NEGATIVE_FIELD_PATTERN.search(source_text):
            return False
        field_values: dict[str, set[tuple[str, ...]]] = {}
        for field_match in _TRAVEL_DATE_FIELD_PATTERN.finditer(source_text):
            value = field_match.group("value")
            if cls._is_negative_source_value(value):
                return False
            dates = cls._parse_travel_date_value(value)
            if dates is None:
                continue
            label = field_match.group("label").casefold()
            if any(term in label for term in ("开始", "出发", "start", "departure")):
                canonical_field = "start_date"
            elif any(term in label for term in ("结束", "返程", "end", "return")):
                canonical_field = "end_date"
            else:
                canonical_field = "travel_date"
            field_values.setdefault(canonical_field, set()).add(tuple(dates))
        return bool(field_values) and all(
            len(values) == 1 for values in field_values.values()
        )

    @staticmethod
    def _parse_travel_date_value(value: str) -> tuple[str, ...] | None:
        normalized = value.strip()
        first_match = _DATE_PATTERN.match(normalized)
        if first_match is None:
            return None

        def parse_date(match: re.Match[str]) -> str | None:
            try:
                return date(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                ).isoformat()
            except ValueError:
                return None

        first_date = parse_date(first_match)
        if first_date is None:
            return None
        remainder = normalized[first_match.end() :]
        if not remainder.strip():
            return (first_date,)
        separator = _DATE_RANGE_SEPARATOR_PATTERN.match(remainder)
        if separator is None:
            return None
        second_match = _DATE_PATTERN.fullmatch(remainder[separator.end() :].strip())
        if second_match is None:
            return None
        second_date = parse_date(second_match)
        if second_date is None:
            return None
        return (first_date, second_date)

    @classmethod
    def _has_positive_travelers(cls, source_text: str) -> bool:
        values: set[int] = set()
        for field_match in _TRAVELER_FIELD_PATTERN.finditer(source_text):
            value = field_match.group("value")
            if cls._is_negative_source_value(value):
                return False
            match = _TRAVELER_VALUE_PATTERN.fullmatch(value.strip())
            if match is not None and int(match.group(1)) > 0:
                values.add(int(match.group(1)))
        return len(values) == 1
    @classmethod
    def _has_positive_budget(cls, source_text: str) -> bool:
        values: set[float] = set()
        for field_match in _BUDGET_FIELD_PATTERN.finditer(source_text):
            value = field_match.group("value")
            if cls._is_negative_source_value(value):
                return False
            match = _BUDGET_VALUE_PATTERN.fullmatch(value.strip())
            if match is None:
                continue
            amount = float(match.group(1))
            if amount <= 0:
                continue
            if match.group(2).casefold() == "万元":
                amount *= 10_000
            values.add(amount)
        return len(values) == 1

    @classmethod
    def _has_positive_topic_evidence(
        cls,
        source_text: str,
        terms: tuple[str, ...],
    ) -> bool:
        for segment in _SOURCE_SEGMENT_SEPARATOR.split(source_text):
            if not any(term in segment for term in terms):
                continue
            if cls._is_negative_source_value(segment):
                continue
            residue = segment
            for term in (*terms, "资料", "攻略", "路线"):
                residue = residue.replace(term, "")
            if len(re.sub(r"\W+", "", residue)) >= 2:
                return True
        return False


def bind_project_attachment_inputs(definition: Mapping[str, Any]) -> None:
    """Add the server-owned project context input to every generated Agent."""

    nodes = definition.get("nodes")
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict) or node.get("executor") != "agent":
            continue
        work = node.get("work")
        if not isinstance(work, dict):
            continue
        inputs = work.get("inputs")
        if not isinstance(inputs, list):
            continue
        if "instance_inputs.project_attachments" not in inputs:
            inputs.append("instance_inputs.project_attachments")


__all__ = [
    "DraftGenerationRejected",
    "DraftCapabilityUnavailable",
    "DraftEvidencePolicy",
    "GeneratedDraftValidator",
    "bind_project_attachment_inputs",
    "MAX_GENERATED_NODES",
]
