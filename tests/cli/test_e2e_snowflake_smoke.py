"""End-to-end smoke test against real Anthropic + real Snowflake (issue #124).

US-005 of the Snowflake epic (#118). The Snowflake analogue of
``tests/cli/test_e2e_bigquery_smoke.py``: it runs the FULL ``signalforge
generate`` pipeline (LLM draft -> prune -> grade -> diff) against the
read-only ``SNOWFLAKE_SAMPLE_DATA.TPCH_SF1`` sample dataset and pins the
same invariants the BigQuery e2e pins — chiefly that the prune step drops
at least one ``always-passes`` test (the v0.1 differentiator) on a natural
NOT NULL source column (the TPCH primary key ``c_custkey``).

``oneshot`` sampling is MANDATORY here, not a tuning choice.
``SNOWFLAKE_SAMPLE_DATA`` is a read-only shared database present in every
Snowflake account. The default ``prune.sample_strategy: materialised``
issues a ``CREATE TEMPORARY TABLE ... AS SELECT`` colocated with the
source database — which a read-only shared database rejects. ``oneshot``
``sample_rows`` reads ``TPCH_SF1.CUSTOMER`` directly with a hash-mod
predicate and never writes, so it is the only strategy that works against
the shared sample data. The per-run ``signalforge.yml`` (written into
``tmp_path`` below) pins ``prune.sample_strategy: oneshot`` for exactly
this reason.

Gated by FIVE prerequisites — this is a FULL-STACK test (warehouse + LLM):

* ``SF_RUN_SNOWFLAKE=1`` — the project-wide opt-in for "this test costs
  real money / talks to a real warehouse" (the Snowflake analogue of
  ``SF_RUN_BQ=1``; mirrors ``tests/warehouse/test_snowflake_estimate_live.py``).
* ``ANTHROPIC_API_KEY`` — without a key the drafter / grader cannot call
  the LLM seam.
* ``SNOWFLAKE_ACCOUNT`` / ``SNOWFLAKE_USER`` / ``SNOWFLAKE_PASSWORD`` — the
  minimal password-auth connection triple (what
  :meth:`SnowflakeAdapter._get_connection` consumes via ``make_real_client``).
* ``SNOWFLAKE_WAREHOUSE`` — required so the prune/sample queries have a
  warehouse compute context.

The test is excluded from default ``pytest`` runs by
``addopts = "... -m '... and not snowflake' ..."`` in ``pyproject.toml``.
The maintainer runs it once before declaring the Snowflake PR ready
(mirrors ``pytest -m snowflake --no-cov`` for the offline fakesnow /
EXPLAIN suites). Cost guidance — set up FIRST:

* Create a **resource monitor** on the test warehouse with a hard cap
  BEFORE running, so a runaway query can't bill unbounded credits.
* Use an **XS warehouse** (smallest compute) — TPCH_SF1 is tiny and a
  ``oneshot`` hash-mod sample over ~150K CUSTOMER rows is sub-second.
* Set **aggressive auto-suspend** (e.g. 60s) so the warehouse parks
  immediately after the run.

Run command::

    export SF_RUN_SNOWFLAKE=1
    export ANTHROPIC_API_KEY=sk-...
    export SNOWFLAKE_ACCOUNT=<org-account>
    export SNOWFLAKE_USER=<user>
    export SNOWFLAKE_PASSWORD=<password>
    export SNOWFLAKE_WAREHOUSE=<xs-warehouse>
    uv run pytest -m snowflake --no-cov

The ``--no-cov`` flag is required because ``--cov-fail-under`` in
``addopts`` would fail any marker-specific run that exercises only a
fraction of the codebase.

Asserts the invariants from the BigQuery e2e (DEC-009 of
``plans/super/10-e2e-bigquery-smoke.md``):

1. ``signalforge.cli.main(...)`` returns ``0``.
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. ``kept_count + flagged_count + dropped_count >= 1`` (non-empty diff).
4. A :class:`PruneDecision` with ``decision == "dropped"`` and
   ``reason == "always-passes"`` exists in the prune audit — the v0.1
   differentiator. Under ``oneshot`` sampling prune queries the read-only
   source ``TPCH_SF1.CUSTOMER`` directly, so the seed declares only REAL
   TPCH columns; the LLM reliably drafts ``not_null`` on every column and
   ``not_null`` on ``c_custkey`` (the TPCH primary key, naturally NOT NULL)
   returns zero failing rows → always-passes (mirrors the Austin bikeshare
   natural-NOT-NULL pattern, NOT engineered literals — a renamed/engineered
   column would not exist on the source and would route to
   kept-without-evidence).
5. ``GradingReport.aggregate_complete is True`` (no degraded grade calls).
6. ``"Traceback" not in stderr`` (DEC-016 of ``cli-layer.md`` — no
   traceback ever leaks).

Traces to: plans/super/118 epic / US-005 (#124).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest
import yaml

from signalforge.cli import main
from signalforge.grade import GradingReport
from tests.cli._e2e_helpers import (
    copy_fixture_to_tmp,
    read_diff_report,
    read_prune_decisions,
)

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "snowflake"

# The seed's model unique_id. The single-model positional path resolves via
# ``Manifest.get_model``, which accepts the unique_id and file-path forms but
# NOT a bare name (a bare name routes to the file-path branch and fails) — see
# ``.claude/rules/testing-signal.md`` § "Multi-surface drift on user-facing
# model arguments". The README pins this unique_id as the canonical arg.
_MODEL_UNIQUE_ID = "model.signalforge_test_tpch.stg_tpch_customers"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# The connection env vars the Snowflake adapter needs for password auth, plus
# the warehouse so the prune/sample queries have compute context. Mirrors
# ``tests/warehouse/test_snowflake_estimate_live.py``.
_REQUIRED_CONN_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_PASSWORD",
    "SNOWFLAKE_WAREHOUSE",
)


def _snowflake_runs_enabled() -> bool:
    """``SF_RUN_SNOWFLAKE`` is set to a truthy value (the Snowflake analogue of
    the ``SF_RUN_BQ`` opt-in; accepts ``1``/``true``/``yes``/``on``)."""
    return os.environ.get("SF_RUN_SNOWFLAKE", "").lower() in _TRUTHY


def _skip_reason() -> str | None:
    """Return a skip-reason string if any required prerequisite is missing.

    Returns ``None`` only when the opt-in flag AND the LLM key AND every
    Snowflake connection env var are present — the test then proceeds to make
    real Anthropic + real Snowflake calls. Each missing prerequisite yields its
    own distinct reason so a maintainer running ``pytest -m snowflake`` sees
    exactly what to set (the belt-and-suspenders runtime gate from
    ``.claude/rules/testing-signal.md`` § "End-to-end gated tests").
    """
    if not _snowflake_runs_enabled():
        return (
            "SF_RUN_SNOWFLAKE=1 required "
            "(e2e test costs real money against a real Snowflake warehouse)"
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY required (e2e test calls the real Anthropic API)"
    for var in _REQUIRED_CONN_VARS:
        if not os.environ.get(var):
            return f"{var} required (Snowflake connection parameter for the live pipeline run)"
    return None


@pytest.mark.snowflake
def test_e2e_signalforge_generate_against_tpch_sf1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end against TPCH_SF1 and pin the invariants.

    Skips cleanly under ``pytest -m snowflake`` when any prerequisite is
    missing — the maintainer runs the gated invocation once before merge and
    the default suite never reaches this test.
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    # Copy the read-only seed to ``tmp_path`` so the audit JSONLs (prune.jsonl,
    # grade.jsonl, llm_response.jsonl, safety.jsonl) and the diff sidecar land
    # in the per-run temp dir, not the committed fixture (DEC-008 of #10).
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # The committed ``profiles.yml`` carries placeholder credentials (regen-only).
    # Rewrite the per-run profile from env vars so the Snowflake adapter can
    # authenticate. ``database: SNOWFLAKE_SAMPLE_DATA`` / ``schema: TPCH_SF1``
    # point at the read-only shared sample database; the model's manifest
    # ``relation_name`` already resolves to
    # ``SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER`` independently, so the SOURCE
    # table is unchanged.
    # Serialise from a structured mapping via ``yaml.safe_dump`` rather than
    # interpolating raw env vars into a YAML string — a credential containing
    # ``#``/``:``/quotes/newlines would otherwise change the parsed value or
    # make the file invalid, failing the smoke test before it reaches the CLI.
    output: dict[str, object] = {
        "type": "snowflake",
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "password": os.environ["SNOWFLAKE_PASSWORD"],
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
        "database": "SNOWFLAKE_SAMPLE_DATA",
        "schema": "TPCH_SF1",
        "threads": 1,
    }
    if role := os.environ.get("SNOWFLAKE_ROLE"):
        output["role"] = role
    profile = {"tpch": {"target": "dev", "outputs": {"dev": output}}}
    (project_dir / "profiles.yml").write_text(yaml.safe_dump(profile, sort_keys=False))

    # The seed ships no ``signalforge.yml``; write one into the per-run copy
    # pinning ``prune.sample_strategy: oneshot``. ``oneshot`` is MANDATORY —
    # TPCH_SF1 is read-only, so the default ``materialised`` strategy's
    # ``CREATE TEMPORARY TABLE`` colocated with the source database would be
    # rejected. ``oneshot`` ``sample_rows`` reads TPCH directly and never
    # writes. ``total_budget_seconds`` is bumped above the 300s default so the
    # ~12 sequential grade calls fit at p99 LLM latency.
    (project_dir / "signalforge.yml").write_text(
        textwrap.dedent(
            """\
            # Snowflake live e2e config (issue #124, US-005). The ``oneshot``
            # sample strategy is load-bearing: TPCH_SF1 is a read-only shared
            # database and rejects the default ``materialised`` CTAS.
            llm:
              model: claude-sonnet-4-6
            safety:
              mode: aggregate-only
            prune:
              sample_strategy: oneshot
            grade:
              total_budget_seconds: 600
            """
        )
    )

    exit_code = main(
        [
            "generate",
            _MODEL_UNIQUE_ID,
            "--project-dir",
            str(project_dir),
        ]
    )

    # 1. Exit code 0 — full pipeline (draft -> prune -> grade -> diff)
    #    completed without a typed-error escape.
    assert exit_code == 0, f"expected clean exit; got exit_code={exit_code}"

    # 2. Diff sidecar landed at the default path.
    sidecar = project_dir / ".signalforge" / "diff.json"
    assert sidecar.is_file(), f"diff sidecar missing at {sidecar}"

    # 3. Non-empty diff — the pipeline produced some shippable artifacts.
    report = read_diff_report(project_dir)
    total_entries = report.kept_count + report.flagged_count + report.dropped_count
    assert total_entries >= 1, (
        f"expected at least one diff entry (non-empty diff); "
        f"got kept={report.kept_count} flagged={report.flagged_count} "
        f"dropped={report.dropped_count}"
    )

    # 4. At least one always-passes drop — the v0.1 differentiator. Under
    #    ``oneshot`` prune queries the read-only source ``TPCH_SF1.CUSTOMER``
    #    directly, so the seed declares only REAL TPCH columns; a drafted
    #    ``not_null`` on ``c_custkey`` (the TPCH primary key, naturally NOT
    #    NULL) sees zero failing rows and drops as always-passes.
    decisions = read_prune_decisions(project_dir)
    has_always_passes_drop = any(
        d.decision == "dropped" and d.reason == "always-passes" for d in decisions
    )
    assert has_always_passes_drop, (
        "expected at least one PruneDecision with decision='dropped' and "
        "reason='always-passes' (the v0.1 differentiator). The TPCH seed "
        "declares real NOT NULL source columns (e.g. the primary key "
        "c_custkey) so a drafted not_null returns zero failing rows."
    )

    # 5. Grade aggregate_complete — no degraded calls.
    grade_sidecar = project_dir / ".signalforge" / "grade.json"
    assert grade_sidecar.is_file(), f"grade sidecar missing at {grade_sidecar}"
    grading_report = GradingReport.model_validate_json(grade_sidecar.read_text())
    assert grading_report.aggregate_complete is True, (
        "expected GradingReport.aggregate_complete=True (every (artifact, criterion) "
        "pair scored cleanly); got False — increase grade.total_budget_seconds in "
        "signalforge.yml or investigate the LLM seam."
    )

    # 6. No traceback in stderr (DEC-016 of cli-layer.md — the CLI's single
    #    ``try / except Exception`` boundary plus the ``_safe_excepthook``
    #    install must prevent any traceback from leaking even if the pipeline
    #    raised internally).
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err, (
        f"stderr leaked a Python traceback (DEC-016 violation):\n{captured.err}"
    )
