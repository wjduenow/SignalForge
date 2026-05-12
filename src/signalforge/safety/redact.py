"""Pure redaction helpers for the PII safety layer (US-008).

This module is the central place where SignalForge decides whether a column
should be sent to the LLM at all (and, when redacted, what placeholder name
to use). Three pieces of public surface plus one underscore-prefixed helper:

* :func:`hash_column_name` — stable per-name ``col_<8 hex>`` placeholder
  (DEC-010). Used by ``schema-only`` and ``aggregate-only`` modes so the
  LLM can still reference a column without the real name leaking PII.
* :func:`redact_rows` — replace values for redacted columns with the
  ``"<REDACTED>"`` constant. Pure, non-mutating, returns a tuple.
* :func:`redact_column_names` — substitute hashed names into a
  ``(name, type)`` schema tuple for redacted columns.
* :func:`_classify_column` — pure precedence-resolved classifier with the
  seven :data:`signalforge.safety.models.RedactionReason` outcomes.
  Underscore-prefixed because callers should usually go through
  :mod:`signalforge.safety.request` (US-009) rather than calling this
  directly; tests import it explicitly.

Design commitments operationalised here:

* **DEC-003** — The four opt-out signals (column meta ``signalforge.sample``,
  column tag ``pii``, column ``meta.contains_pii``, plus their model-level
  twins) take precedence over the pattern matcher. Column-level signals
  beat model-level on conflict.
* **DEC-010** — Column-name redaction via ``blake2b`` (digest_size=4) so
  the LLM can still reference the column by its hashed placeholder.
* **DEC-020** — Tag matching is case-insensitive (``["PII"]`` triggers).
  ``meta.contains_pii`` accepts truthy values; falsy values do not. Pattern
  matching lowercases both sides before ``fnmatch.fnmatchcase``. A column
  whose name contains a suspicious substring (``email``, ``phone``, ``ssn``,
  ``password``, ``token``, ``secret``, ``api_key``) but matches nothing
  emits a one-line WARNING — operator-noticeable but non-fatal.
* **DEC-024** — :data:`signalforge.safety.models.RedactionReason` is a
  ``Literal`` of seven values; ``_classify_column`` returns
  ``RedactionRecord | None`` (``None`` = pass-through, no record needed).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
from collections.abc import Iterable
from typing import Any, Final

from signalforge.manifest.models import Column, Model
from signalforge.safety.models import RedactionRecord
from signalforge.safety.policy import SafetyPolicy

_LOGGER: Final = logging.getLogger("signalforge.safety")

_REDACTED_VALUE: Final[str] = "<REDACTED>"
"""Sentinel substituted for redacted cell values in :func:`redact_rows`."""

_SUSPICIOUS_SUBSTRINGS: Final[tuple[str, ...]] = (
    "email",
    "phone",
    "ssn",
    "password",
    "token",
    "secret",
    "api_key",
)
"""Lowercased substrings that, when present in an unmatched column name, hint
at a misconfigured redaction policy. DEC-020."""


def hash_column_name(name: str) -> str:
    """Return a stable ``col_<8-hex>`` placeholder for ``name`` (DEC-010).

    Schema-only and aggregate-only modes redact column NAMES too — a column
    named ``customer_ssn`` leaks PII via the name itself. This function
    yields a deterministic ``blake2b`` (digest_size=4) hash so the LLM can
    still reference the column in its draft (e.g. ``col_a3f29c61``). The
    real-name -> hashed-name mapping lives in the audit log only.
    """
    digest = hashlib.blake2b(name.encode("utf-8"), digest_size=4).hexdigest()
    return f"col_{digest}"


# ---------------------------------------------------------------------------
# _classify_column — precedence-resolved opt-out / pattern matcher
# ---------------------------------------------------------------------------


def _is_truthy_pii_flag(value: Any) -> bool:
    """Return ``True`` when ``value`` should count as ``contains_pii=True``.

    Booleans pass through unchanged. ``None`` is always false. Strings are
    truthy iff non-empty; numbers iff non-zero; other objects fall back to
    Python truthiness. Coercions (anything other than a plain ``bool``)
    emit a DEBUG log so the audit trail records the loose-typing decision.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        coerced = bool(value)
        if coerced:
            _LOGGER.debug(
                "meta.contains_pii coerced from %s to True",
                type(value).__name__,
            )
        return coerced
    if isinstance(value, str):
        if value:
            _LOGGER.debug("meta.contains_pii coerced from non-empty str to True")
            return True
        return False
    if bool(value):
        _LOGGER.debug(
            "meta.contains_pii coerced from %s to True",
            type(value).__name__,
        )
        return True
    return False


def _has_pii_tag(tags: Iterable[Any] | None) -> bool:
    """Case-insensitive check for the literal tag ``"pii"``."""
    if not tags:
        return False
    return any(isinstance(t, str) and t.lower() == "pii" for t in tags)


def _model_meta(model: Model) -> dict[str, Any]:
    """Resolve the model's meta dict.

    ``Model`` has no top-level ``meta`` field; meta lives in ``config.meta``
    (DEC-011 in the manifest reader). This helper centralises that lookup
    so the classifier reads ``model_meta`` rather than poking at
    ``model.config.meta`` inline at every signal site.
    """
    return getattr(model.config, "meta", {}) or {}


def _classify_column(
    column: Column,
    model: Model,
    policy: SafetyPolicy,
) -> RedactionRecord | None:
    """Return a redaction record for ``column`` or ``None`` if not redacted.

    Precedence (first match wins, top to bottom):

    1. column ``meta.signalforge.skip_draft == True`` -> ``draft_skip_column_meta``
    2. model ``meta.signalforge.skip_draft == True``  -> ``draft_skip_model_meta``
    3. column ``meta.signalforge.sample == False``    -> ``column_meta_optout``
    4. column tag ``pii`` (case-insensitive)          -> ``tag_pii_column``
    5. column ``meta.contains_pii`` truthy            -> ``meta_contains_pii_column``
    6. model ``meta.signalforge.sample == False``     -> ``model_meta_optout``
    7. model tag ``pii`` (case-insensitive)           -> ``tag_pii_model``
    8. model ``meta.contains_pii`` truthy             -> ``meta_contains_pii_model``
    9. column name matches a redact pattern           -> ``pattern_match``

    The two ``draft_skip_*`` reasons (issue #54) are semantically distinct
    from the seven PII reasons below them: a draft-skip column is OMITTED
    entirely from the LLM prompt, whereas a PII-redacted column is sent
    under a hashed placeholder. Skip checks run first because if a column
    is omitted entirely, any PII signal on it is moot. Column-level skip
    is checked before model-level skip so the audit reason names the
    most-specific source.

    When no signal fires, returns ``None``. As a side effect, columns whose
    name contains a "suspicious" substring (DEC-020) but matched no
    pattern emit a single WARNING with a JSON-encoded payload (ANSI-safe).
    """
    name = column.name
    name_lower = name.lower()
    hashed = hash_column_name(name)

    # ----- draft-skip signals (issue #54) — run first; semantics differ -----
    # ``skip_draft`` requires an explicit ``True`` (mirrors the strict
    # ``sample is False`` check below — truthy non-bool values are
    # configuration noise, not opt-in).
    column_meta = column.meta or {}
    sf_meta = column_meta.get("signalforge")
    if isinstance(sf_meta, dict) and sf_meta.get("skip_draft") is True:
        return RedactionRecord(
            column_name=name,
            hashed_name=hashed,
            redacted=True,
            reason="draft_skip_column_meta",
        )
    model_meta = _model_meta(model)
    sf_model_meta = model_meta.get("signalforge")
    if isinstance(sf_model_meta, dict) and sf_model_meta.get("skip_draft") is True:
        return RedactionRecord(
            column_name=name,
            hashed_name=hashed,
            redacted=True,
            reason="draft_skip_model_meta",
        )

    # ----- column-level signals -----
    if isinstance(sf_meta, dict) and sf_meta.get("sample") is False:
        return RedactionRecord(
            column_name=name,
            hashed_name=hashed,
            redacted=True,
            reason="column_meta_optout",
        )
    if _has_pii_tag(getattr(column, "tags", ())):
        return RedactionRecord(
            column_name=name,
            hashed_name=hashed,
            redacted=True,
            reason="tag_pii_column",
        )
    if _is_truthy_pii_flag(column_meta.get("contains_pii")):
        return RedactionRecord(
            column_name=name,
            hashed_name=hashed,
            redacted=True,
            reason="meta_contains_pii_column",
        )

    # ----- model-level signals -----
    if isinstance(sf_model_meta, dict) and sf_model_meta.get("sample") is False:
        return RedactionRecord(
            column_name=name,
            hashed_name=hashed,
            redacted=True,
            reason="model_meta_optout",
        )
    if _has_pii_tag(getattr(model, "tags", ())):
        return RedactionRecord(
            column_name=name,
            hashed_name=hashed,
            redacted=True,
            reason="tag_pii_model",
        )
    if _is_truthy_pii_flag(model_meta.get("contains_pii")):
        return RedactionRecord(
            column_name=name,
            hashed_name=hashed,
            redacted=True,
            reason="meta_contains_pii_model",
        )

    # ----- pattern match -----
    for pattern in policy.redact_patterns:
        if fnmatch.fnmatchcase(name_lower, pattern.lower()):
            return RedactionRecord(
                column_name=name,
                hashed_name=hashed,
                redacted=True,
                reason="pattern_match",
            )

    # ----- no signal: maybe warn, then pass through -----
    if any(s in name_lower for s in _SUSPICIOUS_SUBSTRINGS):
        _LOGGER.warning(
            "suspicious column not redacted: %s",
            json.dumps(
                {
                    "model_unique_id": getattr(model, "unique_id", "<unknown>"),
                    "column": name,
                    "message": (
                        "column name contains a suspicious substring but "
                        "no redaction pattern matched"
                    ),
                }
            ),
        )
    return None


# ---------------------------------------------------------------------------
# redact_rows
# ---------------------------------------------------------------------------


def redact_rows(
    rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    redacted_real_names: frozenset[str] | set[str] | tuple[str, ...] | list[str],
) -> tuple[dict[str, Any], ...]:
    """Replace values for redacted columns with ``"<REDACTED>"``.

    Pure: does not mutate ``rows`` or any of its dicts. Missing-column-in-row
    is silently ignored — a sampled row that lacks one of the redacted keys
    just passes through with its keys unchanged.

    Operates on REAL column names. Callers that also want to rewrite keys to
    hashed placeholders should use :func:`redact_column_names` for the
    schema; rows themselves are kept keyed on real names so audit replay can
    reconcile them with the records.
    """
    redacted_set = frozenset(redacted_real_names)
    new_rows: list[dict[str, Any]] = []
    for row in rows:
        new_row = {k: (_REDACTED_VALUE if k in redacted_set else v) for k, v in row.items()}
        new_rows.append(new_row)
    return tuple(new_rows)


# ---------------------------------------------------------------------------
# redact_column_names
# ---------------------------------------------------------------------------


def redact_column_names(
    columns: tuple[tuple[str, str], ...] | list[tuple[str, str]],
    records: tuple[RedactionRecord, ...] | list[RedactionRecord],
) -> tuple[tuple[str, str], ...]:
    """Substitute hashed placeholders for redacted columns in a schema tuple.

    ``columns`` is a sequence of ``(real_name, type_string)`` tuples for
    every column in the model. ``records`` is the set of redaction records
    produced by :func:`_classify_column` for the same model.

    Returns a tuple of ``(display_name, type_string)`` where
    ``display_name`` is the hashed placeholder for redacted columns and the
    real name for everyone else. Records with ``redacted=False`` are
    ignored — the matching column passes through with its real name.
    """
    redacted_lookup = {r.column_name: r.hashed_name for r in records if r.redacted}
    result: list[tuple[str, str]] = []
    for real_name, type_str in columns:
        display_name = redacted_lookup.get(real_name, real_name)
        result.append((display_name, type_str))
    return tuple(result)


__all__ = [
    "hash_column_name",
    "redact_rows",
    "redact_column_names",
]
