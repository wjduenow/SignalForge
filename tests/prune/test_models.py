"""Tests for ``signalforge.prune.models`` (US-004).

Covers the read-back-stable shapes the prune engine returns: tuple
coercion of the ``decisions`` sequence, computed-property partitioning
into kept/dropped, frozen immutability, validator rejection of unknown
``DropReason`` literals, and discriminated-union round-trip of the
embedded :class:`CandidateTest` (DEC-004 — the typed union, not a loose
dict). The drift detector (one-off ``extra="forbid"`` mirror) lands in
US-010.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from signalforge.draft.models import (
    CandidateTest,
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
)
from signalforge.prune.models import PruneDecision, PruneResult


def _make_decision(
    *,
    decision: str = "kept",
    reason: str = "kept",
    test: CandidateTest | None = None,
    test_anchor: str = "column.email",
    failures: int = 0,
    sampled_rows: int | None = 1000,
    scope: str = "sample",
) -> PruneDecision:
    test_value: CandidateTest = test if test is not None else CandidateTestNotNull(column="email")
    return PruneDecision(
        test_anchor=test_anchor,
        test=test_value,
        decision=decision,  # type: ignore[arg-type]
        reason=reason,  # type: ignore[arg-type]
        failures=failures,
        sampled_rows=sampled_rows,
        scope=scope,  # type: ignore[arg-type]
        elapsed_ms=12,
        compiled_sql_hash="0123456789abcdef",
        compiled_sql="SELECT 1",
        why="kept because signal",
    )


def test_prune_result_decisions_accepts_list_stores_tuple() -> None:
    d1 = _make_decision()
    d2 = _make_decision(test_anchor="column.id")
    result = PruneResult(
        model_unique_id="model.demo.customers",
        decisions=[d1, d2],  # type: ignore[arg-type]
        elapsed_ms=24,
        signalforge_version="0.1.0",
    )
    assert result.decisions == (d1, d2)
    assert isinstance(result.decisions, tuple)


def test_prune_result_kept_decisions_filters_correctly() -> None:
    kept_a = _make_decision(decision="kept", reason="kept", test_anchor="column.a")
    kept_b = _make_decision(decision="kept", reason="kept-without-evidence", test_anchor="column.b")
    dropped_a = _make_decision(decision="dropped", reason="always-passes", test_anchor="column.c")
    dropped_b = _make_decision(
        decision="dropped",
        reason="failed-on-known-clean-data",
        test_anchor="column.d",
    )
    dropped_c = _make_decision(
        decision="dropped",
        reason="requires-future-data",
        test_anchor="column.e",
    )
    result = PruneResult(
        model_unique_id="model.demo.x",
        decisions=(kept_a, kept_b, dropped_a, dropped_b, dropped_c),
        elapsed_ms=40,
        signalforge_version="0.1.0",
    )
    assert result.kept_decisions == (kept_a, kept_b)
    assert result.dropped_decisions == (dropped_a, dropped_b, dropped_c)
    assert result.kept_count == 2
    assert result.dropped_count == 3
    assert result.total_tests == 5
    assert result.kept_count + result.dropped_count == result.total_tests


def test_prune_decision_invalid_drop_reason_raises() -> None:
    with pytest.raises(ValidationError):
        PruneDecision(
            test_anchor="column.email",
            test=CandidateTestNotNull(column="email"),
            decision="dropped",
            reason="phantom-reason",  # type: ignore[arg-type]
            failures=0,
            sampled_rows=1000,
            scope="sample",
            elapsed_ms=10,
            compiled_sql_hash="deadbeefdeadbeef",
            compiled_sql="SELECT 1",
            why="x",
        )


def test_prune_decision_is_frozen() -> None:
    decision = _make_decision()
    with pytest.raises(ValidationError):
        decision.failures = 999  # type: ignore[misc]


def test_prune_decision_carries_typed_candidate_test() -> None:
    # not_null variant
    nn = _make_decision(test=CandidateTestNotNull(column="email"))
    assert nn.test.type == "not_null"
    nn_json = nn.model_dump_json()
    nn_round = PruneDecision.model_validate_json(nn_json)
    assert nn_round == nn
    assert nn_round.test.type == "not_null"

    # accepted_values variant — confirms discriminated-union round-trip
    av_test = CandidateTestAcceptedValues(column="status", values=("placed", "shipped"))
    av = _make_decision(test=av_test, test_anchor="column.status")
    assert av.test.type == "accepted_values"
    av_json = av.model_dump_json()
    av_round = PruneDecision.model_validate_json(av_json)
    assert av_round == av
    assert av_round.test.type == "accepted_values"
    # discriminator dispatched the right concrete subclass
    assert isinstance(av_round.test, CandidateTestAcceptedValues)
    assert av_round.test.values == ("placed", "shipped")


def test_prune_result_repr_redacts_sql_and_sample_failures() -> None:
    """DEC-022: PruneResult repr must NOT leak compiled_sql or sample rows.

    An accidental ``_LOGGER.warning("result: %s", result)`` with the
    default Pydantic repr would dump the full per-decision payload —
    including compiled SQL and any sampled-failure rows (which may
    contain PII) — into log sinks. The custom repr restricts output to
    top-level identity + count aggregates.
    """
    decision = PruneDecision(
        test_anchor="column.email",
        test=CandidateTestNotNull(column="email"),
        decision="kept",
        reason="kept",
        failures=3,
        sampled_rows=100,
        scope="sample",
        elapsed_ms=42,
        compiled_sql_hash="abc123",
        compiled_sql="SELECT secret_column FROM internal_table WHERE 1=1",
        why="3 failures",
        sample_failures=({"secret_column": "leaked-pii-value"},),
    )
    result = PruneResult(
        model_unique_id="model.shop.users",
        decisions=(decision,),
        elapsed_ms=42,
        signalforge_version="0.1.0.dev0",
    )
    rendered = repr(result)
    # Allowed (top-level identity + counts):
    assert "model.shop.users" in rendered
    assert "kept_count=1" in rendered
    assert "dropped_count=0" in rendered
    # Forbidden (sensitive bodies):
    assert "secret_column" not in rendered
    assert "internal_table" not in rendered
    assert "leaked-pii-value" not in rendered
    assert "compiled_sql" not in rendered
    assert "sample_failures" not in rendered
