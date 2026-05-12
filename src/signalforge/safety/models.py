"""Typed models for the PII safety layer (US-004).

Defines the read-back-stable shapes consumed by every other safety-layer
module: :class:`SamplingMode`, :class:`RedactionRecord`, :class:`AuditEvent`,
and :class:`LLMRequest`. The companion :class:`SafetyPolicy` lands separately
in US-005 (it carries config-validation logic and is policy-shaped, not
data-shaped).

Design commitments operationalised here:

* **DEC-014** — :class:`AuditEvent` carries every field needed to reproduce a
  draft run: ``signalforge_version``, ``policy_hash``, ``audit_schema_version``,
  and ``policy_flags``. Audits without these are unreproducible by definition.
* **DEC-015** — Every model uses ``extra="ignore"`` so audit logs written by
  newer SignalForge versions read back cleanly on older ones. The matching
  ``extra="forbid"`` drift detector lives in tests (US-011), per the
  ``manifest-readers.md`` rule.
* **DEC-022** — Sequences are :class:`tuple` rather than :class:`list`. The
  request object is handed to the LLM-drafting layer (issue #5) *after* the
  audit event has been written; making the sequences immutable closes the
  window where a mutation could desync the request from its audit record.
* **DEC-024** — :class:`SamplingMode` uses :class:`enum.StrEnum`. Originally
  ``str + Enum`` to preserve a 3.10 floor; that floor moved to 3.11 in issue
  #46 so the simpler :class:`StrEnum` form replaces the mixin. Type-safe
  ``is``-comparison, string-equality (``SamplingMode.SCHEMA_ONLY == "schema-only"``),
  and YAML / JSON round-trip behaviour are unchanged. ``str(SamplingMode.X)``
  now returns the bare value (``"schema-only"``) instead of the dotted form
  (``"SamplingMode.SCHEMA_ONLY"``); no production code paths exercise
  ``str()`` on the enum (Pydantic ``model_dump_json`` uses ``.value`` and
  ``_LOGGER`` calls go through ``json.dumps``), so the change is invisible
  to audit JSONL consumers.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from signalforge.warehouse.models import ColumnStats

_BASE_MODEL_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class SamplingMode(StrEnum):
    """Sampling-mode enum for the safety layer (DEC-024).

    :class:`enum.StrEnum` member: ``SamplingMode.SCHEMA_ONLY == "schema-only"``
    and instances are :class:`str` subclasses, so YAML / JSON round-trip
    cleanly. See the module docstring for the 3.11-floor history (issue #46).
    """

    SCHEMA_ONLY = "schema-only"
    AGGREGATE_ONLY = "aggregate-only"
    SAMPLE = "sample"


RedactionReason = Literal[
    "column_meta_optout",
    "model_meta_optout",
    "tag_pii_column",
    "tag_pii_model",
    "meta_contains_pii_column",
    "meta_contains_pii_model",
    "pattern_match",
    "draft_skip_column_meta",
    "draft_skip_model_meta",
]

DRAFT_SKIP_REASONS: frozenset[RedactionReason] = frozenset(
    {"draft_skip_column_meta", "draft_skip_model_meta"}
)
"""Reasons that mean "exclude the column from the LLM prompt entirely",
distinct from the seven PII reasons that mean "send a hashed placeholder
in place of the real column name". Columns with a draft-skip reason
never appear in :attr:`LLMRequest.schema` /
:attr:`LLMRequest.columns_sent` / :attr:`LLMRequest.aggregates` /
:attr:`LLMRequest.sampled_rows`; their :class:`RedactionRecord` rides on
the audit event so the operator-chosen omission is durably recorded.
"""


class RedactionRecord(BaseModel):
    """One applied column redaction.

    Emitted only for columns the redactor actually drops or masks. Columns
    that pass through unchanged do not produce a :class:`RedactionRecord`,
    so audit/request payloads capture the redactions that were applied
    rather than the full set of columns considered. The ``reason`` field is
    a closed :data:`RedactionReason` literal so audit-log consumers can
    pattern-match exhaustively on the seven possible signals.
    """

    model_config = _BASE_MODEL_CONFIG

    column_name: str
    hashed_name: str
    redacted: bool
    reason: RedactionReason


class AuditEvent(BaseModel):
    """One row in the JSONL audit log (DEC-014).

    Carries every field needed to reproduce a draft run from the audit log
    alone: SignalForge version, the policy hash that gated the request, the
    audit schema version (so future readers can branch on shape changes),
    and any policy flags that were active. ``row_count`` is ``None`` when
    the run was schema-only.
    """

    model_config = _BASE_MODEL_CONFIG

    timestamp: datetime
    model_unique_id: str
    mode: SamplingMode
    columns_sent: tuple[str, ...]
    redactions: tuple[RedactionRecord, ...]
    row_count: int | None = None
    signalforge_version: str
    policy_hash: str
    audit_schema_version: int = 1
    policy_flags: tuple[str, ...] = ()


class LLMRequest(BaseModel):
    """The request payload handed to issue #5's LLM-drafting layer.

    Construct only via :func:`signalforge.safety.request.build_llm_request` —
    direct construction bypasses the audit log and breaks the reproducibility
    contract documented in DEC-014. The AST scan in US-011 enforces this
    convention at lint time; this docstring is the human-readable companion.

    Sequences are :class:`tuple` (DEC-022) so the request cannot be mutated
    after the audit event has been written.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="ignore",
        populate_by_name=True,
        arbitrary_types_allowed=True,
    )

    model_unique_id: str
    mode: SamplingMode
    columns_sent: tuple[str, ...]
    redactions: tuple[RedactionRecord, ...]
    sampled_rows: tuple[dict[str, Any], ...] | None = None
    # Tuple-of-tuples (not dict) so frozen=True actually prevents mutation:
    # downstream consumers cannot do `request.aggregates["x"] = ...` post-audit
    # (DEC-022 transitive immutability). Convention: list order matches
    # ``columns_sent``; redacted columns appear with their hashed name as key
    # and ``None`` as value.
    aggregates: tuple[tuple[str, ColumnStats | None], ...] | None = None
    # ``schema`` overrides Pydantic v1's deprecated :meth:`BaseModel.schema`
    # method on this subclass. The override is intentional — the field name is
    # part of the documented LLMRequest contract — so pyright's structural
    # complaint is silenced here rather than renamed.
    schema: tuple[tuple[str, str], ...]  # pyright: ignore[reportIncompatibleMethodOverride]


__all__ = [
    "SamplingMode",
    "RedactionReason",
    "DRAFT_SKIP_REASONS",
    "RedactionRecord",
    "AuditEvent",
    "LLMRequest",
]
