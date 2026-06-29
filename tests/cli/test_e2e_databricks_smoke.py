"""End-to-end smoke test against real Anthropic + real Databricks (issue #226).

US-005 of the Databricks epic (#219). The Databricks analogue of
``tests/cli/test_e2e_snowflake_smoke.py``: it runs the FULL ``signalforge
generate`` pipeline (LLM draft -> prune -> grade -> diff) against the read-only
Unity Catalog ``samples.nyctaxi.trips`` sample table and pins the same
invariants the Snowflake / BigQuery e2es pin — chiefly that the prune step
drops at least one ``always-passes`` test (the v0.1 differentiator) on a natural
NOT NULL source column (``tpep_pickup_datetime``, the trip pickup timestamp).

``safety: schema-only`` + ``prune.scope: sample`` + ``sample_strategy: oneshot``
is the configuration this test exercises. Under ``oneshot`` the prune stage
queries the read-only source table directly (no ``CREATE TEMPORARY TABLE``, no
materialisation against the read-only ``samples`` share), and the sample
row-count routes through the vendor-neutral ``WarehouseAdapter.get_row_count``
seam — which ``DatabricksAdapter`` implements via ``SELECT COUNT(*)`` (#224
DEC-003), so ``oneshot`` works against Databricks today (unlike Snowflake at
#124, where ``oneshot``'s row-count seam was still open and forced
``scope: full``). ``safety: schema-only`` sends redacted column names/types to
the LLM and still drives the full pipeline; prune's own ``run_test_sql`` (#224
DEC-007, implemented) does the warehouse work. ``safety: aggregate-only`` is
avoided only to keep the surface minimal — ``DatabricksAdapter.column_stats``
IS implemented (#224 DEC-011, shipped ahead of Snowflake), but ``schema-only``
is the leanest config that certifies the always-passes drop.

Because the seed model declares only REAL nyctaxi source columns (no renamed /
engineered ``'us' AS region`` literals), under ``oneshot`` every declared column
exists on the source — the LLM reliably drafts ``not_null`` on every column,
and ``not_null`` on ``tpep_pickup_datetime`` (a natural NOT NULL pickup
timestamp) returns zero failing rows -> mathematically always-pass -> dropped by
prune (mirrors the Austin bikeshare natural-NOT-NULL pattern, NOT engineered
literals — a renamed/engineered column would not exist on the source and would
route to ``kept-without-evidence``). The warm-up null-guard below verifies zero
nulls in that column BEFORE the run so the always-passes assertion is
engineered, not flaky.

Gated by FIVE prerequisites — this is a FULL-STACK test (warehouse + LLM). The
warehouse-connection base gate lives in the SHARED
:mod:`tests.warehouse._databricks_live` helper (reused by the US-003 estimate
and US-004 prune live tests); the full pipeline ALSO needs ``ANTHROPIC_API_KEY``
for the drafter + grader, layered on via ``extra_required``:

* ``SF_RUN_DATABRICKS=1`` — the project-wide opt-in for "this test costs real
  money / talks to a real Databricks SQL warehouse" (the Databricks analogue of
  ``SF_RUN_BQ`` / ``SF_RUN_SNOWFLAKE``).
* ``DATABRICKS_SERVER_HOSTNAME`` / ``DATABRICKS_HTTP_PATH`` / ``DATABRICKS_TOKEN``
  — the minimal PAT-auth connection triple consumed by
  :func:`signalforge.warehouse.adapters._databricks_client.make_real_client`.
* ``ANTHROPIC_API_KEY`` — without a key the drafter / grader cannot call the
  LLM seam.

The test is excluded from default ``pytest`` runs by
``addopts = "... -m '... and not databricks' ..."`` in ``pyproject.toml``. The
maintainer runs it once before declaring the Databricks PR ready::

    export SF_RUN_DATABRICKS=1
    export DATABRICKS_SERVER_HOSTNAME=<workspace-host>
    export DATABRICKS_HTTP_PATH=<sql-warehouse-http-path>
    export DATABRICKS_TOKEN=<personal-access-token>
    export ANTHROPIC_API_KEY=sk-...
    uv run pytest -m databricks --no-cov

The ``--no-cov`` flag is required because ``--cov-fail-under`` in ``addopts``
would fail any marker-specific run that exercises only a fraction of the
codebase.

Asserts the invariants from the Snowflake / BigQuery e2es (DEC-009 of
``plans/super/10-e2e-bigquery-smoke.md``):

1. ``signalforge.cli.main(...)`` returns ``0``.
2. ``<project_dir>/.signalforge/diff.json`` exists.
3. ``kept_count + flagged_count + dropped_count >= 1`` (non-empty diff).
4. A :class:`PruneDecision` with ``decision == "dropped"`` and
   ``reason == "always-passes"`` exists in the prune audit — the v0.1
   differentiator.
5. ``GradingReport.aggregate_complete is True`` (no degraded grade calls).
6. ``"Traceback" not in stderr`` (DEC-016 of ``cli-layer.md`` — no traceback
   ever leaks).

Traces to: plans/super/226-* / US-005 ; epic #219.
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
from tests.warehouse._databricks_live import build_live_adapter, skip_reason

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "databricks"

# The seed's model unique_id. The single-model positional path resolves via
# ``Manifest.get_model``, which accepts the unique_id and file-path forms but
# NOT a bare name (a bare name routes to the file-path branch and fails) — see
# ``.claude/rules/testing-signal.md`` § "Multi-surface drift on user-facing
# model arguments".
_MODEL_UNIQUE_ID = "model.signalforge_test_nyctaxi.stg_nyctaxi_trips"

# The natural NOT NULL source column the always-passes drop relies on. The
# fixture README declares ``tpep_pickup_datetime`` (the trip pickup timestamp)
# as the engineered-always-pass anchor: every source row carries a value, so a
# drafted ``not_null`` returns zero failing rows. The warm-up guard below
# verifies the zero-null assumption live before relying on it.
_ALWAYS_PASS_COLUMN = "tpep_pickup_datetime"

# The fully-qualified read-only Unity Catalog source the seed's relation
# resolves to (alias-overridden ``stg_nyctaxi_trips`` -> ``samples.nyctaxi.trips``).
_SOURCE_TABLE = "samples.nyctaxi.trips"


@pytest.mark.databricks
def test_e2e_signalforge_generate_against_nyctaxi(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run ``signalforge generate`` end-to-end against nyctaxi and pin the invariants.

    Skips cleanly under ``pytest -m databricks`` when any prerequisite is
    missing (the shared warehouse-connection gate plus the full-pipeline
    ``ANTHROPIC_API_KEY``) — the maintainer runs the gated invocation once
    before merge and the default suite never reaches this test.
    """
    if reason := skip_reason(extra_required=("ANTHROPIC_API_KEY",)):
        pytest.skip(reason)

    # Copy the read-only seed to ``tmp_path`` so the audit JSONLs (prune.jsonl,
    # grade.jsonl, llm_response.jsonl, safety.jsonl) and the diff sidecar land
    # in the per-run temp dir, not the committed fixture (DEC-008 of #10).
    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # The committed ``profiles.yml`` carries placeholder credentials (regen-only).
    # Rewrite the per-run profile from env vars so the Databricks adapter can
    # authenticate. ``catalog: samples`` / ``schema: nyctaxi`` point at the
    # read-only shared sample catalog; the model's manifest ``relation_name``
    # already resolves to ``samples.nyctaxi.trips`` independently, so the SOURCE
    # table is unchanged.
    #
    # Serialise from a structured mapping via ``yaml.safe_dump`` rather than
    # interpolating raw env vars into a YAML string — a credential containing
    # ``#``/``:``/quotes/newlines would otherwise change the parsed value or
    # make the file invalid, failing the smoke test before it reaches the CLI.
    # The profile name ``nyctaxi`` matches ``dbt_project.yml``'s ``profile:``.
    output: dict[str, object] = {
        "type": "databricks",
        "host": os.environ["DATABRICKS_SERVER_HOSTNAME"],
        "http_path": os.environ["DATABRICKS_HTTP_PATH"],
        "token": os.environ["DATABRICKS_TOKEN"],
        "catalog": "samples",
        "schema": "nyctaxi",
        "threads": 1,
    }
    profile = {"nyctaxi": {"target": "dev", "outputs": {"dev": output}}}
    (project_dir / "profiles.yml").write_text(yaml.safe_dump(profile, sort_keys=False))

    # The seed ships no ``signalforge.yml``; write one into the per-run copy.
    # ``prune.scope: sample`` + ``sample_strategy: oneshot`` queries the
    # read-only source table directly — no ``CREATE TEMPORARY TABLE`` against
    # the read-only ``samples`` share — and the sample row-count routes through
    # the implemented ``DatabricksAdapter.get_row_count`` (``SELECT COUNT(*)``,
    # #224 DEC-003). ``safety: schema-only`` sends redacted column names/types
    # to the LLM and still drives the full pipeline; prune's own ``run_test_sql``
    # does the warehouse work. ``total_budget_seconds`` is bumped above the 300s
    # default so the sequential grade calls fit at p99 LLM latency.
    (project_dir / "signalforge.yml").write_text(
        textwrap.dedent(
            """\
            # Databricks live e2e config (issue #226, US-005). ``oneshot``
            # sampling queries the read-only source directly via the
            # implemented ``get_row_count`` seam (#224 DEC-003).
            llm:
              model: claude-sonnet-4-6
            safety:
              mode: schema-only
            prune:
              scope: sample
              sample_strategy: oneshot
            grade:
              total_budget_seconds: 600
            """
        )
    )

    # Warm-up null-guard (engineered-determinism, NOT a flaky live assertion):
    # before relying on ``not_null`` on the always-pass column dropping as
    # always-passes, verify the column actually has zero nulls in the live
    # source table. ``run_test_sql`` wraps the failing-rows SELECT in a
    # ``COUNT(*)`` and returns the count, so a count of 0 confirms the natural
    # NOT NULL assumption the seed README documents. A non-zero count here means
    # the source changed shape — fail the warm-up loudly rather than let the
    # downstream always-passes assertion become flaky.
    warmup_adapter = build_live_adapter()
    with warmup_adapter:
        null_check = warmup_adapter.run_test_sql(
            f"SELECT * FROM {_SOURCE_TABLE} WHERE {_ALWAYS_PASS_COLUMN} IS NULL"
        )
    assert null_check.failure_count == 0, (
        f"warm-up null-guard: expected zero NULLs in "
        f"{_SOURCE_TABLE}.{_ALWAYS_PASS_COLUMN} so the drafted not_null drops as "
        f"always-passes; found {null_check.failure_count} NULL row(s). The "
        f"source table shape changed — pick a different natural-NOT-NULL column."
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
    #    ``oneshot`` prune queries the read-only source ``samples.nyctaxi.trips``
    #    directly, so the seed declares only REAL nyctaxi columns; a drafted
    #    ``not_null`` on ``tpep_pickup_datetime`` (the pickup timestamp, naturally
    #    NOT NULL — confirmed by the warm-up guard above) sees zero failing rows
    #    and drops as always-passes.
    decisions = read_prune_decisions(project_dir)
    has_always_passes_drop = any(
        d.decision == "dropped" and d.reason == "always-passes" for d in decisions
    )
    assert has_always_passes_drop, (
        "expected at least one PruneDecision with decision='dropped' and "
        "reason='always-passes' (the v0.1 differentiator). The nyctaxi seed "
        "declares real NOT NULL source columns (e.g. tpep_pickup_datetime) so a "
        "drafted not_null returns zero failing rows."
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
