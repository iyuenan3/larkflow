"""Safe preview and confirmation rules for controlled graph edits."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
from typing import Any

from .graph import GraphValidationError, validate_snapshot
from .model import (
    AttemptStatus,
    ExecutorKind,
    FrozenDict,
    InstanceSnapshot,
    InstanceStatus,
    NodeAttempt,
    NodeInstance,
    NodeSpec,
    NodeStatus,
    WorkflowInstance,
)
from .serde import snapshot_to_dict, to_json_value


MAX_GRAPH_EDIT_OPERATIONS = 50
MAX_GRAPH_NODES = 100


class GraphEditError(RuntimeError):
    """Base class for an invalid or stale graph edit command."""


class GraphEditNotAllowedError(GraphEditError):
    """The requested edit crosses the frozen execution frontier."""


class GraphEditPreviewNotFoundError(GraphEditError):
    """A durable graph edit preview does not exist in this tenant."""


class GraphEditPreviewExpiredError(GraphEditError):
    """A graph edit preview has passed its confirmation deadline."""


class StaleGraphEditPreviewError(GraphEditError):
    """The aggregate changed after the graph edit preview was created."""


@dataclass(frozen=True)
class GraphEditPlan:
    """Deterministic result of applying normalized operations in memory."""

    operations: tuple[Mapping[str, Any], ...]
    added_node_keys: tuple[str, ...]
    updated_node_keys: tuple[str, ...]
    removed_node_keys: tuple[str, ...]
    candidate_snapshot_hash: str
    proposed_graph_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operations",
            tuple(FrozenDict(operation) for operation in self.operations),
        )


@dataclass(frozen=True)
class GraphEditPreview:
    """Durable, bounded authority for one future-region graph edit."""

    id: str
    tenant_id: str
    instance_id: str
    actor_person_id: str
    operations: tuple[Mapping[str, Any], ...]
    added_node_keys: tuple[str, ...]
    updated_node_keys: tuple[str, ...]
    removed_node_keys: tuple[str, ...]
    candidate_snapshot_hash: str
    expected_instance_version: int
    graph_revision: int
    proposed_graph_revision: int
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    applied_instance_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operations",
            tuple(FrozenDict(operation) for operation in self.operations),
        )
        object.__setattr__(self, "added_node_keys", tuple(self.added_node_keys))
        object.__setattr__(self, "updated_node_keys", tuple(self.updated_node_keys))
        object.__setattr__(self, "removed_node_keys", tuple(self.removed_node_keys))
        if not self.operations:
            raise ValueError("graph edit preview requires operations")
        if self.proposed_graph_revision != self.graph_revision + 1:
            raise ValueError("graph edit preview revision must advance by one")
        if len(self.candidate_snapshot_hash) != 64:
            raise ValueError("graph edit preview requires a SHA-256 snapshot hash")


@dataclass(frozen=True)
class GraphEditConfirmation:
    """Result of consuming or replaying a graph edit preview."""

    instance: WorkflowInstance
    preview: GraphEditPreview
    already_applied: bool = False


def normalize_graph_edit_operations(
    operations: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Validate and freeze the intentionally small edit operation grammar."""

    if isinstance(operations, (str, bytes)) or not isinstance(operations, Sequence):
        raise GraphEditNotAllowedError("graph edit operations must be an array")
    if not operations:
        raise GraphEditNotAllowedError("graph edit requires at least one operation")
    if len(operations) > MAX_GRAPH_EDIT_OPERATIONS:
        raise GraphEditNotAllowedError(
            f"graph edit exceeds {MAX_GRAPH_EDIT_OPERATIONS} operations"
        )

    normalized: list[Mapping[str, Any]] = []
    touched: set[str] = set()
    for raw in operations:
        if not isinstance(raw, Mapping):
            raise GraphEditNotAllowedError("graph edit operation must be an object")
        op = raw.get("op")
        if op == "add_node":
            _require_exact_keys(raw, {"op", "node"})
            node = _node_spec_from_payload(raw.get("node"))
            node_key = node.key
            operation = {
                "op": "add_node",
                "node": _node_spec_to_dict(node),
            }
        elif op == "update_node":
            _require_exact_keys(raw, {"op", "node_key", "set"})
            node_key = _required_text(raw.get("node_key"), "node_key")
            changes = _normalize_changes(raw.get("set"))
            operation = {
                "op": "update_node",
                "node_key": node_key,
                "set": changes,
            }
        elif op == "remove_node":
            _require_exact_keys(raw, {"op", "node_key"})
            node_key = _required_text(raw.get("node_key"), "node_key")
            operation = {"op": "remove_node", "node_key": node_key}
        else:
            raise GraphEditNotAllowedError(f"unsupported graph edit operation: {op!r}")
        if node_key in touched:
            raise GraphEditNotAllowedError(
                f"graph edit touches a node more than once: {node_key}"
            )
        touched.add(node_key)
        normalized.append(FrozenDict(operation))
    return tuple(normalized)


def apply_future_graph_edit(
    instance: WorkflowInstance,
    operations: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> GraphEditPlan:
    """Apply one validated edit to a draft or pristine future nodes."""

    if instance.status not in {InstanceStatus.DRAFT, InstanceStatus.RUNNING}:
        raise GraphEditNotAllowedError("only draft or running instances can be edited")
    if instance.snapshot.locked:
        raise GraphEditNotAllowedError("locked instance snapshots cannot be edited")
    editing_draft = instance.status == InstanceStatus.DRAFT
    if editing_draft and (instance.nodes or instance.attempts):
        raise GraphEditNotAllowedError("draft instance has unexpected runtime state")

    normalized = normalize_graph_edit_operations(operations)
    specs = list(instance.snapshot.nodes)
    by_key = {spec.key: spec for spec in specs}
    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []

    for operation in normalized:
        op = str(operation["op"])
        if op == "add_node":
            node = _node_spec_from_payload(operation["node"])
            if node.key in by_key:
                raise GraphEditNotAllowedError(f"node already exists: {node.key}")
            specs.append(node)
            by_key[node.key] = node
            added.append(node.key)
            continue

        node_key = str(operation["node_key"])
        current = by_key.get(node_key)
        if current is None:
            raise GraphEditNotAllowedError(f"unknown graph edit node: {node_key}")
        if not editing_draft:
            _require_pristine_future_node(instance, node_key)
        if op == "remove_node":
            specs = [spec for spec in specs if spec.key != node_key]
            del by_key[node_key]
            removed.append(node_key)
            continue

        changes = operation["set"]
        replacement = replace(
            current,
            **{
                key: value
                for key, value in changes.items()
                if key in {"title", "owner_person_id", "executor", "deps", "work"}
            },
        )
        if replacement == current:
            raise GraphEditNotAllowedError(f"graph edit does not change node: {node_key}")
        specs = [replacement if spec.key == node_key else spec for spec in specs]
        by_key[node_key] = replacement
        updated.append(node_key)

    if len(specs) > MAX_GRAPH_NODES:
        raise GraphEditNotAllowedError(f"graph exceeds {MAX_GRAPH_NODES} nodes")
    candidate = InstanceSnapshot(
        nodes=tuple(specs),
        goal=instance.snapshot.goal,
        template_version_id=instance.snapshot.template_version_id,
        locked=instance.snapshot.locked,
        inputs=instance.snapshot.inputs,
        schema_version=instance.snapshot.schema_version,
    )
    try:
        validate_snapshot(candidate)
    except GraphValidationError as exc:
        raise GraphEditNotAllowedError(str(exc)) from exc

    if not editing_draft:
        for node_key in removed:
            instance.nodes.pop(node_key)
            for attempt_key in tuple(instance.attempts):
                if attempt_key[0] == node_key:
                    del instance.attempts[attempt_key]

        for node_key in updated:
            spec = candidate.node(node_key)
            node = instance.nodes[node_key]
            node.owner_person_id = spec.owner_person_id
            node.executor = spec.executor
            node.version += 1
            node.status = _future_status(instance, spec)
            node.ready_at = now if node.status == NodeStatus.READY else None
            attempt = instance.current_attempt(node_key)
            attempt.status = AttemptStatus.PENDING
            attempt.input_snapshot = FrozenDict({"deps": spec.deps, "work": spec.work})

        for node_key in added:
            spec = candidate.node(node_key)
            node_id = f"{instance.id}:{node_key}"
            status = _future_status(instance, spec)
            instance.nodes[node_key] = NodeInstance(
                id=node_id,
                instance_id=instance.id,
                node_key=node_key,
                owner_person_id=spec.owner_person_id,
                executor=spec.executor,
                status=status,
                ready_at=now if status == NodeStatus.READY else None,
            )
            instance.attempts[(node_key, 1)] = NodeAttempt(
                id=f"{node_id}:attempt:1",
                node_instance_id=node_id,
                attempt_no=1,
                status=AttemptStatus.PENDING,
                input_snapshot=FrozenDict({"deps": spec.deps, "work": spec.work}),
            )

    instance.snapshot = candidate
    instance.graph_revision += 1
    if not editing_draft and instance.nodes and all(
        node.status == NodeStatus.DONE for node in instance.nodes.values()
    ):
        instance.status = InstanceStatus.DONE
        instance.completed_at = now

    return GraphEditPlan(
        operations=normalized,
        added_node_keys=tuple(added),
        updated_node_keys=tuple(updated),
        removed_node_keys=tuple(removed),
        candidate_snapshot_hash=snapshot_hash(candidate),
        proposed_graph_revision=instance.graph_revision,
    )


def snapshot_hash(snapshot: InstanceSnapshot) -> str:
    """Return a stable digest used to detect preview semantic drift."""

    canonical = json.dumps(
        snapshot_to_dict(snapshot),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_pristine_future_node(
    instance: WorkflowInstance,
    node_key: str,
) -> None:
    node = instance.nodes[node_key]
    attempt = instance.current_attempt(node_key)
    if node.status not in {NodeStatus.PENDING, NodeStatus.READY}:
        raise GraphEditNotAllowedError(f"node has crossed the edit frontier: {node_key}")
    if (
        attempt.status != AttemptStatus.PENDING
        or attempt.result is not None
        or attempt.quality_result is not None
        or attempt.claim_token is not None
        or attempt.claimed_by is not None
        or attempt.claim_expires_at is not None
        or attempt.started_at is not None
        or attempt.completed_at is not None
        or attempt.submitted_by_person_id is not None
        or attempt.error_code is not None
        or attempt.error_message is not None
    ):
        raise GraphEditNotAllowedError(f"node has execution history: {node_key}")


def _future_status(instance: WorkflowInstance, spec: NodeSpec) -> NodeStatus:
    if all(
        dependency in instance.nodes
        and instance.nodes[dependency].status == NodeStatus.DONE
        for dependency in spec.deps
    ):
        return NodeStatus.READY
    return NodeStatus.PENDING


def _normalize_changes(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise GraphEditNotAllowedError("update_node set must be a non-empty object")
    allowed = {"title", "owner_person_id", "executor", "deps", "work"}
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise GraphEditNotAllowedError(
            "unsupported update fields: " + ", ".join(unknown)
        )
    normalized: dict[str, Any] = {}
    if "title" in value:
        normalized["title"] = _required_text(value["title"], "title")
    if "owner_person_id" in value:
        normalized["owner_person_id"] = _required_text(
            value["owner_person_id"], "owner_person_id"
        )
    if "executor" in value:
        try:
            normalized["executor"] = ExecutorKind(value["executor"])
        except (TypeError, ValueError) as exc:
            raise GraphEditNotAllowedError("executor is invalid") from exc
    if "deps" in value:
        normalized["deps"] = _string_tuple(value["deps"], "deps")
    if "work" in value:
        if not isinstance(value["work"], Mapping):
            raise GraphEditNotAllowedError("work must be an object")
        normalized["work"] = FrozenDict(value["work"])
    return FrozenDict(normalized)


def _node_spec_from_payload(value: Any) -> NodeSpec:
    if not isinstance(value, Mapping):
        raise GraphEditNotAllowedError("add_node node must be an object")
    _require_exact_keys(
        value,
        {"key", "title", "owner_person_id", "executor", "deps", "work"},
    )
    try:
        executor = ExecutorKind(value["executor"])
    except (TypeError, ValueError) as exc:
        raise GraphEditNotAllowedError("executor is invalid") from exc
    if not isinstance(value["work"], Mapping):
        raise GraphEditNotAllowedError("work must be an object")
    return NodeSpec(
        key=_required_text(value["key"], "key"),
        title=_required_text(value["title"], "title"),
        owner_person_id=_required_text(value["owner_person_id"], "owner_person_id"),
        executor=executor,
        deps=_string_tuple(value["deps"], "deps"),
        work=value["work"],
    )


def _node_spec_to_dict(node: NodeSpec) -> Mapping[str, Any]:
    return FrozenDict(
        {
            "key": node.key,
            "title": node.title,
            "owner_person_id": node.owner_person_id,
            "executor": node.executor.value,
            "deps": node.deps,
            "work": to_json_value(node.work),
        }
    )


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GraphEditNotAllowedError(f"{name} must be an array")
    items = tuple(_required_text(item, name) for item in value)
    return items


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GraphEditNotAllowedError(f"{name} is required")
    return value.strip()


def _require_exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    actual = {str(key) for key in value}
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise GraphEditNotAllowedError("invalid graph edit fields: " + "; ".join(details))
