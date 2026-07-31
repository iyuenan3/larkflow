"""Validation and traversal rules for immutable instance snapshots."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from .model import InstanceSnapshot, NodeStatus


NODE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class GraphValidationError(ValueError):
    """The snapshot cannot form a safe runtime DAG."""


def validate_snapshot(snapshot: InstanceSnapshot) -> None:
    if snapshot.schema_version != "0.2":
        raise GraphValidationError(
            f"unsupported snapshot schema version: {snapshot.schema_version}"
        )
    if not snapshot.nodes:
        raise GraphValidationError("instance snapshot must contain at least one node")

    keys = [node.key for node in snapshot.nodes]
    if len(keys) != len(set(keys)):
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        raise GraphValidationError(f"duplicate node keys: {', '.join(duplicates)}")

    known = set(keys)
    for node in snapshot.nodes:
        if not NODE_KEY_RE.fullmatch(node.key):
            raise GraphValidationError(
                f"node key must be lower snake_case: {node.key!r}"
            )
        if not node.title.strip():
            raise GraphValidationError(f"node title is required: {node.key}")
        if not node.owner_person_id.strip():
            raise GraphValidationError(f"node owner is required: {node.key}")
        if len(node.deps) != len(set(node.deps)):
            raise GraphValidationError(f"duplicate dependencies: {node.key}")
        missing = sorted(set(node.deps) - known)
        if missing:
            raise GraphValidationError(
                f"node {node.key} has unknown dependencies: {', '.join(missing)}"
            )
        if node.key in node.deps:
            raise GraphValidationError(f"node cannot depend on itself: {node.key}")
        _validate_work(node.key, node.executor.value, node.work)

    topological_order(snapshot)


def _validate_work(node_key: str, executor: str, work: Mapping[str, object]) -> None:
    objective = work.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise GraphValidationError(f"work objective is required: {node_key}")

    outputs = work.get("outputs")
    if not _non_empty_sequence(outputs):
        raise GraphValidationError(f"work outputs are required: {node_key}")

    acceptance = work.get("acceptance")
    if not _non_empty_sequence(acceptance) or not all(
        isinstance(item, str) and item.strip() for item in acceptance
    ):
        raise GraphValidationError(f"work acceptance is required: {node_key}")

    inputs = work.get("inputs", ())
    if not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)):
        raise GraphValidationError(f"work inputs must be a sequence: {node_key}")

    if executor == "tool":
        tool = work.get("tool")
        if not isinstance(tool, Mapping):
            raise GraphValidationError(f"tool definition is required: {node_key}")
        kind = tool.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise GraphValidationError(f"tool kind is required: {node_key}")


def _non_empty_sequence(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) > 0
    )


def topological_order(snapshot: InstanceSnapshot) -> tuple[str, ...]:
    """Return a stable topological order or reject a cycle."""
    order_index = {node.key: index for index, node in enumerate(snapshot.nodes)}
    indegree = {node.key: len(node.deps) for node in snapshot.nodes}
    children = {node.key: [] for node in snapshot.nodes}
    for node in snapshot.nodes:
        for dependency in node.deps:
            if dependency in children:
                children[dependency].append(node.key)

    ready = [key for key, degree in indegree.items() if degree == 0]
    ready.sort(key=order_index.__getitem__)
    ordered: list[str] = []
    while ready:
        key = ready.pop(0)
        ordered.append(key)
        for child in sorted(children[key], key=order_index.__getitem__):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=order_index.__getitem__)

    if len(ordered) != len(snapshot.nodes):
        cyclic = sorted(key for key, degree in indegree.items() if degree > 0)
        raise GraphValidationError(f"graph contains a cycle: {', '.join(cyclic)}")
    return tuple(ordered)


def ready_node_keys(
    snapshot: InstanceSnapshot,
    statuses: Mapping[str, NodeStatus],
) -> tuple[str, ...]:
    """Return pending nodes whose dependencies are all done."""
    ready: list[str] = []
    for key in topological_order(snapshot):
        node = snapshot.node(key)
        if statuses.get(key) != NodeStatus.PENDING:
            continue
        if all(statuses.get(dependency) == NodeStatus.DONE for dependency in node.deps):
            ready.append(key)
    return tuple(ready)


def direct_successors(snapshot: InstanceSnapshot, node_key: str) -> tuple[str, ...]:
    snapshot.node(node_key)
    return tuple(node.key for node in snapshot.nodes if node_key in node.deps)


def reachable_downstream(snapshot: InstanceSnapshot, node_key: str) -> frozenset[str]:
    """Return every node transitively depending on ``node_key``."""
    snapshot.node(node_key)
    found: set[str] = set()
    stack = list(direct_successors(snapshot, node_key))
    while stack:
        current = stack.pop()
        if current in found:
            continue
        found.add(current)
        stack.extend(direct_successors(snapshot, current))
    return frozenset(found)
