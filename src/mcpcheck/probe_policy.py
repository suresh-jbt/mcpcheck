"""Static, pure probe policy for the ``errors`` check (PLAN.md §7.5 step 1, §9).

This module decides *which* tools mcpcheck may call and *what* it may send
them. It is the product's safety promise, so it is pure (no I/O) and every
payload it produces is **proven** schema-invalid against the tool's own
``inputSchema`` with ``jsonschema`` and an empty ``referencing.Registry()``
(no network ``$ref`` resolution). No code path constructs schema-valid
arguments.

Strategies, in order of preference (B > A > C > D):

* **B** ``missing-required`` — ``{}`` when ``required`` is non-empty.
* **A** ``wrong-type-required`` — every required property wrong-typed.
* **C** ``wrong-type-optional`` — every typed optional property wrong-typed.
* **D** ``unexpected-property`` — only when ``additionalProperties`` is literally ``false``.

Under ``--probe safe`` a tool is eligible only when its own annotations say
``readOnlyHint: true`` or ``destructiveHint: false`` (explicitly; the schema
default of ``destructiveHint`` is ``true``, so an unannotated tool is never
called). Annotations are untrusted (TL:304-307), which is why even eligible
tools only ever receive schema-invalid input.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

import referencing
import referencing.exceptions
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

Policy = Literal["safe", "all"]
Strategy = Literal[
    "missing-required", "wrong-type-required", "wrong-type-optional", "unexpected-property"
]

STRATEGY_MISSING_REQUIRED: Strategy = "missing-required"
STRATEGY_WRONG_TYPE_REQUIRED: Strategy = "wrong-type-required"
STRATEGY_WRONG_TYPE_OPTIONAL: Strategy = "wrong-type-optional"
STRATEGY_UNEXPECTED_PROPERTY: Strategy = "unexpected-property"
STRATEGY_ORDER: tuple[Strategy, ...] = (
    STRATEGY_MISSING_REQUIRED,
    STRATEGY_WRONG_TYPE_REQUIRED,
    STRATEGY_WRONG_TYPE_OPTIONAL,
    STRATEGY_UNEXPECTED_PROPERTY,
)

ENUM_SENTINEL = "mcpcheck-invalid-enum-member-7f3a"
UNEXPECTED_PROPERTY_NAME = "mcpcheck_unexpected_property_7f3a"
UNEXPECTED_PROPERTY_VALUE = "mcpcheck-unexpected-property-7f3a"

#: Deterministic wrong-typed sentinel per declared JSON type.
WRONG_VALUES: dict[str, Any] = {
    "string": 12345,
    "integer": "mcpcheck-not-an-integer",
    "number": "mcpcheck-not-a-number",
    "boolean": "mcpcheck-not-a-boolean",
    "array": "mcpcheck-not-an-array",
    "object": "mcpcheck-not-an-object",
    "null": "mcpcheck-not-null",
}
#: A sample value *of* each JSON type (used against ``type: [...]`` lists).
SAMPLE_VALUES: dict[str, Any] = {
    "string": "mcpcheck-wrong-type-7f3a",
    "integer": 12345,
    "number": 12345.5,
    "boolean": True,
    "array": ["mcpcheck-wrong-type-7f3a"],
    "object": {"mcpcheck": "wrong-type-7f3a"},
    "null": None,
}
JSON_TYPES: tuple[str, ...] = ("string", "integer", "number", "boolean", "array", "object", "null")

_UNSURE = object()


@dataclass(frozen=True)
class ToolPlan:
    """The static plan for one listed tool (PLAN.md §7.5 step 1)."""

    name: str
    index: int
    read_only: bool
    destructive: bool
    header_map: dict[tuple[str, ...], str]
    candidates: list[tuple[dict[str, Any], str]]
    skip_reason: str | None
    properties: tuple[str, ...] = field(default=())

    @property
    def best(self) -> tuple[dict[str, Any], str] | None:
        """The preferred candidate (``None`` when the tool is skipped)."""
        return self.candidates[0] if self.candidates else None


# --------------------------------------------------------------------------- wrong values


def wrong_value(prop_schema: Any) -> Any:
    """Return a value that violates ``prop_schema`` or :data:`UNSURE` when unsure.

    ``enum``/``const`` → :data:`ENUM_SENTINEL`; a single declared ``type`` → the
    sentinel of :data:`WRONG_VALUES`; a ``type`` list → a sample of the first
    JSON type not listed; untyped / ``anyOf`` / ``$ref`` → unsure. The caller
    still proves every candidate invalid with the validator.
    """
    if not isinstance(prop_schema, dict):
        return _UNSURE
    if "enum" in prop_schema or "const" in prop_schema:
        return ENUM_SENTINEL
    declared = prop_schema.get("type")
    if isinstance(declared, str):
        return WRONG_VALUES.get(declared, _UNSURE)
    if isinstance(declared, list) and declared and all(isinstance(t, str) for t in declared):
        listed = set(declared)
        if "number" in listed:
            listed.add("integer")  # integers validate as numbers
        for json_type in JSON_TYPES:
            if json_type not in listed:
                return SAMPLE_VALUES[json_type]
        return _UNSURE
    return _UNSURE


def is_unsure(value: Any) -> bool:
    """Whether :func:`wrong_value` could not pick a value."""
    return value is _UNSURE


# --------------------------------------------------------------------------- validator


def _validator(schema: Mapping[str, Any]) -> tuple[Any, str | None]:
    """Build a dialect-aware validator with an empty registry; ``(None, reason)`` on failure."""
    try:
        cls = validator_for(schema, default=Draft202012Validator)
        cls.check_schema(dict(schema))
        return cls(schema, registry=referencing.Registry()), None
    except SchemaError as exc:
        return None, f"inputSchema is not a valid JSON Schema ({exc.message})"[:200]
    except RecursionError:
        return None, "inputSchema nests too deeply to validate"
    except Exception as exc:  # noqa: BLE001 — e.g. AttributeError from validator_for({"$schema": 42})
        return None, f"inputSchema cannot be compiled ({type(exc).__name__})"


def _proven_invalid(validator: Any, args: Mapping[str, Any]) -> bool | None:
    """``False`` when valid, ``True`` when invalid, ``None`` when the schema could not be evaluated."""
    try:
        return not validator.is_valid(dict(args))
    except referencing.exceptions.Unresolvable:
        return None
    except RecursionError:
        return None


# --------------------------------------------------------------------------- candidates


def _required_names(schema: Mapping[str, Any]) -> list[str]:
    required = schema.get("required")
    if not isinstance(required, list):
        return []
    seen: list[str] = []
    for name in required:
        if isinstance(name, str) and name not in seen:
            seen.append(name)
    return seen


def _properties(schema: Mapping[str, Any]) -> dict[str, Any]:
    props = schema.get("properties")
    return dict(props) if isinstance(props, dict) else {}


def _raw_candidates(schema: Mapping[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Unproven candidates in B > A > C > D order (duplicates removed)."""
    required = _required_names(schema)
    props = _properties(schema)
    out: list[tuple[dict[str, Any], str]] = []

    if required:
        out.append(({}, STRATEGY_MISSING_REQUIRED))
        wrong_required = {name: wrong_value(props.get(name)) for name in required}
        if not any(is_unsure(v) for v in wrong_required.values()):
            out.append((wrong_required, STRATEGY_WRONG_TYPE_REQUIRED))

    optional = {name: wrong_value(sub) for name, sub in props.items() if name not in set(required)}
    optional = {name: v for name, v in optional.items() if not is_unsure(v)}
    if optional:
        out.append((optional, STRATEGY_WRONG_TYPE_OPTIONAL))

    if schema.get("additionalProperties") is False:
        out.append(
            ({UNEXPECTED_PROPERTY_NAME: UNEXPECTED_PROPERTY_VALUE}, STRATEGY_UNEXPECTED_PROPERTY)
        )

    deduped: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    for args, strategy in out:
        key = json.dumps(args, sort_keys=True)
        if key not in seen:
            seen.add(key)
            deduped.append((args, strategy))
    return deduped


def analyse_input_schema(
    input_schema: Any,
) -> tuple[list[tuple[dict[str, Any], str]], str | None]:
    """Return ``(proven-invalid candidates, untestable reason)`` for an ``inputSchema``.

    Every returned candidate satisfies
    ``validator_for(schema, default=Draft202012Validator)(schema, registry=referencing.Registry()).is_valid(args) is False``.
    The reason is ``None`` when at least one candidate survives.
    """
    if not isinstance(input_schema, dict):
        return [], "inputSchema is not an object"
    declared = input_schema.get("type")
    if declared is not None and declared != "object":
        return [], f"inputSchema.type is {declared!r}, not 'object'"
    validator, reason = _validator(input_schema)
    if validator is None:
        return [], reason
    proven: list[tuple[dict[str, Any], str]] = []
    for args, strategy in _raw_candidates(input_schema):
        verdict = _proven_invalid(validator, args)
        if verdict is None:
            return [], "inputSchema has an unresolvable $ref (no network resolution)"
        if verdict:
            proven.append((args, strategy))
    if not proven:
        return [], "permissive inputSchema (no provably invalid arguments)"
    return proven, None


def build_invalid_arguments(input_schema: Any) -> list[tuple[dict[str, Any], str]]:
    """Proven-invalid ``(arguments, strategy)`` candidates ordered B > A > C > D.

    An untestable schema (not an object, invalid, unresolvable ``$ref``,
    permissive) yields an empty list; :func:`analyse_input_schema` also
    returns the reason.
    """
    return analyse_input_schema(input_schema)[0]


# --------------------------------------------------------------------------- eligibility


def _annotations(tool: Mapping[str, Any]) -> dict[str, Any]:
    ann = tool.get("annotations")
    return dict(ann) if isinstance(ann, dict) else {}


def annotation_flags(tool: Mapping[str, Any]) -> tuple[bool, bool]:
    """``(read_only, destructive)`` per the schema defaults (``destructiveHint`` defaults to true)."""
    ann = _annotations(tool)
    read_only = ann.get("readOnlyHint") is True
    destructive = ann.get("destructiveHint") is not False
    return read_only, destructive


def _header_map(input_schema: Any) -> tuple[dict[tuple[str, ...], str], str | None]:
    """``x-mcp-header`` map via the SDK; a reason when the annotations are invalid (never call)."""
    from mcp.shared.inbound import find_invalid_x_mcp_header, x_mcp_header_map

    if not isinstance(input_schema, dict):
        return {}, None
    invalid = find_invalid_x_mcp_header(input_schema)
    if invalid is not None:
        return {}, f"invalid x-mcp-header annotation ({invalid})"[:200]
    header_map: dict[tuple[str, ...], str] = x_mcp_header_map(input_schema)
    return header_map, None


def param_headers(
    header_map: Mapping[tuple[str, ...], str], arguments: Mapping[str, Any]
) -> dict[str, str]:
    """``Mcp-Param-*`` headers mirroring annotated argument values (SDK renderer)."""
    from mcp.shared.inbound import mcp_param_headers

    headers: dict[str, str] = mcp_param_headers(header_map, arguments)
    return headers


def is_probe_eligible(tool: Mapping[str, Any], policy: Policy) -> tuple[bool, str]:
    """Decide whether ``tool`` may be probed under ``policy``.

    ``safe``: the tool's own annotations must say ``readOnlyHint: true`` or
    ``destructiveHint: false`` *and* a proven-invalid payload must exist.
    ``all``: any tool with at least one proven-invalid payload. Returns
    ``(eligible, reason)`` where ``reason`` explains a skip.
    """
    return _eligibility(tool, policy)[:2]


def _eligibility(
    tool: Mapping[str, Any], policy: Policy
) -> tuple[bool, str, list[tuple[dict[str, Any], str]], dict[tuple[str, ...], str]]:
    name = tool.get("name")
    if not isinstance(name, str) or not name:
        return False, "tool has no string name", [], {}
    read_only, destructive = annotation_flags(tool)
    if policy == "safe" and not (read_only or not destructive):
        if "annotations" in tool:
            reason = "annotations do not declare readOnlyHint=true or destructiveHint=false (use --probe all)"
        else:
            reason = "unannotated tool (readOnlyHint/destructiveHint absent; use --probe all)"
        return False, reason, [], {}
    header_map, header_reason = _header_map(tool.get("inputSchema"))
    if header_reason is not None:
        return False, header_reason, [], {}
    candidates, untestable = analyse_input_schema(tool.get("inputSchema"))
    if header_map and policy == "safe":
        kept = [c for c in candidates if not param_headers(header_map, c[0])]
        if candidates and not kept:
            return (
                False,
                "every invalid payload would carry x-mcp-header values (use --probe all)",
                [],
                header_map,
            )
        candidates = kept
    if not candidates:
        return False, untestable or "no provably invalid arguments", [], header_map
    return True, "", candidates, header_map


def plan_tool(tool: Mapping[str, Any], index: int, policy: Policy) -> ToolPlan:
    """Build the :class:`ToolPlan` of one listed tool."""
    eligible, reason, candidates, header_map = _eligibility(tool, policy)
    read_only, destructive = annotation_flags(tool)
    name = tool.get("name")
    schema = tool.get("inputSchema")
    props = tuple(_properties(schema)) if isinstance(schema, dict) else ()
    return ToolPlan(
        name=name if isinstance(name, str) else f"(tool #{index})",
        index=index,
        read_only=read_only,
        destructive=destructive,
        header_map=dict(header_map),
        candidates=list(candidates) if eligible else [],
        skip_reason=None if eligible else reason,
        properties=props,
    )


def plan_tools(
    tools: Sequence[Mapping[str, Any]], policy: Policy, max_tools: int
) -> tuple[list[ToolPlan], list[ToolPlan]]:
    """Plan every listed tool; return ``(selected, skipped)`` in listing order.

    Duplicate names keep their first occurrence; selected tools beyond
    ``max_tools`` are moved to ``skipped`` with a ``--max-tools`` reason.
    """
    selected: list[ToolPlan] = []
    skipped: list[ToolPlan] = []
    seen: set[str] = set()
    for index, tool in enumerate(tools):
        plan = plan_tool(tool, index, policy)
        if plan.name in seen:
            skipped.append(_with_reason(plan, "duplicate tool name (first occurrence probed)"))
            continue
        seen.add(plan.name)
        if plan.skip_reason is not None:
            skipped.append(plan)
        elif len(selected) >= max_tools:
            skipped.append(_with_reason(plan, f"beyond --max-tools {max_tools}"))
        else:
            selected.append(plan)
    return selected, skipped


def _with_reason(plan: ToolPlan, reason: str) -> ToolPlan:
    return ToolPlan(
        name=plan.name,
        index=plan.index,
        read_only=plan.read_only,
        destructive=plan.destructive,
        header_map=plan.header_map,
        candidates=[],
        skip_reason=reason,
        properties=plan.properties,
    )


__all__ = [
    "ENUM_SENTINEL",
    "JSON_TYPES",
    "SAMPLE_VALUES",
    "STRATEGY_MISSING_REQUIRED",
    "STRATEGY_ORDER",
    "STRATEGY_UNEXPECTED_PROPERTY",
    "STRATEGY_WRONG_TYPE_OPTIONAL",
    "STRATEGY_WRONG_TYPE_REQUIRED",
    "UNEXPECTED_PROPERTY_NAME",
    "UNEXPECTED_PROPERTY_VALUE",
    "WRONG_VALUES",
    "Policy",
    "Strategy",
    "ToolPlan",
    "analyse_input_schema",
    "annotation_flags",
    "build_invalid_arguments",
    "is_probe_eligible",
    "is_unsure",
    "param_headers",
    "plan_tool",
    "plan_tools",
    "wrong_value",
]
