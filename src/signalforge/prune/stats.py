"""Typed per-decision numerical state for anomaly-style prune tests (US-001).

Establishes the :data:`AnomalyTestStats` discriminated union — a tagged
union of four method-specific Pydantic models that carry the per-decision
numerical state the prune engine extracts from its stats query for the
``row_count_anomaly_by_period`` variant (issue #171). Downstream consumers
— the grader (`grade-layer.md`) and the diff renderer / sidecar — read
these stats via the cross-stage handoff that lands on
:class:`signalforge.prune.PruneDecision` in US-012.

Design commitments operationalised here:

* **DEC-003** — Cross-stage numerical state is a typed value-object, not
  a loose ``dict[str, Any]``. Future primitives that need cross-stage
  numerical state inherit the same seam.
* **DEC-005** — Tagged union via Pydantic's discriminated-union pattern:
  ``Annotated[Mad | Zscore | Percentile | MinMax, Field(discriminator="method")]``.
  The ``method`` field is the canonical dispatcher; mismatched
  method/subclass raises :class:`pydantic.ValidationError` at construction.
* **DOW convention.** When ``seasonality="dow"``, the per-DOW sub-stats
  are keyed by ``int`` 0–6 following the POSIX convention
  (``date.isoweekday() - 1`` or ``date.weekday()``: Monday=0 … Sunday=6).
  The dialect-specific DOW expression (``EXTRACT(DOW FROM date)``) is
  normalised to this POSIX shape before insertion into the dict; see
  :data:`signalforge.warehouse.models.Dialect.dow_sunday_index`.
* **Read-back semantics** — ``frozen=True`` + ``extra="ignore"`` per
  ``.claude/rules/safety-layer.md`` DEC-015. Paired ``Strict<X>`` mirrors
  with ``extra="forbid"`` live in :mod:`tests.prune.test_drift_detector`,
  validated against :file:`tests/fixtures/prune/anomaly_stats_v1.json`.

This module declares only value-object shapes; the SQL stats query that
populates them lives in :mod:`signalforge.prune.compiler` (US-008) and
the engine-side wiring lands in :mod:`signalforge.prune.engine` (US-011).

Reference: ``plans/super/171-row-count-anomaly.md`` § US-001 / DEC-005.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

_BASE_CONFIG = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


# --- Per-DOW sub-stats shapes ----------------------------------------------
# Each method-specific stats class may carry a ``per_dow`` dict keyed by
# the POSIX dow integer (0–6). The values are the same method's stat triple
# narrowed to that DOW bucket. Kept as separate classes (not a generic
# parameter) so each one's ``extra="forbid"`` strict mirror gates the
# field set independently.


class MadDowStats(BaseModel):
    """Per-DOW MAD-method stats (median + median-absolute-deviation + n)."""

    model_config = _BASE_CONFIG

    median: float
    mad: float
    n_periods: int


class ZscoreDowStats(BaseModel):
    """Per-DOW z-score-method stats (mean + standard deviation + n)."""

    model_config = _BASE_CONFIG

    mu: float
    sigma: float
    n_periods: int


class PercentileDowStats(BaseModel):
    """Per-DOW percentile-method stats (lower + upper percentile + n).

    ``p_lo`` and ``p_hi`` are the *computed bucket bounds* (in row-count
    space), NOT the percentile parameters (e.g. 0.1 / 0.9) that produced
    them. The parameters live on the candidate test; this stats class
    carries the materialised bounds for the grader to score against.
    """

    model_config = _BASE_CONFIG

    p_lo: float
    p_hi: float
    n_periods: int


class MinMaxDowStats(BaseModel):
    """Per-DOW min-max-method stats (minimum + maximum + n)."""

    model_config = _BASE_CONFIG

    minimum: float
    maximum: float
    n_periods: int


# --- Method-tagged stats classes -------------------------------------------


class MadStats(BaseModel):
    """MAD-method stats: median + median-absolute-deviation + period count.

    Method tag: ``"mad"``. The MAD (median absolute deviation) is the
    robust replacement for raw standard deviation; the band is typically
    ``median ± threshold * (MAD / 0.6745)`` per Iglewicz & Hoaglin.

    When ``per_dow`` is populated, the engine ran the stats query
    partitioned by day-of-week and the dict carries one entry per
    observed DOW (keys 0–6 POSIX convention).
    """

    model_config = _BASE_CONFIG

    method: Literal["mad"] = "mad"
    median: float
    mad: float
    n_periods: int
    per_dow: dict[int, MadDowStats] | None = None


class ZscoreStats(BaseModel):
    """Z-score-method stats: mean + standard deviation + period count.

    Method tag: ``"zscore"``. The band is typically
    ``mu ± threshold * sigma``.
    """

    model_config = _BASE_CONFIG

    method: Literal["zscore"] = "zscore"
    mu: float
    sigma: float
    n_periods: int
    per_dow: dict[int, ZscoreDowStats] | None = None


class PercentileStats(BaseModel):
    """Percentile-method stats: computed lower + upper bounds + period count.

    Method tag: ``"percentile"``. ``p_lo`` and ``p_hi`` are the
    materialised bucket bounds in row-count space (not the percentile
    parameters that produced them; see :class:`PercentileDowStats`).
    """

    model_config = _BASE_CONFIG

    method: Literal["percentile"] = "percentile"
    p_lo: float
    p_hi: float
    n_periods: int
    per_dow: dict[int, PercentileDowStats] | None = None


class MinMaxStats(BaseModel):
    """Min-max-method stats: minimum + maximum + period count.

    Method tag: ``"min_max"``. The band is the literal observed range
    ``[minimum, maximum]``; no ``threshold`` knob applies.
    """

    model_config = _BASE_CONFIG

    method: Literal["min_max"] = "min_max"
    minimum: float
    maximum: float
    n_periods: int
    per_dow: dict[int, MinMaxDowStats] | None = None


# --- Discriminated union ---------------------------------------------------


AnomalyTestStats = Annotated[
    MadStats | ZscoreStats | PercentileStats | MinMaxStats,
    Field(discriminator="method"),
]
"""Discriminated union over the four method-specific stats classes (DEC-005).

The discriminator field is ``method``; its value space is the closed
:class:`Literal` union of the four method strings. Unknown ``method``
values raise :class:`pydantic.ValidationError` at construction — adding
a fifth statistical method requires extending this union and the
``Literal`` on each variant class. The drift detector
(:mod:`tests.prune.test_drift_detector`) catches the case where a
fixture grows a new method without the model.
"""


__all__ = (
    "AnomalyTestStats",
    "MadDowStats",
    "MadStats",
    "MinMaxDowStats",
    "MinMaxStats",
    "PercentileDowStats",
    "PercentileStats",
    "ZscoreDowStats",
    "ZscoreStats",
)
