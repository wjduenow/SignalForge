"""End-to-end smoke test against real BigQuery + real OpenAI (grader).

Issue #155 / US-005. Pins the full-pipeline cross-provider contract the
in-isolation grade smokes cannot exercise: ``signalforge generate
<model>`` against a real dbt project, talking to a real BigQuery
warehouse with the **OpenAI** provider wired as the grader (drafter
stays Anthropic per the committed fixture's ``llm.model:
claude-sonnet-4-6`` pin and the cost table in
``plans/super/155-gemini-truncation-e2e-gap.md`` DEC-009/DEC-011/DEC-012).
The run must produce a coherent ``diff.json`` whose kept / dropped /
flagged tallies match the fixture's expected shape.

Sibling of ``tests/cli/test_e2e_bigquery_smoke.py`` (Anthropic baseline,
US-007) and ``tests/cli/test_e2e_gemini_smoke.py`` (US-006). DEC-011
keeps the three as separate files rather than one parametrized test so
each provider's failure ergonomics, cost transparency, and marker gating
stay independent.

Gated by FIVE env vars (mirrors the BQ smoke + the Gemini sibling — the
drafter stays Anthropic Sonnet per DEC-011, so the Anthropic auth +
BigQuery opt-in are part of the contract even though the grader swap is
OpenAI):

* ``SF_RUN_OPENAI=1`` — the project-wide opt-in for "this test costs
  real money against the OpenAI API" (mirrors ``SF_RUN_BQ`` /
  ``SF_RUN_SNOWFLAKE``).
* ``OPENAI_API_KEY`` — without a key the OpenAI grader cannot call the
  LLM seam.
* ``SF_RUN_BQ=1`` — the project-wide opt-in for "this test costs real
  money against BigQuery" (the warehouse leg is unchanged from the
  Anthropic baseline; same gate as ``tests/cli/test_e2e_bigquery_smoke.py``).
* ``ANTHROPIC_API_KEY`` — the DRAFTER is Anthropic Sonnet per DEC-011
  (drafter stays Sonnet across all three e2e providers for fixture
  stability); only the grader swaps to gpt-4o.
* ``GOOGLE_CLOUD_PROJECT`` — the BigQuery billing project.

The test is excluded from default ``pytest`` runs by ``addopts = "...
-m 'not e2e and not openai' ..."`` in ``pyproject.toml``. The
maintainer runs it once before declaring an e2e PR ready::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-ant-...
    export OPENAI_API_KEY=sk-...
    SF_RUN_BQ=1 SF_RUN_OPENAI=1 pytest -m openai --no-cov

The ``--no-cov`` flag is required because ``--cov-fail-under`` in
``addopts`` would fail any marker-specific run that exercises only a
fraction of the codebase.

Asserts the seven invariants from the BQ smoke (DEC-009 of #10):

1. ``signalforge.cli.main(...)`` returns ``0``.
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. ``kept_count + flagged_count + dropped_count >= 1`` (SQ-01:
   non-empty diff — at least one artifact survived the pipeline).
4. A :class:`PruneDecision` with ``decision == "dropped"`` and
   ``reason == "always-passes"`` exists in the prune audit (SQ-02 —
   the v0.1 differentiator; warehouse-side, independent of grader
   provider).
5. ``DiffReport.flagged_count >= 1`` (forced by the fixture's tight
   ``min_pass_rate=0.95 / min_mean_score=0.95`` grade thresholds —
   OpenAI's ``gpt-4o`` is the grader and is held to the same bar).
6. ``GradingReport.aggregate_complete is True`` (no degraded grade
   calls — every ``(artifact, criterion)`` pair scored cleanly; this
   is the cross-provider contract pin the in-isolation smokes can't
   provide).
7. ``"Traceback" not in stderr`` (DEC-016 of ``cli-layer.md`` — no
   traceback ever leaks).
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

pytestmark = [pytest.mark.e2e, pytest.mark.openai]

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _openai_runs_enabled() -> bool:
    """``SF_RUN_OPENAI`` is set to a truthy value (mirrors ``SF_RUN_BQ``)."""
    return os.environ.get("SF_RUN_OPENAI", "").lower() in _TRUTHY


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (warehouse opt-in cost gate).

    Mirrors :func:`tests.cli.test_e2e_bigquery_smoke._bq_runs_enabled`. The
    drafter-stays-Anthropic-Sonnet posture from DEC-011 means the
    OpenAI sibling still runs against the same BigQuery warehouse as the
    baseline, so the SF_RUN_BQ opt-in is part of this test's contract too.
    """
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` only when all FIVE gates are satisfied — the test
    then proceeds to make real OpenAI + real Anthropic + real BigQuery
    calls. Each missing prerequisite yields its own distinct reason so a
    maintainer running ``pytest -m openai`` sees exactly what to set.
    Treat an empty / whitespace-only API key as "unset" (an empty value
    would otherwise reach the client and produce a noisy auth failure
    rather than a clean named skip).

    The Anthropic + SF_RUN_BQ checks are required because the drafter
    stays Anthropic Sonnet across all three e2e providers per DEC-011
    (only the grader swaps) — without them the test fails on the FIRST
    drafter call, not at the OpenAI grader, which is confusing.
    """
    if not _openai_runs_enabled():
        return "SF_RUN_OPENAI=1 required (e2e test costs real money against the OpenAI API)"
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return "OPENAI_API_KEY required (e2e test calls the real OpenAI API as the grader)"
    if not _bq_runs_enabled():
        return (
            "SF_RUN_BQ=1 required "
            "(e2e test costs real money against BigQuery — warehouse leg shared with the baseline)"
        )
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return (
            "ANTHROPIC_API_KEY required "
            "(drafter stays Anthropic Sonnet per DEC-011; only the grader swaps to gpt-4o)"
        )
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable but billed to the runner)"
        )
    return None


def test_e2e_signalforge_generate_against_austin_bikeshare_openai_grader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end with OpenAI as the grader.

    Skips cleanly under ``pytest -m openai`` when any of the five env
    vars is missing — the maintainer runs the gated invocation once
    before merge and the default suite never reaches this test.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # DEC-008 of #10 — copy the read-only fixture to ``tmp_path`` so the
    # audit JSONLs (prune.jsonl, grade.jsonl, llm_response.jsonl,
    # safety.jsonl) and the diff sidecar land in the per-run temp dir,
    # not the committed fixture.
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Issue #155 / US-004 / DEC-012 — overlay the grader's provider/model
    # via the canonical helper. The committed fixture's ``llm.model:
    # claude-sonnet-4-6`` pin (drafter = Anthropic Sonnet) is left
    # untouched per DEC-011's cost table: drafter stays Sonnet, only the
    # grader swaps to gpt-4o. The fixture's grade thresholds
    # (``min_pass_rate=0.95 / min_mean_score=0.95 /
    # total_budget_seconds=600``) round-trip unchanged.
    apply_provider_override(
        project_dir,
        grade_provider="openai",
        grade_model="gpt-4o",
    )

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
    assert report.flagged_count >= 1, (
        f"expected flagged_count >= 1 (signalforge.yml pins min_pass_rate=0.95 / "
        f"min_mean_score=0.95 to force at least one flag); got {report.flagged_count}"
    )

    # 4. At least one always-passes drop — the v0.1 differentiator.
    #    This branch is warehouse-side and independent of the grader's
    #    provider: Path A (DEC-024 of #10) aliases the fixture model
    #    directly at the public source table; the always-pass signal
    #    comes from natural NOT NULL columns in bikeshare data
    #    (`trip_id`, `start_time`, `duration_minutes`, `bike_id` all
    #    have nulls=0 in the source). The Anthropic drafter reliably
    #    proposes `not_null` on those; the prune engine sees zero
    #    failing rows and drops them.
    decisions = read_prune_decisions(project_dir)
    has_always_passes_drop = any(
        d.decision == "dropped" and d.reason == "always-passes" for d in decisions
    )
    assert has_always_passes_drop, (
        "expected at least one PruneDecision with decision='dropped' and "
        "reason='always-passes' (SQ-02: the v0.1 differentiator). Path A "
        "(DEC-024 of #10) relies on bikeshare's natural NOT NULL columns "
        "(trip_id, start_time, etc.) rather than engineered literals."
    )

    # 6. Grade aggregate_complete — no degraded calls. This is the
    #    cross-provider contract the in-isolation grade smokes cannot
    #    pin: the OpenAI grader must complete every (artifact,
    #    criterion) pair without truncation, safety-filter, or
    #    parse-failure degrades when wired through the full pipeline
    #    (manifest → safety → draft → prune → grade → diff).
    grade_sidecar = project_dir / ".signalforge" / "grade.json"
    assert grade_sidecar.is_file(), f"grade sidecar missing at {grade_sidecar}"
    grading_report = GradingReport.model_validate_json(grade_sidecar.read_text())
    assert grading_report.aggregate_complete is True, (
        "expected GradingReport.aggregate_complete=True (every (artifact, criterion) "
        "pair scored cleanly under the OpenAI grader); got False — increase "
        "grade.total_budget_seconds in signalforge.yml or investigate the "
        "OpenAIProvider seam (capability flags, JSON-mode wiring, tolerant parser)."
    )

    # 7. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's
    #    single ``try / except Exception`` boundary plus the
    #    ``_safe_excepthook`` install must prevent any traceback from
    #    leaking even if the pipeline raised internally).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
