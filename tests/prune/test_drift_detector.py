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
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from signalforge.draft.models import CandidateTest
from signalforge.prune.audit import PruneEvent
from signalforge.prune.models import DropReason, PruneDecision, PruneResult, Scope
from signalforge.prune.stats import (
    AnomalyTestStats,
    MadDowStats,
    MadStats,
    MinMaxDowStats,
    MinMaxStats,
    PercentileDowStats,
    PercentileStats,
    ZscoreDowStats,
    ZscoreStats,
)

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
    as_of: date | None = None
    stats: AnomalyTestStats | None = None


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

    audit_schema_version: int
    signalforge_version: str
    record_id: str
    timestamp: datetime
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
    as_of: date | None = None
    stats: AnomalyTestStats | None = None


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


def test_prune_event_fixture_audit_schema_version_is_current() -> None:
    """Pin the fixture's ``audit_schema_version`` to the current constant
    so a future bump without updating the sample lines breaks the test
    loudly. Mirrors safety's analogous pin.

    Issue #55 bumped 1 → 2 when ``config_hash`` migrated from
    ``SHA-256[:16]`` to ``blake2b(digest_size=8)``. Issue #171 bumped
    2 → 3 when ``as_of`` (time-bound evaluation date) and ``stats``
    (anomaly per-decision numerical state) landed.
    """
    from signalforge.prune.audit import _PRUNE_AUDIT_SCHEMA_VERSION

    fixture_path = _FIXTURES_DIR / "prune_event_v1.jsonl"
    text = fixture_path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines, f"expected one-or-more JSONL lines in {fixture_path}"

    for line in lines:
        payload = json.loads(line)
        assert payload["audit_schema_version"] == _PRUNE_AUDIT_SCHEMA_VERSION


def test_prune_event_round_trips_legacy_schema_version_1() -> None:
    """A pre-#55 ``prune.jsonl`` record carrying
    ``audit_schema_version: 1`` must still validate against the current
    :class:`PruneEvent` — audit replay across versions is a real
    requirement, mirrors :class:`signalforge.safety.models.AuditEvent`'s
    same round-trip guarantee. This is the load-bearing reason
    :attr:`PruneEvent.audit_schema_version` is typed :class:`int`
    (not :class:`typing.Literal`) in production.
    """
    fixture_path = _FIXTURES_DIR / "prune_event_v1.jsonl"
    first_line = fixture_path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(first_line)
    payload["audit_schema_version"] = 1
    # Drop the v3-only fields too — a true v1 record never had them.
    payload.pop("as_of", None)
    payload.pop("stats", None)
    event = PruneEvent.model_validate(payload)
    assert event.audit_schema_version == 1
    # The new optional fields default to ``None`` on replay.
    assert event.as_of is None
    assert event.stats is None


def test_prune_event_round_trips_legacy_schema_version_2_as_v3() -> None:
    """A v2 ``prune.jsonl`` record (missing ``as_of`` / ``stats``) must
    still validate cleanly against the current v3 :class:`PruneEvent`.

    Issue #171 DEC-013: the schema bump 2 → 3 added two optional fields
    with ``None`` defaults, so v2 records replay as v3 with both new
    fields ``None``. This is the load-bearing inline-v2-dict-replays-as-v3
    regression test required by US-012's acceptance criteria — it
    verifies the ``int`` (not ``Literal``) typing on
    :attr:`PruneEvent.audit_schema_version` preserves replay across the
    2 → 3 bump (matching the same guarantee #55 provided for the 1 → 2
    bump above).
    """
    fixture_path = _FIXTURES_DIR / "prune_event_v1.jsonl"
    first_line = fixture_path.read_text(encoding="utf-8").splitlines()[0]
    payload = json.loads(first_line)
    payload["audit_schema_version"] = 2
    # A genuine v2 record never had these fields — drop to simulate.
    payload.pop("as_of", None)
    payload.pop("stats", None)
    event = PruneEvent.model_validate(payload)
    assert event.audit_schema_version == 2
    assert event.as_of is None
    assert event.stats is None


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


# --- Anomaly stats drift mirrors (issue #171, US-001) ---------------------
#
# Pair each ``extra="ignore"`` production stats class with an ``extra="forbid"``
# strict mirror, validated against :file:`anomaly_stats_v1.json`. Adding a
# field to any production stats class without updating the strict mirror OR
# the fixture breaks the test loudly. Mirrors the prune-decision /
# prune-event drift gates above.


class StrictMadDowStats(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`MadDowStats`."""

    model_config = _STRICT

    median: float
    mad: float
    n_periods: int


class StrictZscoreDowStats(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`ZscoreDowStats`."""

    model_config = _STRICT

    mu: float
    sigma: float
    n_periods: int


class StrictPercentileDowStats(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`PercentileDowStats`."""

    model_config = _STRICT

    p_lo: float
    p_hi: float
    n_periods: int


class StrictMinMaxDowStats(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`MinMaxDowStats`."""

    model_config = _STRICT

    minimum: float
    maximum: float
    n_periods: int


class StrictMadStats(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`MadStats`."""

    model_config = _STRICT

    method: Literal["mad"] = "mad"
    median: float
    mad: float
    n_periods: int
    per_dow: dict[int, StrictMadDowStats] | None = None


class StrictZscoreStats(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`ZscoreStats`."""

    model_config = _STRICT

    method: Literal["zscore"] = "zscore"
    mu: float
    sigma: float
    n_periods: int
    per_dow: dict[int, StrictZscoreDowStats] | None = None


class StrictPercentileStats(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`PercentileStats`."""

    model_config = _STRICT

    method: Literal["percentile"] = "percentile"
    p_lo: float
    p_hi: float
    n_periods: int
    per_dow: dict[int, StrictPercentileDowStats] | None = None


class StrictMinMaxStats(BaseModel):
    """One-off ``extra="forbid"`` mirror of :class:`MinMaxStats`."""

    model_config = _STRICT

    method: Literal["min_max"] = "min_max"
    minimum: float
    maximum: float
    n_periods: int
    per_dow: dict[int, StrictMinMaxDowStats] | None = None


StrictAnomalyTestStats = Annotated[
    StrictMadStats | StrictZscoreStats | StrictPercentileStats | StrictMinMaxStats,
    Field(discriminator="method"),
]
_STRICT_ANOMALY_ADAPTER = TypeAdapter(StrictAnomalyTestStats)


def test_strict_anomaly_stats_validates_fixture() -> None:
    """Every row in :file:`anomaly_stats_v1.json` validates against the
    strict union (``extra="forbid"`` on every class).
    """
    fixture_path = _FIXTURES_DIR / "anomaly_stats_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload, f"expected non-empty JSON array at {fixture_path}"
    seen_methods: set[str] = set()
    for entry in payload:
        validated = _STRICT_ANOMALY_ADAPTER.validate_python(entry)
        seen_methods.add(validated.method)
    assert seen_methods == {"mad", "zscore", "percentile", "min_max"}, (
        f"anomaly_stats_v1.json must cover every method; got {seen_methods}"
    )


def test_strict_anomaly_stats_rejects_unknown_field() -> None:
    """Sanity floor: a fixture row with an extra unknown field raises
    :class:`ValidationError`. Confirms ``extra="forbid"`` is wired up
    across the anomaly-stats drift surface.
    """
    fixture_path = _FIXTURES_DIR / "anomaly_stats_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    first = dict(payload[0])
    first["future_field_that_should_not_exist"] = "boom"
    with pytest.raises(ValidationError):
        _STRICT_ANOMALY_ADAPTER.validate_python(first)


def test_strict_anomaly_stats_rejects_unknown_per_dow_field() -> None:
    """Sanity floor: a per-DOW entry with an extra field is rejected.
    Confirms the nested ``extra="forbid"`` mirror gates the DOW dict too.
    """
    fixture_path = _FIXTURES_DIR / "anomaly_stats_v1.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    seasonal = next(entry for entry in payload if entry.get("per_dow"))
    # Mutate a per-DOW entry to carry a phantom field.
    seasonal = dict(seasonal)
    seasonal["per_dow"] = {
        key: {**value, "future_field": "boom"} for key, value in seasonal["per_dow"].items()
    }
    with pytest.raises(ValidationError):
        _STRICT_ANOMALY_ADAPTER.validate_python(seasonal)


@pytest.mark.parametrize(
    "prod_cls, strict_cls",
    [
        (MadStats, StrictMadStats),
        (ZscoreStats, StrictZscoreStats),
        (PercentileStats, StrictPercentileStats),
        (MinMaxStats, StrictMinMaxStats),
        (MadDowStats, StrictMadDowStats),
        (ZscoreDowStats, StrictZscoreDowStats),
        (PercentileDowStats, StrictPercentileDowStats),
        (MinMaxDowStats, StrictMinMaxDowStats),
    ],
)
def test_anomaly_stats_field_set_parity(
    prod_cls: type[BaseModel], strict_cls: type[BaseModel]
) -> None:
    """Each strict mirror's ``model_fields`` exactly matches its production
    counterpart. Adding a field to one without the other breaks loudly.
    """
    strict_fields = set(strict_cls.model_fields.keys())
    prod_fields = set(prod_cls.model_fields.keys())
    missing_in_strict = prod_fields - strict_fields
    extra_in_strict = strict_fields - prod_fields
    assert not missing_in_strict, (
        f"{strict_cls.__name__} is missing fields present in "
        f"{prod_cls.__name__}: {missing_in_strict}. Update the strict mirror "
        f"to match."
    )
    assert not extra_in_strict, (
        f"{strict_cls.__name__} has fields absent from "
        f"{prod_cls.__name__}: {extra_in_strict}. Remove from the strict "
        f"mirror or add to production."
    )
