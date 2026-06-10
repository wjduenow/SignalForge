"""Maintainer-run Haiku-vs-Sonnet grade-concordance gate (#187 US-005).

The #187 plan ships ``claude-haiku-4-5`` as the new grade-default SKU
(US-001..US-003) behind an empirical gate: **does Haiku grade rubric
artifacts concordantly with the prior Sonnet baseline?** The decision
rule is **≥ 85% per-criterion pass/fail agreement** over the pinned
sample (DEC of the #187 plan; mirrors the concordance bar the epic set).

This module BUILDS that gate. A maintainer RUNS it later with a live
``ANTHROPIC_API_KEY``::

    pytest -m anthropic --no-cov \\
        tests/research/187-haiku-calibration/test_haiku_calibration.py

Then transcribes the printed agreement rate into the "Result
(maintainer-filled)" section of
:file:`docs/research/187-haiku-calibration.md`.

Gating — belt-and-suspenders, mirroring
:file:`tests/grade/test_smoke_real_api.py`:

* ``pytestmark = pytest.mark.anthropic`` — the existing ``anthropic``
  marker, excluded from the default ``pytest`` run via
  :file:`pyproject.toml`'s
  ``addopts = "... -m 'not anthropic ...'"``. Default CI **deselects**
  this test (the module is still imported during collection — hence the
  lazy `_substrate` import below — but the test body never runs).
* A runtime ``pytest.skip(...)`` when ``ANTHROPIC_API_KEY`` is unset (or
  blank) — so a maintainer who runs ``pytest -m anthropic`` without a
  key sees a clean skip-with-reason, not a noisy auth failure.

The substrate (real model + frozen drafted candidate + live Sonnet baseline)
lives in :mod:`tests.research._substrate`; see
:file:`docs/research/187-haiku-calibration.md` for provenance and the recorded
result (Haiku fell short of the 85% bar — 81.8% / 77.0% on two runs).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from signalforge.grade import grade_artifacts
from signalforge.grade.config import GradeConfig

# NOTE: the `_substrate` sibling import is intentionally LAZY (inside the test,
# after the skip) rather than module-level. The harness lives outside the
# importable package tree, so reaching `_substrate` needs a `sys.path` insert —
# doing that at import time would mutate `sys.path` during pytest collection
# even though this test is deselected by `-m 'not anthropic'` (Copilot PR
# review). Keeping it lazy means importing this module has no global side
# effects.

pytestmark = pytest.mark.anthropic

# The decision rule locked by the #187 plan: Haiku must agree with the
# Sonnet baseline on at least this fraction of per-criterion pass/fail
# verdicts for the default to ship.
_CONCORDANCE_THRESHOLD = 0.85


def _skip_reason() -> str | None:
    """Return a clear skip-reason string when the live key is missing."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "ANTHROPIC_API_KEY not set"
    return None


def test_haiku_grade_concordance_vs_sonnet_baseline(tmp_path: Path) -> None:
    """Re-grade the pinned sample with the Haiku opt-in; assert ≥ 85% concordance.

    Builds the Haiku opt-in :class:`GradeConfig(model="claude-haiku-4-5")`
    (the anthropic *default* is Sonnet post-calibration — this gate is why;
    ``max_output_tokens=1024``), grades the pinned
    candidate over the default four-criterion rubric, joins each
    :class:`GradingResult` to the committed Sonnet baseline by
    ``(artifact_id, criterion_id)``, computes per-criterion pass/fail
    agreement, prints the breakdown for the maintainer writeup, and
    asserts the rate clears :data:`_CONCORDANCE_THRESHOLD`.

    Degraded results (``score is None`` — DEC-015 of #7) are excluded
    from the agreement denominator and reported separately: a degraded
    pair could not be positively evaluated, so it is neither a
    concordance nor a discordance.
    """
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    # Lazy sibling import (after the skip) — keeps `sys.path` un-mutated at
    # collection time when this test is deselected (Copilot PR review).
    sys.path.insert(0, str(Path(__file__).parent))
    from _substrate import (
        build_candidate,
        build_model,
        empty_prune_result,
        expected_artifact_ids,
        load_baseline,
    )

    model = build_model()
    candidate = build_candidate()
    prune_result = empty_prune_result(model)
    baseline = load_baseline()

    # The Haiku OPT-IN (post-calibration, the anthropic default is Sonnet —
    # this gate is exactly why). We explicitly select claude-haiku-4-5 to
    # measure whether the opt-in grades concordantly with the Sonnet default;
    # the recorded result is that it does NOT (~77-82% < 85%), which is why
    # Haiku stays opt-in. max_output_tokens defaults to 1024.
    config = GradeConfig(model="claude-haiku-4-5")
    assert config.model == "claude-haiku-4-5"
    assert config.max_output_tokens == 1024
    # Sanity: confirm the anthropic DEFAULT is Sonnet (Haiku is opt-in only).
    assert GradeConfig().model == "claude-sonnet-4-6"

    # Sanity: the committed baseline must cover every artifact_id the
    # engine will grade (engineered determinism — no silent gaps).
    engine_ids = set(expected_artifact_ids(candidate))
    baseline_ids = {artifact_id for (artifact_id, _crit) in baseline}
    missing = engine_ids - baseline_ids
    assert not missing, f"baseline sample is missing artifact_ids: {sorted(missing)}"

    audit_path = tmp_path / "grade.jsonl"
    sidecar_path = tmp_path / "grade.json"

    report = grade_artifacts(
        model,
        candidate,
        prune_result,
        config=config,
        audit_path=audit_path,
        sidecar_path=sidecar_path,
        project_dir=tmp_path,
    )

    agreements = 0
    comparable = 0
    degraded = 0
    discordances: list[tuple[str, str, bool, bool]] = []

    for result in report.results:
        key = (result.artifact_id, result.criterion_id)
        if key not in baseline:
            # Should not happen given the coverage assertion above, but
            # never silently fold an unmatched verdict into the rate.
            continue
        if result.score is None:
            degraded += 1
            continue
        comparable += 1
        baseline_passed = baseline[key]
        if result.passed == baseline_passed:
            agreements += 1
        else:
            discordances.append(
                (result.artifact_id, result.criterion_id, baseline_passed, result.passed)
            )

    rate = agreements / comparable if comparable else 0.0

    # Human-readable breakdown for the maintainer to paste into the
    # writeup's "Result (maintainer-filled)" section. Printed regardless
    # of pass/fail so a sub-threshold run still surfaces the discordances.
    print("\n=== #187 Haiku-vs-Sonnet grade concordance ===")
    print(f"grade model           : {config.model}")
    print(f"max_output_tokens     : {config.max_output_tokens}")
    print(f"comparable verdicts   : {comparable}")
    print(f"agreements            : {agreements}")
    print(f"degraded (score=None) : {degraded}")
    print(f"agreement rate        : {rate:.1%}")
    print(f"decision threshold    : {_CONCORDANCE_THRESHOLD:.0%}")
    if discordances:
        print("discordances (artifact, criterion, sonnet_passed, haiku_passed):")
        for artifact_id, crit, sonnet_p, haiku_p in discordances:
            print(f"  - {artifact_id} / {crit}: sonnet={sonnet_p} haiku={haiku_p}")
    print("================================================")

    # The aggregate must be complete enough to be a meaningful gate. Excluding
    # degraded (score=None) pairs from the denominator is correct (a pair that
    # could not be evaluated is neither concordance nor discordance), but a run
    # where degraded pairs DOMINATE has too small/biased a comparable set to
    # trust the percentage — a high rate over a handful of survivors is not a
    # real ≥85% signal. Require the comparable set to be the majority.
    assert comparable >= 1, "no comparable verdicts — every pair degraded"
    assert comparable >= degraded, (
        f"too many degraded verdicts ({degraded}) vs comparable ({comparable}); "
        "the sample/run is too noisy to trust the concordance number — "
        "raise max_output_tokens or investigate the degradations before judging the gate"
    )

    assert rate >= _CONCORDANCE_THRESHOLD, (
        f"Haiku concordance {rate:.1%} below the {_CONCORDANCE_THRESHOLD:.0%} "
        f"decision rule ({agreements}/{comparable} agreements). "
        "The Haiku opt-in does NOT grade concordantly with the Sonnet "
        "baseline on this sample; record the discordances in "
        "docs/research/187-haiku-calibration.md and reconsider the default."
    )
