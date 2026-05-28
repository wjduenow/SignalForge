"""End-to-end smoke test against real Gemini + real BigQuery.

Issue #155 / US-006. Mirrors :file:`tests/cli/test_e2e_bigquery_smoke.py`
(the Anthropic baseline) verbatim except for the grader: the per-test
``signalforge.yml`` overlay swaps ``grade.provider`` to ``"gemini"``
and ``grade.model`` to ``"gemini-2.5-flash"``. The drafter stays
Anthropic Sonnet (per DEC-011 — fixture stability; only the grader is
parametrised across providers).

The ``grade_max_output_tokens=2048`` overlay is **load-bearing**, not
cosmetic. Issue #155 Finding 2: Gemini 2.5-flash's verbose
``reasoning`` field routinely exceeds ``max_output_tokens=512``/``1024``
on this fixture's 5-pair grading workload, hitting ``MAX_TOKENS`` and
truncating mid-string. The live probe in #155 verified ``2048`` passes
cleanly. Without the cap, this test would flake the same way the
original validation broke. Per DEC-009 the floor lives in the test
overlay rather than as a bumped production default — avoids
over-budgeting Anthropic/OpenAI calls.

Gated by THREE env vars (mirrors BQ smoke's three-env-var skip gate):

* ``SF_RUN_GEMINI=1`` — the project-wide opt-in for the Gemini live
  marker (mirrors :file:`tests/grade/test_gemini_grade_live.py`).
* ``GOOGLE_API_KEY`` — without a key the grader cannot call Gemini.
* ``GOOGLE_CLOUD_PROJECT`` — BigQuery is still the warehouse. The
  Austin source table lives in ``bigquery-public-data`` but the
  runner's own project is billed for the scanned bytes.

Note: ``ANTHROPIC_API_KEY`` is also required because the drafter stays
on Sonnet per DEC-011; check it here for clear skip ergonomics.

The test is excluded from default ``pytest`` runs by ``addopts =
"... -m 'not e2e and not gemini' ..."`` in ``pyproject.toml``. The
maintainer runs it in the pre-release live suite (DEC-010)::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-...
    export GOOGLE_API_KEY=...
    SF_RUN_BQ=1 SF_RUN_GEMINI=1 pytest -m "e2e and gemini" --no-cov

The ``--no-cov`` flag is required because ``--cov-fail-under`` in
``addopts`` would fail any marker-specific run that exercises only a
fraction of the codebase.

Asserts the same seven invariants as :file:`test_e2e_bigquery_smoke.py`:

1. ``signalforge.cli.main(...)`` returns ``0``.
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. ``kept_count + flagged_count + dropped_count >= 1`` (SQ-01 —
   non-empty diff).
4. A :class:`PruneDecision` with ``decision == "dropped"`` and
   ``reason == "always-passes"`` exists in the prune audit (SQ-02 —
   the v0.1 differentiator; warehouse-driven, provider-agnostic).
5. ``DiffReport.flagged_count >= 1`` (forced by the fixture's tight
   grade thresholds ``min_pass_rate=0.95 / min_mean_score=0.95``;
   Gemini's grading distribution should still produce at least one
   flag against thresholds this strict).
6. ``GradingReport.aggregate_complete is True`` — **the load-bearing
   assertion that proves the 2048 cap fixes the truncation bug.**
   If ``max_output_tokens`` were too low, Gemini's verbose
   ``reasoning`` would truncate mid-string, raising
   ``GradeOutputError`` and degrading the result (per
   ``grade-layer.md`` § "Conservative score-and-degrade taxonomy"),
   which would flip ``aggregate_complete`` to ``False``.
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

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# DEC-011 — keep the e2e files separate (not parametrised); per-file
# marker gating aligns with the existing ``@pytest.mark.gemini``
# convention used by ``tests/grade/test_gemini_grade_live.py``.
pytestmark = [pytest.mark.e2e, pytest.mark.gemini]


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (mirrors warehouse integration)."""
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _gemini_runs_enabled() -> bool:
    """``SF_RUN_GEMINI`` is set to a truthy value (mirrors gemini live tests)."""
    return os.environ.get("SF_RUN_GEMINI", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when all gates are satisfied — the test proceeds
    to make real Gemini + real BigQuery calls. Mirrors the
    belt-and-suspenders skip-style used by
    :file:`tests/cli/test_e2e_bigquery_smoke.py` and
    :file:`tests/grade/test_gemini_grade_live.py`.
    """
    if not _gemini_runs_enabled():
        return "SF_RUN_GEMINI=1 required (e2e test calls the real Gemini API)"
    if not os.environ.get("GOOGLE_API_KEY", "").strip():
        return "GOOGLE_API_KEY required (e2e test calls the real Gemini API)"
    if not _bq_runs_enabled():
        return "SF_RUN_BQ=1 required (e2e test costs real money against BigQuery)"
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return (
            "ANTHROPIC_API_KEY required "
            "(drafter stays on Anthropic Sonnet per #155 DEC-011; only the grader is Gemini)"
        )
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable but billed to the runner)"
        )
    return None


def test_e2e_signalforge_generate_against_austin_bikeshare_with_gemini_grader(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end with Gemini as the grader.

    Skips cleanly under ``pytest -m "e2e and gemini"`` when any of the
    five env vars is missing — the maintainer runs the gated invocation
    once per pre-release live suite (DEC-010) and the default suite
    never reaches this test.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # Mirrors BQ smoke DEC-008 — copy the read-only fixture to
    # ``tmp_path`` so the audit JSONLs (prune.jsonl, grade.jsonl,
    # llm_response.jsonl, safety.jsonl) and the diff sidecar land in the
    # per-run temp dir, not the committed fixture.
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Issue #155 / US-006 / DEC-009 / DEC-012 — swap the grader to
    # Gemini via the canonical per-test overlay helper (US-004). The
    # drafter stays Anthropic Sonnet per DEC-011 (no ``llm.provider``
    # override); only ``grade:`` block knobs change here.
    #
    # ``grade_max_output_tokens=2048`` is **load-bearing**, not cosmetic.
    # Issue #155 Finding 2: Gemini 2.5-flash's verbose ``reasoning``
    # field routinely exceeds ``max_output_tokens=512``/``1024`` on this
    # fixture's grading workload, hitting MAX_TOKENS and truncating
    # mid-string. That truncation surfaces downstream as a degraded
    # grade result (``aggregate_complete=False``) and would flake
    # assertion #6 below. The #155 live probe verified ``2048`` passes
    # cleanly. Per DEC-009 the floor lives here in the test overlay
    # rather than as a bumped ``GradeConfig`` production default.
    apply_provider_override(
        project_dir,
        grade_provider="gemini",
        grade_model="gemini-2.5-flash",
        grade_max_output_tokens=2048,
    )

    # The committed `profiles.yml` pins ``project: bigquery-public-data``
    # so the regen script (`dbt parse`) can hit the public dataset; but
    # at query time the BigQuery client uses ``profile.project`` as the
    # *billing* project, and the maintainer can't bill
    # ``bigquery-public-data``. Rewrite the per-run profile to bill the
    # maintainer's project (read from ``GOOGLE_CLOUD_PROJECT``); the
    # manifest still resolves the model's ``relation_name`` to
    # ``bigquery-public-data.austin_bikeshare.bikeshare_trips`` via the
    # model's own ``database``/``schema`` fields, so the SOURCE table is
    # unchanged. Mirrors BQ smoke verbatim (warehouse path is
    # provider-agnostic).
    billing_project = os.environ["GOOGLE_CLOUD_PROJECT"]
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

    # 3 + 5. DiffReport invariants. Same logic as BQ smoke — SQ-01 holds
    #    on the combined kept+flagged+dropped tally; ``flagged_count >= 1``
    #    holds because the fixture's grade thresholds (0.95 / 0.95) are
    #    tight enough that at least one artifact gets force-flagged
    #    regardless of which provider grades it.
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
    #    Warehouse-driven, provider-agnostic: the always-pass signal
    #    comes from natural NOT NULL columns in bikeshare data
    #    (``trip_id``, ``start_time``, etc.); the drafter (still
    #    Anthropic) reliably drafts ``not_null`` on those; the prune
    #    engine sees zero failing rows and drops them. Changing the
    #    *grader* (Gemini vs Anthropic) doesn't affect this branch.
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
    #    This is the **load-bearing assertion** that proves the
    #    ``max_output_tokens=2048`` overlay fixes the truncation bug.
    #    A MAX_TOKENS-mid-string truncation in any Gemini judge call
    #    would surface as ``GradeOutputError(violation_type="json_parse")``
    #    → ``GradingResult(score=None, passed=False, reasoning="call failed: GradeOutputError")``
    #    → ``aggregate_complete=False``. The cap MUST keep every
    #    (artifact, criterion) pair scoring cleanly.
    grade_sidecar = project_dir / ".signalforge" / "grade.json"
    assert grade_sidecar.is_file(), f"grade sidecar missing at {grade_sidecar}"
    grading_report = GradingReport.model_validate_json(grade_sidecar.read_text())
    assert grading_report.aggregate_complete is True, (
        "expected GradingReport.aggregate_complete=True (every (artifact, criterion) "
        "pair scored cleanly with Gemini at max_output_tokens=2048); got False — "
        "either the 2048 cap is no longer sufficient (see #155 Finding 2; raise the "
        "overlay), Gemini hit a safety-filter / RECITATION block, or "
        "grade.total_budget_seconds tripped. Inspect "
        ".signalforge/grade.jsonl for the per-pair degrade reasons."
    )

    # 7. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's
    #    single ``try / except Exception`` boundary plus the
    #    ``_safe_excepthook`` install must prevent any traceback from
    #    leaking even if the pipeline raised internally).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
