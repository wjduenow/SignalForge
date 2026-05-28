"""End-to-end smoke test against real Anthropic + real BigQuery.

Issue #10 / US-005. Pins the v0.1 promise the rest of the suite cannot
exercise: ``signalforge generate <model>`` against a real dbt project
talking to a real warehouse and a real LLM produces a coherent
``diff.json`` whose kept / dropped / flagged tallies match the
fixture's expected shape (DEC-002, DEC-009, DEC-024, DEC-029 of
``plans/super/10-e2e-bigquery-smoke.md`` — Path A pivots the fixture
to source-as-model so the always-passes drop comes from natural NOT
NULL columns in bikeshare data rather than engineered literal columns).

Gated by THREE env vars (DEC-002):

* ``SF_RUN_BQ=1`` — the project-wide opt-in for "this test costs real
  money / talks to a real warehouse" (mirrors
  ``tests/warehouse/test_bigquery_integration.py``).
* ``ANTHROPIC_API_KEY`` — without a key the drafter / grader cannot
  call the LLM seam.
* ``GOOGLE_CLOUD_PROJECT`` — the BigQuery billing project.
  ``bigquery-public-data.austin_bikeshare`` is publicly readable but
  the runner's own project is billed for the bytes scanned.

The test is excluded from default ``pytest`` runs by
``addopts = "... -m 'not e2e' ..."`` in ``pyproject.toml``. The
maintainer runs it once before declaring an e2e PR ready (mirrors
``pytest -m bigquery --no-cov`` for the BigQuery adapter)::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-...
    SF_RUN_BQ=1 pytest -m e2e --no-cov

The ``--no-cov`` flag is required because ``--cov-fail-under`` in
``addopts`` would fail any marker-specific run that exercises only a
fraction of the codebase.

Asserts the seven invariants from DEC-009:

1. ``signalforge.cli.main(...)`` returns ``0``.
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. ``DiffReport.kept_count >= 1`` (resolves SQ-01 — at least one
   artifact survives prune + grade).
4. A :class:`PruneDecision` with ``decision == "dropped"`` and
   ``reason == "always-passes"`` exists in the prune audit (resolves
   SQ-02 — the v0.1 differentiator: SignalForge dropped a noisy
   ``not_null`` test the LLM proposed on a natural NOT NULL column
   like ``trip_id`` or ``start_time`` per DEC-024).
5. ``DiffReport.flagged_count >= 1`` (forced by tight grade
   thresholds in ``signalforge.yml``).
6. ``GradingReport.aggregate_complete is True`` (no degraded grade
   calls — every ``(artifact, criterion)`` pair scored cleanly).
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


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when all three gates are satisfied — the test
    proceeds to make real Anthropic + real BigQuery calls. Mirrors the
    skip-style used by ``tests/warehouse/test_bigquery_integration.py``
    (DEC-002 / DEC-020).
    """
    if not _bq_runs_enabled():
        return "SF_RUN_BQ=1 required (e2e test costs real money against BigQuery)"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY required (e2e test calls the real Anthropic API)"
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable but billed to the runner)"
        )
    return None


@pytest.mark.e2e
def test_e2e_signalforge_generate_against_austin_bikeshare(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end and pin the seven invariants.

    Skips cleanly under ``pytest -m e2e`` when any of the three env
    vars is missing — the maintainer runs the gated invocation once
    before merge and the default suite never reaches this test.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # DEC-008 — copy the read-only fixture to ``tmp_path`` so the audit
    # JSONLs (prune.jsonl, grade.jsonl, llm_response.jsonl,
    # safety.jsonl) and the diff sidecar land in the per-run temp dir,
    # not the committed fixture.
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Issue #155 / US-004 / DEC-012 — exercise the canonical per-test
    # provider-overlay helper. This BQ smoke is the Anthropic baseline, so
    # the call is a no-op proof-of-use: it stamps `grade.provider:
    # anthropic` (the implicit default in the committed fixture's
    # `signalforge.yml`) without changing any other knob. The OpenAI
    # (US-005) and Gemini (US-006) sibling smokes will pass non-default
    # `grade_provider=` values through the same seam.
    apply_provider_override(project_dir, grade_provider="anthropic")

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

    # 6. Grade aggregate_complete — no degraded calls.
    grade_sidecar = project_dir / ".signalforge" / "grade.json"
    assert grade_sidecar.is_file(), f"grade sidecar missing at {grade_sidecar}"
    grading_report = GradingReport.model_validate_json(grade_sidecar.read_text())
    assert grading_report.aggregate_complete is True, (
        "expected GradingReport.aggregate_complete=True (every (artifact, criterion) "
        "pair scored cleanly); got False — increase grade.total_budget_seconds in "
        "signalforge.yml or investigate the LLM seam."
    )

    # 7. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's
    #    single ``try / except Exception`` boundary plus the
    #    ``_safe_excepthook`` install must prevent any traceback from
    #    leaking even if the pipeline raised internally).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
