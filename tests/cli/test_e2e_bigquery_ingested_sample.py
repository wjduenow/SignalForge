"""Gated BigQuery LIVE cert for sampled manifest-ingested tests (#268 / US-008).

**This is the merge gate for #268 (DEC-016), not the sqlglot parse-guard.**

``warehouse-adapters.md``: *"sqlglot-parse + snapshot + fakes certify SHAPE,
NOT that a live warehouse ACCEPTS the SQL — and the gap is not exotic."*
#226's live Databricks run found **three** bugs that the entire offline tier —
snapshots, the sqlglot parse-guard, AND the fakes — passed clean. #121 and #171
carry the same lesson. Everything #268's offline suite pins is therefore
*necessary but not sufficient*; three things can only be certified here:

1. **Warehouse ACCEPTANCE of a token-spliced dbt body.** sqlglot certifies
   syntax; only BigQuery certifies that it runs.
2. **``_SESSION._sf_sample_*`` reachability from a rewritten dbt body.** The
   fake never enforces session binding. Only a live run proves the temp table
   ``materialise_sample`` created inside the BigQuery *session* is visible to
   the ``FROM`` clause the rewriter spliced into an ingested test's SQL — i.e.
   that the ``session_id`` connection property is attached to the per-test
   query too, not just the CTAS.
3. **Real dbt-BigQuery ``compiled_code`` quoting.** There is no
   BigQuery-compiled ``compiled_code`` fixture in this repo — the committed
   ``dbt_project_expectations`` manifest is **DuckDB**-compiled
   (``"dev"."main"."orders"``) and every BigQuery body in the unit suite is
   hand-re-quoted DuckDB output. If real dbt-bigquery emits a shape the AST
   locator does not model, only this test sees it.

Path: ``signalforge prune-existing`` — **no LLM**. The drafter adds nothing to
what this test proves and would cost an extra API key plus a non-deterministic
candidate set. So the gate is two env vars, not three.

Belt-and-suspenders gating (``testing-signal.md``):

1. ``@pytest.mark.bigquery`` — registered in ``pyproject.toml`` and deselected
   by the default ``addopts`` (``-m 'not bigquery and …'``).
2. A runtime :func:`_skip_reason` naming each missing env var.

Maintainer invocation (``--no-cov`` because ``--cov-fail-under`` in ``addopts``
fails any marker-scoped run)::

    gcloud auth application-default login
    export GOOGLE_CLOUD_PROJECT=<billing-project>
    SF_RUN_BQ=1 uv run pytest -m bigquery --no-cov \\
        tests/cli/test_e2e_bigquery_ingested_sample.py

``bigquery-public-data.austin_bikeshare`` is publicly readable but the runner's
own project is billed for the bytes scanned (~$0.005: one full-scan CTAS for
the materialised sample, then three tiny per-test queries against the temp
table).

Engineered determinism (``testing-signal.md`` § "Engineered determinism for
LLM-driven assertions" — the same rule applies to any live assertion):

The Austin fixture is **source-as-model** — the model's ``alias`` is overridden
so its relation resolves directly to the real pre-existing SOURCE table
(``bigquery-public-data.austin_bikeshare.bikeshare_trips``). The prune engine
queries **the relation**, never the model's ``raw_code``. So every column named
in an injected body MUST exist on the SOURCE; an engineered literal / COALESCE
column (``'austin' AS region``) lives only in the never-executed ``raw_code``
and would compile to an *invalid identifier* → ``kept-without-evidence``, never
``always-passes``. (This exact mistake shipped in #124's first seed.) Both
verdicts therefore ride on ``trip_id`` / ``start_time`` — **natural NOT NULL**
columns of the real source table:

* ``expression = (col IS NOT NULL)`` → always ``true`` → the dbt-expectations
  ``where not(expression = true)`` shell returns **zero** rows →
  mathematically guaranteed ``always-passes`` → **dropped**.
* ``expression = (col IS NULL)`` → always ``false`` → the same shell returns
  **every** sampled row → mathematically guaranteed failing rows → **kept**.

``IS NULL`` / ``IS NOT NULL`` never evaluate to NULL, so neither verdict is
exposed to three-valued-logic drift.

DEC-010: sampling only engages with **≥2 samplable ingested candidates** (a
single-samplable batch keeps bypassing to source — a ``SELECT *`` CTAS cannot
pay for one narrow test). Three nodes are injected, so the gate is cleared with
margin; assertions are ``>= 1``-shaped, never ``== N``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signalforge.cli import main
from tests.cli._e2e_helpers import (
    copy_fixture_to_tmp,
    inject_manifest_test_node,
    read_prune_decisions,
)

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "dbt_project_austin"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_MODEL_UNIQUE_ID = "model.signalforge_test_austin.stg_bikeshare_trips"

#: The model's relation, in **dbt-BigQuery** quoting: three separately
#: backtick-quoted components. This is the exact token shape the #268 AST
#: locator + token-splicer must find and rewrite — and the shape NOT present
#: in any committed fixture (the expectations manifest is DuckDB-compiled).
_RELATION = "`bigquery-public-data`.`austin_bikeshare`.`bikeshare_trips`"


def _expectations_not_null_body(column: str, *, negate: bool) -> str:
    """A dbt-expectations ``expect_column_values_to_not_be_null``-shaped body.

    Mirrors the macro's real compiled output: a ``grouped_expression`` CTE
    projecting the boolean predicate, a ``validation_errors`` CTE selecting the
    rows where the predicate did not hold, and a final ``select * from
    validation_errors``. Row-returning (never a bare scalar) — which is why
    dbt-expectations bodies are prunable at all (#154).

    The relation appears **once, inside a CTE body** — the realistic dbt shape,
    and the one that defeated #154's string-substitution approach (the CTE
    aliases ``grouped_expression`` / ``validation_errors`` also parse as
    ``exp.Table`` and must be excluded from the rewrite by the CTE-alias set).

    ``negate=False`` → ``<col> IS NOT NULL`` → always-pass (zero failing rows).
    ``negate=True``  → ``<col> IS NULL``     → every sampled row is a failing
    row. Both are exact on the real source table because ``trip_id`` /
    ``start_time`` are naturally NOT NULL.
    """
    predicate = f"{column} is null" if negate else f"{column} is not null"
    return (
        "with grouped_expression as (\n"
        "  select\n"
        f"    {predicate} as expression\n"
        f"  from {_RELATION}\n"
        "),\n"
        "validation_errors as (\n"
        "  select *\n"
        "  from grouped_expression\n"
        "  where not(expression = true)\n"
        ")\n"
        "select *\n"
        "from validation_errors"
    )


def _skip_reason() -> str | None:
    """Return a skip reason if any required env var is missing, else ``None``.

    Each missing prerequisite names itself so a maintainer running
    ``pytest -m bigquery`` sees exactly what to set. Treats an empty /
    whitespace-only value as unset (it would otherwise reach the BigQuery
    client and surface as a noisy auth failure rather than a clean skip).
    """
    if os.environ.get("SF_RUN_BQ", "").lower() not in _TRUTHY:
        return "SF_RUN_BQ=1 required (this test costs real money against BigQuery)"
    if not os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return (
            "GOOGLE_CLOUD_PROJECT required (BigQuery billing project; "
            "bigquery-public-data is readable but billed to the runner)"
        )
    return None


@pytest.mark.bigquery
def test_e2e_ingested_manifest_tests_sample_against_materialised_temp_table(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LIVE: sampled manifest-ingested tests run against ``_SESSION._sf_sample_*``.

    Runs ``signalforge prune-existing <model> --schema … --from-manifest
    --scope sample --sample-strategy materialised`` against real BigQuery and
    pins the whole #268 contract end-to-end:

    1. exit 0, no traceback (``cli-layer.md`` DEC-016);
    2. every injected ingested test was **dispatched against the materialised
       temp table** — ``decision.compiled_sql`` references
       ``_SESSION._sf_sample_<16-hex>`` and **never** the source relation. *This
       is the assertion that proves the epic works against a real warehouse:*
       it means BigQuery accepted the spliced dbt body AND resolved the
       session-scoped temp table from it;
    3. ``bypassed_to_source is False`` on those decisions (the audit's
       sampled-vs-bypassed discriminator, #268 DEC-011);
    4. ≥1 engineered **always-passes** ingested test was **dropped** — the
       Architectural-Commitment-#1 payoff, now reachable at sample scope;
    5. the engineered-**violation** ingested test was **kept** with ≥1 failure —
       the inverse pin, which is what rules out "the rewrite pointed at an
       empty/wrong table and everything trivially always-passed" (a rewrite that
       silently landed on a zero-row relation would satisfy (4) alone and
       DELETE a real test — the worst outcome the system can produce).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Three ingested candidates. Two always-pass, one always-fails; all three
    # are samplable, clearing the DEC-010 ">= 2 samplable" gate with margin.
    always_pass_ids = {
        inject_manifest_test_node(
            project_dir,
            model_unique_id=_MODEL_UNIQUE_ID,
            test_name="sf268_not_null_trip_id",
            column_name="trip_id",
            compiled_code=_expectations_not_null_body("trip_id", negate=False),
        ),
        inject_manifest_test_node(
            project_dir,
            model_unique_id=_MODEL_UNIQUE_ID,
            test_name="sf268_not_null_start_time",
            column_name="start_time",
            compiled_code=_expectations_not_null_body("start_time", negate=False),
        ),
    }
    inject_manifest_test_node(
        project_dir,
        model_unique_id=_MODEL_UNIQUE_ID,
        test_name="sf268_violation_trip_id",
        column_name="trip_id",
        compiled_code=_expectations_not_null_body("trip_id", negate=True),
    )
    assert len(always_pass_ids) == 2  # unique_ids collided → the seed is broken.

    # The external schema.yml `prune-existing` requires. Deliberately carries
    # NO tests: every candidate in this run comes from --from-manifest, so the
    # routing under test is not diluted by drafted/schema built-ins.
    schema_path = project_dir / "external_schema.yml"
    schema_path.write_text(
        "version: 2\nmodels:\n"
        "  - name: stg_bikeshare_trips\n"
        "    description: Austin bikeshare trips (source-as-model e2e fixture).\n"
    )

    # The committed profile pins `project: bigquery-public-data` so the regen
    # script's `dbt parse` can read the public dataset's metadata; at QUERY time
    # the SDK uses `profile.project` as the *billing* project and nobody can
    # bill `bigquery-public-data`. Rewrite the per-run copy to bill the
    # maintainer. `maximum_bytes_billed: 1 GB` bumps the 100 MB default so the
    # materialised-sample CTAS (a full scan — the hash-mod predicate requires
    # one) fits; the per-test queries against the temp table are tiny.
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
            "prune-existing",
            "stg_bikeshare_trips",
            "--schema",
            str(schema_path),
            "--from-manifest",
            "--scope",
            "sample",
            "--sample-strategy",
            "materialised",
            "--project-dir",
            str(project_dir),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0, (
        f"expected clean exit; got {exit_code}\n--- stdout ---\n{captured.out}\n"
        f"--- stderr ---\n{captured.err}"
    )
    assert "Traceback" not in captured.err

    decisions = read_prune_decisions(project_dir)
    ingested = [d for d in decisions if d.test.type == "custom_sql"]
    assert len(ingested) == 3, (
        "expected the three injected manifest test nodes to reach prune; got "
        f"{[(d.test_anchor, d.test.type) for d in decisions]}"
    )

    # (2) + (3) — THE live-cert assertion. Every ingested body was rewritten to
    # the materialised temp table, BigQuery ACCEPTED the spliced SQL, and the
    # session-scoped temp table was reachable from it.
    for decision in ingested:
        assert decision.compiled_sql is not None
        assert "_SESSION._sf_sample_" in decision.compiled_sql, (
            "ingested test was NOT rewritten to the materialised temp table — "
            f"dispatched SQL:\n{decision.compiled_sql}"
        )
        # The temp relation is `_SESSION._sf_sample_<16-hex>`, so the source
        # table's name is a clean discriminator: if it survives ANYWHERE in the
        # dispatched SQL, the rewrite missed an occurrence and the test read
        # PRODUCTION (the sharpest hazard in the epic — a sample joined against
        # prod still parses cleanly).
        assert "bikeshare_trips" not in decision.compiled_sql, (
            "the SOURCE relation survived the rewrite — the test may have run "
            f"against production:\n{decision.compiled_sql}"
        )
        assert decision.bypassed_to_source is False
        assert decision.scope == "sample"

    # (4) — the always-passes drop, at SAMPLE scope. `>= 1`, never `== N`.
    dropped = [d for d in ingested if d.decision == "dropped" and d.reason == "always-passes"]
    assert len(dropped) >= 1, (
        "no ingested test was dropped as always-passes; verdicts: "
        f"{[(d.test_anchor, d.decision, d.reason, d.failures) for d in ingested]}"
    )

    # (5) — the engineered-violation inverse pin. Without this, a rewrite that
    # silently pointed at an empty relation would satisfy (4) and DELETE a real
    # test.
    kept = [d for d in ingested if d.decision == "kept" and d.reason == "kept"]
    assert len(kept) >= 1, (
        "the engineered-violation ingested test was not kept — the rewritten SQL "
        "may be reading an empty relation; verdicts: "
        f"{[(d.test_anchor, d.decision, d.reason, d.failures) for d in ingested]}"
    )
    assert all(d.failures is not None and d.failures >= 1 for d in kept)
