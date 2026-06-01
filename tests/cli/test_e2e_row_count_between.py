"""End-to-end smoke test for the ``row_count_between`` test variant (#169).

Issue #169 / US-012. Pins the v0.x promise the rest of the suite cannot
exercise: a hand-authored ``dbt_expectations.expect_table_row_count_to_be_between``
test in an external ``schema.yml`` flows through ``signalforge prune-existing``'s
ingest -> prune -> diff pipeline against a real BigQuery warehouse — so an
always-pass row-count bound is DROPPED (signal over volume) and a
mathematically-guaranteed-failing row-count bound is KEPT.

This test uses the ``prune-existing`` subcommand rather than ``generate`` so
the injected ``row_count_between`` test reaches the prune engine WITHOUT a
cooperative LLM step. The LLM drafter has no first-class injection seam for
``row_count_between`` (unlike ``meta.signalforge.business_rules`` -> custom_sql);
the ingest-layer path through ``--schema <file>`` is the direct seam that lets
this test pin the prune outcome deterministically rather than depending on
the drafter's mood.

It reuses the committed Austin-bikeshare e2e fixture verbatim — the
manifest / signalforge.yml / profiles.yml all round-trip unchanged. Only
the per-run hand-crafted ``schema.yml`` (planted under
``<project_dir>/models/staging/`` so the path-containment gate accepts it)
carries the engineered tests.

Gated by TWO env vars (deliberately narrower than the three-env-var gate
on ``test_e2e_bigquery_smoke.py`` / ``test_e2e_business_rules.py``;
``prune-existing`` makes **no** LLM call — ingest -> prune -> diff only,
per ``.claude/rules/cli-layer.md`` § the ``prune-existing`` subcommand
entry — so no Anthropic key is required):

* ``SF_RUN_BQ=1`` — the project-wide opt-in for "this test costs real
  money / talks to a real warehouse".
* ``GOOGLE_CLOUD_PROJECT`` — the BigQuery billing project.

The test is excluded from default ``pytest`` runs by
``addopts = "... -m 'not e2e' ..."`` in ``pyproject.toml``. The maintainer
runs it once before declaring an e2e PR ready::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    SF_RUN_BQ=1 pytest -m e2e -k row_count_between --no-cov

``--no-cov`` is required because ``--cov-fail-under`` in ``addopts`` would
fail any marker-specific run that exercises only a fraction of the codebase.

Engineered determinism (per ``.claude/rules/testing-signal.md`` §
"Engineered determinism for LLM-driven assertions"): the compiled SQL bytes
are deterministic per the compiler contract (``prune-engine.md``), but the
key insight is that the BOUND-VIOLATION predicate makes the prune outcome
mathematically guaranteed regardless of the warehouse data.

* **Engineered-failure-kept (load-bearing)** — ``min_value: 1, where: "1 = 0"``.
  The compiler emits::

      SELECT n FROM (SELECT COUNT(*) AS n FROM <table> WHERE 1 = 0) AS rc
      WHERE n < 1

  The inner ``COUNT(*) WHERE 1 = 0`` is mathematically 0 on any table; the
  outer ``WHERE n < 1`` returns exactly one row with ``n = 0``; the
  adapter's ``SELECT COUNT(*) AS failures FROM (<sql>) AS t`` wrap returns
  ``failures = 1``; the engine routes to ``decision="kept", reason="kept"``.
  Independent of warehouse state, independent of which sample the prune
  engine drew. This is the DETERMINISTIC engineered-failure trick that
  proves the variant produces signal end-to-end.

* **Engineered-always-pass-and-drop** — ``min_value: 0`` (no upper bound).
  The compiler emits::

      SELECT n FROM (SELECT COUNT(*) AS n FROM <table>) AS rc WHERE n < 0

  ``COUNT(*) >= 0`` always; the outer ``WHERE n < 0`` returns zero rows;
  the adapter wrap returns ``failures = 0``; the engine routes to
  ``decision="dropped", reason="always-passes"``. This is the DETERMINISTIC
  always-pass trick — the LLM cooperation needed in ``test_e2e_bigquery_smoke``
  (where the drop comes from ``not_null`` on naturally-non-null columns) is
  replaced here by a mathematically-vacuous bound.

Asserts the engineered-determinism invariants for the new variant:

1. ``signalforge.cli.main(["prune-existing", ...])`` returns ``0``.
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. At least one ``row_count_between`` :class:`PruneDecision` is
   ``decision == "kept"`` and ``reason == "kept"`` (the engineered failure
   produced signal — Architectural Commitment #1 in the kept direction).
4. At least one ``row_count_between`` :class:`PruneDecision` is
   ``decision == "dropped"`` and ``reason == "always-passes"`` (the vacuous
   bound was pruned — Architectural Commitment #1 in the dropped direction).
5. ``"Traceback" not in stderr`` (DEC-016 of ``cli-layer.md`` — no
   traceback ever leaks).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._e2e_helpers import read_diff_report, read_prune_decisions

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_MODEL_UNIQUE_ID = "model.signalforge_test_austin.stg_bikeshare_trips"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Engineered external schema.yml. The two ``row_count_between`` tests are
# the load-bearing surface; ``not_null`` on ``trip_id`` is included only to
# keep the ingest result non-trivial (a schema with zero kept tests would
# short-circuit before any warehouse call per ``prune-engine.md`` §
# "Empty-candidate short-circuit"). The bound-violation predicates make
# each row_count_between's prune outcome mathematically deterministic.
#
# * Engineered failure: ``WHERE 1 = 0`` makes the inner COUNT(*) zero;
#   ``min_value: 1`` then forces the outer ``WHERE n < 1`` to return one
#   row -> failures=1 -> KEPT.
# * Engineered always-pass: ``min_value: 0`` with no upper bound makes the
#   outer ``WHERE n < 0`` vacuously false on any table -> failures=0 ->
#   DROPPED with reason='always-passes'.
_SCHEMA_YML = """\
version: 2

models:
  - name: stg_bikeshare_trips
    description: "Staging model for Austin bikeshare trips (row_count_between e2e)."
    columns:
      - name: trip_id
        description: "Surrogate key for the trip."
        tests:
          - not_null
    # Model-level row_count_between entries (#169) — both deterministic.
    tests:
      # Engineered-failure-kept: WHERE 1 = 0 makes COUNT(*) = 0 < min=1
      # -> failing-rows SELECT returns one row -> failures=1 -> KEPT.
      - dbt_expectations.expect_table_row_count_to_be_between:
          min_value: 1
          where: "1 = 0"
      # Engineered-always-pass-and-drop: min=0, no max -> predicate
      # ``n < 0`` is vacuously false (COUNT(*) >= 0) -> failures=0 ->
      # DROPPED with reason='always-passes'.
      - dbt_expectations.expect_table_row_count_to_be_between:
          min_value: 0
"""


def _bq_runs_enabled() -> bool:
    """``SF_RUN_BQ`` is set to a truthy value (mirrors warehouse integration)."""
    return os.environ.get("SF_RUN_BQ", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required env var is missing.

    Returns ``None`` when both gates are satisfied — the test then
    proceeds to make real BigQuery calls (no Anthropic, no Gemini, no
    OpenAI; ``prune-existing`` makes no LLM call).

    The two-env-var gate is deliberately narrower than the three-env-var
    gate on ``test_e2e_bigquery_smoke.py`` / ``test_e2e_business_rules.py``
    because this path does NOT call the drafter — ``prune-existing`` flows
    ingest -> prune -> diff and never reaches the LLM seam.
    """
    if not _bq_runs_enabled():
        return "SF_RUN_BQ=1 required (e2e test costs real money against BigQuery)"
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required "
            "(BigQuery billing project; bigquery-public-data is readable "
            "but billed to the runner)"
        )
    return None


@pytest.mark.e2e
def test_e2e_row_count_between_engineered_failure_kept_and_always_pass_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge prune-existing`` end-to-end with two engineered
    ``row_count_between`` tests and pin the kept/dropped outcomes.

    Skips cleanly under ``pytest -m e2e`` when either env var is missing —
    the maintainer runs the gated invocation once before merge and the
    default suite never reaches this test.

    AC traces (per ``plans/super/169-row-count-between.md``):
    AC-1 (kept on real failure), AC-2 (dropped on always-passes), AC-7
    (end-to-end pipeline shape).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # Copy the read-only Austin fixture to ``tmp_path`` so the audit JSONLs
    # (prune.jsonl) and the diff sidecar land in the per-run temp dir.
    # Mirrors ``test_e2e_business_rules.py`` and ``test_e2e_bigquery_smoke.py``
    # — the committed fixture must stay byte-equal to ``src/signalforge/_demo/``
    # to satisfy the ``init-demo`` parity gate (DEC-008 of #10).
    project_dir = tmp_path / "project"
    shutil.copytree(_FIXTURE_DIR, project_dir)

    # Plant the external schema.yml INSIDE the project tree so the
    # ``canonicalise_user_path`` containment gate accepts it. Mirrors the
    # ``_setup_project`` helper in ``test_prune_existing.py``.
    schema_path = project_dir / "models" / "staging" / "external_schema.yml"
    schema_path.write_text(_SCHEMA_YML, encoding="utf-8")

    # Bill the maintainer's project. ``bigquery-public-data`` is readable
    # but cannot bill itself; mirrors ``test_e2e_bigquery_smoke.py``. Bump
    # the bytes cap above the default 100 MB so the COUNT(*) on the full
    # ``bikeshare_trips`` source (~2.27M rows, ~200-500 MB scanned by the
    # always-pass test which has no WHERE clause) clears the cap. ~$0.005
    # per run.
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

    # ``--scope full --sample-strategy oneshot``: row_count_between
    # bypasses sampling at the compiler level (DEC-003 of #169 — a sampled
    # COUNT(*) is semantically wrong), but ``--scope full --sample-strategy
    # oneshot`` avoids the materialised-sample CTAS the default
    # ``materialised`` strategy would otherwise run for sibling test types
    # (the schema also carries one ``not_null`` to keep the candidate set
    # non-trivial). Saves the CTAS round-trip on a path that never benefits
    # from it. Mirrors ``_base_argv`` in ``test_prune_existing.py``.
    exit_code = main(
        [
            "prune-existing",
            _MODEL_UNIQUE_ID,
            "--schema",
            str(schema_path),
            "--project-dir",
            str(project_dir),
            "--scope",
            "full",
            "--sample-strategy",
            "oneshot",
        ]
    )

    # 1. Exit code 0 — ingest -> prune -> diff completed cleanly.
    assert exit_code == 0, f"expected clean exit; got exit_code={exit_code}"

    # 2. Diff sidecar landed at the default path.
    sidecar = project_dir / ".signalforge" / "diff.json"
    assert sidecar.is_file(), f"diff sidecar missing at {sidecar}"

    # 3 + 4. row_count_between prune outcomes — the engineered-determinism
    #        payoff. The mathematically-guaranteed predicates make these
    #        assertions robust against any sample the warehouse draws.
    decisions = read_prune_decisions(project_dir)
    row_count_decisions = [d for d in decisions if d.test.type == "row_count_between"]
    assert len(row_count_decisions) >= 2, (
        f"expected at least two row_count_between PruneDecisions (one engineered "
        f"failure + one engineered always-pass); got {len(row_count_decisions)}. "
        f"Inspect .signalforge/prune.jsonl + the ingest skipped-test report on "
        f"stderr. Decisions: "
        f"{[(d.decision, d.reason) for d in row_count_decisions]}"
    )

    # 3. Engineered-failure-kept (load-bearing AC-1). The ``WHERE 1 = 0``
    #    predicate makes COUNT(*) = 0 on any table; ``min_value: 1`` forces
    #    the failing-rows SELECT to return one row -> KEPT with real
    #    evidence (``reason="kept"``, not ``"kept-without-evidence"``).
    has_engineered_failure_kept = any(
        d.decision == "kept" and d.reason == "kept" for d in row_count_decisions
    )
    assert has_engineered_failure_kept, (
        "expected at least one row_count_between PruneDecision kept with "
        "reason='kept' (the engineered ``WHERE 1 = 0`` + ``min_value: 1`` test "
        "is mathematically guaranteed to return one failing row on any table). "
        f"row_count_between decisions: "
        f"{[(d.decision, d.reason, d.failures) for d in row_count_decisions]}"
    )

    # 4. Engineered-always-pass-and-drop (load-bearing AC-2). ``min_value: 0``
    #    with no upper bound makes the failing-rows predicate ``n < 0``
    #    vacuously false (``COUNT(*) >= 0`` always) -> failures=0 ->
    #    always-passes -> DROPPED.
    has_always_passes_drop = any(
        d.decision == "dropped" and d.reason == "always-passes" for d in row_count_decisions
    )
    assert has_always_passes_drop, (
        "expected at least one row_count_between PruneDecision dropped with "
        "reason='always-passes' (the engineered ``min_value: 0`` test has "
        "vacuous bounds; COUNT(*) >= 0 always, so the failing-rows SELECT "
        "returns zero rows on any table). "
        f"row_count_between decisions: "
        f"{[(d.decision, d.reason, d.failures) for d in row_count_decisions]}"
    )

    # 5. Sanity check on the DiffReport: the kept row_count_between test
    #    surfaces in the diff (the dropped one does not, by design — drops
    #    are listed in the kept/dropped table, not the schema.yml diff body).
    report = read_diff_report(project_dir)
    total_entries = report.kept_count + report.flagged_count + report.dropped_count
    assert total_entries >= 1, (
        f"expected at least one diff entry across kept/flagged/dropped tiers; "
        f"got kept={report.kept_count} flagged={report.flagged_count} "
        f"dropped={report.dropped_count}"
    )

    # 6. No traceback in stderr (DEC-016 of cli-layer.md).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
