"""Persistence port and deterministic in-memory implementation."""
from __future__ import annotations

from copy import deepcopy
from typing import Protocol

from .model import WorkflowInstance


class InstanceNotFoundError(KeyError):
    pass


class InstanceAlreadyExistsError(RuntimeError):
    pass


class ConcurrentUpdateError(RuntimeError):
    pass


class WorkflowRepository(Protocol):
    def add(self, instance: WorkflowInstance) -> None:
        ...

    def get(self, instance_id: str) -> WorkflowInstance:
        ...

    def save(self, instance: WorkflowInstance, *, expected_version: int) -> None:
        ...


class InMemoryWorkflowRepository:
    """Copy-on-read repository that exercises optimistic concurrency in tests."""

    def __init__(self) -> None:
        self._instances: dict[str, WorkflowInstance] = {}

    def add(self, instance: WorkflowInstance) -> None:
        if instance.id in self._instances:
            raise InstanceAlreadyExistsError(instance.id)
        self._instances[instance.id] = deepcopy(instance)

    def get(self, instance_id: str) -> WorkflowInstance:
        try:
            return deepcopy(self._instances[instance_id])
        except KeyError as exc:
            raise InstanceNotFoundError(instance_id) from exc

    def save(self, instance: WorkflowInstance, *, expected_version: int) -> None:
        current = self._instances.get(instance.id)
        if current is None:
            raise InstanceNotFoundError(instance.id)
        if current.version != expected_version:
            raise ConcurrentUpdateError(
                f"instance {instance.id} expected version {expected_version}, "
                f"found {current.version}"
            )
        instance.version = expected_version + 1
        self._instances[instance.id] = deepcopy(instance)
