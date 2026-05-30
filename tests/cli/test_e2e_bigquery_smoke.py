"""End-to-end smoke test against real BigQuery + parametrized grader provider.

Issue #10 / US-005 (original Anthropic baseline) extended by issue #155 /
US-007 to parametrize over ``grade.provider ∈ {"anthropic", "openai",
"gemini"}``. Pins the v0.1 promise the rest of the suite cannot
exercise: ``signalforge generate <model>`` against a real dbt project
talking to a real warehouse and a real LLM produces a coherent
``diff.json`` whose kept / dropped / flagged tallies match the
fixture's expected shape (DEC-002, DEC-009, DEC-024, DEC-029 of
``plans/super/10-e2e-bigquery-smoke.md`` — Path A pivots the fixture
to source-as-model so the always-passes drop comes from natural NOT
NULL columns in bikeshare data rather than engineered literal columns).

Issue #155 motivation (DEC-003 / DEC-011 / DEC-012 of
``plans/super/155-gemini-truncation-e2e-gap.md``): the in-isolation
grade smokes (``tests/grade/test_*_grade_live.py``) never see the
diff-sidecar ``evidence`` / ``reasoning`` rendering cascade with a
non-Anthropic judge. Parametrizing this BQ smoke over ``grade.provider``
covers the cross-provider diff-sidecar rendering contract. The drafter
stays Anthropic Sonnet across all three variants per DEC-011
(fixture stability — the LLM payload the drafter sees is unchanged so
the always-passes column the LLM proposes is reproducibly the same).
Per-variant failure ergonomics and cost transparency for the
non-baseline variants also live in dedicated sibling files
(``tests/cli/test_e2e_openai_smoke.py``, ``tests/cli/test_e2e_gemini_smoke.py``).

Each variant carries the baseline three env-var gate (drafter is
always Anthropic, warehouse is always BigQuery) plus per-variant
grader env vars added on top:

* ``anthropic`` — ``SF_RUN_BQ=1``, ``ANTHROPIC_API_KEY``,
  ``GOOGLE_CLOUD_PROJECT`` (only the baseline; the grader reuses the
  drafter's Anthropic key).
* ``openai`` — baseline + ``SF_RUN_OPENAI=1`` + ``OPENAI_API_KEY``.
* ``gemini`` — baseline + ``SF_RUN_GEMINI=1`` + ``GOOGLE_API_KEY``.

``bigquery-public-data.austin_bikeshare`` is publicly readable but the
runner's own project is billed for the bytes scanned.

The test is excluded from default ``pytest`` runs by
``addopts = "... -m 'not e2e' ..."`` in ``pyproject.toml``. The
maintainer runs it once before declaring an e2e PR ready (mirrors
``pytest -m bigquery --no-cov`` for the BigQuery adapter)::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-...
    # Optional for the non-baseline variants:
    export OPENAI_API_KEY=sk-... SF_RUN_OPENAI=1
    export GOOGLE_API_KEY=... SF_RUN_GEMINI=1
    SF_RUN_BQ=1 pytest -m e2e --no-cov

The ``--no-cov`` flag is required because ``--cov-fail-under`` in
``addopts`` would fail any marker-specific run that exercises only a
fraction of the codebase. Per-variant invocation: append
``-k anthropic`` / ``-k openai`` / ``-k gemini`` to the above.

Asserts the seven invariants from DEC-009 across every parametrized
variant:

1. ``signalforge.cli.main(...)`` returns ``0``.
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. ``DiffReport.kept_count + flagged_count + dropped_count >= 1``
   (resolves SQ-01 — non-empty diff).
4. A :class:`PruneDecision` with ``decision == "dropped"`` and
   ``reason == "always-passes"`` exists in the prune audit (resolves
   SQ-02 — the v0.1 differentiator: SignalForge dropped a noisy
   ``not_null`` test the LLM proposed on a natural NOT NULL column
   like ``trip_id`` or ``start_time`` per DEC-024). Warehouse-side,
   independent of grader provider.
5. ``DiffReport.flagged_count >= 1`` (forced by tight grade
   thresholds in ``signalforge.yml``). All three graders are held to
   the same bar; see the inline comment on the assertion for the
   per-variant relaxation risk.
6. ``GradingReport.aggregate_complete is True`` (no degraded grade
   calls — every ``(artifact, criterion)`` pair scored cleanly). This
   is the cross-provider contract pin the in-isolation smokes can't
   provide.
7. ``"Traceback" not in stderr`` (DEC-016 of
   ``cli-layer.md`` — no traceback ever leaks).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signalforge.cli import main
from signalforge.grade import GradingReport
from tests.cli._e2e_helpers import (
    apply_provider_override,
    copy_fixture_to_tmp,
    read_diff_report,
    read_prune_decisions,
)

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (mirrors warehouse integration)."""
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _openai_runs_enabled() -> bool:
    """``SF_RUN_OPENAI`` is set to a truthy value (mirrors ``SF_RUN_BQ``)."""
    return os.environ.get("SF_RUN_OPENAI", "").lower() in _TRUTHY


def _gemini_runs_enabled() -> bool:
    """``SF_RUN_GEMINI`` is set to a truthy value (mirrors gemini live tests)."""
    return os.environ.get("SF_RUN_GEMINI", "").lower() in _TRUTHY


def _skip_reason(grade_provider: str) -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when every gate required by ``grade_provider`` is
    satisfied — the test then proceeds to make real BigQuery + real
    Anthropic (drafter) + real ``<grade_provider>`` (grader) calls.

    The baseline three-env-var gate (``SF_RUN_BQ`` + ``ANTHROPIC_API_KEY``
    + ``GOOGLE_CLOUD_PROJECT``) applies to every variant because the
    drafter stays Anthropic across all three per DEC-011 of
    ``plans/super/155-gemini-truncation-e2e-gap.md``. The non-baseline
    variants layer additional grader-specific env vars on top.

    Each missing prerequisite yields its own distinct reason so a
    maintainer running ``pytest -m e2e -k <variant>`` sees exactly what
    to set. Treat an empty / whitespace-only key as "unset" (an empty
    value would otherwise reach the client and produce a noisy auth
    failure rather than a skip).
    """
    # Baseline (always required — drafter is Anthropic, warehouse is BQ).
    if not _bq_runs_enabled():
        return "SF_RUN_BQ=1 required (e2e test costs real money against BigQuery)"
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return (
            "ANTHROPIC_API_KEY required "
            "(drafter stays on Anthropic Sonnet per #155 DEC-011 across every variant)"
        )
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable but billed to the runner)"
        )
    # Per-variant grader env-var layer.
    if grade_provider == "openai":
        if not _openai_runs_enabled():
            return (
                "SF_RUN_OPENAI=1 required (openai variant costs real money against the OpenAI API)"
            )
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            return (
                "OPENAI_API_KEY required (openai variant calls the real OpenAI API as the grader)"
            )
    elif grade_provider == "gemini":
        if not _gemini_runs_enabled():
            return (
                "SF_RUN_GEMINI=1 required (gemini variant costs real money against the Gemini API)"
            )
        if not os.environ.get("GOOGLE_API_KEY", "").strip():
            return (
                "GOOGLE_API_KEY required (gemini variant calls the real Gemini API as the grader)"
            )
    return None


@pytest.mark.e2e
@pytest.mark.parametrize("grade_provider", ["anthropic", "openai", "gemini"])
def test_e2e_signalforge_generate_against_austin_bikeshare(
    grade_provider: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run ``signalforge generate`` end-to-end and pin the seven invariants.

    Parametrized over ``grade_provider`` per #155 DEC-003 — the
    cross-provider diff-sidecar rendering contract the in-isolation
    grade smokes (``tests/grade/test_*_grade_live.py``) cannot pin.
    Drafter stays Anthropic Sonnet across all three variants per #155
    DEC-011 (fixture stability: the LLM payload the drafter sees is
    unchanged, so the always-passes column the LLM proposes is
    reproducibly the same).

    Skips cleanly under ``pytest -m e2e`` when any of the variant's
    required env vars is missing — the maintainer runs the gated
    invocation once before merge and the default suite never reaches
    this test.
    """
    if reason := _skip_reason(grade_provider):
        pytest.skip(reason)

    # DEC-008 — copy the read-only fixture to ``tmp_path`` so the audit
    # JSONLs (prune.jsonl, grade.jsonl, llm_response.jsonl,
    # safety.jsonl) and the diff sidecar land in the per-run temp dir,
    # not the committed fixture.
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Issue #155 / US-004 / US-007 / DEC-012 — overlay the grader's
    # provider/model via the canonical per-test helper. The drafter
    # stays Anthropic Sonnet across every variant per DEC-011 (no
    # ``llm.provider`` override); only ``grade:`` block knobs change
    # here. The fixture's grade thresholds (``min_pass_rate=0.95 /
    # min_mean_score=0.95 / total_budget_seconds=600``) round-trip
    # unchanged.
    if grade_provider == "anthropic":
        # No-op proof-of-use: stamps `grade.provider: anthropic` (the
        # implicit default in the committed fixture's `signalforge.yml`)
        # without changing any other knob.
        apply_provider_override(project_dir, grade_provider="anthropic")
    elif grade_provider == "openai":
        apply_provider_override(
            project_dir,
            grade_provider="openai",
            grade_model="gpt-4o",
        )
    elif grade_provider == "gemini":
        # ``grade_max_output_tokens=4096`` is **load-bearing**, not
        # cosmetic. Issue #155 Finding 2 + issue #158: Gemini 2.5-flash's
        # verbose ``reasoning`` field truncates at low caps (512/1024)
        # on every fixture; the #155 probe found 2048 sufficient for
        # the 5-pair in-isolation smoke, but #158 caught that this
        # full-pipeline fixture runs 108–116 (artifact × criterion)
        # pairs and 5–6 of them still exceed 2048 (typed-degrading to
        # ``GradeLLMError`` and flipping ``aggregate_complete=False``).
        # 4096 is the #158 floor for the full-fixture workload. Per
        # DEC-009 the floor still lives here in the test overlay rather
        # than as a bumped ``GradeConfig`` production default.
        apply_provider_override(
            project_dir,
            grade_provider="gemini",
            grade_model="gemini-2.5-flash",
            grade_max_output_tokens=4096,
        )
    else:  # pragma: no cover — parametrize guards the value space.
        raise AssertionError(f"unhandled grade_provider: {grade_provider!r}")

    # The committed `profiles.yml` pins ``project: bigquery-public-data``
    # so the regen script (`dbt parse`) can hit the public dataset; but at
    # query time the BigQuery client uses ``profile.project`` as the
    # *billing* project, and the maintainer can't bill ``bigquery-public-data``.
    # Rewrite the per-run profile to bill the maintainer's project (read
    # from ``GOOGLE_CLOUD_PROJECT``); the manifest still resolves the model's
    # ``relation_name`` to ``bigquery-public-data.austin_bikeshare.bikeshare_trips``
    # via the model's own ``database``/``schema`` fields, so the SOURCE
    # table is unchanged.
    billing_project = os.environ["GOOGLE_CLOUD_PROJECT"]
    # `maximum_bytes_billed: 1 GB` bumps the default 100 MB cap so the
    # materialised-sample CTAS can scan the full ~2.27M-row source table
    # (the hash-mod sampling predicate `MOD(...) < 1` requires a full
    # scan, ~200-500 MB billed for `bikeshare_trips`). Per-test queries
    # against the materialised `_SESSION._sf_sample_<run_id>` temp table
    # are tiny (<1 MB) and well under the cap. ~$0.005 per run.
    (project_dir / "profiles.yml").write_text(
        "austin:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: bigquery\n"
        "      method: oauth\n"
        f"      project: {billing_project}\n"
        "      dataset: austin_bikeshare\n"
        "      location: US\n"
        "      maximum_bytes_billed: 1000000000\n"
    )

    exit_code = main(
        [
            "generate",
            "models/staging/stg_bikeshare_trips.sql",
            "--project-dir",
            str(project_dir),
        ]
    )

    # 1. Exit code 0 — full pipeline (draft → prune → grade → diff)
    #    completed without a typed-error escape.
    assert exit_code == 0, f"expected clean exit; got exit_code={exit_code}"

    # 2. Diff sidecar landed at the default path.
    sidecar = project_dir / ".signalforge" / "diff.json"
    assert sidecar.is_file(), f"diff sidecar missing at {sidecar}"

    # 3 + 5. DiffReport invariants.
    #
    # SQ-01 ("non-empty diff") is satisfied by `kept + flagged + dropped >= 1`
    # — the pipeline produced *some* shippable artifacts. With the fixture's
    # tight grade thresholds (`min_pass_rate=0.95 / min_mean_score=0.95`) the
    # textual artifacts that survive prune get force-flagged rather than
    # kept, so `kept_count` can land at 0 while the run is otherwise healthy.
    # The independent ``dropped_count >= 1`` (SQ-02) and ``flagged_count >= 1``
    # checks below pin the signal-bearing branches of the pipeline.
    report = read_diff_report(project_dir)
    total_entries = report.kept_count + report.flagged_count + report.dropped_count
    assert total_entries >= 1, (
        f"expected at least one diff entry (SQ-01: non-empty diff); "
        f"got kept={report.kept_count} flagged={report.flagged_count} "
        f"dropped={report.dropped_count}"
    )
    # Per-variant relaxation risk: the fixture's tight thresholds were
    # calibrated against the Anthropic grader's score distribution
    # (US-005 / #10). OpenAI's ``gpt-4o`` and Gemini's
    # ``gemini-2.5-flash`` may score the same artifacts differently —
    # in principle a more lenient judge could land flagged_count=0 here
    # while the run is otherwise healthy. The #155 live probe in DEC-009
    # observed all three providers still force at least one flag on this
    # fixture, but this is an unverified per-PR assumption (this worker
    # cannot reach the live APIs). If a future maintainer's live run
    # trips this assertion for the openai/gemini variant, relax to
    # ``>= 0`` for that variant and capture the per-grader distribution
    # in a follow-up bead — do NOT delete the assertion (the anthropic
    # variant must still pin the threshold-honouring contract).
    assert report.flagged_count >= 1, (
        f"expected flagged_count >= 1 for grader={grade_provider!r} "
        f"(signalforge.yml pins min_pass_rate=0.95 / min_mean_score=0.95 to "
        f"force at least one flag); got {report.flagged_count}"
    )

    # 4. At least one always-passes drop — the v0.1 differentiator.
    #    Path A (DEC-024) aliases the fixture model directly at the
    #    public source table; the always-pass signal comes from natural
    #    NOT NULL columns in bikeshare data (`trip_id`, `start_time`,
    #    `duration_minutes`, `bike_id` all have nulls=0 in the source).
    #    The LLM reliably drafts `not_null` on those; the prune engine
    #    sees zero failing rows and drops them.
    decisions = read_prune_decisions(project_dir)
    has_always_passes_drop = any(
        d.decision == "dropped" and d.reason == "always-passes" for d in decisions
    )
    assert has_always_passes_drop, (
        "expected at least one PruneDecision with decision='dropped' and "
        "reason='always-passes' (SQ-02: the v0.1 differentiator). Path A "
        "(DEC-024) relies on bikeshare's natural NOT NULL columns (trip_id, "
        "start_time, etc.) rather than engineered literals."
    )

    # 6. Grade aggregate_complete — no degraded calls. This is the
    #    cross-provider contract pin the in-isolation smokes can't
    #    provide: every (artifact, criterion) pair must score cleanly
    #    through the full pipeline (manifest → safety → draft → prune →
    #    grade → diff) under the parametrized grader provider, with no
    #    truncation, safety-filter, or parse-failure degrades.
    grade_sidecar = project_dir / ".signalforge" / "grade.json"
    assert grade_sidecar.is_file(), f"grade sidecar missing at {grade_sidecar}"
    grading_report = GradingReport.model_validate_json(grade_sidecar.read_text())
    assert grading_report.aggregate_complete is True, (
        f"expected GradingReport.aggregate_complete=True for grader={grade_provider!r} "
        f"(every (artifact, criterion) pair scored cleanly); got False — increase "
        f"grade.total_budget_seconds in signalforge.yml or investigate the "
        f"{grade_provider} provider seam (capability flags, JSON-mode wiring, "
        f"tolerant parser, max_output_tokens floor)."
    )

    # 7. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's
    #    single ``try / except Exception`` boundary plus the
    #    ``_safe_excepthook`` install must prevent any traceback from
    #    leaking even if the pipeline raised internally).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
