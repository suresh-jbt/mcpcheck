"""The ``cache`` check (PLAN.md §7.6): ``ttlMs``/``cacheScope`` on every list page.

Every page of ``tools/list``, ``prompts/list``, ``resources/list`` and
``resources/templates/list`` must carry a result-level ``ttlMs`` (JSON integer
``>= 0``) and ``cacheScope`` (exactly ``"public"`` or ``"private"``), the scope must
not change across the pages of one list request, and a ``"public"`` list that looks
user-specific earns a best-practice warning. The pure validator
:func:`validate_cache_hints` is shared with the ``discover`` check (``code_prefix=
"discover"``), which owns the hints of ``server/discover``. ``resources/read`` is
never probed (read-only policy) and this is disclosed as an ``info`` finding.

The check sends no requests of its own: pages come from the shared
:class:`~mcpcheck.checks.base.CheckContext` cache (``--check cache`` alone makes the
context fetch the four list methods plus ``server/discover`` on ``main``).
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, ClassVar

import pydantic

from mcpcheck import spec_urls, wire
from mcpcheck.checks import codes
from mcpcheck.checks.base import LIST_ITEM_KEYS, Check
from mcpcheck.models import Finding
from mcpcheck.wire import OutcomeKind, classify_reply

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcpcheck.checks.base import CheckContext, Findings
    from mcpcheck.client import RawReply

ITEM_KEY: dict[str, str] = dict(LIST_ITEM_KEYS)
"""``method -> result key`` of the four list methods (PLAN.md §7.6)."""
LIST_METHODS: tuple[str, ...] = tuple(ITEM_KEY)
CAPABILITY_KEY: dict[str, str] = {
    "tools/list": "tools",
    "prompts/list": "prompts",
    "resources/list": "resources",
    "resources/templates/list": "resources",
}
USER_TOKENS: frozenset[str] = frozenset({"my", "me", "user", "account", "profile", "self"})
"""Name tokens that make a ``"public"`` list look user-specific (CA:107-110, CA:175-181)."""
VALID_SCOPES: tuple[str, ...] = ("public", "private")
NAME_FIELDS: tuple[str, ...] = ("name", "uri", "uriTemplate")
_CAMEL_LOWER_UPPER = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_ACRONYM = re.compile(r"([A-Z]+)([A-Z][a-z])")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

# outcomes that are the environment's fault: the whole check becomes inconclusive
_ENVIRONMENT_KINDS: frozenset[OutcomeKind] = frozenset(
    {
        OutcomeKind.AUTH_REQUIRED,
        OutcomeKind.FORBIDDEN,
        OutcomeKind.SERVER_ERROR,
        OutcomeKind.REDIRECTED,
        OutcomeKind.TRANSPORT,
        OutcomeKind.NOT_MCP_ENDPOINT,
        OutcomeKind.UNREACHABLE,
    }
)

# short labels used in the one-line summary
_LABELS: dict[str, str] = {
    "ttl_missing": "ttlMs missing",
    "ttl_not_integer": "ttlMs not a number",
    "ttl_not_integral": "ttlMs not a whole number",
    "ttl_integral_float": "ttlMs is an integral float",
    "ttl_negative": "ttlMs negative",
    "ttl_implausible": "ttlMs exceeds 30 days",
    "scope_missing": "cacheScope missing",
    "scope_invalid": "cacheScope invalid",
    "scope_inconsistent_pages": "cacheScope differs across pages",
    "public_user_specific_names": "cacheScope public but item names look user-specific",
    "cursor_not_string": "nextCursor not a string",
    "cursor_loop": "nextCursor repeats (pagination loop)",
    "unevaluated": "not evaluated",
    "item_malformed": "item does not match its schema",
}


# --------------------------------------------------------------------------- pure helpers


def tokenise(name: str) -> list[str]:
    """Split ``name`` on camelCase boundaries and non-alphanumerics; lowercase tokens."""
    spaced = _CAMEL_ACRONYM.sub(r"\1 \2", _CAMEL_LOWER_UPPER.sub(r"\1 \2", name))
    return [t.lower() for t in _NON_ALNUM.split(spaced) if t]


def looks_user_specific(name: str) -> bool:
    """Whether an item name/uri tokenises to one of :data:`USER_TOKENS`.

    ``"list_my_orders"``, ``"listMyOrders"`` and ``"user_profile"`` match;
    ``"resumeUpload"`` and ``"users"`` do not (exact tokens only).
    """
    return any(token in USER_TOKENS for token in tokenise(name))


def item_names(result: Mapping[str, Any], key: str | None) -> list[str]:
    """``name``/``uri``/``uriTemplate`` strings of the object items under ``result[key]``."""
    if key is None:
        return []
    items = result.get(key)
    if not isinstance(items, list):
        return []
    names: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        names.extend(v for field in NAME_FIELDS if isinstance(v := item.get(field), str))
    return names


def is_legacy_shaped(result: Mapping[str, Any]) -> bool:
    """A pre-2026 list result: no ``resultType``, no hints and no ``_meta`` at all."""
    return not any(k in result for k in ("resultType", "ttlMs", "cacheScope", "_meta"))


def _json_value(value: Any) -> Any:
    """A JSON-safe copy of a hint value for ``evidence.got``."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    return repr(value)


def _describe(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return f'"{value}"'
    if isinstance(value, (int, float)):
        return repr(value)
    return type(value).__name__


def validate_cache_hints(
    method: str,
    page: int,
    result: Mapping[str, Any],
    *,
    token_supplied: bool,
    code_prefix: str = "cache",
) -> tuple[str | None, list[Finding]]:
    """Validate the result-level cache hints of one page (pure; shared with ``discover``).

    Args:
        method: The JSON-RPC method that produced ``result``.
        page: 0-based page index (``subject`` becomes ``"<method> page <n>"`` for ``n > 0``).
        result: The raw ``result`` object.
        token_supplied: Whether ``--token`` was given (gates the user-specific heuristic).
        code_prefix: ``"cache"`` or ``"discover"`` so codes keep the ``<check>.<slug>`` grammar.

    Returns:
        ``(resultType, findings)``. ``resultType`` is the raw ``result.resultType`` string
        (``None`` when absent, which clients MUST treat as ``"complete"``, B:87). For
        ``"input_required"`` no findings are produced (CA:25-27): the must about an
        interim result on a list method belongs to the caller. Each
        :class:`~mcpcheck.models.Finding` carries ``evidence = {method, page, got,
        expected, ...}``; feed them to a ``Findings`` builder with :func:`apply_findings`.
    """
    subject = method if page == 0 else f"{method} page {page}"
    result_type = result.get("resultType")
    if result_type == "input_required":
        return "input_required", []
    legacy = is_legacy_shaped(result)
    suffix = " (pre-2026 result shape; see handshake)" if legacy else ""
    findings: list[Finding] = []

    def add(slug: str, message: str, spec_url: str, **evidence: Any) -> None:
        code = f"{code_prefix}.{slug}"
        info = codes.lookup(code)
        findings.append(
            Finding(
                code=code,
                level=info.level,
                subject=subject,
                message=message,
                spec_url=spec_url,
                evidence={"method": method, "page": page, **evidence},
            )
        )

    # ttlMs — CA:45 (integer), CA:62 (>= 0), SCH (number → integer interop)
    if "ttlMs" not in result:
        add(
            "ttl_missing",
            f"result has no ttlMs{suffix}",
            spec_urls.SPEC_CACHEABLE_RESULTS,
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
                "ttl_not_integer",
                f"ttlMs is {_describe(ttl)}, not a JSON number",
                spec_urls.SPEC_TTL,
                got=_json_value(ttl),
                expected="JSON integer (milliseconds)",
            )
        elif isinstance(ttl, float):
            if ttl.is_integer():
                add(
                    "ttl_integral_float",
                    f"ttlMs is the float {ttl!r}; send the integer {int(ttl)}",
                    spec_urls.SPEC_TTL,
                    got=ttl,
                    expected=int(ttl),
                )
            else:
                # CA:45 defines ttlMs as an integer in prose; the schema allows any number
                add(
                    "ttl_not_integral",
                    f"ttlMs is {ttl!r}, not a whole number of milliseconds",
                    spec_urls.SPEC_TTL,
                    got=ttl,
                    expected="JSON integer (milliseconds)",
                )
            if ttl < 0:
                add(
                    "ttl_negative",
                    f"ttlMs is {ttl!r}; servers MUST provide a value >= 0",
                    spec_urls.SPEC_TTL,
                    got=ttl,
                    expected=">= 0",
                )
            elif ttl > wire.TTL_SANITY_MS:
                add(
                    "ttl_implausible",
                    f"ttlMs {ttl!r} exceeds 30 days ({wire.TTL_SANITY_MS} ms)",
                    spec_urls.SPEC_TTL,
                    got=ttl,
                    expected=f"<= {wire.TTL_SANITY_MS}",
                )
        elif ttl < 0:
            add(
                "ttl_negative",
                f"ttlMs is {ttl}; servers MUST provide a value >= 0",
                spec_urls.SPEC_TTL,
                got=ttl,
                expected=">= 0",
            )
        elif ttl > wire.TTL_SANITY_MS:
            add(
                "ttl_implausible",
                f"ttlMs {ttl} exceeds 30 days ({wire.TTL_SANITY_MS} ms)",
                spec_urls.SPEC_TTL,
                got=ttl,
                expected=f"<= {wire.TTL_SANITY_MS}",
            )

    # cacheScope — CA:46, CA:100-110
    if "cacheScope" not in result:
        add(
            "scope_missing",
            f"result has no cacheScope{suffix}",
            spec_urls.SPEC_CACHE_SCOPE,
            got=None,
            expected='"public" or "private"',
        )
    else:
        scope = result["cacheScope"]
        if scope not in VALID_SCOPES or not isinstance(scope, str):
            add(
                "scope_invalid",
                f'cacheScope is {_describe(scope)}; only "public" or "private" are defined',
                spec_urls.SPEC_CACHE_SCOPE,
                got=_json_value(scope),
                expected='"public" or "private"',
            )
        elif scope == "public" and token_supplied:
            names = item_names(result, ITEM_KEY.get(method))
            matched = [n for n in names if looks_user_specific(n)]
            if matched:
                shown = ", ".join(matched[: wire.MAX_ITEMS_SHOWN])
                more = (
                    f" (+{len(matched) - wire.MAX_ITEMS_SHOWN} more)"
                    if len(matched) > wire.MAX_ITEMS_SHOWN
                    else ""
                )
                add(
                    "public_user_specific_names",
                    f'cacheScope is "public" on an authenticated request but item names '
                    f"look user-specific: {shown}{more}",
                    spec_urls.SPEC_CACHE_SECURITY,
                    got="public",
                    expected='"private" when the list varies per user',
                    names=matched[: wire.MAX_ITEMS_SHOWN],
                )
    return result_type if isinstance(result_type, str) else None, findings


def apply_findings(f: Findings, findings: list[Finding]) -> None:
    """Record validator findings on ``f`` at the severity each code is registered with."""
    for finding in findings:
        severity = codes.lookup(finding.code).severity
        kwargs: dict[str, Any] = {
            "subject": finding.subject,
            "spec_url": finding.spec_url,
            "evidence": finding.evidence,
        }
        if severity == "must":
            f.must(finding.code, finding.message, **kwargs)
        elif severity == "should":
            f.should(finding.code, finding.message, **kwargs)
        elif severity == "best-practice":
            f.best_practice(finding.code, finding.message, **kwargs)
        else:
            f.info(finding.code, finding.message, **kwargs)


def sdk_witness(method: str, result: Mapping[str, Any]) -> list[str]:
    """Error locations from the SDK's ``validate_server_result`` (details only, never a verdict)."""
    return sorted(
        {".".join(str(p) for p in e["loc"]) or "<root>" for e in sdk_errors(method, result)}
    )


def sdk_errors(method: str, result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The SDK validator's error records for a list result (``[]`` when it accepts or cannot judge)."""
    try:
        from mcp_types.methods import validate_server_result
    except ImportError:  # pragma: no cover - the SDK is a hard dependency
        return []
    try:
        validate_server_result(method, wire.PROTOCOL_VERSION, result)
    except pydantic.ValidationError as exc:
        return [dict(e) for e in exc.errors()]
    except (KeyError, ValueError, TypeError, RecursionError):  # unknown surface / hostile input
        return []
    return []


ITEM_SPEC_URL: dict[str, str] = {
    "prompts/list": spec_urls.SPEC_PROMPTS_CAPABILITIES,
    "resources/list": spec_urls.SPEC_RESOURCES_CAPABILITIES,
    "resources/templates/list": spec_urls.SPEC_RESOURCES_CAPABILITIES,
}
"""List methods whose item shape is judged here (``tools/list`` items belong to ``schemas``)."""


def item_shape_findings(
    method: str, page: int, result: Mapping[str, Any], errors: list[dict[str, Any]]
) -> list[Finding]:
    """Turn SDK errors located inside a prompt/resource/template item into ``cache.item_malformed``.

    ``Prompt.name`` and ``Resource.uri`` are schema-typed required fields (SCH:2617-2624,
    SCH:2848) and schema-typed fields count as MUST; other locations (cache hints,
    ``nextCursor``, ``_meta``) are owned by other rules and only reach the details.
    """
    key = ITEM_KEY.get(method)
    if key is None or method not in ITEM_SPEC_URL:
        return []
    subject = method if page == 0 else f"{method} page {page}"
    items = result.get(key)
    findings: list[Finding] = []
    seen: set[tuple[Any, ...]] = set()
    for err in errors:
        loc = tuple(err.get("loc", ()))
        if len(loc) < 2 or loc[0] != key or not isinstance(loc[1], int) or loc in seen:
            continue
        seen.add(loc)
        index = loc[1]
        item = items[index] if isinstance(items, list) and index < len(items) else None
        ident = None
        if isinstance(item, dict):
            candidate = item.get("uri") if key != "prompts" else item.get("name")
            ident = candidate if isinstance(candidate, str) else None
        where = ".".join(str(p) for p in loc)
        label = f"{key[:-1] if key != 'resourceTemplates' else 'resource template'} #{index}"
        if ident is not None:
            label += f" ({ident[:60]!r})"
        findings.append(
            Finding(
                code="cache.item_malformed",
                level=codes.lookup("cache.item_malformed").level,
                subject=subject,
                message=f"{label} does not match the {key} item schema at {where}: {err.get('msg', '')}",
                spec_url=ITEM_SPEC_URL[method],
                evidence={
                    "method": method,
                    "page": page,
                    "loc": where,
                    "got": _json_value(err.get("input")),
                    "expected": err.get("type"),
                },
            )
        )
    return findings


def _with_method(detail: str, method: str) -> str:
    """``"Not evaluated: HTTP 401 (pass --token)"`` → ``"… HTTP 401 on tools/list (pass --token)"``."""
    head, sep, tail = detail.partition(" (")
    return f"{head} on {method}{sep}{tail}" if sep else f"{detail} on {method}"


def _supported_versions(reply: RawReply) -> list[str]:
    err = reply.rpc_error
    if err is None or not isinstance(err[2], dict):
        return []
    supported = err[2].get("supported")
    if not isinstance(supported, list):
        return []
    return [v for v in supported if isinstance(v, str)]


def _label(finding: Finding) -> str:
    slug = finding.code.split(".", 1)[1]
    return f"{finding.subject} {_LABELS.get(slug, slug.replace('_', ' '))}"


# --------------------------------------------------------------------------- the check


class CacheCheck(Check):
    """ttlMs/cacheScope present and valid on list results."""

    name: ClassVar[str] = "cache"
    title: ClassVar[str] = "ttlMs/cacheScope present and valid on list results"
    spec_url: ClassVar[str] = spec_urls.SPEC_CACHEABLE_RESULTS
    order: ClassVar[int] = 70

    async def run(self, ctx: CheckContext, f: Findings) -> None:
        """Validate every page of the four list methods from the shared page cache."""
        sent_origin = ctx.cfg.origin is not None
        sent_token = bool(ctx.cfg.token)
        evaluated: list[str] = []
        skipped: list[str] = []
        pages_total = 0
        ttl_lines: list[str] = []
        witness_lines: list[str] = []
        all_zero = True
        saw_ttl = False

        for method in LIST_METHODS:
            pages = await ctx.list_pages(method)
            first = pages[0]
            outcome = classify_reply(first, sent_origin=sent_origin, sent_token=sent_token)
            if outcome.kind is OutcomeKind.METHOD_NOT_FOUND:
                skipped.append(method)
                f.info(
                    "cache.method_not_implemented",
                    "answered -32601 (not implemented); cache hints not required",
                    subject=method,
                    evidence={"method": method, "http_status": first.status, "rpc_code": -32601},
                )
                continue
            if outcome.kind is OutcomeKind.MODERN_REJECTION and (
                outcome.rpc_code == wire.UNSUPPORTED_PROTOCOL_VERSION
            ):
                supported = _supported_versions(first)
                tail = f" (supports: {', '.join(supported)})" if supported else ""
                f.inconclusive(f"Not evaluated: server does not support 2026-07-28{tail}")
                return
            if outcome.kind in _ENVIRONMENT_KINDS:
                f.inconclusive(_with_method(outcome.detail, method))
                return
            if not outcome.ok:
                # -32020/-32021, non-modern JSON-RPC errors, 403 with a JSON-RPC body,
                # not-JSON 2xx, id mismatch: another check owns the must; here it is unevaluated
                f.should(
                    "cache.unevaluated",
                    f"could not be evaluated: {outcome.detail}",
                    subject=method,
                    evidence={
                        "method": method,
                        "page": 0,
                        "http_status": first.status,
                        "rpc_code": outcome.rpc_code,
                        "outcome": outcome.kind.value,
                    },
                )
                continue

            evaluated.append(method)
            scope0: str | None = None
            inconsistent_reported = False
            loop_reported = False
            cursors: list[str] = []
            ttls: list[Any] = []
            for index, page in enumerate(pages):
                subject = method if index == 0 else f"{method} page {index}"
                if index > 0:
                    page_outcome = classify_reply(
                        page, sent_origin=sent_origin, sent_token=sent_token
                    )
                    if not page_outcome.ok:
                        f.should(
                            "cache.unevaluated",
                            f"could not be evaluated: {page_outcome.detail}",
                            subject=subject,
                            evidence={
                                "method": method,
                                "page": index,
                                "http_status": page.status,
                                "rpc_code": page_outcome.rpc_code,
                                "outcome": page_outcome.kind.value,
                            },
                        )
                        break
                pages_total += 1
                result = page.result or {}
                result_type, findings = validate_cache_hints(
                    method, index, result, token_supplied=sent_token
                )
                if result_type == "input_required":
                    f.details(
                        f"{subject}: interim input_required result carries no cache hints "
                        "(CA:25-27); the list-method violation is reported by handshake/discover."
                    )
                    continue
                apply_findings(f, findings)
                errors = sdk_errors(method, result)
                item_findings = item_shape_findings(method, index, result, errors)
                apply_findings(f, item_findings)
                item_locs = {fi.evidence["loc"] for fi in item_findings if fi.evidence}
                locs = sorted(
                    {".".join(str(p) for p in e["loc"]) or "<root>" for e in errors} - item_locs
                )
                if locs:
                    witness_lines.append(f"{subject}: {', '.join(locs)}")

                ttl = result.get("ttlMs")
                ttls.append(ttl)
                if isinstance(ttl, (int, float)) and not isinstance(ttl, bool):
                    saw_ttl = True
                    if ttl != 0:
                        all_zero = False

                scope = result.get("cacheScope")
                if isinstance(scope, str) and scope in VALID_SCOPES:
                    if index == 0:
                        scope0 = scope
                    elif scope0 is not None and scope != scope0 and not inconsistent_reported:
                        inconsistent_reported = True
                        f.must(
                            "cache.scope_inconsistent_pages",
                            "cacheScope differs across pages of one list request",
                            subject=subject,
                            spec_url=spec_urls.SPEC_CACHE_PAGINATION,
                            evidence={
                                "method": method,
                                "page": index,
                                "got": {"page0": scope0, f"page{index}": scope},
                                "expected": "same cacheScope on every page",
                            },
                        )

                if "nextCursor" in result:
                    nxt = result["nextCursor"]
                    if nxt is None:
                        pass  # null ends pagination like an absent cursor (SDK accepts it)
                    elif not isinstance(nxt, str):
                        f.must(
                            "cache.cursor_not_string",
                            f"nextCursor is {_describe(nxt)}, not a string; pagination stopped",
                            subject=subject,
                            spec_url=spec_urls.SPEC_PAGINATION_GUIDELINES,
                            evidence={
                                "method": method,
                                "page": index,
                                "got": _json_value(nxt),
                                "expected": "opaque string",
                            },
                        )
                    elif nxt in cursors and not loop_reported:
                        loop_reported = True
                        f.should(
                            "cache.cursor_loop",
                            f"nextCursor {nxt!r} repeats; pagination stopped after {len(pages)} pages",
                            subject=subject,
                            spec_url=spec_urls.SPEC_PAGINATION_GUIDELINES,
                            evidence={
                                "method": method,
                                "page": index,
                                "got": nxt,
                                "expected": "a cursor that advances to a new page",
                            },
                        )
                    else:
                        cursors.append(nxt)
            stopped = ctx.walk_stopped(method)
            if stopped is not None:
                f.details(f"{method}: pagination stopped by mcpcheck after {stopped}.")
            if ttls:
                shown = ", ".join(_describe(t) for t in ttls[: wire.MAX_ITEMS_SHOWN])
                more = f", … ({len(ttls)} pages)" if len(ttls) > wire.MAX_ITEMS_SHOWN else ""
                ttl_lines.append(f"{method}: {shown}{more}")

        f.info(
            "cache.resources_read_not_probed",
            "cache hints not verified: mcpcheck never calls resources/read (read-only policy)",
            subject="resources/read",
        )

        if not evaluated and len(skipped) == len(LIST_METHODS):
            f.inconclusive("Skipped: no list method implemented (all four answered -32601)")
            return

        await self._details(ctx, f, skipped, ttl_lines, witness_lines, all_zero and saw_ttl)
        self._summary(f, evaluated, skipped, pages_total)

    @staticmethod
    async def _details(
        ctx: CheckContext,
        f: Findings,
        skipped: list[str],
        ttl_lines: list[str],
        witness_lines: list[str],
        all_zero: bool,
    ) -> None:
        caps = await ctx.capabilities()
        if caps is None:
            f.details("Capabilities unknown (server/discover result not available).")
        else:
            declared = [k for k in ("tools", "prompts", "resources") if k in caps]
            f.details(
                "Capabilities declared: " + (", ".join(declared) if declared else "none") + "."
            )
            undeclared = [m for m in skipped if CAPABILITY_KEY[m] in caps]
            if undeclared:
                f.details(
                    "Declared but answering -32601: "
                    + ", ".join(undeclared)
                    + " (reported by the discover check)."
                )
        if skipped:
            f.details("Not implemented (-32601, skipped): " + ", ".join(skipped) + ".")
        if ttl_lines:
            f.details("ttlMs per page — " + "; ".join(ttl_lines) + ".")
        if all_zero:
            f.details("All ttlMs are 0 (legal: results are immediately stale).")
        if witness_lines:
            f.details("SDK validate_server_result also rejects: " + "; ".join(witness_lines) + ".")
        f.details(f"Era: {ctx.era.value}. resources/read hints not verified (read-only policy).")

    @staticmethod
    def _summary(f: Findings, evaluated: list[str], skipped: list[str], pages_total: int) -> None:
        def plural(n: int, word: str) -> str:
            return f"{n} {word}{'' if n == 1 else 's'}"

        if f.failures:
            labels = [_label(x) for x in f.failures]
            shown = "; ".join(labels[:2])
            more = f" (+{len(labels) - 2} more)" if len(labels) > 2 else ""
            noun = (
                "list violation"
                if any(x.code == "cache.item_malformed" for x in f.failures)
                else "cache-hint violation"
            )
            f.summary(f"{plural(len(labels), noun)}: {shown}{more}")
            return
        if f.warnings:
            labels = [_label(x) for x in f.warnings]
            f.summary(f"Cache hints valid; {plural(len(labels), 'warning')}: {labels[0]}")
            return
        tail = f"; {plural(len(skipped), 'method')} not implemented" if skipped else ""
        f.summary(
            f"Cache hints valid on {plural(len(evaluated), 'list method')} "
            f"({plural(pages_total, 'page')}){tail}"
        )


__all__ = [
    "CAPABILITY_KEY",
    "ITEM_KEY",
    "LIST_METHODS",
    "USER_TOKENS",
    "CacheCheck",
    "apply_findings",
    "is_legacy_shaped",
    "item_names",
    "item_shape_findings",
    "looks_user_specific",
    "sdk_errors",
    "sdk_witness",
    "tokenise",
    "validate_cache_hints",
]
