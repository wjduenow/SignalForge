"""Tests for the typed ingest result models (US-002).

NOTE — no ``extra="forbid"`` drift detector lives here, and that is
deliberate: ``IngestResult`` / ``SkippedTest`` are produced *in process*
by the ingest reader and handed straight to the prune stage. They are NOT
serialised to a JSONL audit / sidecar and read back from disk, so the
read-back drift-detector convention (``docs/rules/testing-signal.md``
§ "Drift detection" — pair every ``extra="ignore"`` *parser* with a
one-off ``StrictModel(extra="forbid")``) does not apply. Their
``extra="ignore"`` is forward-compat for a future field we add ourselves,
not tolerance of an upstream on-disk schema we don't control. A reviewer
should NOT add a spurious drift detector for these models.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from signalforge.draft import CandidateColumn, CandidateSchema
from signalforge.ingest import IngestResult, SkippedTest


def _minimal_candidate() -> CandidateSchema:
    """Smallest valid CandidateSchema: one column, no tests."""
    return CandidateSchema(
        name="stg_orders",
        description="",
        columns=(CandidateColumn(name="id", description=""),),
    )


@pytest.mark.parametrize(
    "reason",
    [
        "unsupported-test-type",
        "custom-or-generic-test",
        "malformed-supported-test",
    ],
)
def test_skipped_test_accepts_each_reason(reason: str) -> None:
    skipped = SkippedTest(test_name="some_test", column="id", reason=reason)  # type: ignore[arg-type]
    assert skipped.reason == reason
    assert skipped.detail == ""


def test_skipped_test_rejects_unknown_reason() -> None:
    with pytest.raises(ValidationError):
        SkippedTest(test_name="t", column=None, reason="not-a-real-reason")  # type: ignore[arg-type]


def test_skipped_test_column_may_be_none_for_model_level() -> None:
    skipped = SkippedTest(
        test_name="my_singular_test",
        column=None,
        reason="custom-or-generic-test",
        detail="singular test in tests/ dir",
    )
    assert skipped.column is None
    assert skipped.detail == "singular test in tests/ dir"


def test_skipped_test_extra_key_is_ignored() -> None:
    # extra="ignore" must tolerate an unknown key without raising.
    skipped = SkippedTest(
        test_name="t",
        column="id",
        reason="unsupported-test-type",
        unknown_field="surprise",  # type: ignore[call-arg]
    )
    assert skipped.test_name == "t"
    assert not hasattr(skipped, "unknown_field")


def test_ingest_result_field_access_and_roundtrip() -> None:
    candidate = _minimal_candidate()
    skipped = (
        SkippedTest(
            test_name="dbt_utils.expression_is_true",
            column="id",
            reason="custom-or-generic-test",
        ),
        SkippedTest(
            test_name="not_null",
            column="id",
            reason="malformed-supported-test",
            detail="oops",
        ),
    )
    result = IngestResult(candidate=candidate, skipped=skipped)

    assert result.candidate.name == "stg_orders"
    assert result.candidate is candidate
    assert len(result.skipped) == 2
    assert result.skipped[0].reason == "custom-or-generic-test"
    assert result.skipped[1].detail == "oops"


def test_ingest_result_skipped_defaults_empty() -> None:
    result = IngestResult(candidate=_minimal_candidate())
    assert result.skipped == ()


def test_ingest_result_extra_key_is_ignored() -> None:
    result = IngestResult(
        candidate=_minimal_candidate(),
        bogus="ignored",  # type: ignore[call-arg]
    )
    assert not hasattr(result, "bogus")


def test_ingest_result_repr_is_minimal() -> None:
    # Custom repr surfaces identity + skip count, NOT nested candidate content.
    result = IngestResult(
        candidate=_minimal_candidate(),
        skipped=(SkippedTest(test_name="t", column="id", reason="unsupported-test-type"),),
    )
    rendered = repr(result)
    assert rendered == "IngestResult(candidate='stg_orders', skipped=1)"
    # The column payload must NOT leak into the repr.
    assert "CandidateColumn" not in rendered


def test_ingest_result_is_frozen() -> None:
    result = IngestResult(candidate=_minimal_candidate())
    with pytest.raises(ValidationError):
        result.skipped = ()  # type: ignore[misc]
