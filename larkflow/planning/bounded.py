"""Bounded PlannerRuntime adapter around the current safe baseline."""
from __future__ import annotations

from collections.abc import Callable

from larkflow.workflow.draft_generation import DraftDefinitionGenerator

from .contracts import PlannerRequest, PlannerResult


class BoundedPlannerRuntime:
    """Preserve the current two-call maximum and deterministic validation."""

    NAME = "bounded"

    def __init__(self, generator: DraftDefinitionGenerator) -> None:
        self.generator = generator

    def plan(
        self,
        request: PlannerRequest,
        *,
        on_repair: Callable[[], None] | None = None,
    ) -> PlannerResult:
        candidate = self.generator.generate(
            brief=request.brief,
            context=request.context,
            context_bundle=request.context_bundle,
            on_repair=on_repair,
        )
        return PlannerResult(
            candidate=candidate,
            runtime_metadata={
                "runtime": self.NAME,
                "adapter": "draft_definition_generator",
                "adapter_version": "1",
            },
        )


__all__ = ["BoundedPlannerRuntime"]
