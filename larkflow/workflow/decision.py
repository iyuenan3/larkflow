"""Explicit Human accept or reject decisions for review nodes."""
from __future__ import annotations

from collections.abc import Mapping
from enum import Enum


HUMAN_DECISION_KIND = "accept_reject"
HUMAN_DECISION_ACTION_NAME = "human_decision"


class HumanDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"


class HumanDecisionNotAllowedError(RuntimeError):
    """The current Human node is not an actionable decision gate."""


class StaleHumanDecisionError(HumanDecisionNotAllowedError):
    """The decision card targets an older aggregate or Attempt."""


def human_decision_action_name(decision: HumanDecision | str) -> str:
    """Return the unique Card 2.0 element name for one Human decision."""

    return f"{HUMAN_DECISION_ACTION_NAME}_{HumanDecision(decision).value}"


def human_decision_config(work: Mapping[str, object]) -> Mapping[str, object] | None:
    """Return the explicit accept or reject contract, if this work has one."""

    value = work.get("decision")
    if isinstance(value, Mapping) and value.get("kind") == HUMAN_DECISION_KIND:
        return value
    return None


__all__ = [
    "HUMAN_DECISION_ACTION_NAME",
    "HUMAN_DECISION_KIND",
    "HumanDecision",
    "HumanDecisionNotAllowedError",
    "StaleHumanDecisionError",
    "human_decision_action_name",
    "human_decision_config",
]
