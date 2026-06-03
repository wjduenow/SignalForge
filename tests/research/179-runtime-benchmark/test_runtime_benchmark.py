"""Maintainer-run per-stage runtime benchmark for the #179 retest (SKELETON).

Companion to the "Runtime benchmark retest" story on epic #179. Measures the
per-stage wall-clock of the draft → prune → grade → diff pipeline so the
efficiency improvements that landed since the 2026-05-30 baseline can be
attributed per stage:

* **#186** — grade-layer ``asyncio`` refactor (PR #190, ``f0e315b``).
  Always-on; concurrent ``(artifact × criterion)`` grade calls.
* **#187** — faster grade defaults: Haiku + per-provider fast models
  (PR #193, ``4a70799``). Opt-in (the anthropic grade default stays Sonnet);
  time it by setting ``SF_BENCH_GRADE_MODEL=claude-haiku-4-5``.
* **#188** — bulk-mode shared cached prefix for ``--select`` (PR #195,
  ``d280fc6``). Multi-model batch amortisation — measured via the CLI
  ``--select`` extension (see the module TODO), not this in-process harness.

The 2026-05-30 baseline (``docs/research/179-test-primitive-expansion-retest.md``)
hit the grade-budget ceiling: **17 of 34 doc/rationale grade attempts degraded
with ``grade budget exceeded (300s)``**. That degraded count is the headline
correctness signal here — #186/#187 should drive it toward 0.

A maintainer RUNS this later with a live ``ANTHROPIC_API_KEY``::

    pytest -m anthropic --no-cov -s \\
        tests/research/179-runtime-benchmark/test_runtime_benchmark.py

The ``-s`` is required — the per-stage timing table is printed (not asserted),
and the maintainer transcribes it into the "Result (maintainer-filled)" tables
of :file:`docs/research/179-runtime-benchmark.md`.

## The before/after A/B is a TWO-checkout protocol

Wall-clock is machine- and network-dependent, so the only honest A/B re-runs the
OLD revision on the SAME machine rather than comparing against the preserved
2026-05-30 sidecars:

1. **Before:** ``git checkout 90af28b`` (the ``#179`` empirical-retest-writeup
   commit — the last commit before #186 landed), then run this harness. Record
   the per-stage table.
2. **After:** ``git checkout dev`` (currently ``d280fc6``), run again. Record.
3. Diff the two tables; the grade-stage delta is the #186/#187 win.

This skeleton harness lives ONLY on the after-revision branch — for the before
run, copy this file (and ``_substrate.py``) onto the ``90af28b`` checkout, or
cherry-pick the directory. The substrate uses only public + long-stable APIs so
it imports on both revisions.

## Why ``prune`` is disabled / not timed

The baseline ran ``prune.enabled: false`` (no Snowflake auth). The harness
mirrors that: the prune stage does no warehouse work and its timing is ~0. Keep
it disabled for the apples-to-apples grade-stage A/B — lifting it (real
Snowflake) changes what's being measured.

## TODO(maintainer): the #188 ``--select`` batch measurement

#188 amortises the shared cached prefix across models in ONE
``signalforge generate --select <expr>`` process. That is a CLI / multi-process
concern, not an in-process orchestrator call, so it is NOT timed here. Measure it
separately with the real intuit_airflow slice::

    time signalforge generate --select 'path:models/reporting/*' \\
        --project-dir <intuit>/plugins/dbt --profiles-dir /tmp/sf-demo-profiles

against the equivalent shell-loop (one process per model) on both revisions, and
record the total batch wall-clock + per-model ``[i/N]`` timings in the doc.

Gating — belt-and-suspenders, mirroring the #187 calibration harness:

* ``pytestmark = pytest.mark.anthropic`` — excluded from default CI via
  :file:`pyproject.toml`'s ``addopts = "... -m 'not anthropic ...'"``. The module
  is imported at collection time (hence the lazy ``_substrate`` import below) but
  the test body never runs by default.
* A runtime ``pytest.skip(...)`` when ``ANTHROPIC_API_KEY`` is unset — a
  maintainer running ``pytest -m anthropic`` without a key sees a clean
  skip-with-reason, not a noisy auth failure.

**This is a SKELETON.** The timing scaffold, degraded-grade counting, and table
output are complete and runnable against the inlined substrate model. The real
measurement requires (a) swapping ``_substrate.build_models`` to the intuit_airflow
slice and (b) the two-checkout A/B above. Acceptance thresholds are deliberately
NOT asserted — a benchmark records numbers; it does not gate a build.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.anthropic

# Stages timed, in pipeline order. ``prune`` is included so the table shape is
# stable across the with-warehouse future; it reads ~0 while prune is disabled.
_STAGES = ("draft", "prune", "grade", "diff")


def _skip_reason() -> str | None:
    """Return a clear skip-reason string when the live key is missing."""
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "ANTHROPIC_API_KEY not set (benchmark issues real Anthropic draft + grade calls)"
    return None


@contextmanager
def _timed(stage: str, sink: dict[str, float]):
    """Accumulate wall-clock seconds for ``stage`` into ``sink``.

    Uses ``time.perf_counter`` (monotonic, highest resolution). Accumulates
    (``+=``) so the per-model loop sums each stage across models into one total.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        sink[stage] = sink.get(stage, 0.0) + (time.perf_counter() - start)


def _count_grade_degradations(report) -> tuple[int, int, int]:
    """Return ``(comparable, degraded_total, degraded_budget)`` for a GradingReport.

    * ``degraded_total`` — results with ``score is None`` (could not be positively
      evaluated; DEC-015 of #7).
    * ``degraded_budget`` — the subset whose ``reasoning`` names a budget timeout
      (the 2026-05-30 baseline's 17/34 failure mode). This is the number #186/#187
      must drive down.
    """
    comparable = degraded_total = degraded_budget = 0
    for result in report.results:
        if result.score is None:
            degraded_total += 1
            if "budget" in (result.reasoning or "").lower():
                degraded_budget += 1
        else:
            comparable += 1
    return comparable, degraded_total, degraded_budget


def test_pipeline_runtime_benchmark(tmp_path: Path) -> None:
    """Time draft → (prune-disabled) → grade → diff per stage; print the table.

    Records nothing as an assertion threshold (a benchmark measures, it does not
    gate). The two hard assertions are sanity floors only: the pipeline produced
    a real candidate and a real grading report, so a silently-empty run can't be
    mistaken for a fast one.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # Lazy sibling import (after the skip) — keeps ``sys.path`` un-mutated at
    # collection time when this test is deselected by ``-m 'not anthropic'``.
    sys.path.insert(0, str(Path(__file__).parent))
    from _substrate import (  # noqa: PLC0415 — intentional lazy import (see above)
        build_manifest,
        build_models,
        diff_config,
        draft_config,
        empty_prune_result,
        grade_config,
        null_adapter,
        schema_only_policy,
    )

    from signalforge.draft import draft_schema
    from signalforge.grade import grade_artifacts
    from signalforge.diff import render_diff

    # The #187 opt-in lever: unset → shipped anthropic default (Sonnet, captures
    # #186 only); ``claude-haiku-4-5`` → the #187 fast-grade path.
    grade_model_override = os.environ.get("SF_BENCH_GRADE_MODEL", "").strip() or None

    models = build_models()
    manifest = build_manifest(models)
    adapter = null_adapter()

    timings: dict[str, float] = {stage: 0.0 for stage in _STAGES}
    total_artifacts = 0
    total_comparable = 0
    total_degraded = 0
    total_degraded_budget = 0
    wall_start = time.perf_counter()

    for index, model in enumerate(models):
        # Per-model audit paths under tmp_path so nothing pollutes the repo
        # (mirrors the e2e-gated-tests tmp_path isolation convention).
        model_dir = tmp_path / f"model_{index}"
        model_dir.mkdir()
        audit_path = model_dir / "audit.jsonl"
        policy = schema_only_policy(audit_path)

        # --- draft (live LLM) ---------------------------------------------
        with _timed("draft", timings):
            outcome = draft_schema(
                model, adapter, policy, manifest, config=draft_config()
            )
        candidate = outcome.candidate

        # --- prune (disabled — empty result, no warehouse) ----------------
        with _timed("prune", timings):
            prune_result = empty_prune_result(model)

        # --- grade (live LLM; the stage the efficiency work targets) ------
        with _timed("grade", timings):
            report = grade_artifacts(
                model,
                candidate,
                prune_result,
                config=grade_config(grade_model_override),
                audit_path=model_dir / "grade.jsonl",
                sidecar_path=model_dir / "grade.json",
                project_dir=model_dir,
            )

        # --- diff ----------------------------------------------------------
        with _timed("diff", timings):
            render_diff(
                model,
                candidate,
                prune_result,
                grading_report=report,
                config=diff_config(),
                sidecar_path=model_dir / "diff.json",
                project_dir=model_dir,
            )

        comparable, degraded, degraded_budget = _count_grade_degradations(report)
        total_artifacts += len(report.results)
        total_comparable += comparable
        total_degraded += degraded
        total_degraded_budget += degraded_budget

    wall_total = time.perf_counter() - wall_start

    # --- human-readable per-stage table for the maintainer writeup --------
    grade_label = grade_model_override or "(anthropic default: sonnet)"
    print("\n=== #179 pipeline runtime benchmark ===")
    print(f"signalforge version    : {__import__('signalforge').__version__}")
    print(f"models timed           : {len(models)}")
    print(f"grade model            : {grade_label}")
    print(f"grade artifacts graded : {total_artifacts}")
    print("--- per-stage wall-clock (summed across models) ---")
    for stage in _STAGES:
        pct = (timings[stage] / wall_total * 100.0) if wall_total else 0.0
        note = " (disabled)" if stage == "prune" else ""
        print(f"  {stage:<6}: {timings[stage]:8.2f}s  ({pct:4.1f}%){note}")
    print(f"  {'TOTAL':<6}: {wall_total:8.2f}s")
    print("--- grade degradation (the 2026-05-30 baseline was 17/34 budget-exceeded) ---")
    print(f"  comparable (scored)        : {total_comparable}")
    print(f"  degraded (score=None)      : {total_degraded}")
    print(f"  degraded — budget exceeded : {total_degraded_budget}")
    print("=======================================")

    # Sanity floors ONLY — a benchmark records numbers, it does not gate a build.
    # These two assertions just prevent a silently-empty run from masquerading as
    # a fast one.
    assert total_artifacts >= 1, "grade produced no results — the run is not measurable"
    assert wall_total > 0.0, "wall-clock did not advance — timing scaffold is broken"
