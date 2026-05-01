"""Schema-drift detection for the prune layer (US-010, DEC-010).

Pairs production ``extra="ignore"`` models with ``extra="forbid"`` Strict
mirrors validated against committed JSON / JSONL fixtures. Adding a field
to a production model without updating the strict mirror OR the fixture
breaks the test loudly.

Mirrors :mod:`tests.safety.test_drift_detector` shape exactly. The three
prune-layer read-back models covered here are:

* :class:`signalforge.prune.models.PruneDecision`
* :class:`signalforge.prune.models.PruneResult`
* :class:`signalforge.prune.audit.PruneEvent`

:class:`signalforge.prune.config.PruneConfig` is already ``extra="forbid"``
in production (DEC-015), so no drift gate is needed there.
:class:`signalforge.draft.models.CandidateTest` is the draft layer's
responsibility and is covered by :mod:`tests.draft` — this module reuses
the discriminated union as-is for the ``test:`` field on each event.

Reference: ``.claude/rules/manifest-readers.md`` (DEC-008 — drift detectors
mandatory for ``extra="ignore"`` reader-shaped models),
``.claude/rules/safety-layer.md`` (DEC-014 / DEC-015 — pair every read-back
model with a one-off ``extra="forbid"`` mirror).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from signalforge.draft.models import CandidateTest
from signalforge.prune.audit import PruneEvent
from signalforge.prune.models import DropReason, PruneDecision, PruneResult, Scope

_STRICT = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "prune"


class StrictPruneDecision(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`PruneDecision` (DEC-010).

    If you add a field to :class:`PruneDecision`, you MUST:

    1. Add it here, and
    2. Update :file:`tests/fixtures/prune/prune_decision_v1.json` (and the
       ``decisions`` arrays in the result / event fixtures if appropriate).

    The field-set parity test below catches additions that arrive via one
    side but not the other.
    """

    model_config = _STRICT

    test_anchor: str
    test: CandidateTest
    decision: Literal["kept", "dropped"]
    reason: DropReason
    failures: int
    sampled_rows: int | None
    scope: Scope
    elapsed_ms: int
    compiled_sql_hash: str
    compiled_sql: str
    why: str
    sample_failures: tuple[dict[str, Any], ...] | None = None


class StrictPruneResult(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`PruneResult` (DEC-010).

    Note: production :class:`PruneResult` exposes ``kept_decisions``,
    ``dropped_decisions``, ``kept_count``, ``dropped_count``, and
    ``total_tests`` as :func:`pydantic.computed_field` properties (DEC-003).
    These live in ``model_computed_fields``, NOT in ``model_fields``, so
    the field-set parity test does not need to filter them out — it only
    compares the stored-field set, which is what drift detection cares
    about.
    """

    model_config = _STRICT

    prune_schema_version: Literal[1] = 1
    model_unique_id: str
    decisions: tuple[StrictPruneDecision, ...]
    elapsed_ms: int
    signalforge_version: str


class StrictPruneEvent(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`PruneEvent` (DEC-010).

    Mirrors the flat shape (DEC-014 of llm-drafter.md / DEC-018 of
    prune-engine plan): the decision's fields are flattened in rather than
    nested under a ``decision:`` key, so a reviewer can ``jq`` over the
    JSONL without descending one level per field.
    """

    model_config = _STRICT

    audit_schema_version: Literal[1] = 1
    signalforge_version: str
    record_id: str
    timestamp: str
    config_hash: str
    model_unique_id: str
    test: CandidateTest
    test_anchor: str
    decision: Literal["kept", "dropped"]
    reason: DropReason
    failures: int
    sampled_rows: int | None
    scope: Scope
    elapsed_ms: int
    compiled_sql_hash: str
    compiled_sql: str
    why: str
    sample_failures: tuple[dict[str, Any], ...] | None = None


# --- Fixture validation ----------------------------------------------------


def test_strict_prune_decision_validates_fixture() -> None:
    """Each entry in ``prune_decision_v1.json`` validates against
    :class:`StrictPruneDecision` (``extra="forbid"``).

    If this raises, an unknown field was introduced in the fixture without
    being mirrored on :class:`StrictPruneDecision` (or vice versa). Update
    production :class:`PruneDecision`, :class:`StrictPruneDecision`, and
    the fixture together.
    """
    fixture_path = _FIXTURES_DIR / "prune_decision_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload, (
        f"expected a non-empty JSON array at {fixture_path}"
    )
    for entry in payload:
        StrictPruneDecision.model_validate(entry)


def test_strict_prune_result_validates_fixture() -> None:
    """The :file:`prune_result_v1.json` fixture validates against
    :class:`StrictPruneResult`.
    """
    fixture_path = _FIXTURES_DIR / "prune_result_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    StrictPruneResult.model_validate(payload)


def test_strict_prune_event_validates_jsonl_fixture() -> None:
    """Each line of :file:`prune_event_v1.jsonl` validates against
    :class:`StrictPruneEvent`. Covers all five :data:`DropReason` values.
    """
    fixture_path = _FIXTURES_DIR / "prune_event_v1.jsonl"
    text = fixture_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, f"expected one-or-more JSONL lines in {fixture_path}"

    seen_reasons: set[str] = set()
    for line in lines:
        event = StrictPruneEvent.model_validate_json(line)
        seen_reasons.add(event.reason)

    # All five DropReason values exercised in the fixture.
    expected_reasons = {
        "always-passes",
        "requires-future-data",
        "failed-on-known-clean-data",
        "kept",
        "kept-without-evidence",
    }
    assert seen_reasons == expected_reasons, (
        f"prune_event_v1.jsonl must cover every DropReason; "
        f"missing: {expected_reasons - seen_reasons}, "
        f"unexpected: {seen_reasons - expected_reasons}"
    )


# --- Field-set parity ------------------------------------------------------


def test_prune_decision_field_set_parity() -> None:
    """:class:`StrictPruneDecision` model_fields exactly match
    :class:`PruneDecision` model_fields. Adding a field to one without
    the other breaks this test loudly.
    """
    strict_fields = set(StrictPruneDecision.model_fields.keys())
    prod_fields = set(PruneDecision.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictPruneDecision is missing fields present in PruneDecision: "
        f"{missing_in_strict}. Update StrictPruneDecision to match."
    )
    assert not extra_in_strict, (
        f"StrictPruneDecision has fields absent from PruneDecision: "
        f"{extra_in_strict}. Remove from StrictPruneDecision or add to "
        f"PruneDecision."
    )


def test_prune_result_field_set_parity() -> None:
    """:class:`StrictPruneResult` model_fields exactly match
    :class:`PruneResult` model_fields.

    Note: ``kept_decisions`` / ``dropped_decisions`` / ``kept_count`` /
    ``dropped_count`` / ``total_tests`` are :func:`pydantic.computed_field`
    properties on production :class:`PruneResult` (DEC-003) — they live
    in ``model_computed_fields``, NOT in ``model_fields``, so the parity
    check focuses on stored fields only.
    """
    strict_fields = set(StrictPruneResult.model_fields.keys())
    prod_fields = set(PruneResult.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictPruneResult is missing fields present in PruneResult: "
        f"{missing_in_strict}. Update StrictPruneResult to match."
    )
    assert not extra_in_strict, (
        f"StrictPruneResult has fields absent from PruneResult: "
        f"{extra_in_strict}. Remove from StrictPruneResult or add to "
        f"PruneResult."
    )


def test_prune_event_field_set_parity() -> None:
    """:class:`StrictPruneEvent` model_fields exactly match
    :class:`PruneEvent` model_fields.
    """
    strict_fields = set(StrictPruneEvent.model_fields.keys())
    prod_fields = set(PruneEvent.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"StrictPruneEvent is missing fields present in PruneEvent: "
        f"{missing_in_strict}. Update StrictPruneEvent to match."
    )
    assert not extra_in_strict, (
        f"StrictPruneEvent has fields absent from PruneEvent: "
        f"{extra_in_strict}. Remove from StrictPruneEvent or add to "
        f"PruneEvent."
    )


# --- Sanity floor: extra="forbid" actually fires --------------------------


def test_strict_prune_event_rejects_unknown_field() -> None:
    """Sanity floor: a fixture line with an extra unknown field raises
    :class:`ValidationError`. Confirms ``extra="forbid"`` is wired up — a
    silently-accepted unknown field would defeat the entire drift gate.
    """
    fixture_path = _FIXTURES_DIR / "prune_event_v1.jsonl"
    first_line = fixture_path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(first_line)
    payload["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        StrictPruneEvent.model_validate(payload)
