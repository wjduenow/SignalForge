"""Unit tests for :mod:`signalforge.prune.stats` (US-001, issue #171).

Covers the :data:`AnomalyTestStats` tagged union and its four
method-specific subclasses + the four per-DOW sub-stats classes.

Test surface (per US-001 TDD shape):

* Tagged-union dispatches on ``method`` field correctly.
* Each subclass round-trips through ``model_dump_json`` /
  ``model_validate_json``.
* Mismatched method/subclass raises :class:`pydantic.ValidationError`.
* Per-DOW dict serialises with int keys (Pydantic v2 dict-key coercion).
* Fixture validates end-to-end.

Reference: ``plans/super/171-row-count-anomaly.md`` § US-001 / DEC-005;
``.claude/rules/safety-layer.md`` § "Config-shaped models use
``extra='forbid'``; read-back models use ``extra='ignore'``".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

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

_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / "fixtures" / "prune" / "anomaly_stats_v1.json"
)

# A reusable TypeAdapter that knows how to dispatch the union by ``method``.
_ANOMALY_ADAPTER = TypeAdapter(AnomalyTestStats)


# --- Discriminator dispatch ------------------------------------------------


def test_union_dispatches_mad() -> None:
    """``method="mad"`` validates as :class:`MadStats`."""
    value = _ANOMALY_ADAPTER.validate_python(
        {"method": "mad", "median": 100.0, "mad": 5.0, "n_periods": 14}
    )
    assert isinstance(value, MadStats)
    assert value.median == 100.0
    assert value.mad == 5.0
    assert value.n_periods == 14
    assert value.per_dow is None


def test_union_dispatches_zscore() -> None:
    """``method="zscore"`` validates as :class:`ZscoreStats`."""
    value = _ANOMALY_ADAPTER.validate_python(
        {"method": "zscore", "mu": 100.0, "sigma": 4.5, "n_periods": 14}
    )
    assert isinstance(value, ZscoreStats)
    assert value.mu == 100.0
    assert value.sigma == 4.5


def test_union_dispatches_percentile() -> None:
    """``method="percentile"`` validates as :class:`PercentileStats`."""
    value = _ANOMALY_ADAPTER.validate_python(
        {"method": "percentile", "p_lo": 80.0, "p_hi": 120.0, "n_periods": 14}
    )
    assert isinstance(value, PercentileStats)
    assert value.p_lo == 80.0
    assert value.p_hi == 120.0


def test_union_dispatches_min_max() -> None:
    """``method="min_max"`` validates as :class:`MinMaxStats`."""
    value = _ANOMALY_ADAPTER.validate_python(
        {"method": "min_max", "minimum": 50.0, "maximum": 200.0, "n_periods": 14}
    )
    assert isinstance(value, MinMaxStats)
    assert value.minimum == 50.0
    assert value.maximum == 200.0


def test_union_unknown_method_raises_validation_error() -> None:
    """An unknown ``method`` value is rejected by the discriminated union."""
    with pytest.raises(ValidationError):
        _ANOMALY_ADAPTER.validate_python(
            {"method": "iqr", "p_lo": 1.0, "p_hi": 2.0, "n_periods": 14}
        )


def test_mismatched_method_field_raises_validation_error() -> None:
    """A payload declaring ``method="mad"`` but carrying z-score fields
    (no ``median`` / ``mad``) is rejected.

    The discriminator picks :class:`MadStats`, then Pydantic enforces
    that class's required fields — ``mu`` / ``sigma`` are silently dropped
    by ``extra="ignore"`` and the missing ``median`` / ``mad`` raise.
    """
    with pytest.raises(ValidationError):
        _ANOMALY_ADAPTER.validate_python(
            {"method": "mad", "mu": 100.0, "sigma": 4.5, "n_periods": 14}
        )


# --- Round-trip --------------------------------------------------------


def test_mad_stats_round_trip() -> None:
    """``MadStats`` survives ``model_dump_json`` / ``model_validate_json``."""
    original = MadStats(median=100.5, mad=2.5, n_periods=30)
    payload = original.model_dump_json()
    restored = MadStats.model_validate_json(payload)
    assert restored == original


def test_zscore_stats_round_trip() -> None:
    original = ZscoreStats(mu=10.0, sigma=1.2, n_periods=30)
    payload = original.model_dump_json()
    restored = ZscoreStats.model_validate_json(payload)
    assert restored == original


def test_percentile_stats_round_trip() -> None:
    original = PercentileStats(p_lo=5.0, p_hi=95.0, n_periods=30)
    payload = original.model_dump_json()
    restored = PercentileStats.model_validate_json(payload)
    assert restored == original


def test_min_max_stats_round_trip() -> None:
    original = MinMaxStats(minimum=0.0, maximum=100.0, n_periods=30)
    payload = original.model_dump_json()
    restored = MinMaxStats.model_validate_json(payload)
    assert restored == original


def test_union_round_trip_dispatches_through_method() -> None:
    """A polymorphic round-trip through the union preserves the concrete
    subclass."""
    original = ZscoreStats(mu=42.0, sigma=3.14, n_periods=21)
    # Dump via the union adapter; restore via the union adapter.
    payload = _ANOMALY_ADAPTER.dump_json(original)
    restored = _ANOMALY_ADAPTER.validate_json(payload)
    assert isinstance(restored, ZscoreStats)
    assert restored == original


# --- per_dow dict with int keys ----------------------------------------


def test_per_dow_dict_serialises_with_int_keys_after_round_trip() -> None:
    """JSON has no integer keys; Pydantic v2 coerces them back from
    strings on validate. The round-tripped dict carries ``int`` keys.
    """
    original = MadStats(
        median=100.0,
        mad=5.0,
        n_periods=28,
        per_dow={
            0: MadDowStats(median=110.0, mad=4.5, n_periods=4),
            6: MadDowStats(median=90.0, mad=6.0, n_periods=4),
        },
    )
    payload = original.model_dump_json()
    # JSON keys are strings on the wire.
    raw = json.loads(payload)
    assert set(raw["per_dow"].keys()) == {"0", "6"}
    # Pydantic coerces back to int on validate.
    restored = MadStats.model_validate_json(payload)
    assert restored.per_dow is not None
    assert set(restored.per_dow.keys()) == {0, 6}
    assert restored.per_dow[0].median == 110.0
    assert restored.per_dow[6].n_periods == 4


def test_per_dow_dict_rejects_out_of_range_via_runtime_pass() -> None:
    """The model itself does NOT enforce 0–6 range on per-DOW keys —
    enforcement lives at the engine site that BUILDS the dict (US-011)
    after dialect-specific DOW normalisation. This test pins that the
    type-level shape is the unconstrained ``int`` from ``dict[int, ...]``
    so the engine, not the value object, owns the range guarantee.
    """
    out_of_range = MadStats(
        median=1.0,
        mad=1.0,
        n_periods=1,
        per_dow={99: MadDowStats(median=1.0, mad=1.0, n_periods=1)},
    )
    assert out_of_range.per_dow is not None
    assert 99 in out_of_range.per_dow


# --- Fixture round-trip ------------------------------------------------


def test_fixture_validates_via_union() -> None:
    """Every row in :file:`anomaly_stats_v1.json` round-trips through the
    discriminated union."""
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and payload, (
        f"expected non-empty JSON array at {_FIXTURE_PATH}"
    )
    seen_methods: set[str] = set()
    seen_per_dow = False
    for entry in payload:
        value = _ANOMALY_ADAPTER.validate_python(entry)
        seen_methods.add(value.method)
        if value.per_dow is not None:
            seen_per_dow = True

    assert seen_methods == {"mad", "zscore", "percentile", "min_max"}, (
        f"anomaly_stats_v1.json must cover every method; got {seen_methods}"
    )
    assert seen_per_dow, (
        "anomaly_stats_v1.json must include at least one entry with per_dow populated"
    )


def test_fixture_round_trip_byte_stable_via_union() -> None:
    """A round-trip through the union preserves field values for every row.

    Catches a refactor that drops or renames a method-specific field.
    """
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    for entry in payload:
        validated = _ANOMALY_ADAPTER.validate_python(entry)
        re_dumped = json.loads(_ANOMALY_ADAPTER.dump_json(validated).decode("utf-8"))
        # The fixture is authored canonically; the re-dump preserves every
        # key present in the source.
        for key, value in entry.items():
            assert re_dumped[key] == value, (
                f"field {key!r} drifted after round-trip: "
                f"fixture={value!r} round-tripped={re_dumped[key]!r}"
            )


# --- Frozen / immutability ---------------------------------------------


def test_mad_stats_is_frozen() -> None:
    """``frozen=True`` blocks post-construction attribute assignment."""
    stats = MadStats(median=1.0, mad=1.0, n_periods=1)
    with pytest.raises(ValidationError):
        stats.median = 2.0  # type: ignore[misc]


# --- Per-DOW sub-stats classes --------------------------------------


def test_mad_dow_stats_round_trip() -> None:
    original = MadDowStats(median=100.0, mad=2.5, n_periods=4)
    restored = MadDowStats.model_validate_json(original.model_dump_json())
    assert restored == original


def test_zscore_dow_stats_round_trip() -> None:
    original = ZscoreDowStats(mu=10.0, sigma=1.0, n_periods=4)
    restored = ZscoreDowStats.model_validate_json(original.model_dump_json())
    assert restored == original


def test_percentile_dow_stats_round_trip() -> None:
    original = PercentileDowStats(p_lo=5.0, p_hi=95.0, n_periods=4)
    restored = PercentileDowStats.model_validate_json(original.model_dump_json())
    assert restored == original


def test_min_max_dow_stats_round_trip() -> None:
    original = MinMaxDowStats(minimum=0.0, maximum=100.0, n_periods=4)
    restored = MinMaxDowStats.model_validate_json(original.model_dump_json())
    assert restored == original
