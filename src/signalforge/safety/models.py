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

import warnings
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_serializer, model_validator

from signalforge._common.timestamp import iso8601_z
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

    v4 shape (issue #185):

    * The v3 ``redactions: tuple[RedactionRecord, ...]`` field is dropped.
      Redactions ride as a **symbol-table-by-reason** dict
      (``redactions_by_reason: dict[RedactionReason, tuple[str, ...]]`` —
      keyed by reason, values are hashed names) plus a sibling
      ``column_name_map: dict[hashed_name, real_column_name]`` that preserves
      the (real → hashed) reviewer mapback. Compresses ~75% on a 170-col
      schema-only event vs. the v3 record-per-column layout.
    * Three new chunk-correlation fields support multi-line splitting of
      events that don't fit under the POSIX-atomic-append cap:
      ``audit_id`` (correlation key shared across chunks of one logical
      event), ``chunk_index`` (0-indexed position; 0 = header),
      ``chunk_count`` (total chunks for the logical event; ≥ 2 when
      chunked).
    * Existing metadata fields (``model_unique_id``, ``mode``,
      ``columns_sent``, ``row_count``, ``signalforge_version``,
      ``policy_hash``, ``policy_flags``, ``timestamp``) are now
      ``| None = None`` so chunk-continuation rows can omit them and
      carry only the redaction slice.

    Shape rules enforced by ``@model_validator(mode="after")``:

    1. **Non-chunked** — ``audit_id`` AND ``chunk_index`` AND
       ``chunk_count`` are all ``None``. All metadata required (non-None).
       Both ``redactions_by_reason`` and ``column_name_map`` present
       (may be empty dicts).
    2. **Chunk header** — ``audit_id`` set, ``chunk_index == 0``,
       ``chunk_count >= 2``. All metadata required (non-None). Both
       ``redactions_by_reason`` and ``column_name_map`` MUST be empty
       dicts (the redaction body rides on continuation rows).
    3. **Chunk continuation** — ``audit_id`` set,
       ``1 <= chunk_index < chunk_count``, ``chunk_count >= 2``.
       Every metadata field MUST be ``None``. ``redactions_by_reason``
       and ``column_name_map`` carry the per-chunk slice.

    The custom ``__repr__`` omits ``column_name_map`` because the dict's
    values are the **real** column names — potentially PII-bearing per
    ``safety-layer.md`` DEC-022. Field access still exposes them; the
    redaction only blunts the casual debug-print path.
    """

    model_config = _BASE_MODEL_CONFIG

    # Metadata fields — non-None on non-chunked + chunk-header rows,
    # None on chunk-continuation rows (the validator enforces this).
    timestamp: datetime | None = None
    model_unique_id: str | None = None
    mode: SamplingMode | None = None
    columns_sent: tuple[str, ...] | None = None
    row_count: int | None = None
    signalforge_version: str | None = None
    policy_hash: str | None = None
    policy_flags: tuple[str, ...] | None = None

    # v4 redaction representation (symbol-table-by-reason + mapback).
    # Empty dict on a chunk header; full payload on non-chunked; slice
    # on chunk continuation.
    redactions_by_reason: dict[RedactionReason, tuple[str, ...]] | None = None
    column_name_map: dict[str, str] | None = None

    # Chunk-correlation triple. All three ``None`` means non-chunked.
    audit_id: str | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None

    audit_schema_version: int = 4
    """Frozen at the writer's :data:`_AUDIT_SCHEMA_VERSION` constant.
    Issue #54 bumped 1 → 2 when the :data:`RedactionReason` literal
    gained ``draft_skip_*`` values and the LLM-payload omission
    semantics became dependent on the reason. Issue #55 bumped 2 → 3
    when :func:`signalforge.safety.policy._compute_policy_hash` migrated
    from ``SHA-256[:16]`` to ``blake2b(digest_size=8)`` so the audit
    corpus reads one hash recipe across every writer. Issue #185 bumped
    3 → 4 when the v3 ``redactions: tuple[RedactionRecord, ...]`` field
    was replaced by ``redactions_by_reason`` + ``column_name_map`` and
    the chunk-correlation triple (``audit_id`` / ``chunk_index`` /
    ``chunk_count``) was added. Per #185 DEC-005, backward-compat read of
    v3 records is NOT supported (the library is pre-1.0 and was not yet
    adopted). The field stays :class:`int` (not :class:`typing.Literal`)
    so future bumps round-trip without a schema migration."""

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return iso8601_z(value)

    @model_validator(mode="after")
    def _enforce_chunk_shape(self) -> AuditEvent:
        """Reject mismatched (chunk-triple × metadata × redactions) combinations.

        Three valid shapes (see class docstring). Any other combination is a
        construction bug — the writer / reader contract assumes the validator
        has gated this.
        """
        # The chunk-correlation triple is three-valued: either every member
        # is ``None`` (non-chunked) or every member is set (chunked). A
        # mixed shape (e.g. ``audit_id`` set but ``chunk_index`` None) is
        # always a bug.
        triple_all_none = (
            self.audit_id is None and self.chunk_index is None and self.chunk_count is None
        )
        triple_all_set = (
            self.audit_id is not None
            and self.chunk_index is not None
            and self.chunk_count is not None
        )
        if not (triple_all_none or triple_all_set):
            raise ValueError(
                "audit_id, chunk_index, and chunk_count must all be set together or all be None"
            )

        if triple_all_none:
            # ---- Non-chunked ----
            # All metadata required; both v4 redaction maps required
            # (may be empty dicts).
            missing_metadata = [
                name
                for name, value in (
                    ("timestamp", self.timestamp),
                    ("model_unique_id", self.model_unique_id),
                    ("mode", self.mode),
                    ("columns_sent", self.columns_sent),
                    ("signalforge_version", self.signalforge_version),
                    ("policy_hash", self.policy_hash),
                    ("policy_flags", self.policy_flags),
                )
                if value is None
            ]
            if missing_metadata:
                raise ValueError(
                    "non-chunked AuditEvent requires metadata fields: "
                    f"{', '.join(missing_metadata)}"
                )
            if self.redactions_by_reason is None or self.column_name_map is None:
                raise ValueError(
                    "non-chunked AuditEvent requires both redactions_by_reason "
                    "and column_name_map (may be empty dicts)"
                )
            return self

        # ---- Chunked: triple_all_set ----
        # Narrowing for the type checker: all three are non-None here.
        assert self.chunk_index is not None  # noqa: S101
        assert self.chunk_count is not None  # noqa: S101

        if self.chunk_count < 2:
            raise ValueError(
                f"chunk_count must be >= 2 on a chunked event (got {self.chunk_count})"
            )
        if self.chunk_index < 0:
            raise ValueError(
                f"chunk_index must be >= 0 on a chunked event (got {self.chunk_index})"
            )
        if self.chunk_index >= self.chunk_count:
            raise ValueError(
                f"chunk_index ({self.chunk_index}) must be < chunk_count ({self.chunk_count})"
            )

        if self.chunk_index == 0:
            # ---- Chunk header ----
            # All metadata required; the v4 redaction maps MUST be empty
            # dicts (the redaction body rides on continuation rows).
            missing_metadata = [
                name
                for name, value in (
                    ("timestamp", self.timestamp),
                    ("model_unique_id", self.model_unique_id),
                    ("mode", self.mode),
                    ("columns_sent", self.columns_sent),
                    ("signalforge_version", self.signalforge_version),
                    ("policy_hash", self.policy_hash),
                    ("policy_flags", self.policy_flags),
                )
                if value is None
            ]
            if missing_metadata:
                raise ValueError(
                    "chunk-header AuditEvent (chunk_index=0) requires metadata "
                    f"fields: {', '.join(missing_metadata)}"
                )
            if self.redactions_by_reason is None or self.column_name_map is None:
                raise ValueError(
                    "chunk-header AuditEvent requires redactions_by_reason and "
                    "column_name_map to be empty dicts (not None)"
                )
            if self.redactions_by_reason or self.column_name_map:
                raise ValueError(
                    "chunk-header AuditEvent (chunk_index=0) requires "
                    "redactions_by_reason and column_name_map to be EMPTY dicts; "
                    "the redaction body rides on continuation rows"
                )
            return self

        # ---- Chunk continuation (chunk_index >= 1) ----
        # Metadata fields MUST be None; redaction maps carry the slice.
        present_metadata = [
            name
            for name, value in (
                ("timestamp", self.timestamp),
                ("model_unique_id", self.model_unique_id),
                ("mode", self.mode),
                ("columns_sent", self.columns_sent),
                ("row_count", self.row_count),
                ("signalforge_version", self.signalforge_version),
                ("policy_hash", self.policy_hash),
                ("policy_flags", self.policy_flags),
            )
            if value is not None
        ]
        if present_metadata:
            raise ValueError(
                "chunk-continuation AuditEvent (chunk_index >= 1) must omit "
                f"metadata fields: {', '.join(present_metadata)} present"
            )
        if self.redactions_by_reason is None or self.column_name_map is None:
            raise ValueError(
                "chunk-continuation AuditEvent requires redactions_by_reason "
                "and column_name_map (carrying the per-chunk slice)"
            )
        return self

    def __repr__(self) -> str:
        """Compact repr that omits ``column_name_map`` (safety-layer DEC-022).

        ``column_name_map`` carries the (hashed → real) mapping, so its values
        are real column names — potentially PII-bearing. Casual debug prints
        (``print(event)`` / ``repr(event)``) must not leak them; field access
        still exposes them. Shows redaction counts rather than contents.
        """
        red_count = (
            sum(len(v) for v in self.redactions_by_reason.values())
            if self.redactions_by_reason is not None
            else 0
        )
        map_count = len(self.column_name_map) if self.column_name_map is not None else 0
        return (
            f"AuditEvent(model_unique_id={self.model_unique_id!r}, "
            f"mode={self.mode!r}, "
            f"audit_id={self.audit_id!r}, "
            f"chunk_index={self.chunk_index!r}, "
            f"chunk_count={self.chunk_count!r}, "
            f"audit_schema_version={self.audit_schema_version!r}, "
            f"redactions_by_reason_count={red_count}, "
            f"column_name_map_count={map_count})"
        )


# The ``schema`` field name shadows Pydantic v1's deprecated
# :meth:`BaseModel.schema` method, which makes Pydantic emit a UserWarning at
# class-creation time. The field name is part of the documented LLMRequest
# contract (audit-log shape per safety-layer.md DEC-014), so the override is
# intentional. Scope the suppression to this one class definition rather than
# mutating the global filter list — issue #93.
with warnings.catch_warnings():
    # Relaxed regex (not the literal full message) so a future Pydantic
    # version rewording the suffix still gets caught. Scoped to a narrow
    # message anchor + ``UserWarning`` category — broad enough to survive
    # message-wording drift, narrow enough not to swallow unrelated
    # warnings.
    warnings.filterwarnings(
        "ignore",
        message=r'Field name "schema".*shadows.*',
        category=UserWarning,
    )

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
        # method on this subclass; the structural override is silenced for
        # pyright here and the runtime UserWarning is silenced by the
        # ``catch_warnings`` block above.
        schema: tuple[tuple[str, str], ...]  # pyright: ignore[reportIncompatibleMethodOverride]


__all__ = [
    "SamplingMode",
    "RedactionReason",
    "DRAFT_SKIP_REASONS",
    "RedactionRecord",
    "AuditEvent",
    "LLMRequest",
]
