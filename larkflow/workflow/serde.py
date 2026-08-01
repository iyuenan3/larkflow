"""Stable JSON serialization for workflow persistence."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Any

from .model import InstanceSnapshot, NodeSpec, QualityResult, QualityVerdict


def to_json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json_value(item) for item in value]
    return value


def snapshot_to_dict(snapshot: InstanceSnapshot) -> dict[str, Any]:
    return {
        "schema_version": snapshot.schema_version,
        "goal": snapshot.goal,
        "template_version_id": snapshot.template_version_id,
        "locked": snapshot.locked,
        "inputs": to_json_value(snapshot.inputs),
        "nodes": [
            {
                "key": node.key,
                "title": node.title,
                "owner_person_id": node.owner_person_id,
                "executor": node.executor.value,
                "deps": list(node.deps),
                "work": to_json_value(node.work),
            }
            for node in snapshot.nodes
        ],
    }


def snapshot_from_dict(data: Mapping[str, Any]) -> InstanceSnapshot:
    return InstanceSnapshot(
        schema_version=str(data["schema_version"]),
        goal=str(data.get("goal", "")),
        template_version_id=data.get("template_version_id"),
        locked=bool(data.get("locked", False)),
        inputs=data.get("inputs") or {},
        nodes=tuple(
            NodeSpec(
                key=str(node["key"]),
                title=str(node["title"]),
                owner_person_id=str(node["owner_person_id"]),
                executor=str(node["executor"]),
                deps=tuple(node.get("deps") or ()),
                work=node.get("work") or {},
            )
            for node in data["nodes"]
        ),
    )


def quality_to_dict(quality: QualityResult | None) -> dict[str, Any] | None:
    if quality is None:
        return None
    return {
        "verdict": quality.verdict.value,
        "evidence": quality.evidence,
        "suggestion": quality.suggestion,
    }


def quality_from_dict(data: Mapping[str, Any] | None) -> QualityResult | None:
    if data is None:
        return None
    return QualityResult(
        verdict=QualityVerdict(str(data["verdict"])),
        evidence=str(data.get("evidence", "")),
        suggestion=str(data.get("suggestion", "")),
    )
