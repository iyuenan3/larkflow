"""Application-facing planning service with a legacy-compatible facade."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from larkflow.workflow.draft_validation import GeneratedDraftValidator

from .contracts import (
    PlannerRequest,
    PlannerResult,
    PlannerRuntime,
    to_mutable,
)
from .context import ContextBundle


class PlanningService:
    """Invoke a replaceable runtime while keeping draft persistence in larkflow."""

    def __init__(
        self,
        runtime: PlannerRuntime,
        *,
        allow_web_search: bool = False,
    ) -> None:
        self.runtime = runtime
        self.validator = GeneratedDraftValidator(
            allow_web_search=allow_web_search,
        )

    def plan(
        self,
        request: PlannerRequest,
        *,
        on_repair: Callable[[], None] | None = None,
    ) -> PlannerResult:
        self.validator.validate_request(
            brief=request.brief,
            context=request.context,
            context_bundle=request.context_bundle,
        )
        result = self.runtime.plan(request, on_repair=on_repair)
        candidate = to_mutable(result.candidate)
        if not isinstance(candidate, dict):
            raise TypeError("planner candidate must be an object")
        candidate["schema_version"] = "0.2"
        candidate["inputs"] = {
            "brief": request.brief,
            "context": request.context,
        }
        self.validator.validate(
            candidate,
            context_bundle=request.context_bundle,
        )
        return PlannerResult(
            candidate=candidate,
            validation_report=result.validation_report,
            planning_evidence=result.planning_evidence,
            usage=result.usage,
            runtime_metadata=result.runtime_metadata,
            trace_ref=result.trace_ref,
        )

    def generate(
        self,
        *,
        tenant_id: str,
        actor_person_id: str,
        request_id: str,
        brief: str,
        context: str,
        context_bundle: ContextBundle | None = None,
        on_repair: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Preserve the draft-worker facade while carrying server identity."""
        result = self.plan(
            PlannerRequest(
                tenant_id=tenant_id,
                actor_person_id=actor_person_id,
                request_id=request_id,
                brief=brief,
                context=context,
                context_bundle=context_bundle,
            ),
            on_repair=on_repair,
        )
        candidate = to_mutable(result.candidate)
        if not isinstance(candidate, dict):
            raise TypeError("planner candidate must be an object")
        return candidate


__all__ = ["PlanningService"]
