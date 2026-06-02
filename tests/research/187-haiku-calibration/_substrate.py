"""Shared substrate for the #187 Haiku-calibration harness (US-005).

Builds the pinned :class:`Model` + :class:`CandidateSchema` whose
artifacts the calibration harness re-grades, and the loader for the
committed Sonnet-baseline verdict sample.

The substrate is *engineered-deterministic* per
:file:`.claude/rules/testing-signal.md` § "Engineered determinism over
snapshot normalisation": the candidate is hand-authored so the engine's
:func:`signalforge.grade.engine._stable_artifact_pairs` emits a fixed,
known set of ``artifact_id`` strings. The committed baseline JSON keys on
exactly those ``(artifact_id, criterion_id)`` pairs, so the only live
variable when the maintainer runs the gate is the Haiku re-grade verdict.

The Sonnet baseline is a **curated sample**, not the raw #179 Phase-B
``grade.jsonl`` dump (which is not committed anywhere in this repo —
``find . -name grade.jsonl`` finds only the drift-detector fixture).
The ``passed`` verdicts in :data:`BASELINE_PATH` are hand-assigned
plausible Sonnet outcomes that span the rubric's calibration space
(clear/strong artifacts pass; vague/weak/redundant artifacts fail), so a
concordant Haiku run reproduces the same verdict distribution. See
:file:`docs/research/187-haiku-calibration.md` for the full provenance
note.
"""

from __future__ import annotations

import json
from pathlib import Path

import signalforge as _sf
from signalforge.draft.models import (
    CandidateColumn,
    CandidateSchema,
    CandidateTestAcceptedValues,
    CandidateTestNotNull,
    CandidateTestUnique,
)
from signalforge.manifest.models import Column, Model
from signalforge.prune.models import PruneResult

# The committed Sonnet-baseline verdict sample lives next to this module.
BASELINE_PATH = Path(__file__).with_name("sonnet_baseline_sample.json")


def build_model() -> Model:
    """Return the pinned manifest :class:`Model` the harness grades.

    Carries exactly the columns referenced by :func:`build_candidate`
    so the candidate's tests resolve against real columns.
    """
    return Model(
        unique_id="model.sf_calib.dim_customers",
        name="dim_customers",
        resource_type="model",
        package_name="sf_calib",
        original_file_path="models/marts/dim_customers.sql",
        path="marts/dim_customers.sql",
        database="sf-calib-proj",
        schema="main",  # type: ignore[call-arg]
        columns={
            "customer_id": Column(name="customer_id"),
            "email": Column(name="email"),
            "status": Column(name="status"),
        },
        raw_code=("select customer_id, email, status from {{ ref('stg_customers') }}"),
    )


def build_candidate() -> CandidateSchema:
    """Return the pinned :class:`CandidateSchema` the harness grades.

    Hand-authored to span the rubric's calibration space:

    * ``customer_id`` — strong, specific description + rationale
      (expected baseline ``passed=True`` on every criterion).
    * ``email`` — adequate description, thin rationale (mixed).
    * ``status`` — deliberately vague description ("a status field")
      and a redundant rationale that restates the description
      (expected baseline ``passed=False`` on clarity / rationale /
      no-redundant).

    The engine's :func:`_stable_artifact_pairs` derives the
    ``artifact_id`` set from this shape; the committed baseline keys on
    exactly those ids. See :func:`expected_artifact_ids`.
    """
    return CandidateSchema(
        name="dim_customers",
        description=(
            "Curated one-row-per-customer dimension joining stg_customers "
            "with stg_customer_status to expose the current lifecycle state "
            "of every customer for analytics."
        ),
        rationale=(
            "Materialises the conformed customer dimension consumed by the "
            "orders and subscriptions fact tables; resolves status at load "
            "time so downstream marts never re-derive lifecycle logic."
        ),
        columns=(
            CandidateColumn(
                name="customer_id",
                description=(
                    "Surrogate primary key uniquely identifying each "
                    "customer. Generated from the source system's natural "
                    "key via dbt_utils.generate_surrogate_key."
                ),
                rationale=(
                    "Used as the join key by every downstream fact table; "
                    "stability across loads is contractually required."
                ),
                tests=(
                    CandidateTestNotNull(
                        column="customer_id",
                        rationale="Primary keys must never be null.",
                    ),
                    CandidateTestUnique(
                        column="customer_id",
                        rationale=(
                            "One row per customer is the table's declared "
                            "grain; duplicates indicate a broken join."
                        ),
                    ),
                ),
            ),
            CandidateColumn(
                name="email",
                description=(
                    "Customer's primary contact email address, lower-cased "
                    "and trimmed at load time."
                ),
                rationale="Contact channel.",
                tests=(),
            ),
            CandidateColumn(
                name="status",
                description="A status field for the customer.",
                rationale="Stores the status of the customer.",
                tests=(
                    CandidateTestAcceptedValues(
                        column="status",
                        values=("active", "churned", "trialing"),
                        rationale=(
                            "The customer lifecycle is a closed set of "
                            "three states; any other value is a data error."
                        ),
                    ),
                ),
            ),
        ),
        tests=(),
    )


def empty_prune_result(model: Model) -> PruneResult:
    """Return an empty :class:`PruneResult` linked to ``model``.

    The no-redundant criterion is the only consumer of dropped tests;
    the curated baseline grades the artifacts standalone, so an empty
    decision tuple is correct here.
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
    :func:`signalforge.grade.engine._stable_artifact_pairs` so the
    harness never hand-enumerates ids (which would drift the moment the
    formatter changes). Importing the private helper is acceptable here:
    this is research-tier test code, and the alternative — duplicating
    the dotted-path grammar — is exactly the drift risk
    :file:`.claude/rules/grade-layer.md` § "_artifact_id_for ... hoist"
    warns against.
    """
    from signalforge.grade.engine import _stable_artifact_pairs

    return [artifact_id for artifact_id, _text in _stable_artifact_pairs(candidate)]


def load_baseline() -> dict[tuple[str, str], bool]:
    """Load the committed Sonnet baseline as ``{(artifact_id, crit): passed}``.

    The on-disk shape is a JSON object with a ``"verdicts"`` array of
    ``{"artifact_id", "criterion_id", "baseline_passed"}`` records.
    """
    raw = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    out: dict[tuple[str, str], bool] = {}
    for record in raw["verdicts"]:
        key = (record["artifact_id"], record["criterion_id"])
        out[key] = bool(record["baseline_passed"])
    return out
