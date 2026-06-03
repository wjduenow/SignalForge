"""Shared substrate for the #187 Haiku-calibration harness (US-005).

Provides the pinned :class:`Model` + :class:`CandidateSchema` whose artifacts
the calibration harness re-grades, and the loader for the committed Sonnet
baseline verdicts.

**Real artifacts, real Sonnet baseline.** The original US-005 harness shipped a
hand-authored candidate + hand-assigned Sonnet verdicts. This version uses a
real model from the ``intuit_airflow`` repo
(``plugins/dbt/models/analytical/calendar_hour.sql``):

* :func:`build_model` constructs that model **deterministically** (its SQL +
  columns are inlined here, so the capture is reproducible without that repo).
* :func:`build_candidate` loads ``real_candidate.json`` — the artifacts the
  production drafter (``claude-sonnet-4-6``, schema-only) emitted for the model,
  frozen by :mod:`capture_sonnet_baseline`. Freezing makes the LLM-drafted
  artifacts deterministic so the only live variable when the maintainer runs the
  gate is the Haiku re-grade verdict.
* ``sonnet_baseline_sample.json`` holds **live** ``claude-sonnet-4-6`` grades of
  those frozen artifacts (NOT hand-authored). Regenerate both files via
  ``capture_sonnet_baseline.py``.

The engine's :func:`signalforge.grade.engine._stable_artifact_pairs` derives the
``artifact_id`` set from the frozen candidate; the committed baseline keys on
exactly those ``(artifact_id, criterion_id)`` pairs. See
:file:`docs/research/187-haiku-calibration.md` for the full provenance note.
"""

from __future__ import annotations

import json
from pathlib import Path

import signalforge as _sf
from signalforge.draft.models import CandidateSchema
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult

# The frozen drafted candidate + the committed Sonnet-baseline verdicts live
# next to this module (written by capture_sonnet_baseline.py).
CANDIDATE_PATH = Path(__file__).with_name("real_candidate.json")
BASELINE_PATH = Path(__file__).with_name("sonnet_baseline_sample.json")

# Inlined verbatim from intuit_airflow plugins/dbt/models/analytical/calendar_hour.sql
# (HEAD at capture time) so the capture reproduces without that repo present.
_CALENDAR_HOUR_SQL = """with final as (
    select
        cd.date_id as date_id,
        h.hour_of_day,
        timestampadd(hour, h.hour_of_day, cd.date_id) as date_hour,
        dateadd(hour, h.hour_of_day, date(cd.prior_year_cal_dt, 'yyyymmdd')) as prior_year_date_hour
    from {{ ref('calendar_date') }} cd
    cross join (
        select
            seq4() as hour_of_day
        from table(generator(rowcount=>24))) h
    where cd.date_id > '2018-01-31'
)

{{ audit_columns('final') }}"""


def build_model() -> Model:
    """Return the pinned manifest :class:`Model` — the real intuit_airflow
    ``calendar_hour`` hour-grain time dimension.

    Constructed deterministically (no LLM, no warehouse) so the drafter and
    grader have a stable target. Columns are the four business columns the
    model's final SELECT projects (the ``audit_columns`` macro injects audit
    columns downstream; those are out of scope for calibration).
    """
    return Model(
        unique_id="model.bi.calendar_hour",
        name="calendar_hour",
        resource_type="model",
        package_name="bi",
        original_file_path="models/analytical/calendar_hour.sql",
        path="analytical/calendar_hour.sql",
        database="intuit-bi",
        schema="analytical",  # type: ignore[call-arg]
        columns={
            "date_id": Column(name="date_id", data_type="DATE"),
            "hour_of_day": Column(name="hour_of_day", data_type="NUMBER"),
            "date_hour": Column(name="date_hour", data_type="TIMESTAMP"),
            "prior_year_date_hour": Column(name="prior_year_date_hour", data_type="TIMESTAMP"),
        },
        raw_code=_CALENDAR_HOUR_SQL,
    )


def build_candidate() -> CandidateSchema:
    """Return the frozen drafted :class:`CandidateSchema` the harness grades.

    Loads ``real_candidate.json`` — the artifacts the production drafter
    (``claude-sonnet-4-6``, schema-only) emitted for :func:`build_model`,
    frozen by :mod:`capture_sonnet_baseline`. The load is lazy (inside the
    function) so importing this module never requires the file; the only caller
    is the gated harness, which is deselected from the default suite.
    """
    if not CANDIDATE_PATH.exists():
        raise FileNotFoundError(
            f"{CANDIDATE_PATH.name} not found — run capture_sonnet_baseline.py "
            "with an ANTHROPIC_API_KEY to draft + freeze the real artifacts first."
        )
    return CandidateSchema.model_validate_json(CANDIDATE_PATH.read_text(encoding="utf-8"))


def empty_prune_result(model: Model) -> PruneResult:
    """Return an empty :class:`PruneResult` linked to ``model``.

    The no-redundant criterion is the only consumer of dropped tests; the
    calibration grades the artifacts standalone, so an empty decision tuple is
    correct here.
    """
    return PruneResult(
        model_unique_id=model.unique_id,
        decisions=(),
        elapsed_ms=0,
        signalforge_version=_sf.__version__,
    )


def expected_artifact_ids(candidate: CandidateSchema) -> list[str]:
    """Return the engine's canonical artifact_id set for ``candidate``.

    Thin wrapper over the engine's own
    :func:`signalforge.grade.engine._stable_artifact_pairs` so the harness never
    hand-enumerates ids (which would drift the moment the formatter changes).
    Importing the private helper is acceptable here: this is research-tier test
    code, and the alternative — duplicating the dotted-path grammar — is exactly
    the drift risk :file:`.claude/rules/grade-layer.md` § "_artifact_id_for ...
    hoist" warns against.
    """
    from signalforge.grade.engine import _stable_artifact_pairs

    return [artifact_id for artifact_id, _text in _stable_artifact_pairs(candidate)]


def load_baseline() -> dict[tuple[str, str], bool]:
    """Load the committed Sonnet baseline as ``{(artifact_id, crit): passed}``.

    The on-disk shape is a JSON object with a ``"verdicts"`` array of
    ``{"artifact_id", "criterion_id", "baseline_passed"}`` records (extra keys
    such as ``baseline_score`` are ignored).
    """
    raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], bool] = {}
    for record in raw["verdicts"]:
        key = (record["artifact_id"], record["criterion_id"])
        out[key] = bool(record["baseline_passed"])
    return out
