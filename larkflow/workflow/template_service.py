"""Versioned template lifecycle and deterministic instance materialization."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from .graph import GraphValidationError, validate_snapshot
from .model import (
    InstanceSnapshot,
    NodeSpec,
    TemplateAuditEvent,
    TemplateStatus,
    WorkflowTemplate,
    WorkflowTemplateVersion,
)
from .repository import TemplateStore
from .serde import to_json_value


TEMPLATE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
PARAMETER_TYPES = {
    "array",
    "boolean",
    "document_ref",
    "integer",
    "number",
    "object",
    "string",
    "text",
}


class TemplateValidationError(ValueError):
    """A template document cannot be safely published or instantiated."""


class InvalidTemplateTransitionError(RuntimeError):
    """A lifecycle command is not legal from the current template status."""


class DuplicateTemplateContentError(ValueError):
    """A new immutable version must change the template definition."""


@dataclass(frozen=True)
class ParsedTemplateDocument:
    template_id: str
    name: str
    version: int
    locked: bool
    definition: Mapping[str, Any]
    content_hash: str


class TemplateService:
    """Coordinate immutable versions and template lifecycle transitions."""

    def __init__(
        self,
        store: TemplateStore,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.id_factory = id_factory or (lambda: str(uuid4()))

    def create_template(
        self,
        *,
        tenant_id: str,
        actor_person_id: str,
        document: Mapping[str, Any],
    ) -> tuple[WorkflowTemplate, WorkflowTemplateVersion]:
        _required_text(tenant_id, "tenant_id")
        _required_text(actor_person_id, "actor_person_id")
        parsed = parse_template_document(document, expected_version=1)
        now = self.clock()
        template = WorkflowTemplate(
            id=parsed.template_id,
            tenant_id=tenant_id,
            name=parsed.name,
            status=TemplateStatus.DRAFT,
            version=0,
            created_at=now,
            updated_at=now,
        )
        version = self._version(template, parsed, now=now)
        event = self._event(
            template,
            "template.created",
            actor_person_id=actor_person_id,
            aggregate_version=0,
            payload={
                "template_version_id": version.id,
                "content_hash": version.content_hash,
            },
            now=now,
        )
        self.store.add_template(template, version, event)
        return (
            self.store.get_template(tenant_id, template.id),
            self.store.get_template_version(tenant_id, template.id, 1),
        )

    def add_version(
        self,
        *,
        tenant_id: str,
        template_id: str,
        actor_person_id: str,
        document: Mapping[str, Any],
    ) -> tuple[WorkflowTemplate, WorkflowTemplateVersion]:
        _required_text(actor_person_id, "actor_person_id")
        template = self.store.get_template(tenant_id, template_id)
        if template.status not in {TemplateStatus.DRAFT, TemplateStatus.DISABLED}:
            raise InvalidTemplateTransitionError(
                "template must be draft or disabled before adding a version"
            )
        latest = self.store.get_template_version(tenant_id, template_id)
        parsed = parse_template_document(
            document,
            expected_template_id=template_id,
            expected_version=latest.version + 1,
        )
        if parsed.name != template.name:
            raise TemplateValidationError("template name cannot change in a version")
        if parsed.content_hash == latest.content_hash:
            raise DuplicateTemplateContentError(
                "new template version has the same content hash"
            )
        now = self.clock()
        version = self._version(template, parsed, now=now)
        event = self._event(
            template,
            "template.version_added",
            actor_person_id=actor_person_id,
            aggregate_version=template.version + 1,
            payload={
                "template_version_id": version.id,
                "version": version.version,
                "content_hash": version.content_hash,
            },
            now=now,
        )
        updated = self.store.add_template_version(
            version,
            expected_template_version=template.version,
            updated_at=now,
            event=event,
        )
        return updated, self.store.get_template_version(
            tenant_id,
            template_id,
            version.version,
        )

    def enable(
        self,
        tenant_id: str,
        template_id: str,
        *,
        actor_person_id: str,
    ) -> WorkflowTemplate:
        template = self.store.get_template(tenant_id, template_id)
        if template.status not in {TemplateStatus.DRAFT, TemplateStatus.DISABLED}:
            raise InvalidTemplateTransitionError(
                f"template cannot be enabled from {template.status.value}"
            )
        latest = self.store.get_template_version(tenant_id, template_id)
        validate_template_definition(latest.definition)
        return self._set_status(
            template,
            TemplateStatus.ENABLED,
            actor_person_id=actor_person_id,
            payload={"active_template_version_id": latest.id},
        )

    def disable(
        self,
        tenant_id: str,
        template_id: str,
        *,
        actor_person_id: str,
    ) -> WorkflowTemplate:
        template = self.store.get_template(tenant_id, template_id)
        if template.status != TemplateStatus.ENABLED:
            raise InvalidTemplateTransitionError(
                f"template cannot be disabled from {template.status.value}"
            )
        return self._set_status(
            template,
            TemplateStatus.DISABLED,
            actor_person_id=actor_person_id,
        )

    def delete(
        self,
        tenant_id: str,
        template_id: str,
        *,
        actor_person_id: str,
    ) -> WorkflowTemplate:
        template = self.store.get_template(tenant_id, template_id)
        if template.status not in {TemplateStatus.DRAFT, TemplateStatus.DISABLED}:
            raise InvalidTemplateTransitionError(
                "enabled templates must be disabled before deletion"
            )
        return self._set_status(
            template,
            TemplateStatus.DELETED,
            actor_person_id=actor_person_id,
        )

    def list_templates(self, tenant_id: str) -> tuple[WorkflowTemplate, ...]:
        return self.store.list_templates(tenant_id)

    def get_template(
        self,
        tenant_id: str,
        template_id: str,
    ) -> WorkflowTemplate:
        return self.store.get_template(tenant_id, template_id)

    def get_version(
        self,
        tenant_id: str,
        template_id: str,
        version: int | None = None,
    ) -> WorkflowTemplateVersion:
        return self.store.get_template_version(tenant_id, template_id, version)

    def instantiate(
        self,
        tenant_id: str,
        template_id: str,
        *,
        inputs: Mapping[str, Any],
        owner_bindings: Mapping[str, str],
    ) -> InstanceSnapshot:
        template = self.store.get_template(tenant_id, template_id)
        if template.status != TemplateStatus.ENABLED:
            raise InvalidTemplateTransitionError("template is not enabled")
        version = self.store.get_template_version(tenant_id, template_id)
        return instantiate_template_version(
            version,
            inputs=inputs,
            owner_bindings=owner_bindings,
        )

    def _set_status(
        self,
        template: WorkflowTemplate,
        status: TemplateStatus,
        *,
        actor_person_id: str,
        payload: Mapping[str, Any] | None = None,
    ) -> WorkflowTemplate:
        _required_text(actor_person_id, "actor_person_id")
        now = self.clock()
        event = self._event(
            template,
            f"template.{status.value}",
            actor_person_id=actor_person_id,
            aggregate_version=template.version + 1,
            payload=payload or {},
            now=now,
        )
        return self.store.set_template_status(
            template.tenant_id,
            template.id,
            expected_template_version=template.version,
            status=status,
            updated_at=now,
            deleted_at=now if status == TemplateStatus.DELETED else None,
            event=event,
        )

    @staticmethod
    def _version(
        template: WorkflowTemplate,
        parsed: ParsedTemplateDocument,
        *,
        now: datetime,
    ) -> WorkflowTemplateVersion:
        return WorkflowTemplateVersion(
            id=f"{template.id}:{parsed.version}",
            tenant_id=template.tenant_id,
            template_id=template.id,
            version=parsed.version,
            schema_version="0.2",
            locked=parsed.locked,
            definition=parsed.definition,
            content_hash=parsed.content_hash,
            created_at=now,
        )

    def _event(
        self,
        template: WorkflowTemplate,
        event_type: str,
        *,
        actor_person_id: str,
        aggregate_version: int,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> TemplateAuditEvent:
        return TemplateAuditEvent(
            id=self.id_factory(),
            tenant_id=template.tenant_id,
            template_id=template.id,
            event_type=event_type,
            actor_person_id=actor_person_id,
            aggregate_version=aggregate_version,
            payload=payload,
            occurred_at=now,
        )


def parse_template_document(
    document: Mapping[str, Any],
    *,
    expected_template_id: str | None = None,
    expected_version: int | None = None,
) -> ParsedTemplateDocument:
    _reject_unknown_fields(
        document,
        {"schema_version", "template", "goal", "parameters", "nodes"},
        "template document",
    )
    if str(document.get("schema_version", "")) != "0.2":
        raise TemplateValidationError("unsupported template schema version")
    metadata = document.get("template")
    if not isinstance(metadata, Mapping):
        raise TemplateValidationError("template metadata is required")
    _reject_unknown_fields(
        metadata,
        {"id", "version", "name", "status", "locked"},
        "template metadata",
    )
    template_id = _required_text(metadata.get("id"), "template.id")
    if not TEMPLATE_ID_RE.fullmatch(template_id):
        raise TemplateValidationError("template.id must be lower snake_case")
    if expected_template_id is not None and template_id != expected_template_id:
        raise TemplateValidationError("template.id does not match the target template")
    name = _required_text(metadata.get("name"), "template.name")
    raw_version = metadata.get("version")
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise TemplateValidationError("template.version must be a positive integer")
    if raw_version < 1:
        raise TemplateValidationError("template.version must be a positive integer")
    if expected_version is not None and raw_version != expected_version:
        raise TemplateValidationError(
            f"template.version must be {expected_version}"
        )
    if metadata.get("status") != TemplateStatus.DRAFT.value:
        raise TemplateValidationError("authored template status must be draft")
    locked = metadata.get("locked", False)
    if not isinstance(locked, bool):
        raise TemplateValidationError("template.locked must be boolean")
    goal = document.get("goal", "")
    if not isinstance(goal, str):
        raise TemplateValidationError("template goal must be text")
    definition = {
        "goal": goal,
        "parameters": to_json_value(document.get("parameters") or {}),
        "nodes": to_json_value(document.get("nodes") or ()),
    }
    validate_template_definition(definition)
    canonical = json.dumps(
        {"locked": locked, "definition": definition},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ParsedTemplateDocument(
        template_id=template_id,
        name=name,
        version=raw_version,
        locked=locked,
        definition=definition,
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def validate_template_definition(definition: Mapping[str, Any]) -> None:
    parameters = definition.get("parameters") or {}
    if not isinstance(parameters, Mapping):
        raise TemplateValidationError("template parameters must be an object")
    for key, spec in parameters.items():
        if not isinstance(key, str) or not TEMPLATE_ID_RE.fullmatch(key):
            raise TemplateValidationError("parameter ids must be lower snake_case")
        if not isinstance(spec, Mapping):
            raise TemplateValidationError(f"parameter definition must be an object: {key}")
        _reject_unknown_fields(
            spec,
            {"type", "required", "default"},
            f"parameter {key}",
        )
        kind = spec.get("type")
        if kind not in PARAMETER_TYPES:
            raise TemplateValidationError(f"unsupported parameter type: {key}")
        required = spec.get("required", False)
        if not isinstance(required, bool):
            raise TemplateValidationError(f"parameter required must be boolean: {key}")
        if "default" in spec:
            _validate_parameter_value(key, kind, spec["default"])

    nodes = definition.get("nodes")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)) or not nodes:
        raise TemplateValidationError("template must contain at least one node")
    materialized = []
    for raw_node in nodes:
        if not isinstance(raw_node, Mapping):
            raise TemplateValidationError("template nodes must be objects")
        _reject_unknown_fields(
            raw_node,
            {"id", "title", "owner_role", "executor", "deps", "work"},
            "template node",
        )
        if "owner_person_id" in raw_node:
            raise TemplateValidationError("templates cannot contain owner_person_id")
        node_id = _required_text(raw_node.get("id"), "node.id")
        title = _required_text(raw_node.get("title"), f"node title: {node_id}")
        owner_role = _required_text(raw_node.get("owner_role"), "node.owner_role")
        if not TEMPLATE_ID_RE.fullmatch(owner_role):
            raise TemplateValidationError(
                f"node owner_role must be lower snake_case: {node_id}"
            )
        work = raw_node.get("work") or {}
        if not isinstance(work, Mapping):
            raise TemplateValidationError(f"node work must be an object: {node_id}")
        _reject_unknown_fields(
            work,
            {"objective", "inputs", "outputs", "acceptance", "agent", "tool"},
            f"node work {node_id}",
        )
        executor = _required_text(raw_node.get("executor"), f"node executor: {node_id}")
        agent = work.get("agent")
        if isinstance(agent, Mapping) and any(
            key in agent for key in ("api_key", "base_url", "model")
        ):
            raise TemplateValidationError(
                f"template agent cannot contain provider configuration: {node_id}"
            )
        if executor == "agent":
            if not isinstance(agent, Mapping):
                raise TemplateValidationError(
                    f"template agent definition is required: {node_id}"
                )
            _reject_unknown_fields(
                agent,
                {"kind", "model_role", "instructions"},
                f"node agent {node_id}",
            )
        elif agent is not None:
            raise TemplateValidationError(
                f"agent definition requires agent executor: {node_id}"
            )
        tool = work.get("tool")
        if executor == "tool":
            if not isinstance(tool, Mapping):
                raise TemplateValidationError(
                    f"template tool definition is required: {node_id}"
                )
            _reject_unknown_fields(tool, {"kind", "args"}, f"node tool {node_id}")
        elif tool is not None:
            raise TemplateValidationError(
                f"tool definition requires tool executor: {node_id}"
            )
        inputs = work.get("inputs") or ()
        if not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)):
            raise TemplateValidationError(f"node inputs must be a sequence: {node_id}")
        raw_deps = raw_node.get("deps") or ()
        if not isinstance(raw_deps, Sequence) or isinstance(raw_deps, (str, bytes)):
            raise TemplateValidationError(f"node deps must be a sequence: {node_id}")
        if not all(isinstance(item, str) for item in raw_deps):
            raise TemplateValidationError(f"node deps must contain strings: {node_id}")
        deps = tuple(raw_deps)
        for reference in inputs:
            if not isinstance(reference, str):
                raise TemplateValidationError(
                    f"template input references must be strings: {node_id}"
                )
            if reference.startswith("instance_inputs."):
                parameter = reference.removeprefix("instance_inputs.")
                if parameter not in parameters:
                    raise TemplateValidationError(
                        f"node references unknown parameter {parameter}: {node_id}"
                    )
            elif reference.startswith("dependencies."):
                dependency = reference.removeprefix("dependencies.")
                if dependency not in deps:
                    raise TemplateValidationError(
                        f"node references undeclared dependency {dependency}: {node_id}"
                    )
            else:
                raise TemplateValidationError(
                    f"unsupported template input reference {reference}: {node_id}"
                )
        try:
            materialized.append(
                NodeSpec(
                    key=node_id,
                    title=title,
                    owner_person_id=f"role:{owner_role}",
                    executor=executor,
                    deps=deps,
                    work=work,
                )
            )
        except (TypeError, ValueError) as exc:
            raise TemplateValidationError(str(exc)) from exc
    try:
        validate_snapshot(InstanceSnapshot(nodes=tuple(materialized)))
    except GraphValidationError as exc:
        raise TemplateValidationError(str(exc)) from exc


def instantiate_template_version(
    version: WorkflowTemplateVersion,
    *,
    inputs: Mapping[str, Any],
    owner_bindings: Mapping[str, str],
) -> InstanceSnapshot:
    validate_template_definition(version.definition)
    parameters = version.definition.get("parameters") or {}
    unknown_inputs = sorted(set(inputs) - set(parameters))
    if unknown_inputs:
        raise TemplateValidationError(
            "unknown template inputs: " + ", ".join(unknown_inputs)
        )
    resolved_inputs = {}
    for key, spec in parameters.items():
        if key in inputs:
            value = inputs[key]
        elif "default" in spec:
            value = to_json_value(spec["default"])
        elif spec.get("required", False):
            raise TemplateValidationError(f"missing required template input: {key}")
        else:
            continue
        _validate_parameter_value(key, spec["type"], value)
        resolved_inputs[key] = to_json_value(value)

    nodes = version.definition["nodes"]
    required_roles = {str(node["owner_role"]) for node in nodes}
    unknown_roles = sorted(set(owner_bindings) - required_roles)
    missing_roles = sorted(required_roles - set(owner_bindings))
    if unknown_roles:
        raise TemplateValidationError(
            "unknown owner bindings: " + ", ".join(unknown_roles)
        )
    if missing_roles:
        raise TemplateValidationError(
            "missing owner bindings: " + ", ".join(missing_roles)
        )
    for role, person_id in owner_bindings.items():
        _required_text(person_id, f"owner binding {role}")

    snapshot = InstanceSnapshot(
        schema_version=version.schema_version,
        goal=str(version.definition.get("goal", "")),
        template_version_id=version.id,
        locked=version.locked,
        inputs=resolved_inputs,
        nodes=tuple(
            NodeSpec(
                key=str(node["id"]),
                title=str(node["title"]),
                owner_person_id=owner_bindings[str(node["owner_role"])],
                executor=str(node["executor"]),
                deps=tuple(node.get("deps") or ()),
                work=to_json_value(node.get("work") or {}),
            )
            for node in nodes
        ),
    )
    validate_snapshot(snapshot)
    return snapshot


def template_document(
    template: WorkflowTemplate,
    version: WorkflowTemplateVersion,
) -> dict[str, Any]:
    return {
        "schema_version": version.schema_version,
        "template": {
            "id": template.id,
            "version": version.version,
            "name": template.name,
            "status": template.status.value,
            "locked": version.locked,
        },
        "goal": str(version.definition.get("goal", "")),
        "parameters": to_json_value(version.definition.get("parameters") or {}),
        "nodes": to_json_value(version.definition.get("nodes") or ()),
    }


def _validate_parameter_value(key: str, kind: str, value: Any) -> None:
    valid = False
    if kind in {"string", "text", "document_ref"}:
        valid = isinstance(value, str)
    elif kind == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif kind == "boolean":
        valid = isinstance(value, bool)
    elif kind == "object":
        valid = isinstance(value, Mapping)
    elif kind == "array":
        valid = isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    if not valid:
        raise TemplateValidationError(
            f"template input {key} does not match type {kind}"
        )


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TemplateValidationError(f"{field} is required")
    return value.strip()


def _reject_unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    field: str,
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise TemplateValidationError(
            f"unsupported fields in {field}: {', '.join(unknown)}"
        )
