"""Pure tests for ``mcpcheck.probe_policy`` (PLAN.md §7.5 step 1, §9 safety model).

The safety promise: no code path builds schema-valid arguments, and under
``safe`` an unannotated tool is never eligible.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest
import referencing
from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from mcp.shared.inbound import encode_header_value

from mcpcheck import probe_policy as pp
from tests.fixtures.misbehaving_server import CATALOGUE_TOOLS, DEFAULT_TOOLS

READ_ONLY: dict[str, bool] = {"readOnlyHint": True, "destructiveHint": False}


def tool(name: str, schema: Any, annotations: dict[str, Any] | None = READ_ONLY) -> dict[str, Any]:
    t: dict[str, Any] = {"name": name, "inputSchema": schema}
    if annotations is not None:
        t["annotations"] = annotations
    return t


def is_valid(schema: dict[str, Any], args: dict[str, Any]) -> bool:
    cls = validator_for(schema, default=Draft202012Validator)
    return bool(cls(schema, registry=referencing.Registry()).is_valid(args))


# --------------------------------------------------------------------------- the 16+ schemas

SCHEMAS: list[dict[str, Any]] = [
    {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
    {
        "type": "object",
        "properties": {"message": {"type": "string"}, "repeat": {"type": "integer"}},
        "required": ["message", "repeat"],
    },
    {"type": "object", "properties": {"q": {"type": "string"}}},
    {"type": "object", "properties": {"n": {"type": "number"}}},
    {"type": "object", "properties": {"flag": {"type": "boolean"}}, "required": ["flag"]},
    {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"]},
    {"type": "object", "properties": {"opts": {"type": "object"}}, "required": ["opts"]},
    {"type": "object", "properties": {"nothing": {"type": "null"}}, "required": ["nothing"]},
    {"type": "object", "properties": {"mode": {"enum": ["a", "b"]}}, "required": ["mode"]},
    {"type": "object", "properties": {"mode": {"const": "fixed"}}},
    {"type": "object", "properties": {"v": {"type": ["string", "null"]}}, "required": ["v"]},
    {"type": "object", "properties": {"v": {"type": ["integer", "number"]}}},
    {"type": "object", "properties": {"x": {"type": "string"}}, "additionalProperties": False},
    {"type": "object", "additionalProperties": False},
    {
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/s"}},
        "$defs": {"s": {"type": "string"}},
        "required": ["a"],
    },
    {
        "type": "object",
        "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
        "required": ["a"],
    },
    {"properties": {"q": {"type": "string"}}, "required": ["q"]},
    {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    },
    {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "region": {"type": "string", "x-mcp-header": "Region"},
        },
        "required": ["q"],
    },
    {
        "type": "object",
        "properties": {"city": {"type": "string"}, "units": {"type": "string", "default": "c"}},
        "required": ["city"],
    },
]


@pytest.mark.parametrize("schema", SCHEMAS, ids=[json.dumps(s)[:60] for s in SCHEMAS])
def test_build_invalid_arguments_every_payload_is_invalid(schema: dict[str, Any]) -> None:
    candidates = pp.build_invalid_arguments(schema)
    for args, strategy in candidates:
        assert strategy in pp.STRATEGY_ORDER
        assert isinstance(args, dict)
        assert is_valid(schema, args) is False, (args, strategy)
    # candidates are ordered B > A > C > D
    order = [pp.STRATEGY_ORDER.index(s) for _, s in candidates]
    assert order == sorted(order)


def _random_schema(rng: random.Random) -> dict[str, Any]:
    types = [*pp.JSON_TYPES, "enum", "list", "untyped"]
    props: dict[str, Any] = {}
    for i in range(rng.randint(0, 4)):
        kind = rng.choice(types)
        sub: dict[str, Any]
        if kind == "enum":
            sub = {"enum": [rng.choice(["a", "b", 1, True, None]) for _ in range(2)]}
        elif kind == "list":
            sub = {"type": rng.sample(list(pp.JSON_TYPES), rng.randint(1, 7))}
        elif kind == "untyped":
            sub = {"description": "anything"}
        else:
            sub = {"type": kind}
        props[f"p{i}"] = sub
    schema: dict[str, Any] = {"type": "object"}
    if props:
        schema["properties"] = props
        required = [n for n in props if rng.random() < 0.5]
        if required:
            schema["required"] = required
    if rng.random() < 0.3:
        schema["additionalProperties"] = False
    return schema


def test_property_no_candidate_is_ever_valid() -> None:
    rng = random.Random(0x7F3A)  # noqa: S311 - deterministic seed for a property-style test
    total = 0
    for _ in range(400):
        schema = _random_schema(rng)
        for args, _strategy in pp.build_invalid_arguments(schema):
            total += 1
            assert is_valid(schema, args) is False, (schema, args)
    assert total > 200


# --------------------------------------------------------------------------- verified examples


def test_echo_required_candidates_match_plan() -> None:
    schema = DEFAULT_TOOLS[1]["inputSchema"]
    assert DEFAULT_TOOLS[1]["name"] == "echo_required"
    assert pp.build_invalid_arguments(schema) == [
        ({}, "missing-required"),
        ({"message": 12345, "repeat": "mcpcheck-not-an-integer"}, "wrong-type-required"),
    ]


@pytest.mark.parametrize(
    ("schema", "reason"),
    [
        ({}, "permissive"),
        ({"type": "object"}, "permissive"),
        (None, "not an object"),
        ("x", "not an object"),
        ({"type": "string"}, "not 'object'"),
        ({"type": "object", "required": "a"}, "not a valid JSON Schema"),
        ({"$schema": 42, "type": "object", "required": ["a"]}, "cannot be compiled"),
        (
            {"$ref": "https://example.com/remote", "type": "object", "required": ["a"]},
            "unresolvable $ref",
        ),
        ({"type": "object", "properties": {"a": {"description": "untyped"}}}, "permissive"),
    ],
)
def test_untestable_schemas(schema: Any, reason: str) -> None:
    candidates, why = pp.analyse_input_schema(schema)
    assert candidates == []
    assert why is not None
    assert reason in why
    assert pp.build_invalid_arguments(schema) == []


def test_wrong_value_rules() -> None:
    assert pp.wrong_value({"enum": ["a"]}) == pp.ENUM_SENTINEL
    assert pp.wrong_value({"const": 1}) == pp.ENUM_SENTINEL
    for json_type, sentinel in pp.WRONG_VALUES.items():
        assert pp.wrong_value({"type": json_type}) == sentinel
    assert pp.wrong_value({"type": ["string", "null"]}) == 12345  # first type not listed: integer
    assert pp.wrong_value({"type": ["number"]}) == "mcpcheck-wrong-type-7f3a"  # integer skipped
    assert pp.is_unsure(pp.wrong_value({"type": list(pp.JSON_TYPES)}))
    assert pp.is_unsure(pp.wrong_value({"anyOf": [{"type": "string"}]}))
    assert pp.is_unsure(pp.wrong_value({"$ref": "#/x"}))
    assert pp.is_unsure(pp.wrong_value(None))
    assert pp.is_unsure(pp.wrong_value({"type": "weird"}))


def test_strategy_d_only_when_additional_properties_false() -> None:
    assert pp.build_invalid_arguments({"type": "object", "additionalProperties": False}) == [
        ({pp.UNEXPECTED_PROPERTY_NAME: pp.UNEXPECTED_PROPERTY_VALUE}, "unexpected-property")
    ]
    assert pp.build_invalid_arguments({"type": "object", "additionalProperties": True}) == []
    assert pp.build_invalid_arguments({"type": "object", "additionalProperties": {}}) == []


# --------------------------------------------------------------------------- eligibility (U2)

Q = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


@pytest.mark.parametrize(
    ("annotations", "eligible"),
    [
        (None, False),
        ({}, False),
        ({"idempotentHint": True}, False),
        ({"readOnlyHint": "true"}, False),  # a string is not True
        ({"readOnlyHint": 1}, False),
        ({"destructiveHint": True}, False),
        ({"destructiveHint": 0}, False),
        ({"readOnlyHint": False, "destructiveHint": True}, False),
        ({"readOnlyHint": True}, True),
        ({"destructiveHint": False}, True),
        ({"readOnlyHint": True, "destructiveHint": True}, True),
        ({"readOnlyHint": False, "destructiveHint": False}, True),
    ],
)
def test_safe_requires_explicit_annotations(
    annotations: dict[str, Any] | None, eligible: bool
) -> None:
    ok, reason = pp.is_probe_eligible(tool("t", Q, annotations), "safe")
    assert ok is eligible
    if not eligible:
        assert "--probe all" in reason
    assert pp.is_probe_eligible(tool("t", Q, annotations), "all") == (True, "")


def test_annotation_flags_follow_schema_defaults() -> None:
    assert pp.annotation_flags({"name": "t"}) == (False, True)
    assert pp.annotation_flags({"name": "t", "annotations": {"readOnlyHint": True}}) == (True, True)
    assert pp.annotation_flags({"name": "t", "annotations": {"destructiveHint": False}}) == (
        False,
        False,
    )


def test_permissive_schema_untestable_under_both_policies() -> None:
    for policy in ("safe", "all"):
        ok, reason = pp.is_probe_eligible(tool("t", {"type": "object"}), policy)
        assert ok is False
        assert "permissive" in reason


def test_tool_without_string_name_is_skipped() -> None:
    assert pp.is_probe_eligible({"inputSchema": Q, "annotations": READ_ONLY}, "all")[0] is False
    assert pp.is_probe_eligible({"name": 3, "inputSchema": Q}, "all")[0] is False


# --------------------------------------------------------------------------- x-mcp-header handling

HDR_OPTIONAL = {
    "type": "object",
    "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
}
HDR_REQUIRED_Q = {
    "type": "object",
    "properties": {
        "q": {"type": "string"},
        "region": {"type": "string", "x-mcp-header": "Region"},
    },
    "required": ["q"],
}


def test_safe_drops_candidates_carrying_annotated_values() -> None:
    plan = pp.plan_tool(tool("hdr", HDR_OPTIONAL), 0, "safe")
    assert plan.candidates == []
    assert plan.skip_reason is not None
    assert "x-mcp-header" in plan.skip_reason
    assert plan.header_map == {("region",): "Region"}


def test_all_keeps_annotated_candidate_and_renders_param_header() -> None:
    plan = pp.plan_tool(tool("hdr", HDR_OPTIONAL), 0, "all")
    assert plan.best == ({"region": 12345}, "wrong-type-optional")
    assert pp.param_headers(plan.header_map, plan.best[0]) == {"Mcp-Param-Region": "12345"}


def test_missing_required_preferred_over_annotated_paths() -> None:
    for policy in ("safe", "all"):
        plan = pp.plan_tool(tool("hdr", HDR_REQUIRED_Q), 0, policy)
        assert plan.best == ({}, "missing-required")
        assert pp.param_headers(plan.header_map, {}) == {}


def test_invalid_x_mcp_header_is_never_called() -> None:
    bad = {
        "type": "object",
        "properties": {"amount": {"type": "number", "x-mcp-header": "Amount"}},
        "required": ["amount"],
    }
    ok, reason = pp.is_probe_eligible(tool("t", bad), "all")
    assert ok is False
    assert "x-mcp-header" in reason


# --------------------------------------------------------------------------- plan_tools


def test_plan_tools_dedupes_and_truncates() -> None:
    tools = [tool("a", Q), tool("b", Q), tool("a", Q), tool("c", Q, None), tool("d", Q)]
    selected, skipped = pp.plan_tools(tools, "safe", 2)
    assert [p.name for p in selected] == ["a", "b"]
    reasons = {(p.name, p.index): p.skip_reason for p in skipped}
    assert reasons[("a", 2)] is not None
    assert "duplicate" in str(reasons[("a", 2)])
    assert "--probe all" in str(reasons[("c", 3)])
    assert reasons[("d", 4)] == "beyond --max-tools 2"
    assert all(p.candidates == [] for p in skipped)


def test_catalogue_destructive_norequired_never_eligible_under_safe() -> None:
    tools = [*DEFAULT_TOOLS, *CATALOGUE_TOOLS]
    selected, skipped = pp.plan_tools(tools, "safe", 100)
    names = {p.name for p in selected}
    assert "destructive_norequired" not in names
    skip = next(p for p in skipped if p.name == "destructive_norequired")
    assert skip.skip_reason is not None
    assert "unannotated" in skip.skip_reason
    assert next(p for p in skipped if p.name == "readonly_permissive").skip_reason is not None
    assert "工具_unicode" in names
    for plan in selected:
        assert plan.read_only or not plan.destructive


def test_catalogue_destructive_norequired_under_all_gets_wrong_type_payload() -> None:
    selected, _ = pp.plan_tools([*DEFAULT_TOOLS, *CATALOGUE_TOOLS], "all", 100)
    plan = next(p for p in selected if p.name == "destructive_norequired")
    assert plan.best == ({"q": 12345}, "wrong-type-optional")
    assert plan.read_only is False
    assert plan.destructive is True


def test_encode_header_value_sentinel_for_unicode_name() -> None:
    assert encode_header_value("工具_unicode").startswith("=?base64?")
    assert encode_header_value("ascii_name") == "ascii_name"


def test_unresolvable_ref_is_untestable_only_when_the_proof_needs_it() -> None:
    """``{}`` violates ``required`` before any ``$ref`` is consulted: the proof is sound and kept."""
    schema = {
        "type": "object",
        "properties": {"a": {"$ref": "https://example.com/remote"}},
        "required": ["a"],
    }
    assert pp.build_invalid_arguments(schema) == [({}, "missing-required")]
    rooted = {"$ref": "https://example.com/remote", "type": "object", "required": ["a"]}
    assert pp.build_invalid_arguments(rooted) == []
