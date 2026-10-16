"""The ``discover`` check (PLAN.md §7.3): ``server/discover`` implemented and well-formed.

One ``server/discover`` POST on the shared ``main`` client (cached in the
:class:`~mcpcheck.checks.base.CheckContext`) is classified per §6.6, its JSON-RPC
envelope and SSE framing are judged, the result object is validated field by
field by the pure :func:`validate_discover_result` (raw checks first — the SDK
validator is type-lax — then the SDK validator as a safety net), and finally
the advertised capabilities are cross-checked against the four cached list
methods. Nothing else is sent (§4, §9).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

import pydantic

from mcpcheck import spec_urls as su
from mcpcheck import wire
from mcpcheck.checks.base import LIST_ITEM_KEYS, Check, items_of
from mcpcheck.wire import OutcomeKind, classify_reply

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mcpcheck.checks.base import CheckContext, Findings
    from mcpcheck.client import RawReply
    from mcpcheck.wire import Outcome

Severity = Literal["must", "should", "best-practice", "info"]

METHOD = "server/discover"
KNOWN_RESULT_KEYS: frozenset[str] = frozenset(
    {
        "resultType",
        "supportedVersions",
        "capabilities",
        "instructions",
        "ttlMs",
        "cacheScope",
        "_meta",
    }
)
"""Top-level keys of ``DiscoverResult`` (SCH:4023-4031)."""
PRIMITIVE_CAPABILITIES: tuple[str, ...] = ("tools", "prompts", "resources")
KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {*PRIMITIVE_CAPABILITIES, "logging", "completions", "experimental", "extensions", "tasks"}
)
"""Keys of ``ServerCapabilities`` in mcp-types 2.x (the set is open, SCH:4243)."""
CAPABILITY_FLAGS: tuple[str, ...] = ("listChanged", "subscribe")
KNOWN_RESULT_META_KEYS: frozenset[str] = frozenset({wire.META_SERVER_INFO})
LIST_METHODS_BY_CAPABILITY: dict[str, tuple[str, ...]] = {
    "tools": ("tools/list",),
    "prompts": ("prompts/list",),
    "resources": ("resources/list", "resources/templates/list"),
}
CACHE_HEADER_NAMES: tuple[str, ...] = (
    "cache-control",
    "etag",
    "expires",
    "last-modified",
    "vary",
    "age",
    "pragma",
)
LEGACY_INITIALIZE_KEYS: tuple[str, ...] = ("serverInfo", "protocolVersion")
_DATE_SHAPE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EVIDENCE_CLIP = 200
_SUMMARY_MESSAGE_CLIP = 60

# outcomes that are the environment's fault: inconclusive, never a verdict (§6.6)
_INFRA_KINDS: frozenset[OutcomeKind] = frozenset(
    {
        OutcomeKind.TRANSPORT,
        OutcomeKind.UNREACHABLE,
        OutcomeKind.AUTH_REQUIRED,
        OutcomeKind.FORBIDDEN,
        OutcomeKind.SERVER_ERROR,
        OutcomeKind.REDIRECTED,
    }
)


# --------------------------------------------------------------------------- pure helpers


@dataclass(frozen=True)
class Item:
    """One finding produced by the pure validators, before it reaches a ``Findings`` builder.

    ``key`` is the top-level result key the item is about (used to suppress duplicate
    SDK-validator findings and to build the one-line summary); ``label`` is the short
    phrase used in the summary (``'cacheScope "user"'``); ``missing`` marks a
    "required field absent" item.
    """

    severity: Severity
    code: str
    subject: str
    message: str
    spec_url: str | None = None
    evidence: dict[str, Any] | None = None
    key: str = ""
    label: str = ""
    missing: bool = False


def short(value: Any, limit: int = _EVIDENCE_CLIP) -> Any:
    """A JSON-safe, size-bounded copy of ``value`` for ``evidence.got``."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 1] + "…"
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return text[: limit - 1] + "…"


def describe(value: Any) -> str:
    """A one-word-ish description of a JSON value for messages (``null``, ``"x"``, ``array``)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(short(value, 60), ensure_ascii=False)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _validate_cache_hints(result: Mapping[str, Any]) -> list[Item]:
    """The §7.6 hint rules applied to ``server/discover`` (codes ``discover.*``).

    Same rules as the ``cache`` check: ``ttlMs`` must be present, a JSON integer and
    ``>= 0`` (integral floats and values above 30 days are best-practice warnings);
    ``cacheScope`` must be exactly ``"public"`` or ``"private"``.
    """
    items: list[Item] = []

    def add(
        severity: Severity,
        slug: str,
        key: str,
        message: str,
        spec_url: str,
        label: str,
        *,
        missing: bool = False,
        **evidence: Any,
    ) -> None:
        items.append(
            Item(
                severity,
                f"discover.{slug}",
                f"result.{key}",
                message,
                spec_url,
                evidence or None,
                key=key,
                label=label,
                missing=missing,
            )
        )

    if "ttlMs" not in result:
        add(
            "must",
            "ttl_missing",
            "ttlMs",
            "result has no ttlMs; servers MUST include caching hints on server/discover",
            su.SPEC_CACHEABLE_RESULTS,
            "ttlMs",
            missing=True,
            got=None,
            expected="integer ttlMs >= 0",
        )
    else:
        ttl = result["ttlMs"]
        if (
            isinstance(ttl, bool)
            or not isinstance(ttl, (int, float))
            or (isinstance(ttl, float) and not math.isfinite(ttl))
        ):
            # schema.ts types ttlMs as `number`: a string/bool/null/NaN/Infinity is a type error
            add(
                "must",
                "ttl_not_integer",
                "ttlMs",
                f"ttlMs is {describe(ttl)}, not a JSON number",
                su.SPEC_TTL,
                f"ttlMs {describe(ttl)}",
                got=short(ttl),
                expected="JSON integer (milliseconds)",
            )
        else:
            if isinstance(ttl, float) and ttl.is_integer():
                add(
                    "best-practice",
                    "ttl_integral_float",
                    "ttlMs",
                    f"ttlMs is the float {ttl!r}; send the integer {int(ttl)}",
                    su.SPEC_TTL,
                    f"ttlMs {ttl!r}",
                    got=ttl,
                    expected=int(ttl),
                )
            elif isinstance(ttl, float):
                # CA:45 defines ttlMs as an integer in prose; the schema allows any number
                add(
                    "should",
                    "ttl_not_integral",
                    "ttlMs",
                    f"ttlMs is {ttl!r}, not a whole number of milliseconds",
                    su.SPEC_TTL,
                    f"ttlMs {ttl!r}",
                    got=short(ttl),
                    expected="JSON integer (milliseconds)",
                )
            if ttl < 0:
                add(
                    "must",
                    "ttl_negative",
                    "ttlMs",
                    f"ttlMs is {ttl!r}; servers MUST provide a value >= 0",
                    su.SPEC_TTL,
                    f"ttlMs {ttl!r}",
                    got=ttl,
                    expected=">= 0",
                )
            elif ttl > wire.TTL_SANITY_MS:
                add(
                    "best-practice",
                    "ttl_implausible",
                    "ttlMs",
                    f"ttlMs {ttl!r} exceeds 30 days ({wire.TTL_SANITY_MS} ms)",
                    su.SPEC_TTL,
                    f"ttlMs {ttl!r}",
                    got=ttl,
                    expected=f"<= {wire.TTL_SANITY_MS}",
                )

    if "cacheScope" not in result:
        add(
            "must",
            "scope_missing",
            "cacheScope",
            "result has no cacheScope; servers MUST include caching hints on server/discover",
            su.SPEC_CACHE_SCOPE,
            "cacheScope",
            missing=True,
            got=None,
            expected='"public" or "private"',
        )
    else:
        scope = result["cacheScope"]
        if not isinstance(scope, str) or scope not in ("public", "private"):
            add(
                "must",
                "scope_invalid",
                "cacheScope",
                f'cacheScope is {describe(scope)}; only "public" or "private" are defined',
                su.SPEC_CACHE_SCOPE,
                f"cacheScope {describe(scope)}",
                got=short(scope),
                expected='"public" or "private"',
            )
    return items


def _validate_result_type(result: Mapping[str, Any]) -> tuple[str | None, list[Item]]:
    """Return ``(resultType or None, items)`` (B:75, B:86, V:123-125)."""
    if "resultType" not in result:
        return None, [
            Item(
                "must",
                "discover.result_type_missing",
                "result.resultType",
                "result has no resultType; every 2026-07-28 result MUST carry one",
                su.SPEC_RESULT_RESPONSES,
                {"got": None, "expected": '"complete"'},
                key="resultType",
                label="resultType",
                missing=True,
            )
        ]
    rt = result["resultType"]
    if not isinstance(rt, str):
        return None, [
            Item(
                "must",
                "discover.result_type_not_string",
                "result.resultType",
                f"resultType is {describe(rt)}, not a string",
                su.SPEC_RESULT_RESPONSES,
                {"got": short(rt), "expected": '"complete"'},
                key="resultType",
                label=f"resultType {describe(rt)}",
            )
        ]
    if rt == "complete":
        return rt, []
    if rt == "input_required":
        return rt, [
            Item(
                "must",
                "discover.unexpected_result_type",
                "result.resultType",
                'resultType "input_required" on server/discover, which is not a '
                "multi-round-trip operation",
                su.SPEC_RESULT_RESPONSES,
                {"got": rt, "expected": '"complete"'},
                key="resultType",
                label='resultType "input_required"',
            )
        ]
    return rt, [
        Item(
            "must",
            "discover.unrecognized_result_type",
            "result.resultType",
            f"resultType {describe(rt)} is not recognized; clients MUST treat the result as invalid",
            su.SPEC_RESULT_RESPONSES,
            {"got": rt, "expected": '"complete"'},
            key="resultType",
            label=f"resultType {describe(rt)}",
        )
    ]


def supported_versions_of(result: Mapping[str, Any]) -> list[str] | None:
    """``result.supportedVersions`` when it is a list of strings, else ``None``."""
    versions = result.get("supportedVersions")
    if isinstance(versions, list) and all(isinstance(v, str) for v in versions):
        return list(versions)
    return None


def _validate_supported_versions(result: Mapping[str, Any]) -> list[Item]:
    """``supportedVersions`` rules (SCH:4026, DI:96-97, V terminology)."""
    key, subject = "supportedVersions", "result.supportedVersions"
    if "supportedVersions" not in result:
        return [
            Item(
                "must",
                "discover.supported_versions_invalid",
                subject,
                "result has no supportedVersions (required string array)",
                su.SPEC_SCHEMA_DISCOVER_RESULT,
                {"got": None, "expected": '["2026-07-28", ...]'},
                key=key,
                label="supportedVersions",
                missing=True,
            )
        ]
    versions = result["supportedVersions"]
    if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
        return [
            Item(
                "must",
                "discover.supported_versions_invalid",
                subject,
                f"supportedVersions is {describe(versions)}, not an array of strings",
                su.SPEC_SCHEMA_DISCOVER_RESULT,
                {"got": short(versions), "expected": '["2026-07-28", ...]'},
                key=key,
                label=f"supportedVersions {describe(versions)}",
            )
        ]
    items: list[Item] = []
    if wire.PROTOCOL_VERSION not in versions:
        items.append(
            Item(
                "best-practice",
                "discover.supported_versions_omit_served",
                subject,
                f"supportedVersions {json.dumps(versions)} omits {wire.PROTOCOL_VERSION}, the "
                "version this request was just served with (the SDK's ClientSession.adopt() "
                "refuses such servers)",
                su.SPEC_DISCOVER_RESULT,
                {"got": versions, "expected": f'contains "{wire.PROTOCOL_VERSION}"'},
                key=key,
                label=f"supportedVersions omits {wire.PROTOCOL_VERSION}",
            )
        )
    duplicates = sorted({v for v in versions if versions.count(v) > 1})
    if duplicates:
        items.append(
            Item(
                "should",
                "discover.supported_versions_duplicate",
                subject,
                f"supportedVersions lists {', '.join(duplicates)} more than once",
                su.SPEC_DISCOVER_RESULT,
                {"got": versions, "duplicates": duplicates},
                key=key,
                label="supportedVersions duplicates",
            )
        )
    nondate = [v for v in versions if not _DATE_SHAPE.match(v)]
    if nondate:
        items.append(
            Item(
                "info",
                "discover.supported_versions_nondate",
                subject,
                f"supportedVersions has non date-shaped entries: {', '.join(nondate)} "
                "(future identifiers need not be dates)",
                su.SPEC_VERSIONING_TERMINOLOGY,
                {"got": nondate},
                key=key,
            )
        )
    return items


def _validate_capabilities(result: Mapping[str, Any]) -> list[Item]:
    """``capabilities`` rules (SCH:4027, SCH:4243, V:87-89, DP:14-16)."""
    key = "capabilities"
    if "capabilities" not in result:
        return [
            Item(
                "must",
                "discover.capabilities_invalid",
                "result.capabilities",
                "result has no capabilities object",
                su.SPEC_SCHEMA_DISCOVER_RESULT,
                {"got": None, "expected": "ServerCapabilities object"},
                key=key,
                label="capabilities",
                missing=True,
            )
        ]
    caps = result["capabilities"]
    if not isinstance(caps, dict):
        return [
            Item(
                "must",
                "discover.capabilities_invalid",
                "result.capabilities",
                f"capabilities is {describe(caps)}, not an object",
                su.SPEC_SCHEMA_DISCOVER_RESULT,
                {"got": short(caps), "expected": "ServerCapabilities object"},
                key=key,
                label=f"capabilities {describe(caps)}",
            )
        ]
    items: list[Item] = []
    for name, value in caps.items():
        subject = f"result.capabilities.{name}"
        if name == "extensions":
            items.extend(_validate_extensions(value))
            continue
        if name not in KNOWN_CAPABILITIES:
            items.append(
                Item(
                    "info",
                    "discover.capability_unknown",
                    subject,
                    f"unknown capability key {name!r} (the capability set is open)",
                    su.SPEC_SCHEMA_SERVER_CAPABILITIES,
                    {"got": short(value)},
                    key=key,
                )
            )
            continue
        if name == "logging":
            items.append(
                Item(
                    "should",
                    "discover.logging_deprecated",
                    subject,
                    "the logging capability is deprecated; new implementations SHOULD NOT adopt it",
                    su.SPEC_DEPRECATED,
                    {"got": short(value)},
                    key=key,
                    label="capabilities.logging deprecated",
                )
            )
        if value is None:
            items.append(
                Item(
                    "must",
                    "discover.capability_null",
                    subject,
                    f"capabilities.{name} is null; the schema requires an object (omit the key "
                    "to leave the capability undeclared)",
                    su.SPEC_SCHEMA_SERVER_CAPABILITIES,
                    {"got": None, "expected": "object"},
                    key=key,
                    label=f"capabilities.{name} null",
                )
            )
            continue
        if not isinstance(value, dict):
            items.append(
                Item(
                    "must",
                    "discover.capabilities_invalid",
                    subject,
                    f"capabilities.{name} is {describe(value)}, not an object",
                    su.SPEC_SCHEMA_SERVER_CAPABILITIES,
                    {"got": short(value), "expected": "object"},
                    key=key,
                    label=f"capabilities.{name} {describe(value)}",
                )
            )
            continue
        items.extend(
            Item(
                "must",
                "discover.capability_flag_not_boolean",
                f"{subject}.{flag}",
                f"capabilities.{name}.{flag} is {describe(value[flag])}, not a boolean",
                su.SPEC_SCHEMA_SERVER_CAPABILITIES,
                {"got": short(value[flag]), "expected": "true or false"},
                key=key,
                label=f"capabilities.{name}.{flag} {describe(value[flag])}",
            )
            for flag in CAPABILITY_FLAGS
            if flag in value and not isinstance(value[flag], bool)
        )
    return items


def _validate_extensions(value: Any) -> list[Item]:
    """``capabilities.extensions``: ids follow the ``_meta`` key rules with a prefix (V:87-89)."""
    key = "capabilities"
    if not isinstance(value, dict):
        return [
            Item(
                "must",
                "discover.capabilities_invalid",
                "result.capabilities.extensions",
                f"capabilities.extensions is {describe(value)}, not an object",
                su.SPEC_EXTENSION_NEGOTIATION,
                {"got": short(value), "expected": "object keyed by extension id"},
                key=key,
                label=f"capabilities.extensions {describe(value)}",
            )
        ]
    items: list[Item] = []
    for ext_id, ext in value.items():
        subject = f"result.capabilities.extensions[{json.dumps(ext_id)}]"
        if "/" not in ext_id or not wire.META_KEY_RE.match(ext_id):
            items.append(
                Item(
                    "must",
                    "discover.extension_id_invalid",
                    subject,
                    f"extension id {ext_id!r} must be a prefixed _meta-style key "
                    "(reverse-DNS prefix, '/', name)",
                    su.SPEC_EXTENSION_NEGOTIATION,
                    {"got": ext_id, "expected": "e.g. io.example/my-extension"},
                    key=key,
                    label=f"extension id {ext_id!r}",
                )
            )
        if not isinstance(ext, dict):
            items.append(
                Item(
                    "must",
                    "discover.extension_value_not_object",
                    subject,
                    f"extension {ext_id!r} value is {describe(ext)}, not an object",
                    su.SPEC_EXTENSION_NEGOTIATION,
                    {"got": short(ext), "expected": "object"},
                    key=key,
                    label=f"extension {ext_id!r} {describe(ext)}",
                )
            )
    return items


def server_info_of(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """``result._meta["io.modelcontextprotocol/serverInfo"]`` when it is an object."""
    meta = result.get("_meta")
    if isinstance(meta, dict) and isinstance(meta.get(wire.META_SERVER_INFO), dict):
        info: dict[str, Any] = meta[wire.META_SERVER_INFO]
        return info
    return None


def _validate_meta(result: Mapping[str, Any]) -> list[Item]:
    """``_meta`` rules (B:329-346) and ``serverInfo`` (DI:100-101, SCH:4187-4194)."""
    key = "_meta"
    si_subject = f'result._meta["{wire.META_SERVER_INFO}"]'
    missing_info = Item(
        "should",
        "discover.server_info_missing",
        si_subject,
        "server does not identify itself",
        su.SPEC_DISCOVER_RESULT,
        {
            "got": None,
            "expected": '{"name": string, "version": string}',
            "hint": f"Servers SHOULD include {wire.META_SERVER_INFO} in every result's _meta "
            "(basic#_meta).",
        },
        key=key,
        label="serverInfo",
    )
    if "_meta" not in result:
        return [missing_info]
    meta = result["_meta"]
    if not isinstance(meta, dict):
        return [
            Item(
                "must",
                "discover.meta_not_object",
                "result._meta",
                f"_meta is {describe(meta)}, not an object",
                su.SPEC_META,
                {"got": short(meta), "expected": "object"},
                key=key,
                label=f"_meta {describe(meta)}",
            )
        ]
    items: list[Item] = []
    for name in meta:
        if not isinstance(name, str) or not wire.META_KEY_RE.match(name):
            items.append(
                Item(
                    "must",
                    "discover.meta_key_invalid",
                    f"result._meta[{json.dumps(name)}]",
                    f"_meta key {name!r} violates the key format (labels, optional "
                    "reverse-DNS prefix, no spaces)",
                    su.SPEC_META,
                    {"got": name},
                    key=key,
                    label=f"_meta key {name!r}",
                )
            )
        elif name not in KNOWN_RESULT_META_KEYS and wire.RESERVED_PREFIX_RE.match(name):
            # B:362-365: official extensions define further io.modelcontextprotocol/ keys, so
            # an unknown reserved key is an observation, not a violation
            items.append(
                Item(
                    "info",
                    "discover.meta_reserved_key_unknown",
                    f"result._meta[{json.dumps(name)}]",
                    f"_meta key {name!r} uses a reserved prefix but is not a key defined by the "
                    "core specification (an extension may define it)",
                    su.SPEC_META,
                    {"got": name},
                    key=key,
                    label=f"_meta reserved key {name!r}",
                )
            )
    if wire.META_SERVER_INFO not in meta:
        items.append(missing_info)
        return items
    info = meta[wire.META_SERVER_INFO]
    error = _implementation_error(info)
    if error is not None:
        items.append(
            Item(
                "must",
                "discover.server_info_malformed",
                si_subject,
                f"serverInfo is not a valid Implementation: {error}",
                su.SPEC_SCHEMA_IMPLEMENTATION,
                {"got": short(info), "expected": '{"name": string, "version": string}'},
                key=key,
                label="serverInfo malformed",
            )
        )
        return items
    bad_urls = _bad_implementation_urls(info)
    if bad_urls:
        items.append(
            Item(
                "best-practice",
                "discover.server_info_url_scheme",
                si_subject,
                "serverInfo URLs use unexpected schemes: " + ", ".join(bad_urls),
                su.SPEC_ICONS,
                {"got": bad_urls, "expected": "websiteUrl http(s)://, icon src https:// or data:"},
                key=key,
                label="serverInfo URL scheme",
            )
        )
    return items


def _implementation_error(info: Any) -> str | None:
    """Validate ``info`` as an SDK ``Implementation``; return a short error or ``None``."""
    if not isinstance(info, dict):
        return f"{describe(info)} is not an object"
    from mcp_types import Implementation  # lazy: checks import before the SDK is validated

    try:
        Implementation.model_validate(info)
    except pydantic.ValidationError as exc:
        return "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
    return None


def _bad_implementation_urls(info: Mapping[str, Any]) -> list[str]:
    bad: list[str] = []
    website = info.get("websiteUrl")
    if isinstance(website, str) and not website.lower().startswith(("http://", "https://")):
        bad.append(f"websiteUrl {short(website, 60)!r}")
    icons = info.get("icons")
    if isinstance(icons, list):
        for icon in icons:
            src = icon.get("src") if isinstance(icon, dict) else None
            if isinstance(src, str) and not src.lower().startswith(("https://", "data:")):
                bad.append(f"icon src {short(src, 60)!r}")
    return bad


def _validate_instructions(result: Mapping[str, Any]) -> list[Item]:
    if "instructions" not in result:
        return [
            Item(
                "info",
                "discover.instructions_absent",
                "result.instructions",
                "no instructions provided (optional)",
                su.SPEC_DISCOVER_RESULT,
                None,
                key="instructions",
            )
        ]
    instructions = result["instructions"]
    if not isinstance(instructions, str):
        return [
            Item(
                "must",
                "discover.instructions_not_string",
                "result.instructions",
                f"instructions is {describe(instructions)}, not a string",
                su.SPEC_SCHEMA_DISCOVER_RESULT,
                {"got": short(instructions), "expected": "string"},
                key="instructions",
                label=f"instructions {describe(instructions)}",
            )
        ]
    return []


def _validate_extra_keys(result: Mapping[str, Any]) -> list[Item]:
    items: list[Item] = []
    legacy = [k for k in LEGACY_INITIALIZE_KEYS if k in result]
    if legacy:
        items.append(
            Item(
                "best-practice",
                "discover.legacy_initialize_fields",
                "result." + "/".join(legacy),
                f"top-level {', '.join(legacy)} belong to the legacy initialize result; "
                f"2026-07-28 carries serverInfo in _meta and versions in supportedVersions",
                su.SPEC_DISCOVER_RESULT,
                {"got": {k: short(result[k]) for k in legacy}},
                key=legacy[0],
                label=f"legacy {'/'.join(legacy)}",
            )
        )
    if "nextCursor" in result:
        # DiscoverResult allows unknown keys (SCH:4023-4031): mcpcheck's opinion, not a SHOULD
        items.append(
            Item(
                "best-practice",
                "discover.next_cursor_present",
                "result.nextCursor",
                "nextCursor on server/discover, which is not paginated",
                su.SPEC_DISCOVER_RESULT,
                {"got": short(result["nextCursor"])},
                key="nextCursor",
                label="nextCursor present",
            )
        )
    others = sorted(
        k
        for k in result
        if k not in KNOWN_RESULT_KEYS and k not in LEGACY_INITIALIZE_KEYS and k != "nextCursor"
    )
    items.extend(
        Item(
            "info",
            "discover.extra_key",
            f"result.{k}",
            f"unknown top-level result key {k!r} (ignored)",
            su.SPEC_DISCOVER_RESULT,
            {"got": short(result[k])},
            key=k,
        )
        for k in others
    )
    return items


def _sdk_safety_net(result: Mapping[str, Any], covered: set[str]) -> list[Item]:
    """Run the SDK validator; report locs whose top-level key has no finding yet."""
    from mcp_types.methods import validate_server_result  # lazy: see _implementation_error

    try:
        validate_server_result(METHOD, wire.PROTOCOL_VERSION, dict(result))
    except pydantic.ValidationError as exc:
        return [
            Item(
                "must",
                "discover.schema_validation",
                "result." + ".".join(str(p) for p in e["loc"]),
                f"SDK validator rejects the result: {e['msg']} at {list(e['loc'])}",
                su.SPEC_SCHEMA_DISCOVER_RESULT,
                {"loc": list(e["loc"]), "type": e["type"]},
                key=str(e["loc"][0]) if e["loc"] else "",
                label=f"{'.'.join(str(p) for p in e['loc'])} rejected by the SDK",
            )
            for e in exc.errors()
            if not e["loc"] or str(e["loc"][0]) not in covered
        ]
    return []


def validate_discover_result(result: Mapping[str, Any]) -> list[Item]:
    """Validate a ``server/discover`` ``result`` object (pure; PLAN.md §7.3 step 4 + 5).

    Raw checks run first because the SDK validator is type-lax (it accepts
    ``ttlMs: "5"``, ``tools: null`` and ``listChanged: "yes"``); the SDK validator is
    then applied as a safety net for anything the raw rules do not cover.
    """
    result_type, items = _validate_result_type(result)
    items.extend(_validate_supported_versions(result))
    items.extend(_validate_capabilities(result))
    items.extend(_validate_meta(result))
    items.extend(_validate_instructions(result))
    if result_type != "input_required":
        items.extend(_validate_cache_hints(result))
    items.extend(_validate_extra_keys(result))
    covered = {item.key for item in items if item.key}
    items.extend(_sdk_safety_net(result, covered))
    return items


def fields_summary(items: Sequence[Item]) -> str | None:
    """The §7.3(5) one-line summary for result-field failures, or ``None`` when none."""
    musts = [i for i in items if i.severity == "must"]
    if not musts:
        return None
    if all(i.missing for i in musts):
        return f"`{METHOD}` result missing " + ", ".join(i.label for i in musts)
    first = next(i for i in musts if not i.missing)
    more = f" (+{len(musts) - 1} more)" if len(musts) > 1 else ""
    return f"`{METHOD}` result invalid: {first.label}{more}"


def required_capabilities_valid(value: Any) -> bool:
    """``data.requiredCapabilities`` of a -32021 error MUST be a non-empty array of strings (B:389-394)."""
    return isinstance(value, list) and bool(value) and all(isinstance(v, str) for v in value)


def supported_from_error_data(data: Any) -> list[str] | None:
    """``data.supported`` of an ``UnsupportedProtocolVersionError`` when well-formed."""
    if not isinstance(data, dict):
        return None
    supported = data.get("supported")
    if isinstance(supported, list) and all(isinstance(v, str) for v in supported):
        return list(supported)
    return None


def error_code_range_item(code: int) -> Item | None:
    """N26: forbidden / reserved / legacy error-code ranges (B:119-146)."""
    if code in (-32002, -32042) or (
        -32099 <= code <= -32020 and code not in wire.MODERN_ERROR_CODES
    ):
        return Item(
            "must",
            "discover.forbidden_error_code",
            "rpc.error.code",
            f"error code {code} is forbidden or undefined in the reserved -32020..-32099 range",
            su.SPEC_ERROR_CODES,
            {"got": code},
        )
    if -32019 <= code <= -32000:
        return Item(
            "should",
            "discover.legacy_error_code",
            "rpc.error.code",
            f"error code {code} is from the legacy -32000..-32019 range (SHOULD NOT be used)",
            su.SPEC_ERROR_CODES,
            {"got": code},
        )
    return None


# --------------------------------------------------------------------------- the check


def _emit(f: Findings, items: Sequence[Item]) -> None:
    for item in items:
        kwargs: dict[str, Any] = {"subject": item.subject, "evidence": item.evidence}
        if item.spec_url is not None:
            kwargs["spec_url"] = item.spec_url
        if item.severity == "must":
            f.must(item.code, item.message, **kwargs)
        elif item.severity == "should":
            f.should(item.code, item.message, **kwargs)
        elif item.severity == "best-practice":
            f.best_practice(item.code, item.message, **kwargs)
        else:
            f.info(item.code, item.message, **kwargs)


def _first_rpc_error(reply: RawReply) -> tuple[int, str, Any] | None:
    for msg in reply.messages:
        err = wire.rpc_error_of(msg)
        if err is not None:
            return err
    return None


def _http_evidence(reply: RawReply) -> dict[str, Any]:
    return {
        "http_status": reply.status,
        "media_type": reply.media_type or None,
        "elapsed_ms": reply.elapsed_ms,
    }


def _id_matches(got: Any, want: Any) -> bool:
    return got is not None and bool(got == want and type(got) is type(want))


async def primary_reply(ctx: CheckContext) -> RawReply:
    """The cached ``server/discover`` reply, fetched with ``wait_for_eof`` when we are first.

    ``CheckContext.discover()`` does not wait for the SSE stream to end, so a reply it
    fetched cannot be judged for termination (T:130-132). When nothing is cached yet
    the POST is made here with ``wait_for_eof=True`` and stored in the same cache slot
    so later ``ctx.discover()`` / ``ctx.capabilities()`` calls reuse it (still exactly one
    ``server/discover`` POST on ``main``).
    """
    if ctx._discover is None:  # shared cache slot; see docstring
        ctx._discover = await ctx.raw.post(
            METHOD, wait_for_eof=True, purpose=METHOD, check=ctx.current_check
        )
    return await ctx.discover()


class DiscoverCheck(Check):
    """server/discover implemented and well-formed."""

    name: ClassVar[str] = "discover"
    title: ClassVar[str] = "server/discover implemented and well-formed"
    spec_url: ClassVar[str] = su.SPEC_DISCOVER
    order: ClassVar[int] = 30

    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Classify the cached ``server/discover`` reply and validate it (PLAN.md §7.3)."""
        reply = await primary_reply(ctx)
        outcome = classify_reply(
            reply, sent_origin=ctx.cfg.origin is not None, sent_token=bool(ctx.cfg.token)
        )
        self._cache_headers(f, reply)

        if outcome.kind in _INFRA_KINDS or reply.status == 403:
            f.inconclusive(outcome.detail)
            return
        if outcome.kind is OutcomeKind.NOT_MCP_ENDPOINT:
            if reply.status == 405:
                f.must(
                    "discover.post_rejected",
                    f"POST {METHOD} answered HTTP 405 (Allow: {reply.headers.get('allow', '-')}); "
                    "the MCP endpoint MUST accept POST",
                    subject="http.status",
                    spec_url=su.SPEC_SENDING_MESSAGES,
                    evidence={**_http_evidence(reply), "expected": "200 application/json or SSE"},
                )
                f.summary(f"`{METHOD}` returned HTTP 405")
                return
            f.inconclusive(outcome.detail)
            return

        response = reply.response or reply.any_response
        both = response is not None and "result" in response and "error" in response
        if outcome.rpc_code is not None and not both:
            self._judge_error(ctx, f, reply, outcome)
            return
        if outcome.kind is OutcomeKind.NON_MODERN_REJECTION and outcome.rpc_code is None:
            f.inconclusive(outcome.detail)  # 4xx without a JSON-RPC body (429, 418, empty 400)
            return

        summary: str | None = None
        result, summary = self._envelope(f, reply, outcome)
        if reply.is_sse:
            self._judge_stream(f, reply)
        if result is None:
            self._details(f, reply, None)
            if summary is not None:
                f.summary(summary)
            return

        items = validate_discover_result(result)
        _emit(f, items)
        summary = summary or fields_summary(items)
        caps = result.get("capabilities")
        if isinstance(caps, dict):
            cross = await self._cross_check(ctx, f, caps)
            summary = summary or cross
        self._compose(ctx, result)
        self._details(f, reply, result)
        if summary is None:
            if f.failures:
                summary = f.failures[0].message
            else:
                n = len(f.warnings)
                suffix = f" ({n} warning{'s' if n != 1 else ''})" if n else ""
                summary = f"`{METHOD}` responds correctly{suffix}"
        f.summary(summary)

    # ------------------------------------------------------------------ steps

    @staticmethod
    def _cache_headers(f: Findings, reply: RawReply) -> None:
        """HTTP cache headers are observed only (no normative text; §7.3(4))."""
        present = {
            name: reply.headers.get(name) for name in CACHE_HEADER_NAMES if name in reply.headers
        }
        if present:
            shown = ", ".join(f"{k}: {v}" for k, v in present.items())
            f.info(
                "discover.cache_headers",
                f"HTTP cache headers observed (not evaluated): {shown}",
                subject="http.headers",
                evidence={"cache_headers": present},
            )

    def _judge_error(
        self, ctx: CheckContext, f: Findings, reply: RawReply, outcome: Outcome
    ) -> None:
        """Step 1 rows for a JSON-RPC error answer."""
        err = _first_rpc_error(reply)
        code = outcome.rpc_code if outcome.rpc_code is not None else (err[0] if err else 0)
        message = outcome.rpc_message
        data = err[2] if err else None
        status = reply.status
        ev = {**_http_evidence(reply), "rpc_code": code, "rpc_message": short(message)}
        rejected = (
            f"`{METHOD}` rejected a valid request ({code}: {short(message, _SUMMARY_MESSAGE_CLIP)})"
        )

        if code == wire.METHOD_NOT_FOUND:
            f.must(
                "discover.not_implemented",
                f"{METHOD} answered -32601 Method not found; servers MUST implement it",
                subject=METHOD,
                spec_url=su.SPEC_DISCOVER,
                evidence={**ev, "expected": "DiscoverResult"},
            )
            if status != 404:
                f.must(
                    "discover.wrong_status_for_32601",
                    f"-32601 returned with HTTP {status}; unknown methods MUST be answered with "
                    "HTTP 404",
                    subject="http.status",
                    spec_url=su.SPEC_PROTOCOL_VERSION_HEADER,
                    evidence={**ev, "expected": 404},
                )
            f.summary(f"`{METHOD}` not implemented (-32601)")
            return

        if code == wire.UNSUPPORTED_PROTOCOL_VERSION:
            supported = supported_from_error_data(data)
            f.must(
                "discover.version_unsupported",
                f"server rejects {wire.PROTOCOL_VERSION} with -32022 Unsupported protocol version"
                + (f" (supported: {', '.join(supported)})" if supported else ""),
                subject=METHOD,
                spec_url=su.SPEC_PROTOCOL_VERSION_HEADER,
                evidence={**ev, "data": short(data)},
            )
            if supported is None:
                f.must(
                    "discover.unsupported_data_malformed",
                    f"-32022 data is {describe(data)}; UnsupportedProtocolVersionError data MUST "
                    'carry {"supported": string[], "requested": string}',
                    subject="rpc.error.data",
                    spec_url=su.SPEC_SCHEMA_UNSUPPORTED_VERSION,
                    evidence={
                        "got": short(data),
                        "expected": '{"supported": [...], "requested": "..."}',
                    },
                )
            elif wire.PROTOCOL_VERSION in supported:
                f.must(
                    "discover.contradictory_unsupported",
                    f"-32022 although data.supported lists {wire.PROTOCOL_VERSION}, the version "
                    "that was requested",
                    subject="rpc.error.data.supported",
                    spec_url=su.SPEC_PROTOCOL_VERSION_HEADER,
                    evidence={"got": supported, "requested": wire.PROTOCOL_VERSION},
                )
            if status != 400:
                f.must(
                    "discover.wrong_status_for_32022",
                    f"-32022 returned with HTTP {status}; the transport requires HTTP 400",
                    subject="http.status",
                    spec_url=su.SPEC_PROTOCOL_VERSION_HEADER,
                    evidence={**ev, "expected": 400},
                )
            if supported and ctx.server_supported_versions is None:
                ctx.server_supported_versions = supported
            shown = ", ".join(supported) if supported else "unknown"
            f.summary(f"Server does not support {wire.PROTOCOL_VERSION} (supports: {shown})")
            return

        if code == wire.HEADER_MISMATCH:
            f.must(
                "discover.header_mismatch_on_valid_request",
                f"-32020 Header mismatch on a well-formed request ({short(message, 120)}); "
                "Mcp-Method, MCP-Protocol-Version and _meta all agreed",
                subject=METHOD,
                spec_url=su.SPEC_SERVER_VALIDATION,
                evidence=ev,
            )
            f.summary(rejected)
            return

        if code == wire.MISSING_REQUIRED_CLIENT_CAPABILITY:
            required = data.get("requiredCapabilities") if isinstance(data, dict) else None
            shown = json.dumps(required) if required is not None else "unspecified"
            f.should(
                "discover.client_capability_demanded",
                f"server demands client capabilities {shown} before answering {METHOD}; "
                "mcpcheck declares none",
                subject=METHOD,
                spec_url=su.SPEC_META,
                evidence={**ev, "required_capabilities": short(required)},
            )
            # B:389-394: the MUSTs about the error's shape and status are observable even
            # though the verdict stays inconclusive
            if not required_capabilities_valid(required):
                f.must(
                    "discover.capability_error_malformed",
                    f"-32021 data.requiredCapabilities is {describe(required)}; it MUST list the "
                    "missing capabilities as a non-empty array of strings",
                    subject="rpc.error.data.requiredCapabilities",
                    spec_url=su.SPEC_META,
                    evidence={"got": short(required), "expected": '["<capability>", ...]'},
                )
            if status != 400:
                f.must(
                    "discover.wrong_status_for_32021",
                    f"-32021 returned with HTTP {status}; on HTTP the status MUST be 400",
                    subject="http.status",
                    spec_url=su.SPEC_META,
                    evidence={**ev, "expected": 400},
                )
            f.inconclusive(f"Not evaluated: server demands a client capability (-32021: {shown})")
            return

        if code == wire.INVALID_PARAMS:
            f.must(
                "discover.invalid_params_on_valid_request",
                f"-32602 Invalid params on a valid {METHOD} request whose params carry only _meta "
                f"({short(message, 120)})",
                subject=METHOD,
                spec_url=su.SPEC_DISCOVER,
                evidence=ev,
            )
            f.summary(rejected)
            return

        f.must(
            "discover.unexpected_error",
            f"{METHOD} answered JSON-RPC error {code} {short(message, 120)!r} on HTTP {status}",
            subject=METHOD,
            spec_url=su.SPEC_DISCOVER,
            evidence=ev,
        )
        range_item = error_code_range_item(code)
        if range_item is not None:
            _emit(f, [range_item])
        f.summary(rejected)

    def _envelope(
        self, f: Findings, reply: RawReply, outcome: Outcome
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Steps 2/3: find the response message and judge its envelope.

        Returns ``(result object or None, summary or None)``; a summary is set for
        envelope-level failures so that later field findings do not override it.
        """
        summary: str | None = None
        response: dict[str, Any] | None
        if outcome.kind is OutcomeKind.NOT_JSONRPC_2XX:
            response, summary = self._not_jsonrpc(f, reply, outcome)
            if response is None:
                return None, summary
        else:
            response = reply.response
        if response is None:  # pragma: no cover — classify_reply guarantees a response for ok
            return None, summary

        ev = _http_evidence(reply)
        if response.get("jsonrpc") != wire.JSONRPC_VERSION:
            f.must(
                "discover.envelope_invalid",
                f'response "jsonrpc" is {describe(response.get("jsonrpc"))}; it MUST be "2.0"',
                subject="rpc.jsonrpc",
                spec_url=su.SPEC_RESULT_RESPONSES,
                evidence={**ev, "got": short(response.get("jsonrpc")), "expected": "2.0"},
            )
            summary = summary or f'`{METHOD}` response "jsonrpc" is not "2.0"'
        rid = response.get("id")
        if rid is None:
            f.must(
                "discover.id_mismatch",
                f"response id is null; it MUST equal the request id {reply.request_id!r}",
                subject="rpc.id",
                spec_url=su.SPEC_RESULT_RESPONSES,
                evidence={**ev, "got": None, "expected": reply.request_id},
            )
            summary = summary or (
                f"`{METHOD}` response id null does not match request id {reply.request_id}"
            )
        if "result" in response and "error" in response:
            f.must(
                "discover.envelope_invalid",
                "response carries both result and error; a response MUST have exactly one",
                subject="rpc.result/error",
                spec_url=su.SPEC_RESULT_RESPONSES,
                evidence={**ev, "got": "result and error"},
            )
            summary = summary or f"`{METHOD}` response carries both result and error"
        result = response.get("result")
        if not isinstance(result, dict):
            f.must(
                "discover.envelope_invalid",
                f"response result is {describe(result)}; it MUST be an object",
                subject="rpc.result",
                spec_url=su.SPEC_RESULT_RESPONSES,
                evidence={**ev, "got": short(result), "expected": "object"},
            )
            return None, summary or f"`{METHOD}` result is {describe(result)}, not an object"
        return result, summary

    def _not_jsonrpc(
        self, f: Findings, reply: RawReply, outcome: Outcome
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The ``not_jsonrpc_2xx`` rows: 202, wrong media type, unparsable body, id mismatch."""
        ev = _http_evidence(reply)
        if outcome.extra.get("id_mismatch"):
            response = reply.any_response
            got = response.get("id") if response else None
            f.must(
                "discover.id_mismatch",
                f"response id {got!r} does not match request id {reply.request_id!r} "
                "(compared type-sensitively)",
                subject="rpc.id",
                spec_url=su.SPEC_RESULT_RESPONSES,
                evidence={**ev, "got": short(got), "expected": reply.request_id},
            )
            summary = (
                f"`{METHOD}` response id {json.dumps(got)} does not match request id "
                f"{reply.request_id}"
            )
            return response, summary
        if reply.is_sse and reply.parse_error is None and reply.status != 202:
            f.must(
                "discover.no_response",
                "SSE stream closed without a JSON-RPC response to the request",
                subject="http.body",
                spec_url=su.SPEC_RESULT_RESPONSES,
                evidence={**ev, "messages": len(reply.messages)},
            )
            return None, f"`{METHOD}` stream carried no JSON-RPC response"
        if reply.status == 202:
            f.must(
                "discover.not_jsonrpc",
                "HTTP 202 Accepted to a JSON-RPC request; requests MUST be answered with a "
                "JSON or SSE response body",
                subject="http.status",
                spec_url=su.SPEC_SENDING_MESSAGES,
                evidence={**ev, "expected": "200 with application/json or text/event-stream"},
            )
            return None, f"`{METHOD}` returned HTTP 202"
        media = reply.media_type or "no Content-Type"
        if reply.media_type in (wire.MEDIA_JSON, wire.MEDIA_SSE):
            f.must(
                "discover.not_jsonrpc",
                f"HTTP {reply.status} {reply.media_type} body is not a JSON-RPC response "
                f"({reply.parse_error or 'no response message'})",
                subject="http.body",
                spec_url=su.SPEC_SENDING_MESSAGES,
                evidence={**ev, "parse_error": reply.parse_error},
            )
            return (
                None,
                f"`{METHOD}` response is not JSON-RPC ({reply.parse_error or 'no response'})",
            )
        f.must(
            "discover.not_jsonrpc",
            f"HTTP {reply.status} with Content-Type {media}; responses MUST be "
            "application/json or text/event-stream",
            subject="http.content-type",
            spec_url=su.SPEC_SENDING_MESSAGES,
            evidence={**ev, "expected": "application/json or text/event-stream"},
        )
        summary = f"`{METHOD}` response is not JSON-RPC (Content-Type {media})"
        # body still validated when it parses as a JSON-RPC response anyway (E:7)
        messages, _err = wire.parse_json_body(reply.raw) if reply.raw else ((), None)
        response = next(
            (
                m
                for m in messages
                if wire.is_response(m)
                and (m.get("id") is None or _id_matches(m.get("id"), reply.request_id))
            ),
            None,
        )
        return response, summary

    @staticmethod
    def _judge_stream(f: Findings, reply: RawReply) -> None:
        """Step 2: SSE framing rules (T:112-134, LG:65-66)."""
        messages, _err = wire.sse_messages(wire.parse_sse_events(reply.raw))
        responses = [m for m in messages if wire.is_response(m)]
        ev = _http_evidence(reply)
        if len(responses) > 1:
            f.must(
                "discover.multiple_responses",
                f"{len(responses)} JSON-RPC responses on the stream for one request",
                subject="http.body",
                spec_url=su.SPEC_RESULT_RESPONSES,
                evidence={**ev, "ids": [short(m.get("id")) for m in responses]},
            )
        requests = [m for m in messages if wire.is_server_request(m)]
        if requests:
            methods = sorted({str(m.get("method")) for m in requests})
            f.must(
                "discover.server_request_on_stream",
                f"server sent JSON-RPC request(s) {', '.join(methods)} on the response stream; "
                "the stream MUST NOT carry server requests",
                subject="http.body",
                spec_url=su.SPEC_RECEIVING_MESSAGES,
                evidence={**ev, "methods": methods},
            )
        notifications = [m for m in messages if wire.is_notification(m)]
        if any(m.get("method") == "notifications/message" for m in notifications):
            f.must(
                "discover.log_without_loglevel",
                f"notifications/message emitted although the request carried no "
                f"{wire.META_LOG_LEVEL}",
                subject="http.body",
                spec_url=su.SPEC_LOGGING,
                evidence=ev,
            )
        if any(m.get("method") == "notifications/progress" for m in notifications):
            f.info(
                "discover.progress_notification",
                "notifications/progress received although no progressToken was sent",
                subject="http.body",
                evidence=ev,
            )
        others = sorted(
            {
                str(m.get("method"))
                for m in notifications
                if m.get("method") not in ("notifications/message", "notifications/progress")
            }
        )
        if others:
            f.details(f"Other notifications on the stream: {', '.join(others)}.")
        if reply.headers.get("x-accel-buffering", "").strip().lower() != "no":
            f.should(
                "discover.no_accel_buffering_header",
                "SSE response lacks X-Accel-Buffering: no (proxies may buffer the stream)",
                subject="http.headers",
                spec_url=su.SPEC_RECEIVING_MESSAGES,
                evidence={**ev, "got": reply.headers.get("x-accel-buffering"), "expected": "no"},
            )
        if reply.stream_terminated is False:
            f.should(
                "discover.stream_not_terminated",
                f"SSE stream still open {wire.SSE_TERMINATION_GRACE_S:g}s after the response; "
                "the server SHOULD close it after the response",
                subject="http.body",
                spec_url=su.SPEC_RECEIVING_MESSAGES,
                evidence=ev,
            )

    async def _cross_check(
        self, ctx: CheckContext, f: Findings, caps: Mapping[str, Any]
    ) -> str | None:
        """Step 6: declared capabilities vs the list methods actually served."""
        summary: str | None = None
        for prim, methods in LIST_METHODS_BY_CAPABILITY.items():
            declared = prim in caps
            states: list[tuple[str, str, int, str]] = []  # (state, method, items, detail)
            for method in methods:
                pages = await ctx.list_pages(method)
                page0 = pages[0]
                out = classify_reply(
                    page0,
                    sent_origin=ctx.cfg.origin is not None,
                    sent_token=bool(ctx.cfg.token),
                )
                if out.ok:
                    count = len(items_of(pages, LIST_ITEM_KEYS[method]) or [])
                    states.append(("ok", method, count, out.detail))
                elif out.rpc_code == wire.METHOD_NOT_FOUND:
                    states.append(("missing", method, 0, out.detail))
                else:
                    states.append(("error", method, 0, out.detail))
            ok_states = [s for s in states if s[0] == "ok"]
            if ok_states:
                state, items = "ok", sum(s[2] for s in ok_states)
                served = [s[1] for s in ok_states]
            elif all(s[0] == "missing" for s in states):
                state, items, served = "missing", 0, [s[1] for s in states]
            else:
                state, items, served = "error", 0, [s[1] for s in states if s[0] == "error"]
            method_text = ", ".join(served) if served else ", ".join(methods)
            subject = f"capabilities.{prim} ↔ {method_text}"
            detail = next((s[3] for s in states if s[0] == "error"), "")
            ev: dict[str, Any] = {"declared": declared, "list_state": state, "items": items}
            if declared and state == "ok":
                f.info(
                    "discover.capability_consistent",
                    f"capabilities.{prim} declared and {method_text} serves {items} item(s)",
                    subject=subject,
                    evidence=ev,
                )
            elif declared and state == "missing":
                f.must(
                    "discover.capability_declared_but_unimplemented",
                    f"capabilities.{prim} is declared but {method_text} answers -32601; a "
                    "declared capability MUST answer its list method",
                    subject=subject,
                    spec_url=_capability_url(prim),
                    evidence=ev,
                )
                summary = summary or (
                    f"Capabilities inconsistent: capabilities.{prim} declared but {method_text} "
                    "answers -32601"
                )
            elif declared:
                f.should(
                    "discover.capability_unverified",
                    f"capabilities.{prim} is declared but {method_text} could not be verified "
                    f"({detail})",
                    subject=subject,
                    spec_url=_capability_url(prim),
                    evidence={**ev, "detail": detail},
                )
            elif state == "ok" and items > 0:
                f.must(
                    "discover.capability_undeclared",
                    f"{method_text} serves {items} item(s) but capabilities.{prim} is not declared; "
                    "servers that support a primitive MUST declare its capability",
                    subject=subject,
                    spec_url=_capability_url(prim),
                    evidence=ev,
                )
                summary = summary or (
                    f"Capabilities inconsistent: {method_text} works but capabilities.{prim} "
                    "undeclared"
                )
            elif state == "ok":
                # P:43 obliges only servers that *support* the primitive: an empty list without
                # the capability violates nothing, so this stays mcpcheck's opinion
                f.best_practice(
                    "discover.capability_undeclared_empty",
                    f"{method_text} serves an empty list but capabilities.{prim} is undeclared; "
                    "declare the capability or answer -32601",
                    subject=subject,
                    spec_url=_capability_url(prim),
                    evidence=ev,
                )
            else:
                f.info(
                    "discover.capability_consistent",
                    f"capabilities.{prim} undeclared and {method_text} "
                    + ("answers -32601" if state == "missing" else f"not evaluated ({detail})"),
                    subject=subject,
                    evidence={**ev, "detail": detail},
                )
        return summary

    @staticmethod
    def _compose(ctx: CheckContext, result: Mapping[str, Any]) -> None:
        """Step 7: fill the report fields the handshake did not already set."""
        if ctx.protocol_version is None:
            ctx.protocol_version = wire.PROTOCOL_VERSION
        info = server_info_of(result)
        if ctx.server_info is None and info is not None:
            ctx.server_info = dict(info)
        versions = supported_versions_of(result)
        if ctx.server_supported_versions is None and versions is not None:
            ctx.server_supported_versions = versions

    @staticmethod
    def _details(f: Findings, reply: RawReply, result: Mapping[str, Any] | None) -> None:
        f.details(
            f"server/discover: HTTP {reply.status} {reply.media_type or '(no content-type)'} "
            f"in {reply.elapsed_ms} ms."
        )
        if result is None:
            return
        f.details(f"Result keys: {', '.join(sorted(result))}.")
        versions = supported_versions_of(result)
        if versions is not None:
            f.details(f"supportedVersions: {', '.join(versions) or '(empty)'}.")
        caps = result.get("capabilities")
        if isinstance(caps, dict):
            f.details(f"capabilities: {', '.join(sorted(caps)) or '(none)'}.")
        instructions = result.get("instructions")
        if isinstance(instructions, str):
            f.details(f"instructions: {len(instructions)} characters.")
        if reply.is_sse and reply.stream_terminated is None:
            f.details("SSE stream termination not observed (reply cached without an EOF wait).")


def _capability_url(prim: str) -> str:
    return {
        "tools": su.SPEC_TOOLS_CAPABILITIES,
        "prompts": su.SPEC_PROMPTS_CAPABILITIES,
        "resources": su.SPEC_RESOURCES_CAPABILITIES,
    }[prim]


__all__ = [
    "KNOWN_CAPABILITIES",
    "KNOWN_RESULT_KEYS",
    "DiscoverCheck",
    "Item",
    "describe",
    "error_code_range_item",
    "fields_summary",
    "primary_reply",
    "server_info_of",
    "short",
    "supported_from_error_data",
    "supported_versions_of",
    "validate_discover_result",
]
