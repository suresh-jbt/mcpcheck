"""The ``schemas`` check (PLAN.md §7.4): every advertised tool publishes valid schemas.

The check walks the complete ``tools/list`` (shared, cached pages) and judges
each *raw* tool dictionary: ``inputSchema`` present, object-typed, root
``type: "object"`` and valid under its declared or default dialect;
``outputSchema`` valid when present (optional per spec → best-practice
warning when absent, "trivially permissive" warning for ``{}``); the six
``x-mcp-header`` MUSTs via the SDK's own :func:`find_invalid_x_mcp_header`
(a conforming client silently drops violating tools); the tool-name SHOULDs;
external ``$ref`` (never fetched) and icon URI schemes. The SDK's
``validate_server_result`` is run per page as a witness for wire-shape
violations mcpcheck failed to model. It never calls a tool.

Raw JSON is authoritative: ``mcp_types.Tool`` crashes on exactly the listings
this check diagnoses (``inputSchema: null``) and ``ClientSession`` silently
masks invalid ``x-mcp-header`` tools (verified on mcp 2.2.0 and 2.3.0).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

from jsonschema import validators
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from mcpcheck import spec_urls, wire
from mcpcheck.checks.base import Check
from mcpcheck.wire import OutcomeKind, classify_reply

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jsonschema.protocols import Validator

    from mcpcheck.checks.base import CheckContext, Findings
    from mcpcheck.client import RawReply

__all__ = ["SchemasCheck", "ToolIssue", "evaluate_listing", "evaluate_tool"]

DEFAULT_DIALECT = "https://json-schema.org/draft/2020-12/schema"
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
SENSITIVE_RE = re.compile(
    r"pass(word|wd)?|secret|token|api[_-]?key|authorization|credential|ssn|credit[_-]?card",
    re.IGNORECASE,
)
_ANNOTATION_KEYS = frozenset(
    {
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "examples",
        "default",
        "deprecated",
        "readOnly",
        "writeOnly",
    }
)
# dict-valued keywords whose *keys* are names, not JSON Schema keywords
_NAME_MAPS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
_ICON_SCHEMES = frozenset({"https", "data"})
_SAFE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WITNESS_IGNORED_LOCS = frozenset({"ttlMs", "cacheScope", "resultType", "_meta"})
_X_MCP_PROPERTY_RE = re.compile(r"property '((?:[^'\\]|\\.)*)'")
_X_MCP_EXPECTED = (
    "x-mcp-header only on integer/string/boolean properties reachable via a pure "
    "properties chain; non-empty RFC 9110 token; case-insensitively unique"
)
_X_MCP_CLIENT_NOTE = "conforming Streamable HTTP clients MUST exclude this tool from tools/list"
_UNKNOWN_DIALECT: Any = object()  # sentinel default for validator_for()

# per-tool rule table order (§7.4 R1-R11, X) — tie-break for the headline
_TABLE_ORDER: tuple[str, ...] = (
    "schemas.tool_malformed",
    "schemas.input_schema_missing",
    "schemas.input_schema_not_object",
    "schemas.input_schema_type",
    "schemas.input_schema_invalid",
    "schemas.dialect_unsupported",
    "schemas.dialect_not_2020_12",
    "schemas.schema_unverifiable",
    "schemas.output_schema_missing",
    "schemas.output_schema_not_object",
    "schemas.output_schema_invalid",
    "schemas.output_schema_permissive",
    "schemas.x_mcp_header_invalid",
    "schemas.x_mcp_header_sensitive",
    "schemas.name_invalid",
    "schemas.ref_external",
    "schemas.icon_src_scheme",
    "schemas.name_duplicate",
    "schemas.listing_malformed",
    "schemas.pagination_malformed",
    "schemas.pagination_rejected_own_cursor",
    "schemas.pagination_incomplete",
    "schemas.sdk_rejects_listing",
    "schemas.not_jsonrpc",
    "schemas.tools_list_unimplemented",
)
# summary phrases ("<n> of <N> tools <phrase>"); findings sharing a phrase form one headline group
PHRASE: dict[str, str] = {
    "schemas.input_schema_missing": "have invalid inputSchema",
    "schemas.input_schema_not_object": "have invalid inputSchema",
    "schemas.input_schema_type": "have invalid inputSchema",
    "schemas.input_schema_invalid": "have invalid inputSchema",
    "schemas.output_schema_invalid": "have invalid outputSchema",
    "schemas.output_schema_not_object": "have invalid outputSchema",
    "schemas.x_mcp_header_invalid": "dropped by conforming clients (invalid x-mcp-header)",
    "schemas.output_schema_missing": "missing outputSchema",
    "schemas.output_schema_permissive": "have a permissive outputSchema",
    "schemas.name_invalid": "have non-conforming names",
    "schemas.name_duplicate": "have non-conforming names",
    "schemas.dialect_unsupported": "use a non-default schema dialect",
    "schemas.dialect_not_2020_12": "use a non-default schema dialect",
    "schemas.ref_external": "reference external $ref",
    "schemas.schema_unverifiable": "exceed schema bounds (not validated)",
    "schemas.x_mcp_header_sensitive": "mirror sensitive parameters into headers",
    "schemas.icon_src_scheme": "have non-https icon URIs",
    "schemas.tool_malformed": "have schema errors",
}


# --------------------------------------------------------------------------- pure layer


@dataclass
class ToolIssue:
    """One finding produced by the pure evaluation layer (before it reaches ``Findings``)."""

    severity: str  # "must" | "should" | "best-practice"
    code: str
    message: str
    subject: str
    tool: str | None
    evidence: dict[str, Any] = field(default_factory=dict)
    spec_url: str | None = None
    page: int = 0
    index: int | None = None  # index within the page (None for page-level issues)


def _jp(base: str, *parts: str | int) -> str:
    """Append JSONPath segments to ``base`` (``$.tools[4].inputSchema.properties.amount``)."""
    out = base
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        elif _SAFE_KEY_RE.match(part):
            out += f".{part}"
        else:
            out += f"[{json.dumps(part)}]"
    return out


def _got(value: Any, limit: int = 200) -> Any:
    """A compact, size-bounded rendering of a server value for ``evidence.got``."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        text = repr(value)
    if isinstance(value, str) and len(value) <= limit:
        return value
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def schema_bounds(schema: Any) -> tuple[int, int, bool]:
    """Iteratively measure ``(depth, nodes, within_bounds)`` of a JSON value.

    Stops early once :data:`wire.MAX_SCHEMA_DEPTH` or :data:`wire.MAX_SCHEMA_NODES`
    is exceeded, so arbitrarily large payloads cost bounded CPU.
    """
    stack: list[tuple[Any, int]] = [(schema, 1)]
    nodes = 0
    depth = 0
    while stack:
        node, d = stack.pop()
        if isinstance(node, dict):
            children: list[Any] = list(node.values())
        elif isinstance(node, list):
            children = node
        else:
            continue
        nodes += 1
        depth = max(depth, d)
        if depth > wire.MAX_SCHEMA_DEPTH or nodes > wire.MAX_SCHEMA_NODES:
            return depth, nodes, False
        stack.extend((child, d + 1) for child in children)
    return depth, nodes, True


def _keyword_positions(schema: Any) -> Iterator[tuple[tuple[str | int, ...], dict[str, Any]]]:
    """Yield every object in *keyword* position (never the name-map objects themselves)."""
    stack: list[tuple[Any, tuple[str | int, ...], bool]] = [(schema, (), False)]
    while stack:
        node, path, is_name_map = stack.pop()
        if isinstance(node, dict):
            if not is_name_map:
                yield path, node
            for key, value in node.items():
                child_is_map = not is_name_map and key in _NAME_MAPS and isinstance(value, dict)
                stack.append((value, (*path, key), child_is_map))
        elif isinstance(node, list):
            stack.extend((item, (*path, i), False) for i, item in enumerate(node))


def external_refs(schema: Any) -> list[tuple[tuple[str | int, ...], str]]:
    """Static ``$ref`` walk: ``(path, ref)`` for every ref that is not resolvable locally.

    A ref is local iff it starts with ``#`` or its document part equals a declared
    ``$id`` in the same schema. Nothing is ever fetched (B:303-312).
    """
    ids: set[str] = set()
    refs: list[tuple[tuple[str | int, ...], str]] = []
    for path, node in _keyword_positions(schema):
        sid = node.get("$id")
        if isinstance(sid, str):
            ids.add(sid.split("#", 1)[0])
        ref = node.get("$ref")
        if isinstance(ref, str):
            refs.append((path, ref))
    return [
        (path, ref)
        for path, ref in refs
        if not ref.startswith("#") and ref.split("#", 1)[0] not in ids
    ]


def is_trivially_permissive(schema: Any) -> bool:
    """``True``/annotation-only object: accepts every JSON value."""
    if schema is True:
        return True
    return isinstance(schema, dict) and not (set(schema) - _ANNOTATION_KEYS)


def resolve_dialect(
    schema: dict[str, Any],
) -> tuple[type[Validator] | None, str | None, str | None]:
    """Return ``(validator_cls, dialect_uri, problem)`` for a schema object.

    ``validator_cls`` is ``None`` when the dialect is unknown (validity unasserted);
    ``problem`` is set when ``$schema`` is present but not a string.
    """
    if "$schema" not in schema:
        return validators.Draft202012Validator, DEFAULT_DIALECT, None
    declared = schema["$schema"]
    if not isinstance(declared, str):
        return None, None, f"$schema must be a string, got {_type_name(declared)}"
    cls = validators.validator_for(schema, default=_UNKNOWN_DIALECT)
    if cls is _UNKNOWN_DIALECT:
        return None, declared, None
    return cls, declared, None


def _is_2020_12(cls: type[Validator]) -> bool:
    return cls is validators.Draft202012Validator


class _ToolEvaluator:
    """Applies rules R1-R11 to one raw tool dict and collects :class:`ToolIssue` records."""

    def __init__(self, tool: Any, *, page: int, index: int, strict: bool) -> None:
        self.raw = tool
        self.page = page
        self.index = index
        self.strict = strict
        self.base = _jp("$.tools", index)
        name = tool.get("name") if isinstance(tool, dict) else None
        self.name: str | None = name if isinstance(name, str) else None
        self.subject = f"tool:{self.name}" if self.name is not None else f"tool:#{page}.{index}"
        self.issues: list[ToolIssue] = []

    # -- recording -----------------------------------------------------------------

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        *,
        path: str,
        spec_url: str | None = None,
        **evidence: Any,
    ) -> None:
        ev: dict[str, Any] = {"path": path, "page": self.page, **evidence}
        if self.strict and severity != "must":
            ev["promoted_by_strict"] = True
        self.issues.append(
            ToolIssue(
                severity,
                code,
                message,
                self.subject,
                self.name,
                ev,
                spec_url,
                self.page,
                self.index,
            )
        )

    # -- rules ---------------------------------------------------------------------

    def run(self) -> list[ToolIssue]:
        tool = self.raw
        if not isinstance(tool, dict):
            self.add(
                "must",
                "schemas.tool_malformed",
                f"tool entry is {_type_name(tool)}, not an object",
                path=self.base,
                got=_got(tool),
                expected="Tool object",
            )
            return self.issues
        self.rule_shape(tool)
        self.rule_name()
        self.rule_input_schema(tool.get("inputSchema"), present="inputSchema" in tool)
        self.rule_output_schema(tool.get("outputSchema"), present="outputSchema" in tool)
        self.rule_icons(tool.get("icons"))
        return self.issues

    def rule_shape(self, tool: dict[str, Any]) -> None:
        """R1: ``mcp_types.Tool`` shape, excluding the schema fields handled by R2-R6."""
        import mcp_types

        try:
            mcp_types.Tool.model_validate(tool)
        except ValidationError as exc:
            problems = [
                err
                for err in exc.errors()
                if not err["loc"] or err["loc"][0] not in ("inputSchema", "outputSchema")
            ]
            if not problems:
                return
            parts = [
                f"{_loc_text(err['loc'])}: {err['msg']}" for err in problems[: wire.MAX_ITEMS_SHOWN]
            ]
            more = len(problems) - len(parts)
            text = "; ".join(parts) + (f" (+{more} more)" if more > 0 else "")
            self.add(
                "must",
                "schemas.tool_malformed",
                f"tool object does not match the Tool schema: {text}",
                path=self.base,
                got=_got({k: tool[k] for k in tool if k not in ("inputSchema", "outputSchema")}),
                expected="Tool object (name, title?, description?, icons?, annotations?, _meta?)",
            )
        except (TypeError, ValueError, RecursionError) as exc:
            self.add(
                "must",
                "schemas.tool_malformed",
                f"tool object cannot be validated: {type(exc).__name__}",
                path=self.base,
                expected="Tool object",
            )

    def rule_name(self) -> None:
        """R9: name syntax SHOULDs (1-128 chars, no spaces/commas/special characters)."""
        if self.name is None:
            return  # R1 already reports a missing / non-string name
        if NAME_RE.match(self.name):
            return
        if not self.name:
            why = "empty"
        elif len(self.name) > 128:
            why = f"{len(self.name)} characters long (SHOULD be 1-128)"
        else:
            why = "contains spaces, commas or other special characters"
        self.add(
            "should",
            "schemas.name_invalid",
            f"tool name {self.name[:60]!r} {why}",
            path=_jp(self.base, "name"),
            got=_got(self.name),
            expected="1-128 characters from [A-Za-z0-9_.-]",
        )

    def rule_input_schema(self, schema: Any, *, present: bool) -> None:
        """R2-R5 and R7-R8, R10 for ``inputSchema``."""
        path = _jp(self.base, "inputSchema")
        if not present:
            self.add(
                "must",
                "schemas.input_schema_missing",
                "inputSchema is missing (MUST be a valid JSON Schema object)",
                path=path,
                expected='{"type": "object", ...}',
            )
            return
        if not isinstance(schema, dict):
            self.add(
                "must",
                "schemas.input_schema_not_object",
                f"inputSchema is {_type_name(schema)}, not a JSON Schema object (MUST not be null)",
                path=path,
                got=_got(schema),
                expected='{"type": "object", ...}',
            )
            return
        root_type = schema.get("type")
        if root_type != "object":
            self.add(
                "must",
                "schemas.input_schema_type",
                'inputSchema root type is missing; the Tool schema requires the literal "object"'
                if "type" not in schema
                else f'inputSchema root type is {_got(root_type)!r}, not the literal "object"',
                path=_jp(path, "type"),
                got=_got(root_type),
                expected='"object"',
            )
        self.validate_schema(schema, path=path, which="inputSchema")
        self.rule_x_mcp_header(schema, path=path)

    def rule_output_schema(self, schema: Any, *, present: bool) -> None:
        """R6: ``outputSchema`` optional; valid when present; ``{}`` is permissive."""
        path = _jp(self.base, "outputSchema")
        if not present or schema is None:
            self.add(
                "best-practice",
                "schemas.output_schema_missing",
                "outputSchema is explicitly null (treated as absent); publishing one lets clients validate structuredContent"
                if present
                else "no outputSchema (optional per spec); publishing one lets clients validate structuredContent",
                path=path,
                expected="a JSON Schema describing structuredContent",
            )
            return
        if not isinstance(schema, dict):
            self.add(
                "must",
                "schemas.output_schema_not_object",
                f"outputSchema is {_type_name(schema)}, not a JSON Schema object",
                path=path,
                got=_got(schema),
                expected="JSON Schema object",
            )
            return
        valid = self.validate_schema(schema, path=path, which="outputSchema")
        if valid and is_trivially_permissive(schema):
            self.add(
                "best-practice",
                "schemas.output_schema_permissive",
                "outputSchema is trivially permissive (annotations only); it constrains nothing",
                path=path,
                got=_got(schema),
                expected="constraints such as type/properties",
            )

    def validate_schema(self, schema: dict[str, Any], *, path: str, which: str) -> bool:
        """R5 pipeline: dialect → bounds → ``check_schema``; also R10. Returns validity."""
        invalid_code = (
            "schemas.input_schema_invalid"
            if which == "inputSchema"
            else "schemas.output_schema_invalid"
        )
        cls, dialect, problem = resolve_dialect(schema)
        if problem is not None:
            self.add(
                "must",
                invalid_code,
                f"{which} is not a valid JSON Schema: {problem}",
                path=_jp(path, "$schema"),
                got=_got(schema.get("$schema")),
                expected="a dialect URI string (default 2020-12)",
            )
            return False
        depth, nodes, within = schema_bounds(schema)
        if not within:
            self.add(
                "should",
                "schemas.schema_unverifiable",
                f"{which} exceeds mcpcheck's bounds (depth {depth} > {wire.MAX_SCHEMA_DEPTH} or "
                f"nodes {nodes} > {wire.MAX_SCHEMA_NODES}); validity not asserted",
                path=path,
                dialect=dialect,
                expected=f"depth <= {wire.MAX_SCHEMA_DEPTH}, nodes <= {wire.MAX_SCHEMA_NODES}",
            )
            return False
        if cls is None:
            self.add(
                "should",
                "schemas.dialect_unsupported",
                f"{which} declares an unknown $schema dialect {dialect!r}; validity not asserted "
                "(clients MUST handle unsupported dialects gracefully)",
                path=_jp(path, "$schema"),
                got=_got(dialect),
                dialect=dialect,
                expected="2020-12 (default) or a documented supported dialect",
            )
            return False
        if not _is_2020_12(cls):
            self.add(
                "should",
                "schemas.dialect_not_2020_12",
                f"{which} declares dialect {dialect!r}; 2020-12 is the RECOMMENDED default",
                path=_jp(path, "$schema"),
                got=_got(dialect),
                dialect=dialect,
                expected=DEFAULT_DIALECT,
            )
        try:
            cls.check_schema(schema)
        except SchemaError as exc:
            where = _jp(path, *exc.path)
            self.add(
                "must",
                invalid_code,
                f"{which} is not a valid JSON Schema under {dialect}: {exc.message} (at {where})",
                path=where,
                got=_got(exc.instance),
                dialect=dialect,
                expected=f"valid {dialect} schema",
            )
            return False
        except RecursionError:
            self.add(
                "should",
                "schemas.schema_unverifiable",
                f"{which} is too deeply nested to validate (RecursionError); validity not asserted",
                path=path,
                dialect=dialect,
                expected=f"depth <= {wire.MAX_SCHEMA_DEPTH}",
            )
            return False
        for ref_path, ref in external_refs(schema):
            self.add(
                "should",
                "schemas.ref_external",
                f"{which} references external $ref {ref[:120]!r}; mcpcheck never fetches it and "
                "implementations MUST NOT dereference network $ref",
                path=_jp(path, *ref_path, "$ref"),
                got=_got(ref),
                expected='local "#..." reference or a declared $id',
            )
        return True

    def rule_x_mcp_header(self, schema: dict[str, Any], *, path: str) -> None:
        """R7/R8 via the SDK helpers (stack-based and total)."""
        from mcp.shared.inbound import find_invalid_x_mcp_header, x_mcp_header_map

        reason: str | None = find_invalid_x_mcp_header(schema)
        if reason is not None:
            where = path
            m = _X_MCP_PROPERTY_RE.search(reason)
            if m:
                parts: list[str | int] = []
                for prop in m.group(1).split("."):
                    parts.extend(("properties", prop))
                where = _jp(path, *parts)
            self.add(
                "must",
                "schemas.x_mcp_header_invalid",
                f"{reason} — {_X_MCP_CLIENT_NOTE}",
                path=where,
                got=_got(_dig(schema, where, path)),
                expected=_X_MCP_EXPECTED,
            )
            return
        for prop_path, token in x_mcp_header_map(schema).items():
            if any(SENSITIVE_RE.search(p) for p in prop_path) or SENSITIVE_RE.search(token):
                parts2: list[str | int] = []
                for prop in prop_path:
                    parts2.extend(("properties", prop))
                self.add(
                    "should",
                    "schemas.x_mcp_header_sensitive",
                    f"property {'.'.join(prop_path)!r} looks sensitive but is mirrored into header "
                    f"{token!r}; servers SHOULD NOT expose sensitive parameters via x-mcp-header "
                    "(header values are visible to intermediaries)",
                    path=_jp(path, *parts2, "x-mcp-header"),
                    got=_got(token),
                    expected="no x-mcp-header on secrets/credentials/PII (heuristic)",
                )

    def rule_icons(self, icons: Any) -> None:
        """R11: icon ``src`` MUST be https or ``data:`` (clients reject other schemes)."""
        if not isinstance(icons, list):
            return  # non-list icons are an R1 shape error
        for i, icon in enumerate(icons):
            if not isinstance(icon, dict):
                continue
            src = icon.get("src")
            if not isinstance(src, str):
                continue
            scheme = urlsplit(src).scheme.lower()
            if scheme in _ICON_SCHEMES:
                continue
            self.add(
                "best-practice",
                "schemas.icon_src_scheme",
                f"icon src uses scheme {scheme or '(relative)'!r}; clients MUST reject icon URIs that "
                "are not https or data:",
                path=_jp(self.base, "icons", i, "src"),
                got=_got(src),
                expected="https:// or data: URI",
            )


def _loc_text(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(p) for p in loc) or "(root)"


def _dig(schema: dict[str, Any], where: str, base: str) -> Any:
    """Return the sub-object of ``schema`` at JSONPath ``where`` relative to ``base``."""
    if where == base:
        return schema
    rest = where[len(base) :]
    node: Any = schema
    for part in re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\"(?:[^\"\\]|\\.)*\")\]", rest):
        key = part[0] or json.loads(part[1])
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def evaluate_tool(
    tool: Any, *, page: int = 0, index: int = 0, strict: bool = False
) -> list[ToolIssue]:
    """Apply rules R1-R11 to one raw tool dict (pure; no I/O)."""
    return _ToolEvaluator(tool, page=page, index=index, strict=strict).run()


def evaluate_listing(
    page_results: list[dict[str, Any]], *, strict: bool = False
) -> tuple[list[ToolIssue], int]:
    """Evaluate every tool of the ``ok`` page results in order; return ``(issues, tool_count)``.

    Includes the listing-shape rules (``result.tools`` must be a list), duplicate
    names (X) and the per-page SDK witness (``sdk_rejects_listing``).
    """
    issues: list[ToolIssue] = []
    seen_names: dict[str, tuple[int, int]] = {}
    total = 0
    for page, result in enumerate(page_results):
        tools = result.get("tools")
        page_issues: list[ToolIssue] = []
        if not isinstance(tools, list):
            page_issues.append(
                ToolIssue(
                    "must",
                    "schemas.listing_malformed",
                    f"result.tools is {_type_name(tools)}, not an array"
                    if "tools" in result
                    else "result.tools is missing (ListToolsResult requires a tools array)",
                    f"tools/list page {page}",
                    None,
                    {"path": "$.tools", "page": page, "got": _got(tools), "expected": "array"},
                    page=page,
                )
            )
        else:
            for index, tool in enumerate(tools):
                total += 1
                tool_issues = evaluate_tool(tool, page=page, index=index, strict=strict)
                page_issues.extend(tool_issues)
                name = tool.get("name") if isinstance(tool, dict) else None
                if isinstance(name, str):
                    if name in seen_names:
                        first = seen_names[name]
                        ev: dict[str, Any] = {
                            "path": _jp("$.tools", index, "name"),
                            "page": page,
                            "got": _got(name),
                            "expected": "unique tool names within a server",
                            "first_seen": f"page {first[0]} index {first[1]}",
                        }
                        if strict:
                            ev["promoted_by_strict"] = True
                        page_issues.append(
                            ToolIssue(
                                "should",
                                "schemas.name_duplicate",
                                f"tool name {name[:60]!r} is listed more than once (first on page "
                                f"{first[0]}, index {first[1]}); names SHOULD be unique",
                                f"tool:{name}",
                                name,
                                ev,
                                page=page,
                                index=index,
                            )
                        )
                    else:
                        seen_names[name] = (page, index)
        page_issues.extend(_witness(result, page, page_issues))
        issues.extend(page_issues)
    return issues, total


def _witness(result: dict[str, Any], page: int, covered: list[ToolIssue]) -> list[ToolIssue]:
    """Run the SDK's strict validator; report locs that R1-R6 did not already explain."""
    from mcp_types.methods import validate_server_result

    try:
        validate_server_result("tools/list", wire.PROTOCOL_VERSION, result)
    except ValidationError as exc:
        errors = exc.errors()
    except (TypeError, ValueError, RecursionError, KeyError):
        return []
    else:
        return []
    musts = [i for i in covered if i.severity == "must"]
    out: list[ToolIssue] = []
    for err in errors:
        loc = tuple(err["loc"])
        if not loc or loc[0] in _WITNESS_IGNORED_LOCS:
            continue
        if _witness_covered(loc, musts):
            continue
        subject = f"tools/list page {page}"
        tool_name: str | None = None
        if len(loc) >= 2 and loc[0] == "tools" and isinstance(loc[1], int):
            tools = result.get("tools")
            if isinstance(tools, list) and loc[1] < len(tools) and isinstance(tools[loc[1]], dict):
                candidate = tools[loc[1]].get("name")
                if isinstance(candidate, str):
                    tool_name = candidate
                    subject = f"tool:{candidate}"
                else:
                    subject = f"tool:#{page}.{loc[1]}"
            else:
                subject = f"tool:#{page}.{loc[1]}"
        out.append(
            ToolIssue(
                "must",
                "schemas.sdk_rejects_listing",
                f"the SDK's ListToolsResult validator rejects {_loc_text(loc)}: {err['msg']} "
                "(a wire-shape violation mcpcheck did not model separately)",
                subject,
                tool_name,
                {
                    "path": _jp("$", *loc),
                    "page": page,
                    "got": _got(err.get("input")),
                    "expected": err["type"],
                },
                page=page,
            )
        )
    return out


def _witness_covered(loc: tuple[int | str, ...], musts: list[ToolIssue]) -> bool:
    if loc[0] == "tools" and len(loc) == 1:
        return any(i.code == "schemas.listing_malformed" for i in musts)
    if loc[0] == "nextCursor":
        return True  # pagination_malformed is reported from the walk itself
    if loc[0] == "tools" and len(loc) >= 2 and isinstance(loc[1], int):
        index = loc[1]
        mine = [i for i in musts if i.index == index]
        if any(i.code == "schemas.tool_malformed" for i in mine):
            return True
        if len(loc) >= 3 and loc[2] in ("inputSchema", "outputSchema"):
            family = "input" if loc[2] == "inputSchema" else "output"
            return any(i.code.startswith(f"schemas.{family}_schema_") for i in mine)
        return False
    return False


# --------------------------------------------------------------------------- the check


class SchemasCheck(Check):
    """Tool inputSchema/outputSchema are valid JSON Schema."""

    name: ClassVar[str] = "schemas"
    title: ClassVar[str] = "Tool inputSchema/outputSchema are valid JSON Schema"
    spec_url: ClassVar[str] = spec_urls.SPEC_TOOL
    order: ClassVar[int] = 50

    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Walk the shared ``tools/list`` pages and judge every raw tool definition."""
        pages = await ctx.tools_list_pages()
        sent_origin = ctx.cfg.origin is not None
        sent_token = bool(ctx.cfg.token)
        first = classify_reply(pages[0], sent_origin=sent_origin, sent_token=sent_token)
        if not first.ok:
            await self._page0_not_ok(ctx, f, pages[0], first)
            return

        results: list[dict[str, Any]] = []
        incomplete: str | None = None
        for n, reply in enumerate(pages):
            outcome = classify_reply(reply, sent_origin=sent_origin, sent_token=sent_token)
            if outcome.ok:
                result = reply.result
                if result is None:
                    f.must(
                        "schemas.listing_malformed",
                        f"tools/list page {n} result is not an object",
                        subject=f"tools/list page {n}",
                        evidence={"page": n, "got": _got(reply.response), "expected": "object"},
                    )
                    incomplete = f"Listing incomplete (malformed result on page {n})"
                    break
                results.append(result)
                continue
            cursor = _cursor_of(results[-1]) if results else None
            if outcome.rpc_code == wire.INVALID_PARAMS:
                f.should(
                    "schemas.pagination_rejected_own_cursor",
                    f"server rejected the nextCursor it issued ({cursor!r}) with -32602 on page {n}: "
                    f"{outcome.rpc_message!r}; the remaining tools were not evaluated",
                    subject=f"tools/list page {n}",
                    evidence={
                        "page": n,
                        "cursor": cursor,
                        "http_status": reply.status,
                        "rpc_code": outcome.rpc_code,
                    },
                )
                break
            if outcome.kind is OutcomeKind.NOT_JSONRPC_2XX:
                f.must(
                    "schemas.not_jsonrpc",
                    f"tools/list page {n}: {outcome.detail}",
                    subject=f"tools/list page {n}",
                    evidence={"page": n, "http_status": reply.status, "cursor": cursor},
                )
                incomplete = f"Listing incomplete (non-JSON-RPC reply on page {n})"
                break
            f.should(
                "schemas.pagination_incomplete",
                f"tools/list page {n} could not be fetched ({outcome.detail}); "
                f"{len(results)} page(s) evaluated",
                subject=f"tools/list page {n}",
                evidence={"page": n, "cursor": cursor, "http_status": reply.status},
            )
            incomplete = f"Listing incomplete ({_short(outcome.detail)} on page {n})"
            break
        else:
            incomplete = self._check_walk_end(f, results, ctx.walk_stopped("tools/list"))
            if (
                incomplete is not None
                and "cursor loop" in incomplete
                and len(results) > 1
                and results[-1].get("tools") in [r.get("tools") for r in results[:-1]]
            ):
                results.pop()  # the looping page repeats an earlier one: do not double-count

        issues, total = evaluate_listing(results, strict=ctx.cfg.strict)
        for issue in issues:
            _emit(f, issue)
        f.details(
            f"Evaluated {total} tool{'s' if total != 1 else ''} over {len(results)} "
            f"tools/list page{'s' if len(results) != 1 else ''}; schemas validated with "
            f"jsonschema under their declared or default dialect; no $ref or icon was fetched."
        )
        if incomplete is not None:
            f.inconclusive(incomplete)
        _summarise(f, total, incomplete=incomplete)

    async def _page0_not_ok(
        self, ctx: CheckContext, f: Findings, reply: RawReply, outcome: wire.Outcome
    ) -> None:
        if outcome.kind is OutcomeKind.METHOD_NOT_FOUND:
            caps = await ctx.capabilities()
            declared = caps is not None and caps.get("tools") not in (None, False)
            if declared:
                f.must(
                    "schemas.tools_list_unimplemented",
                    "server/discover declares capabilities.tools but tools/list answers -32601 "
                    "(servers that support tools MUST implement tools/list)",
                    subject="tools/list",
                    evidence={
                        "http_status": reply.status,
                        "rpc_code": outcome.rpc_code,
                        "capabilities.tools": _got(caps.get("tools") if caps else None),
                    },
                )
                f.summary("capabilities.tools declared but tools/list is not implemented (-32601)")
            else:
                f.inconclusive(
                    "Not evaluated: server does not advertise tools (tools/list → -32601)"
                )
            return
        if outcome.kind is OutcomeKind.NOT_JSONRPC_2XX:
            f.must(
                "schemas.not_jsonrpc",
                f"tools/list: {outcome.detail}",
                subject="tools/list",
                evidence={"http_status": reply.status, "media_type": reply.media_type},
            )
            f.summary(f"tools/list answered without a JSON-RPC body ({_short(outcome.detail)})")
            return
        if outcome.kind is OutcomeKind.RPC_ERROR:
            f.inconclusive(
                f"Not evaluated: tools/list answered JSON-RPC error {outcome.rpc_code} "
                f"on HTTP {reply.status}"
            )
            return
        f.inconclusive(outcome.detail)

    @staticmethod
    def _check_walk_end(
        f: Findings, results: list[dict[str, Any]], stopped: str | None = None
    ) -> str | None:
        """Explain why the cached walker stopped although every page was ``ok``.

        ``stopped`` is :meth:`CheckContext.walk_stopped` for ``tools/list``: set when
        mcpcheck's own page or byte budget ended the walk.
        """
        if not results:
            return None
        last = results[-1]
        nxt = last.get("nextCursor")
        if nxt is None:
            return None  # absent / null: the listing is complete
        n = len(results) - 1
        if stopped is not None and "budget" in stopped:
            f.should(
                "schemas.pagination_incomplete",
                f"tools/list still had a nextCursor when {stopped}; pagination stopped and the "
                "remaining tools were not evaluated",
                subject=f"tools/list page {n}",
                evidence={"page": n, "cursor": nxt, "pages": len(results), "stopped": stopped},
            )
            return "Listing incomplete (walk byte budget reached)"
        if not isinstance(nxt, str):
            f.must(
                "schemas.pagination_malformed",
                f"nextCursor on page {n} is {_type_name(nxt)}, not a string; pagination stopped",
                subject=f"tools/list page {n}",
                evidence={
                    "page": n,
                    "path": "$.nextCursor",
                    "got": _got(nxt),
                    "expected": "string",
                },
            )
            return f"Listing incomplete (non-string nextCursor on page {n})"
        earlier = [_cursor_of(r) for r in results[:-1]]
        if nxt in earlier:
            f.should(
                "schemas.pagination_incomplete",
                f"nextCursor {nxt!r} on page {n} repeats an earlier cursor (loop); "
                f"pagination stopped after {len(results)} page(s)",
                subject=f"tools/list page {n}",
                evidence={"page": n, "cursor": nxt, "pages": len(results)},
            )
            return f"Listing incomplete (cursor loop after {len(results)} pages)"
        f.should(
            "schemas.pagination_incomplete",
            f"tools/list still had a nextCursor after {len(results)} pages (mcpcheck's MAX_PAGES); "
            "pagination stopped",
            subject=f"tools/list page {n}",
            evidence={"page": n, "cursor": nxt, "pages": len(results)},
        )
        return f"Listing incomplete (MAX_PAGES={wire.MAX_PAGES} reached)"


def _cursor_of(result: dict[str, Any]) -> Any:
    return result.get("nextCursor")


def _short(detail: str) -> str:
    text = detail.removeprefix("Not evaluated: ")
    return text[:60]


def _emit(f: Findings, issue: ToolIssue) -> None:
    kwargs: dict[str, Any] = {
        "subject": issue.subject,
        "evidence": issue.evidence or None,
        "tool": issue.tool,
        "spec_url": issue.spec_url,
    }
    if issue.severity == "must":
        f.must(issue.code, issue.message, **kwargs)
    elif issue.severity == "should":
        f.should(issue.code, issue.message, **kwargs)
    else:
        f.best_practice(issue.code, issue.message, **kwargs)


def _tools_word(n: int) -> str:
    return "tool" if n == 1 else "tools"


def _summarise(f: Findings, total: int, *, incomplete: str | None) -> None:
    """§7.4(6): pool = must findings if any else warnings; headline = biggest group."""
    if f.failures:
        pool = f.failures
        others = len(f.warnings)
        noun = "issue"
    elif f.warnings:
        if incomplete is not None:
            return  # status is inconclusive: the walk reason is the summary
        pool = f.warnings
        others = 0
        noun = "warning"
    else:
        if incomplete is not None:
            return
        if total == 0:
            f.summary("No tools advertised (nothing to check)")
        else:
            f.summary(f"All {total} {_tools_word(total)} publish valid schemas")
        return

    groups: dict[str, list[Any]] = {}
    for finding in pool:
        groups.setdefault(PHRASE.get(finding.code, finding.code), []).append(finding)

    def rank(item: tuple[str, list[Any]]) -> tuple[int, int]:
        _key, members = item
        tools = {m.subject for m in members if m.subject.startswith("tool:")}
        order = min(
            (_TABLE_ORDER.index(m.code) if m.code in _TABLE_ORDER else len(_TABLE_ORDER))
            for m in members
        )
        return (-len(tools), order)

    key, members = min(groups.items(), key=rank)
    rest = len(pool) - len(members) + others
    suffix = f" (+{rest} more {noun}{'s' if rest != 1 else ''})" if rest else ""
    tools = {m.subject for m in members if m.subject.startswith("tool:")}
    if tools and key in PHRASE.values():
        f.summary(f"{len(tools)} of {total} {_tools_word(total)} {key}{suffix}")
    else:
        f.summary(f"{members[0].message}{suffix}")
