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
from collections.abc import Mapping
from pathlib import Path

import pytest

from signalforge.cli import main
from signalforge.prune.models import PruneDecision
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


def _commented_not_null_body(column: str, *, negate: bool) -> str:
    """A dbt-expectations not-null body carrying real ``--`` and ``/* */`` comments.

    #270 US-005 (G3). Identical row-returning shape to
    :func:`_expectations_not_null_body`, but interleaved with the two comment
    styles real dbt ``compiled_code`` routinely emits: leading ``--`` /
    ``/* */`` banners and trailing ``--`` line comments. The prune compiler's
    ``from_manifest`` arm strips these once, on the complete string, BEFORE the
    adapter's comment-**intolerant** execution-time ``validate_test_sql`` sees
    it (#270 DEC-001) — so the certification this body carries is that
    **BigQuery ACCEPTS the comment-stripped SQL and returns a real verdict**.
    Pre-#270 the comment-intolerant validator rejected such a body and it never
    pruned at all (G3); only a live warehouse proves the stripped bytes run.

    ``negate=False`` → ``<col> IS NOT NULL`` → always-pass (zero failing rows);
    ``negate=True`` → ``<col> IS NULL`` → every sampled row is a failing row.
    Both exact on the real source because ``trip_id`` / ``start_time`` are
    naturally NOT NULL (the engineered-determinism argument in the module
    docstring applies verbatim).
    """
    predicate = f"{column} is null" if negate else f"{column} is not null"
    return (
        f"-- dbt-expectations expect_column_values_to_not_be_null ({column})\n"
        "/* generated by `dbt compile`; do not edit by hand */\n"
        "with grouped_expression as (\n"
        "  select\n"
        f"    {predicate} as expression  -- the not-null predicate\n"
        f"  from {_RELATION}  /* the model relation */\n"
        "),\n"
        "validation_errors as (\n"
        "  select *\n"
        "  from grouped_expression\n"
        "  where not(expression = true)  -- keep only the failing rows\n"
        ")\n"
        "select *\n"
        "from validation_errors"
    )


def _count_scalar_body(column: str, *, empty: bool) -> str:
    """A bare ``SELECT count(*) FROM <relation> WHERE <engineered>`` count scalar.

    #270 US-005 (the #267 count-scalar restructure cert). A count-of-rows
    scalar is NOT ``is_row_returning`` — under the adapter's
    ``SELECT COUNT(*) AS failures FROM (<sql>) AS t`` envelope a scalar body
    collapses to ONE row, so the outer ``COUNT(*)`` would be ``1`` regardless of
    the count's value (the always-1 bug). #267 restructures a prunable count
    scalar to
    ``SELECT sf_agg_value FROM (SELECT (<body>) AS sf_agg_value) AS sf_agg WHERE
    sf_agg_value <> 0`` so the envelope yields **0 rows (pass) / 1 row (fail)**.

    A count scalar is NEVER sampled (#268 DEC-003 — a ``COUNT(*)`` over a
    hash-mod'd sample is meaningless), so under ``--scope sample`` it bypasses to
    the SOURCE (``bypassed_to_source is True``) and the body runs verbatim
    against the real relation. Engineered determinism against natural NOT NULL
    columns:

    * ``empty=True``  → ``WHERE <col> IS NULL``     → count is **0** →
      restructure returns 0 rows → ``always-passes`` → **dropped**.
    * ``empty=False`` → ``WHERE <col> IS NOT NULL`` → count is the (large,
      non-zero) row total → restructure returns exactly **1** row → **kept**
      with ``failures >= 1``.

    ``IS NULL`` / ``IS NOT NULL`` never evaluate to NULL, so neither verdict is
    exposed to three-valued-logic drift; the count's exact magnitude is
    irrelevant (only zero-vs-non-zero drives the restructure), so both verdicts
    are robust against the source table's row-count drift.
    """
    condition = f"{column} is null" if empty else f"{column} is not null"
    return f"select count(*)\nfrom {_RELATION}\nwhere {condition}"


def _index_ingested_by_predicate(
    ingested: list[PruneDecision], predicates: Mapping[str, str]
) -> dict[str, PruneDecision]:
    """Map each label to the ONE ingested decision whose SQL carries its predicate.

    The manifest-ingested candidates are model-level (``column=None``), so they
    share a ``test_anchor`` and cannot be told apart by it — the engineered WHERE
    predicate embedded in ``compiled_sql`` is the only stable per-candidate key.
    Asserts a strict 1:1 mapping (every decision matches exactly one label, every
    label exactly one decision) so a mislabelled or duplicated match fails loud
    rather than silently letting a swapped verdict pass. Callers must choose
    predicates that are mutually non-substring (e.g. ``x is not null`` is NOT a
    substring of ``x is null``).
    """
    result: dict[str, PruneDecision] = {}
    for decision in ingested:
        sql = (decision.compiled_sql or "").lower()
        matched = [label for label, needle in predicates.items() if needle.lower() in sql]
        assert len(matched) == 1, (
            f"expected each ingested body to match exactly one predicate label; "
            f"{decision.compiled_sql!r} matched {matched}"
        )
        label = matched[0]
        assert label not in result, f"two ingested bodies matched label {label!r}"
        result[label] = decision
    assert set(result) == set(predicates), (
        f"not every predicate label matched a decision: "
        f"expected {set(predicates)}, got {set(result)}"
    )
    return result


def _write_external_schema_and_billing_profile(project_dir: Path) -> Path:
    """Write the (test-less) external ``schema.yml`` + a billing-project profile.

    Factored out of the first live-cert test so the two #270 US-005 additions
    reuse the identical setup without re-inlining it. The external schema
    ``prune-existing`` requires deliberately carries NO tests — every candidate
    in these runs comes from ``--from-manifest`` so the routing under test is not
    diluted by drafted / schema built-ins. The committed profile pins
    ``project: bigquery-public-data`` (publicly readable but un-billable), so the
    per-run copy is rewritten to bill the maintainer's ``GOOGLE_CLOUD_PROJECT``;
    ``maximum_bytes_billed: 1 GB`` clears the 100 MB default for any full-scan.

    Returns the path to the external schema, ready to pass as ``--schema``.
    """
    schema_path = project_dir / "external_schema.yml"
    schema_path.write_text(
        "version: 2\nmodels:\n"
        "  - name: stg_bikeshare_trips\n"
        "    description: Austin bikeshare trips (source-as-model e2e fixture).\n"
    )
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
    return schema_path


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


@pytest.mark.bigquery
def test_e2e_ingested_comment_bearing_body_executes_against_bigquery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LIVE (#270 G3): a comment-bearing dbt body prunes to a real verdict.

    Injects three ingested manifest tests whose ``compiled_code`` carries real
    dbt-style ``--`` line comments AND ``/* */`` block comments, then runs
    ``prune-existing … --from-manifest --scope sample --sample-strategy
    materialised`` against real BigQuery and pins the #270 DEC-001 (G3) fix
    end-to-end:

    1. exit 0, no traceback (``cli-layer.md`` DEC-016);
    2. **the compiler stripped the comments** — every dispatched
       ``decision.compiled_sql`` is comment-free (no ``--`` / ``/*`` / ``*/``),
       which is what lets the adapter's execution-time comment-**intolerant**
       ``validate_test_sql`` accept it. Pre-#270 a comment-bearing body was
       rejected there and never pruned at all (G3);
    3. **BigQuery accepted the stripped SQL and returned real verdicts** — ≥1
       engineered always-passes body dropped, ≥1 engineered-violation body kept
       with ≥1 failure. Snapshot / parse-guard equality certifies the stripped
       *shape*; only this live run certifies the warehouse *runs* it (the
       ``warehouse-adapters.md`` "SHAPE ≠ ACCEPTANCE" lesson).

    Routing (sample vs source-bypass) is deliberately NOT asserted here — the
    first test already pins the sample-rewrite cert with comment-free bodies;
    this test's contract is *comment acceptance + a real verdict*, which holds
    on either routing (the compiler strips comments in BOTH the rewritten-override
    and the verbatim-source return paths, #270 DEC-001).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # Two always-pass + one always-fail, all comment-bearing. `>= 1`-shaped
    # assertions never depend on the exact split, but engineering both verdicts
    # rules out "the stripped SQL trivially always-passed / always-failed".
    always_pass_ids = {
        inject_manifest_test_node(
            project_dir,
            model_unique_id=_MODEL_UNIQUE_ID,
            test_name="sf270_comment_not_null_trip_id",
            column_name="trip_id",
            compiled_code=_commented_not_null_body("trip_id", negate=False),
        ),
        inject_manifest_test_node(
            project_dir,
            model_unique_id=_MODEL_UNIQUE_ID,
            test_name="sf270_comment_not_null_start_time",
            column_name="start_time",
            compiled_code=_commented_not_null_body("start_time", negate=False),
        ),
    }
    inject_manifest_test_node(
        project_dir,
        model_unique_id=_MODEL_UNIQUE_ID,
        test_name="sf270_comment_violation_trip_id",
        column_name="trip_id",
        compiled_code=_commented_not_null_body("trip_id", negate=True),
    )
    assert len(always_pass_ids) == 2  # unique_ids collided → the seed is broken.

    schema_path = _write_external_schema_and_billing_profile(project_dir)

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
        "expected the three injected comment-bearing manifest test nodes to reach "
        f"prune; got {[(d.test_anchor, d.test.type) for d in decisions]}"
    )

    # (2) — THE G3 cert: the compiler stripped every comment before the adapter's
    # comment-intolerant validator ran. If any marker survived, the body would
    # have been rejected at execution and could not have produced a verdict.
    for decision in ingested:
        assert decision.compiled_sql is not None
        assert "--" not in decision.compiled_sql, (
            "a `--` line comment survived into the dispatched SQL — the "
            f"comment-strip did not run:\n{decision.compiled_sql}"
        )
        assert "/*" not in decision.compiled_sql and "*/" not in decision.compiled_sql, (
            "a `/* */` block comment survived into the dispatched SQL — the "
            f"comment-strip did not run:\n{decision.compiled_sql}"
        )

    # (3) — real verdicts, proving BigQuery accepted and ran the stripped SQL,
    # asserted PER CANDIDATE by the body's predicate (the model-level test_anchor
    # is shared, so compiled_sql is the only stable per-candidate key). A
    # `>= 1 dropped AND >= 1 kept` check would pass even if the always-pass and
    # violation verdicts were swapped; keying by predicate rules that out.
    # `<col> is not null` is not a substring of `<col> is null`, so the keys are
    # unambiguous; magnitudes are irrelevant (`>= 1`, never `== N`).
    verdicts = _index_ingested_by_predicate(
        ingested,
        {
            "pass_trip_id": "trip_id is not null",
            "pass_start_time": "start_time is not null",
            "violation": "trip_id is null",
        },
    )
    for key in ("pass_trip_id", "pass_start_time"):
        d = verdicts[key]
        assert d.decision == "dropped" and d.reason == "always-passes", (
            f"comment-bearing always-pass body {key!r} was not dropped: "
            f"{(d.decision, d.reason, d.failures)}"
        )
    violation = verdicts["violation"]
    assert violation.decision == "kept" and violation.reason == "kept", (
        "the comment-bearing engineered-violation test was not kept: "
        f"{(violation.decision, violation.reason, violation.failures)}"
    )
    assert violation.failures is not None and violation.failures >= 1


@pytest.mark.bigquery
def test_e2e_ingested_count_scalar_restructure_executes_against_bigquery(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LIVE (#270 / #267): a count-scalar body restructures to a real verdict.

    Injects two ingested manifest tests whose ``compiled_code`` is a bare
    ``SELECT count(*) FROM <relation> WHERE <engineered>`` scalar, then runs
    ``prune-existing … --from-manifest --scope sample --sample-strategy
    materialised`` against real BigQuery and pins the #267 count-scalar
    restructure end-to-end:

    1. exit 0, no traceback;
    2. a count scalar is NOT ``is_row_returning``, so it is **never sampled**
       (#268 DEC-003) — every count-scalar decision has ``bypassed_to_source is
       True`` and runs against the SOURCE relation, at ``scope="sample"``;
    3. **the #267 restructure was emitted** — every ``decision.compiled_sql``
       carries the ``SELECT sf_agg_value FROM (SELECT (<body>) AS sf_agg_value)
       AS sf_agg WHERE sf_agg_value <> 0`` wrap (so the adapter's
       ``SELECT COUNT(*) AS failures FROM (<sql>) AS t`` envelope yields 0 rows /
       1 row instead of the always-1 bug);
    4. **BigQuery ran it with the correct verdict** — the engineered
       ``WHERE trip_id IS NULL`` (count 0) body is DROPPED as ``always-passes``;
       the engineered ``WHERE trip_id IS NOT NULL`` (count > 0) body is KEPT with
       ``failures >= 1``. Only a live run proves the warehouse accepts the
       restructured SQL and the reinterpretation (``0 = pass``) is faithful to
       the real row data (``testing-signal.md`` engineered determinism).
    """
    if reason := _skip_reason():
        pytest.skip(reason)

    project_dir = copy_fixture_to_tmp(_FIXTURE_DIR, tmp_path)

    # One guaranteed-zero count (→ drop) and one guaranteed-non-zero count
    # (→ keep), both against natural NOT NULL source columns.
    inject_manifest_test_node(
        project_dir,
        model_unique_id=_MODEL_UNIQUE_ID,
        test_name="sf270_count_zero_trip_id",
        column_name="trip_id",
        compiled_code=_count_scalar_body("trip_id", empty=True),
        macro_name="assert_row_count",
        macro_namespace=None,
    )
    inject_manifest_test_node(
        project_dir,
        model_unique_id=_MODEL_UNIQUE_ID,
        test_name="sf270_count_nonzero_trip_id",
        column_name="trip_id",
        compiled_code=_count_scalar_body("trip_id", empty=False),
        macro_name="assert_row_count",
        macro_namespace=None,
    )

    schema_path = _write_external_schema_and_billing_profile(project_dir)

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
    assert len(ingested) == 2, (
        "expected the two injected count-scalar manifest test nodes to reach "
        f"prune; got {[(d.test_anchor, d.test.type) for d in decisions]}"
    )

    # (2) + (3) — a count scalar bypasses to source and carries the restructure.
    for decision in ingested:
        assert decision.compiled_sql is not None
        assert decision.bypassed_to_source is True, (
            "a count scalar must never be sampled (#268 DEC-003); dispatched SQL:\n"
            f"{decision.compiled_sql}"
        )
        assert decision.scope == "sample"
        assert "SELECT sf_agg_value FROM (SELECT (" in decision.compiled_sql, (
            "the #267 count-scalar restructure prefix is missing — the body was "
            f"NOT restructured before the failing-rows envelope:\n{decision.compiled_sql}"
        )
        assert ") AS sf_agg_value) AS sf_agg WHERE sf_agg_value <> 0" in decision.compiled_sql, (
            f"the #267 count-scalar restructure suffix is missing:\n{decision.compiled_sql}"
        )

    # (4) — the engineered verdicts, asserted PER CANDIDATE by the body's WHERE
    # predicate (both nodes share the model-level ``test_anchor``, so the
    # compiled_sql is the only stable per-candidate key). A `>= 1 dropped AND
    # >= 1 kept` check would pass even if the two verdicts were SWAPPED; keying by
    # predicate proves the zero-count body drops and the non-zero body keeps.
    # `trip_id is not null` is not a substring of `trip_id is null`, so the keys
    # are unambiguous; magnitudes are irrelevant (`>= 1`, never `== N`).
    verdicts = _index_ingested_by_predicate(
        ingested, {"zero": "trip_id is null", "nonzero": "trip_id is not null"}
    )
    zero = verdicts["zero"]
    nonzero = verdicts["nonzero"]
    assert zero.decision == "dropped" and zero.reason == "always-passes", (
        "the guaranteed-zero count scalar was not dropped as always-passes — the "
        f"restructure or the 0 = pass reinterpretation is wrong: "
        f"{(zero.decision, zero.reason, zero.failures)}"
    )
    assert nonzero.decision == "kept" and nonzero.reason == "kept", (
        "the guaranteed-non-zero count scalar was not kept — the restructure may "
        f"have collapsed a non-zero count to zero rows: "
        f"{(nonzero.decision, nonzero.reason, nonzero.failures)}"
    )
    assert nonzero.failures is not None and nonzero.failures >= 1
