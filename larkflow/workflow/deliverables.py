"""Typed, server-validated deliverable contracts for workflow nodes."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import json
import re
from typing import Any

from .model import QualityResult


OUTPUT_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SUPPORTED_OUTPUT_TYPES = {
    "boolean",
    "choice",
    "data",
    "date",
    "decision",
    "document",
    "file",
    "integer",
    "long_text",
    "money",
    "number",
    "object",
    "string_list",
    "text",
    "url",
}
MAX_DELIVERABLE_TEXT_CHARS = 12_000
MAX_DELIVERABLE_JSON_BYTES = 32_000


class DeliverableContractError(ValueError):
    """A declared output contract is malformed."""


class DeliverableValidationError(ValueError):
    """A submitted node result does not satisfy its output contract."""

    error_code = "deliverable_invalid"


def validate_output_contract(outputs: object, *, node_key: str) -> None:
    """Validate the bounded output grammar while preserving legacy declarations."""

    if not _sequence(outputs) or not outputs:
        raise DeliverableContractError(f"work outputs are required: {node_key}")
    seen: set[str] = set()
    for raw in outputs:
        if not isinstance(raw, Mapping):
            raise DeliverableContractError(
                f"work output must be an object: {node_key}"
            )
        output_id = raw.get("id")
        if not isinstance(output_id, str) or not OUTPUT_ID_RE.fullmatch(output_id):
            raise DeliverableContractError(
                f"work output id must be lower snake_case: {node_key}"
            )
        if output_id in seen:
            raise DeliverableContractError(
                f"duplicate work output id {output_id}: {node_key}"
            )
        seen.add(output_id)
        kind = raw.get("type")
        if kind not in SUPPORTED_OUTPUT_TYPES:
            raise DeliverableContractError(
                f"unsupported work output type {kind!r}: {node_key}"
            )
        required = raw.get("required", False)
        if not isinstance(required, bool):
            raise DeliverableContractError(
                f"work output required must be boolean: {node_key}"
            )
        for field in ("label", "help", "placeholder"):
            if field in raw and (
                not isinstance(raw[field], str) or not raw[field].strip()
            ):
                raise DeliverableContractError(
                    f"work output {field} must be text: {node_key}"
                )
        if kind == "choice":
            options = raw.get("options")
            if not _sequence(options) or not options or not all(
                isinstance(item, str) and item.strip() for item in options
            ):
                raise DeliverableContractError(
                    f"choice output requires text options: {node_key}"
                )
        elif "options" in raw:
            raise DeliverableContractError(
                f"only choice output accepts options: {node_key}"
            )


def requires_structured_human_input(work: Mapping[str, object]) -> bool:
    """Return whether native task completion is insufficient for this node."""

    outputs = work.get("outputs")
    return bool(
        _sequence(outputs)
        and any(
            isinstance(item, Mapping) and item.get("required") is True
            for item in outputs
        )
    )


def validate_node_deliverable(
    work: Mapping[str, object],
    result: Mapping[str, Any],
    *,
    allow_undeclared: bool = False,
) -> dict[str, Any]:
    """Validate and normalize one node result against declared outputs."""

    if not isinstance(result, Mapping) or not result:
        raise DeliverableValidationError("节点交付物不能为空")
    normalized = {str(key): value for key, value in result.items()}
    if not any(_meaningful(value) for value in normalized.values()):
        raise DeliverableValidationError("节点交付物不能为空")

    outputs = work.get("outputs")
    strict_contract = False
    if _sequence(outputs):
        declared = {
            str(item["id"]): item
            for item in outputs
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        }
        required = {
            output_id: declaration
            for output_id, declaration in declared.items()
            if declaration.get("required") is True
        }
        if required:
            strict_contract = True
            unknown = sorted(set(normalized) - set(declared))
            if unknown and not allow_undeclared:
                raise DeliverableValidationError(
                    "交付物包含未声明字段：" + "、".join(unknown)
                )
            for output_id, declaration in required.items():
                if output_id not in normalized or not _meaningful(
                    normalized[output_id]
                ):
                    label = declaration.get("label") or output_id
                    raise DeliverableValidationError(f"请填写交付物：{label}")
            for output_id, value in normalized.items():
                declaration = declared.get(output_id)
                if declaration is not None and (
                    declaration.get("required") is True or _meaningful(value)
                ):
                    normalized[output_id] = _validate_value(declaration, value)

    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DeliverableValidationError("节点交付物必须是可保存的 JSON 数据") from exc
    if strict_contract and len(encoded) > MAX_DELIVERABLE_JSON_BYTES:
        raise DeliverableValidationError(
            f"节点交付物超过 {MAX_DELIVERABLE_JSON_BYTES} 字节"
        )
    return normalized


def validate_human_deliverable(
    work: Mapping[str, object],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Backward-compatible name for Human submission callers."""

    return validate_node_deliverable(work, result)


def validate_automated_quality_result(
    quality_result: QualityResult | None,
) -> QualityResult | None:
    """Reject malformed quality metadata before any persistence mutation."""

    if quality_result is None:
        return None
    if not isinstance(quality_result, QualityResult):
        raise DeliverableValidationError("自动执行质量结果结构无效")
    if not isinstance(quality_result.evidence, str) or not isinstance(
        quality_result.suggestion,
        str,
    ):
        raise DeliverableValidationError("自动执行质量结果必须使用文本字段")
    return quality_result


def _validate_value(declaration: Mapping[str, object], value: Any) -> Any:
    kind = str(declaration.get("type"))
    label = str(declaration.get("label") or declaration.get("id"))
    if kind in {"text", "long_text", "document", "file", "url"}:
        if not isinstance(value, str) or not value.strip():
            raise DeliverableValidationError(f"{label}必须填写文本")
        normalized = value.strip()
        if len(normalized) > MAX_DELIVERABLE_TEXT_CHARS:
            raise DeliverableValidationError(
                f"{label}超过 {MAX_DELIVERABLE_TEXT_CHARS} 个字符"
            )
        if kind == "url" and not normalized.startswith(("https://", "http://")):
            raise DeliverableValidationError(f"{label}必须是 HTTP 或 HTTPS 地址")
        return normalized
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise DeliverableValidationError(f"{label}必须是整数")
        return value
    if kind in {"number", "money"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DeliverableValidationError(f"{label}必须是数字")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise DeliverableValidationError(f"{label}必须是是或否")
        return value
    if kind == "date":
        if not isinstance(value, str):
            raise DeliverableValidationError(f"{label}必须是日期")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise DeliverableValidationError(f"{label}必须是有效日期") from exc
        return value
    if kind == "choice":
        options = tuple(str(item) for item in declaration.get("options", ()))
        if not isinstance(value, str) or value not in options:
            raise DeliverableValidationError(f"{label}必须从给定选项中选择")
        return value
    if kind == "string_list":
        if not _sequence(value) or not value or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise DeliverableValidationError(f"{label}必须包含至少一项文本")
        return [str(item).strip() for item in value]
    if kind == "object" and not isinstance(value, Mapping):
        raise DeliverableValidationError(f"{label}必须是结构化对象")
    return value


def _meaningful(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    if _sequence(value):
        return bool(value)
    return True


def _sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))
