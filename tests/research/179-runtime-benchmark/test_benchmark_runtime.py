"""Unit tests for the #179 runtime-benchmark harness's pure-stdlib parsing.

The benchmark harness :file:`benchmark_runtime.py` is a standalone script: its
*live* path shells out to a real ``signalforge generate`` against a real
Anthropic API key (operator-only, metered — never run in CI). But its
sidecar-parsing helpers are pure stdlib over an in-memory ``grade.json`` dict,
so the non-live half is unit-testable WITHOUT any network, key, or subprocess.

These tests pin the #202 wiring the harness gained for the grade-to-completion
retest (SignalForge-0z5.9):

* ``_grade_degradations`` / ``_is_budget_degrade`` — budget classification that
  prefers the #202 ``degrade_reason_type`` discriminator (US-001) and falls
  back to prose for pre-#202 sidecars.
* ``_degrade_reason_counts`` — the per-reason split (the #202 lens: 0 transient
  / ``GradeLLMError`` is the retest target).
* ``_ungraded_pairs`` — the ``(artifact_id, criterion_id)`` list the
  ``--require-complete`` PASS condition names.
* ``_aggregate_complete`` — the top-level completeness flag (the OTHER half of
  the PASS condition), with a derive-from-results fallback for old sidecars.

The harness lives outside the importable package tree (it must stay
``pip install signalforge-dbt``-only for the prod arm). We load it by file path
via :mod:`importlib.util` so collection does NOT mutate ``sys.path`` — mirroring
the lazy-import caution in the sibling ``187-haiku-calibration`` tests.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit


def _load_harness() -> ModuleType:
    """Import ``benchmark_runtime.py`` by path without touching ``sys.path``."""
    path = Path(__file__).parent / "benchmark_runtime.py"
    spec = importlib.util.spec_from_file_location("_benchmark_runtime_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_HARNESS = _load_harness()


def _result(
    *,
    score: float | None,
    artifact_id: str = "column.email.description",
    criterion_id: str = "accuracy",
    reasoning: str = "",
    degrade_reason_type: str | None = None,
) -> dict:
    """Build one ``results[]`` entry shaped like a real ``grade.json`` record."""
    rec: dict = {
        "artifact_id": artifact_id,
        "criterion_id": criterion_id,
        "score": score,
        "passed": score is not None and score >= 0.5,
        "reasoning": reasoning,
    }
    if degrade_reason_type is not None:
        rec["degrade_reason_type"] = degrade_reason_type
    return rec


# --- _grade_degradations / _is_budget_degrade --------------------------------


def test_grade_degradations_counts_scored_and_null() -> None:
    data = {
        "results": [
            _result(score=0.9),
            _result(score=0.1),
            _result(score=None, degrade_reason_type="transient"),
        ]
    }
    comparable, degraded_total, degraded_budget = _HARNESS._grade_degradations(data)
    assert comparable == 2
    assert degraded_total == 1
    assert degraded_budget == 0


def test_budget_degrade_prefers_structured_discriminator() -> None:
    # #202: classify on degrade_reason_type, NOT the prose — a transient pair
    # whose reasoning happens to mention "budget" must NOT count as budget.
    transient = _result(
        score=None,
        degrade_reason_type="transient",
        reasoning="exhausted retries; unrelated budget word in text",
    )
    budget = _result(score=None, degrade_reason_type="budget", reasoning="grade budget exceeded")
    assert _HARNESS._is_budget_degrade(transient) is False
    assert _HARNESS._is_budget_degrade(budget) is True


def test_budget_degrade_prose_fallback_for_pre_202_sidecar() -> None:
    # Prod 0.5.0 emits no discriminator — fall back to the reasoning substring
    # so the prod arm of the table still classifies budget exceedances.
    legacy_budget = _result(score=None, reasoning="grade budget exceeded (300s)")
    legacy_other = _result(score=None, reasoning="parser returned malformed JSON")
    assert "degrade_reason_type" not in legacy_budget
    assert _HARNESS._is_budget_degrade(legacy_budget) is True
    assert _HARNESS._is_budget_degrade(legacy_other) is False


# --- _degrade_reason_counts --------------------------------------------------


def test_degrade_reason_counts_splits_by_discriminator() -> None:
    data = {
        "results": [
            _result(score=0.9),  # scored — excluded
            _result(score=None, degrade_reason_type="transient"),
            _result(score=None, degrade_reason_type="transient"),
            _result(score=None, degrade_reason_type="budget"),
            _result(score=None, degrade_reason_type="ceiling"),
            _result(score=None),  # no discriminator → unclassified
        ]
    }
    counts = _HARNESS._degrade_reason_counts(data)
    assert counts == {"transient": 2, "budget": 1, "ceiling": 1, "unclassified": 1}


def test_degrade_reason_counts_zero_transient_is_the_pass_signal() -> None:
    # The #202 retest target: a fully-scored corpus has no degrade reasons.
    data = {"results": [_result(score=0.8), _result(score=0.6)]}
    assert _HARNESS._degrade_reason_counts(data) == {}


# --- _ungraded_pairs ---------------------------------------------------------


def test_ungraded_pairs_names_every_null_score() -> None:
    data = {
        "results": [
            _result(score=0.9, artifact_id="column.a.description", criterion_id="accuracy"),
            _result(
                score=None,
                artifact_id="test.column.user_id.not_null",
                criterion_id="signal",
                degrade_reason_type="transient",
            ),
            _result(
                score=None,
                artifact_id="column.b.description",
                criterion_id="clarity",
                degrade_reason_type="budget",
            ),
        ]
    }
    assert _HARNESS._ungraded_pairs(data) == [
        ("test.column.user_id.not_null", "signal"),
        ("column.b.description", "clarity"),
    ]


def test_ungraded_pairs_empty_when_complete() -> None:
    data = {"results": [_result(score=0.9), _result(score=0.5)]}
    assert _HARNESS._ungraded_pairs(data) == []


# --- _aggregate_complete -----------------------------------------------------


def test_aggregate_complete_reads_top_level_flag() -> None:
    assert _HARNESS._aggregate_complete({"aggregate_complete": True, "results": []}) is True
    assert _HARNESS._aggregate_complete({"aggregate_complete": False, "results": []}) is False


def test_aggregate_complete_derives_when_flag_absent() -> None:
    # Pre-#198 sidecar without the computed field → derive from per-result score.
    complete = {"results": [_result(score=0.9), _result(score=0.5)]}
    partial = {"results": [_result(score=0.9), _result(score=None)]}
    assert _HARNESS._aggregate_complete(complete) is True
    assert _HARNESS._aggregate_complete(partial) is False


def test_aggregate_complete_none_when_underivable() -> None:
    assert _HARNESS._aggregate_complete({}) is None
