"""Explicit Human accept or reject decisions for review nodes."""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum


HUMAN_DECISION_KIND = "accept_reject"
HUMAN_DECISION_ACTION_NAME = "human_decision"
HUMAN_DECISION_FEEDBACK_FIELD = "rejection_feedback"
MAX_HUMAN_DECISION_FEEDBACK_CHARS = 1_000


class HumanDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


class HumanDecisionNotAllowedError(RuntimeError):
    """The current Human node is not an actionable decision gate."""


class StaleHumanDecisionError(HumanDecisionNotAllowedError):
    """The decision card targets an older aggregate or Attempt."""


class HumanDecisionFeedbackError(HumanDecisionNotAllowedError):
    """A rejection is missing bounded, actionable feedback."""


def human_decision_action_name(decision: HumanDecision | str) -> str:
    """Return the unique Card 2.0 element name for one Human decision."""

    return f"{HUMAN_DECISION_ACTION_NAME}_{HumanDecision(decision).value}"


def human_decision_config(work: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the explicit accept or reject contract, if this work has one."""

    value = work.get("decision")
    if isinstance(value, Mapping) and value.get("kind") == HUMAN_DECISION_KIND:
        return value
    return None


def normalize_human_decision_feedback(
    decision: HumanDecision | str,
    feedback: object,
) -> str | None:
    """Normalize reject feedback while discarding it for acceptance."""

    if HumanDecision(decision) == HumanDecision.ACCEPT:
        return None
    if not isinstance(feedback, str) or not feedback.strip():
        raise HumanDecisionFeedbackError("退回时必须填写具体意见")
    normalized = feedback.strip()
    if len(normalized) > MAX_HUMAN_DECISION_FEEDBACK_CHARS:
        raise HumanDecisionFeedbackError(
            "退回意见不能超过 "
            f"{MAX_HUMAN_DECISION_FEEDBACK_CHARS} 个字符"
        )
    return normalized


__all__ = [
    "HUMAN_DECISION_ACTION_NAME",
    "HUMAN_DECISION_FEEDBACK_FIELD",
    "HUMAN_DECISION_KIND",
    "MAX_HUMAN_DECISION_FEEDBACK_CHARS",
    "HumanDecision",
    "HumanDecisionFeedbackError",
    "HumanDecisionNotAllowedError",
    "StaleHumanDecisionError",
    "human_decision_action_name",
    "human_decision_config",
    "normalize_human_decision_feedback",
]
