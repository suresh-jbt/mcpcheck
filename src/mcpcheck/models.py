"""Report data model (pydantic v2) and JSON Schema generation (PLAN.md §6.11, §8.7).

``schema_version`` 1. Additive changes keep the version; removals, renames or
type changes bump it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, computed_field

SCHEMA_VERSION: Final = 1
SPEC_REVISION: Final = "2026-07-28"
SCHEMA_ID = "https://raw.githubusercontent.com/suresh-jbt/mcpcheck/main/schema/mcpcheck-report.v1.schema.json"
CODE_PATTERN = r"^[a-z]+\.[a-z0-9_]+$"
TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
DATE_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"


class Status(str, Enum):
    """Outcome of one check (a ``(str, Enum)`` pair for Python 3.10 compatibility)."""

    PASS = "pass"  # noqa: S105 — not a password
    WARN = "warn"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class Level(str, Enum):
    """Normative weight of a finding, derived from the RFC keyword of the quoted clause."""

    MUST = "must"
    SHOULD = "should"
    BEST_PRACTICE = "best-practice"


class CheckName(str, Enum):
    """The eight checks in canonical order (closed enum within schema_version 1)."""

    HANDSHAKE = "handshake"
    SESSION = "session"
    DISCOVER = "discover"
    PROTOCOL = "protocol"
    SCHEMAS = "schemas"
    ERRORS = "errors"
    CACHE = "cache"
    ORIGIN = "origin"


class Era(str, Enum):
    """Which protocol eras the server was observed to serve."""

    MODERN_ONLY = "modern-only"
    DUAL_ERA = "dual-era"
    LEGACY_ONLY = "legacy-only"
    UNKNOWN = "unknown"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Finding(_Model):
    """One observation: a spec clause, where it was violated and the evidence."""

    code: str = Field(
        pattern=CODE_PATTERN, description="<check>.<snake_slug>, registered in checks/codes.py"
    )
    level: Level
    subject: str = Field(
        description="Tool name, RPC method, header, 'tools/list page 2', probe id …"
    )
    message: str = Field(max_length=500, description="One sanitized sentence")
    spec_url: str | None = Field(default=None, description="Absent only for best-practice findings")
    evidence: dict[str, Any] | None = Field(
        default=None, description="got/expected/http_status/rpc_code/probe/request/path/page/hint"
    )
    tool: str | None = Field(
        default=None, description="Handoff compat: tool name when subject is a tool"
    )

    @computed_field(description="Handoff compatibility: same text as message.")  # type: ignore[prop-decorator]
    @property
    def issue(self) -> str:
        """Same text as ``message`` (handoff compatibility)."""
        return self.message


class CheckError(_Model):
    """Why a check ended in status ``error``."""

    type: str
    message: str


class CheckResult(_Model):
    """One row of the report."""

    name: CheckName
    title: str
    status: Status
    summary: str = Field(max_length=100)
    details: str = ""
    spec_url: str
    failures: list[Finding] = Field(default_factory=list)
    warnings: list[Finding] = Field(default_factory=list)
    info: list[Finding] = Field(default_factory=list)
    duration_ms: int = Field(ge=0)
    requests: int = Field(default=0, ge=0)
    error: CheckError | None = None

    _strict: bool = PrivateAttr(default=False)

    def with_strict(self, strict: bool) -> CheckResult:
        """Record whether ``--strict`` was active (affects ``passed``); returns self."""
        self._strict = strict
        return self

    @computed_field(description="Highest level among failures and warnings; null when none.")  # type: ignore[prop-decorator]
    @property
    def severity(self) -> Level | None:
        """Highest level among ``failures`` + ``warnings``."""
        order = {Level.MUST: 0, Level.SHOULD: 1, Level.BEST_PRACTICE: 2}
        levels = [f.level for f in (*self.failures, *self.warnings)]
        return min(levels, key=lambda lv: order[lv]) if levels else None

    @computed_field(description="true iff this check did not raise the exit code.")  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        """Whether this check contributed nothing to the exit code."""
        return exit_code_for(self.status, strict=self._strict) == 0


class Summary(_Model):
    """Counts over the selected checks and the process exit code."""

    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    warned: int = Field(ge=0)
    failed: int = Field(ge=0)
    inconclusive: int = Field(ge=0)
    errored: int = Field(ge=0)
    skipped: list[CheckName] = Field(default_factory=list, json_schema_extra={"uniqueItems": True})
    exit_code: Literal[0, 1, 2]


class Request(_Model):
    """How mcpcheck talked to the server (never the secret values themselves)."""

    origin: str | None
    auth: Literal["none", "bearer"]
    extra_headers: list[str] = Field(default_factory=list, description="Header names only.")
    tls_verify: bool


class Options(_Model):
    """Behavioural flags of the run."""

    strict: bool
    probe: Literal["safe", "all", "none"]
    max_tools: int = Field(ge=1)
    timeout: float = Field(gt=0)
    legacy_probes: bool


class Report(_Model):
    """Machine-readable result of one mcpcheck run."""

    schema_version: Literal[1] = SCHEMA_VERSION
    mcpcheck_version: str
    sdk_version: str = Field(description="importlib.metadata.version('mcp')")
    python_version: str
    spec_revision: Literal["2026-07-28"] = SPEC_REVISION
    server: str = Field(description="SERVER_URL as given (handoff shape); userinfo never present.")
    request: Request
    options: Options
    protocol_version: str | None = Field(
        pattern=DATE_PATTERN,
        description="The version mcpcheck sent and the server served; null if no modern request succeeded.",
    )
    server_supported_versions: list[str] | None = Field(
        description="From discover.supportedVersions or -32022 data.supported."
    )
    server_info: dict[str, Any] | None = Field(
        description="Copy of _meta['io.modelcontextprotocol/serverInfo'] (self-reported, unverified)."
    )
    era: Era
    timestamp: str = Field(pattern=TIMESTAMP_PATTERN)
    duration_ms: int = Field(ge=0)
    checks: list[CheckResult] = Field(
        description="One entry per selected check in canonical order; deselected checks are listed in summary.skipped."
    )
    summary: Summary


# --------------------------------------------------------------------------- exit codes (§6.10)

_EXIT_DEFAULT = {
    Status.PASS: 0,
    Status.WARN: 0,
    Status.INCONCLUSIVE: 0,
    Status.FAIL: 1,
    Status.ERROR: 2,
}
_EXIT_STRICT = {
    Status.PASS: 0,
    Status.WARN: 1,
    Status.INCONCLUSIVE: 1,
    Status.FAIL: 1,
    Status.ERROR: 2,
}


def exit_code_for(status: Status, *, strict: bool) -> int:
    """Exit-code contribution of one status (§6.10 table)."""
    return (_EXIT_STRICT if strict else _EXIT_DEFAULT)[status]


def aggregate_exit_code(statuses: list[Status], *, strict: bool) -> Literal[0, 1, 2]:
    """``max`` over the selected checks (2 > 1 > 0); an empty run exits 0."""
    code = max((exit_code_for(s, strict=strict) for s in statuses), default=0)
    if code == 0:
        return 0
    return 1 if code == 1 else 2


# --------------------------------------------------------------------------- JSON Schema (§8.7)

_CHECK_CONDITIONALS: list[dict[str, Any]] = [
    {
        "if": {"properties": {"status": {"const": "fail"}}},
        "then": {"properties": {"failures": {"minItems": 1}, "passed": {"const": False}}},
    },
    {
        "if": {"properties": {"status": {"const": "pass"}}},
        "then": {
            "properties": {
                "failures": {"maxItems": 0},
                "warnings": {"maxItems": 0},
                "passed": {"const": True},
                "severity": {"type": "null"},
            }
        },
    },
    {
        "if": {"properties": {"status": {"const": "error"}}},
        "then": {"properties": {"error": {"type": "object"}, "passed": {"const": False}}},
    },
]


def report_json_schema() -> dict[str, Any]:
    """The shipped JSON Schema: pydantic's serialization schema plus ``$schema``/``$id`` and §8.7 rules."""
    generated = Report.model_json_schema(mode="serialization")
    schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "mcpcheck report",
        "description": (
            "Machine-readable result of one mcpcheck run (schema_version 1). "
            "Additive changes keep schema_version; removals or type changes bump it."
        ),
    }
    generated.pop("title", None)
    generated.pop("description", None)
    properties: dict[str, Any] = {"$schema": {"type": "string", "format": "uri"}}
    properties.update(generated.pop("properties"))
    generated["properties"] = properties
    # defaulted fields are always emitted by the writer, so consumers may rely on them
    required = list(generated.get("required", []))
    for always in ("schema_version", "spec_revision"):
        if always not in required:
            required.append(always)
    generated["required"] = [k for k in properties if k in required]
    defs = generated.get("$defs", {})
    check_def = defs.get("CheckResult")
    if check_def is not None:
        check_def["allOf"] = _CHECK_CONDITIONALS
    # key order: $schema, $id, title, description, then the generated keys
    schema.update(generated)
    return schema


__all__ = [
    "CODE_PATTERN",
    "SCHEMA_ID",
    "SCHEMA_VERSION",
    "SPEC_REVISION",
    "CheckError",
    "CheckName",
    "CheckResult",
    "Era",
    "Finding",
    "Level",
    "Options",
    "Report",
    "Request",
    "Status",
    "Summary",
    "aggregate_exit_code",
    "exit_code_for",
    "report_json_schema",
]
