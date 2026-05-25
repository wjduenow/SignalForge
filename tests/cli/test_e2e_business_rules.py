"""End-to-end smoke test for custom business-rule (``custom_sql``) tests.

Issue #116 / US-015 (DEC-009 of ``plans/super/116-business-rule-tests.md``).
Pins the v2 promise the rest of the suite cannot exercise: a model that
carries ``meta.signalforge.business_rules`` drives the drafter to propose
custom singular-SQL tests, which then flow through the EXISTING prune →
grade → diff pipeline — so an always-pass business rule is DROPPED (signal
over volume) and a finds-failures business rule is KEPT.

This test reuses the committed Austin-bikeshare e2e fixture verbatim and
injects the engineered business rules into the per-run ``tmp_path`` copy of
the manifest (via :func:`tests.cli._e2e_helpers.inject_model_business_rules`).
Injecting at run time — rather than committing a second hand-crafted manifest
— keeps the e2e fixture decoupled from the ``init-demo`` parity tree
(``tests/test_demo_fixture_parity.py``), which requires the committed Austin
fixture to stay byte-equal to ``src/signalforge/_demo/``.

Gated by THREE env vars (mirrors ``test_e2e_bigquery_smoke.py`` exactly):

* ``SF_RUN_BQ=1`` — the project-wide opt-in for "this test costs real
  money / talks to a real warehouse".
* ``ANTHROPIC_API_KEY`` — without a key the drafter / grader cannot call
  the LLM seam.
* ``GOOGLE_CLOUD_PROJECT`` — the BigQuery billing project.

The test is excluded from default ``pytest`` runs by
``addopts = "... -m 'not e2e' ..."`` in ``pyproject.toml``. The maintainer
runs it once before declaring an e2e PR ready::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    export ANTHROPIC_API_KEY=sk-...
    SF_RUN_BQ=1 pytest -m e2e --no-cov

``--no-cov`` is required because ``--cov-fail-under`` in ``addopts`` would
fail any marker-specific run that exercises only a fraction of the codebase.

Engineered determinism (per ``testing-signal.md`` § "Engineered determinism
for LLM-driven assertions"): the LLM's exact SQL bytes are non-deterministic
across runs, but the business-rule SEMANTICS make the prune outcome
mathematically guaranteed regardless of which sample the warehouse draws.

* The always-pass rule is a tautology — "``duration_minutes`` must always be
  greater than or equal to itself". Any failing-rows SELECT for it
  (``WHERE duration_minutes < duration_minutes``) returns zero rows on every
  possible sample → ``always-passes`` → DROPPED.
* The finds-failures rule is guaranteed violated by real bikeshare data —
  "every trip must start and end at the same station". A→B trips dominate the
  dataset, so the failing-rows SELECT
  (``WHERE start_station_id <> end_station_id``) returns rows on any
  reasonable sample → real signal → KEPT.

Asserts the invariants from DEC-009:

1. ``signalforge.cli.main(...)`` returns ``0``.
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. At least one ``custom_sql`` :class:`PruneDecision` is
   ``decision == "dropped"`` / ``reason == "always-passes"`` (the
   always-pass tautology was pruned — Architectural Commitment #1).
4. At least one ``custom_sql`` :class:`PruneDecision` is
   ``decision == "kept"`` (the finds-failures rule found real failing rows).
5. The ``DiffReport`` is non-empty and surfaces the kept custom_sql test as
   a proposed ``.sql`` file (``proposed_test_files``).
6. ``"Traceback" not in stderr`` (DEC-016 of ``cli-layer.md`` — no traceback
   ever leaks).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._e2e_helpers import (
    copy_fixture_to_tmp,
    inject_model_business_rules,
    read_diff_report,
    read_prune_decisions,
)

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_MODEL_UNIQUE_ID = "model.signalforge_test_austin.stg_bikeshare_trips"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Engineered business rules. The SEMANTICS — not the LLM's exact SQL bytes —
# make the prune outcome deterministic (see module docstring).
_ALWAYS_PASS_RULE = (
    "duration_minutes must always be greater than or equal to itself "
    "(a row violates this rule only when duration_minutes < duration_minutes, "
    "which is never true)"
)
_FINDS_FAILURES_RULE = (
    "every trip must start and end at the same station "
    "(a row violates this rule when start_station_id <> end_station_id)"
)


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (mirrors warehouse integration)."""
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when all three gates are satisfied — the test
    proceeds to make real Anthropic + real BigQuery calls. Mirrors the
    skip-style used by ``test_e2e_bigquery_smoke.py`` (DEC-002 of #10).
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
def test_e2e_business_rules_drafts_prunes_custom_sql(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end on a business-rule model.

    Skips cleanly under ``pytest -m e2e`` when any of the three env vars
    is missing — the maintainer runs the gated invocation once before
    merge and the default suite never reaches this test.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # DEC-008 of #10 — copy the read-only fixture to ``tmp_path`` so the
    # audit JSONLs and the diff sidecar land in the per-run temp dir, not
    # the committed fixture.
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Inject the engineered business rules into the per-run manifest copy.
    # The committed Austin manifest ships an empty config.meta; rewriting it
    # in tmp_path keeps the e2e fixture decoupled from the init-demo parity
    # tree (the committed fixture must stay byte-equal to _demo/).
    inject_model_business_rules(
        project_dir,
        _MODEL_UNIQUE_ID,
        [_ALWAYS_PASS_RULE, _FINDS_FAILURES_RULE],
    )

    # Bill the maintainer's project (bigquery-public-data can't bill itself);
    # bump the bytes cap so the materialised-sample CTAS clears the default
    # 100 MB cap. Mirrors ``test_e2e_bigquery_smoke.py`` verbatim.
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

    # 1. Exit code 0 — full pipeline completed without a typed-error escape.
    assert exit_code == 0, f"expected clean exit; got exit_code={exit_code}"

    # 2. Diff sidecar landed at the default path.
    sidecar = project_dir / ".signalforge" / "diff.json"
    assert sidecar.is_file(), f"diff sidecar missing at {sidecar}"

    # 3 + 4. Custom-SQL prune outcomes — the engineered determinism payoff.
    decisions = read_prune_decisions(project_dir)
    custom_sql_decisions = [d for d in decisions if d.test.type == "custom_sql"]
    assert custom_sql_decisions, (
        "expected at least one custom_sql PruneDecision (the drafter should "
        "translate each meta.signalforge.business_rules entry into a custom_sql "
        "test); got none. Inspect .signalforge/prune.jsonl + llm_response.jsonl."
    )

    has_always_passes_drop = any(
        d.decision == "dropped" and d.reason == "always-passes" for d in custom_sql_decisions
    )
    assert has_always_passes_drop, (
        "expected at least one custom_sql PruneDecision dropped with "
        "reason='always-passes' (the tautological business rule "
        "'duration_minutes >= itself' can never return failing rows). "
        f"custom_sql decisions: {[(d.decision, d.reason) for d in custom_sql_decisions]}"
    )

    has_kept_with_evidence = any(
        d.decision == "kept" and d.reason == "kept" for d in custom_sql_decisions
    )
    assert has_kept_with_evidence, (
        "expected at least one custom_sql PruneDecision kept with "
        "reason='kept' (the 'same start/end station' rule is violated by real "
        "A->B bikeshare trips, so it returns failing rows). "
        f"custom_sql decisions: {[(d.decision, d.reason) for d in custom_sql_decisions]}"
    )

    # 5. The DiffReport is non-empty and the kept custom_sql test is surfaced
    #    as a proposed .sql file (DEC-011 — singular tests are .sql files,
    #    not schema.yml blocks).
    report = read_diff_report(project_dir)
    total_entries = report.kept_count + report.flagged_count + report.dropped_count
    assert total_entries >= 1, (
        f"expected at least one diff entry; got kept={report.kept_count} "
        f"flagged={report.flagged_count} dropped={report.dropped_count}"
    )
    assert report.proposed_test_files, (
        "expected at least one proposed_test_files entry for the kept custom_sql "
        "test (DEC-011 — kept singular tests are surfaced as standalone .sql "
        f"files); got {report.proposed_test_files!r}"
    )

    # 6. No traceback in stderr (DEC-016 of cli-layer.md).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
