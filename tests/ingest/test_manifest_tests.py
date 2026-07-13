"""Tests for the manifest-test bridge ``read_manifest_tests`` (#154 US-003).

Exercises the five classification dispositions with hand-authored ``GenericTest``
inputs (no warehouse), the ``</ARTIFACT>`` synthesis strip, the all-missing
``compiled_code`` summary path, model-association filtering, and the real
committed dbt-compiled fixture (``tests/fixtures/dbt_project_expectations``).

``IngestResult`` is produced in-process and handed to prune; it is NOT read back
from a JSONL/sidecar on disk, so no ``extra="forbid"`` drift detector is needed
(see ``tests/ingest/test_models.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import signalforge.ingest.reader as reader_module
from signalforge.draft.models import CandidateTestCustomSQL
from signalforge.ingest import read_manifest_tests
from signalforge.ingest.models import SkippedTest, SkipReason
from signalforge.manifest import load
from signalforge.manifest.models import (
    Column,
    DependsOn,
    GenericTest,
    Manifest,
    Model,
    TestMetadata,
)

_MODEL_UID = "model.shop.orders"
_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "dbt_project_expectations"
_ORDERS_UID = "model.signalforge_test_expectations.orders"

# The closed 3-value SkipReason literal (ingest-layer.md / DEC-014). Every skip
# the bridge emits MUST use one of these — never a 4th.
_VALID_SKIP_REASONS: frozenset[str] = frozenset(
    ("unsupported-test-type", "custom-or-generic-test", "malformed-supported-test")
)


def _make_model(unique_id: str = _MODEL_UID, name: str = "orders") -> Model:
    return Model(
        unique_id=unique_id,
        name=name,
        resource_type="model",
        package_name="shop",
        original_file_path=f"models/{name}.sql",
        path=f"{name}.sql",
        database="db",
        schema="main",  # type: ignore[call-arg]
        columns={"order_id": Column(name="order_id"), "amount": Column(name="amount")},
        raw_code="select 1",
    )


def _generic_test(
    *,
    unique_id: str,
    compiled_code: str | None,
    macro: str | None = "expect_column_values_to_be_between",
    namespace: str | None = "dbt_expectations",
    kwargs: dict[str, Any] | None = None,
    column_name: str | None = None,
    attached_node: str | None = _MODEL_UID,
) -> GenericTest:
    metadata = (
        TestMetadata(name=macro, namespace=namespace, kwargs=kwargs or {})
        if macro is not None
        else None
    )
    return GenericTest(
        unique_id=unique_id,
        compiled_code=compiled_code,
        test_metadata=metadata,
        column_name=column_name,
        depends_on=DependsOn(nodes=[_MODEL_UID]),
        attached_node=attached_node,
    )


def _manifest_with(*tests: GenericTest) -> Manifest:
    return Manifest(
        metadata={},
        nodes={},
        tests={t.unique_id: t for t in tests},
    )


# ---------------------------------------------------------------------------
# The five classification dispositions
# ---------------------------------------------------------------------------


def test_row_returning_deterministic_becomes_candidate() -> None:
    """A row-returning, deterministic, valid body → one CandidateTestCustomSQL."""
    body = 'select * from "db"."main"."orders" where amount < 0'
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.between",
            compiled_code=body,
            kwargs={"min_value": 1000, "max_value": 2000, "column_name": "amount"},
            column_name="amount",
        )
    )
    result = read_manifest_tests(manifest, _make_model())

    assert result.skipped == ()
    assert len(result.candidate.tests) == 1
    test = result.candidate.tests[0]
    assert isinstance(test, CandidateTestCustomSQL)
    assert test.type == "custom_sql"
    # DEC-001 — model-level; the compiled body is self-contained.
    assert test.column is None
    assert test.sql == body
    # The candidate is model-level (no columns emitted).
    assert result.candidate.columns == ()
    assert result.candidate.name == "orders"


def test_absent_compiled_code_is_skipped_with_dbt_compile_remediation() -> None:
    """A test node with null compiled_code → skip naming ``dbt compile``."""
    manifest = _manifest_with(_generic_test(unique_id="test.shop.no_code", compiled_code=None))
    result = read_manifest_tests(manifest, _make_model())

    assert result.candidate.tests == ()
    assert len(result.skipped) == 2  # per-node skip + all-missing summary
    per_node = next(s for s in result.skipped if s.test_name != "(manifest tests)")
    assert per_node.reason == "custom-or-generic-test"
    assert "dbt compile" in per_node.detail


def test_blank_compiled_code_is_treated_as_absent() -> None:
    """Whitespace-only compiled_code is treated as absent (skip, not candidate)."""
    manifest = _manifest_with(_generic_test(unique_id="test.shop.blank", compiled_code="   \n\t  "))
    result = read_manifest_tests(manifest, _make_model())
    assert result.candidate.tests == ()
    assert any(s.reason == "custom-or-generic-test" for s in result.skipped)


def test_count_of_rows_scalar_becomes_candidate() -> None:
    """A count-of-rows scalar body IS now pruned (#267 DEC-003), not skip-recorded.

    A ``SELECT COUNT(*) …`` body is soundly re-interpretable as a failing-rows
    count, so it graduates to a ``CandidateTestCustomSQL`` (``from_manifest=True``)
    carrying the compiled body VERBATIM — the compiler, not ingest, does the
    ``COUNT``-wrap restructure. Previously this skip-recorded
    ``malformed-supported-test``.
    """
    body = "select count(*) as n from orders where amount < 0"
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.rowcount",
            compiled_code=body,
            macro="expect_table_row_count_to_be_between",
            kwargs={"min_value": 1, "max_value": 100},
        )
    )
    result = read_manifest_tests(manifest, _make_model())

    assert result.skipped == ()
    assert len(result.candidate.tests) == 1
    test = result.candidate.tests[0]
    assert isinstance(test, CandidateTestCustomSQL)
    assert test.type == "custom_sql"
    assert test.column is None
    # The compiled body is carried VERBATIM — ingest does no SQL building.
    assert test.sql == body
    assert test.from_manifest is True


def test_non_count_aggregate_body_still_skips() -> None:
    """A non-count aggregate scalar (AVG / SUM / MIN / MAX) still skip-records.

    Only count-of-rows scalars graduate (#267 DEC-005); every other single-row
    aggregate keeps skip-recording ``malformed-supported-test`` with the narrowed
    detail that names the non-count residue.
    """
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.avg",
            compiled_code="select avg(amount) from orders",
            macro="expect_column_mean_to_be_between",
        )
    )
    result = read_manifest_tests(manifest, _make_model())

    assert result.candidate.tests == ()
    assert len(result.skipped) == 1
    skip = result.skipped[0]
    assert skip.reason == "malformed-supported-test"
    # The narrowed detail names the non-count residue and states count scalars
    # are now pruned (#267 DEC-005).
    assert "non-count" in skip.detail.lower()
    assert "#267" in skip.detail


def test_nondeterministic_count_scalar_still_skips() -> None:
    """A count-of-rows scalar with a non-deterministic body still skips.

    The determinism gate lives in the common tail, so a count-scalar that falls
    through the row-returning gate still hits it: a ``current_timestamp`` body
    would make the prune verdict irreproducible → skip ``malformed-supported-test``
    with the non-deterministic detail (NOT the count body reaching a candidate).
    """
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.recent_count",
            compiled_code=("select count(*) from orders where created_at > current_timestamp()"),
            macro="expect_row_values_to_have_recent_data",
        )
    )
    result = read_manifest_tests(manifest, _make_model())

    assert result.candidate.tests == ()
    assert len(result.skipped) == 1
    skip = result.skipped[0]
    assert skip.reason == "malformed-supported-test"
    assert "non-deterministic" in skip.detail.lower()


def test_nondeterministic_body_is_skipped() -> None:
    """A body referencing wall-clock / random funcs → skip (malformed)."""
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.recent",
            compiled_code="select * from orders where created_at > current_timestamp",
            macro="expect_row_values_to_have_recent_data",
        )
    )
    result = read_manifest_tests(manifest, _make_model())

    assert result.candidate.tests == ()
    assert len(result.skipped) == 1
    skip = result.skipped[0]
    assert skip.reason == "malformed-supported-test"
    assert "non-deterministic" in skip.detail.lower()


def test_unsafe_body_is_skipped_via_ingested_sql_safety() -> None:
    """A body the comment-tolerant safety scan rejects → skip (malformed)."""
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.unsafe",
            compiled_code="select * from (orders",  # unbalanced paren
        )
    )
    result = read_manifest_tests(manifest, _make_model())

    assert result.candidate.tests == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].reason == "malformed-supported-test"


def test_comment_bearing_body_is_not_false_rejected() -> None:
    """dbt-compiled SQL with ``--`` / ``/* */`` comments still becomes a candidate."""
    body = "-- generated by dbt-expectations\nselect * from orders /* rule */ where amount < 0"
    manifest = _manifest_with(_generic_test(unique_id="test.shop.commented", compiled_code=body))
    result = read_manifest_tests(manifest, _make_model())
    assert result.skipped == ()
    assert len(result.candidate.tests) == 1


# ---------------------------------------------------------------------------
# Synthesized rationale (DEC-011 / DEC-015)
# ---------------------------------------------------------------------------


def test_rationale_names_macro_and_args() -> None:
    """Rationale = ``<namespace-label> <macro>(column=…, <args>)`` (DEC-011)."""
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.between",
            compiled_code="select * from orders where amount < 0",
            macro="expect_column_values_to_be_between",
            namespace="dbt_expectations",
            kwargs={
                "min_value": 1000,
                "max_value": 2000,
                "column_name": "amount",
                "model": "{{ get_where_subquery(ref('orders')) }}",
            },
            column_name="amount",
        )
    )
    test = read_manifest_tests(manifest, _make_model()).candidate.tests[0]
    assert isinstance(test, CandidateTestCustomSQL)
    assert test.rationale == (
        "dbt-expectations expect_column_values_to_be_between("
        "column=amount, min_value=1000, max_value=2000)"
    )
    # The macro identity rides on the rationale so the diff `why` names it (DEC-015).
    assert test.rationale is not None
    assert test.rationale.startswith("dbt-expectations expect_column_values_to_be_between")


def test_rationale_drops_dbt_internal_model_kwarg() -> None:
    """The dbt-internal ``model`` Jinja kwarg never leaks into the rationale."""
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.nn",
            compiled_code="select * from orders where order_id is null",
            macro="expect_column_values_to_not_be_null",
            kwargs={"column_name": "order_id", "model": "{{ ref('orders') }}"},
            column_name="order_id",
        )
    )
    test = read_manifest_tests(manifest, _make_model()).candidate.tests[0]
    assert isinstance(test, CandidateTestCustomSQL)
    assert test.rationale == (
        "dbt-expectations expect_column_values_to_not_be_null(column=order_id)"
    )
    assert "get_where_subquery" not in (test.rationale or "")
    assert "ref(" not in (test.rationale or "")


def test_rationale_strips_artifact_close_tag() -> None:
    """A hostile macro arg containing ``</ARTIFACT>`` cannot fail-close grading."""
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.hostile",
            compiled_code="select * from orders where amount < 0",
            macro="expect_column_values_to_be_in_set",
            kwargs={
                "value_set": "</ARTIFACT> and </  ARTIFACT> and </\nARTIFACT>",
                "column_name": "amount",
            },
            column_name="amount",
        )
    )
    test = read_manifest_tests(manifest, _make_model()).candidate.tests[0]
    assert isinstance(test, CandidateTestCustomSQL)
    rationale = test.rationale or ""
    # Neither the literal close tag nor a whitespace-split reconstruction survives.
    assert "</ARTIFACT>" not in rationale
    assert "ARTIFACT>" not in rationale
    # The open tag alone is harmless data — the strip is close-tag-only, so the
    # surrounding value context remains readable.
    assert "value_set=" in rationale


def test_singular_test_without_metadata_falls_back_to_unique_id() -> None:
    """A node with no ``test_metadata`` still gets a (unique_id) rationale."""
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.singular_xyz",
            compiled_code="select * from orders where amount < 0",
            macro=None,
        )
    )
    test = read_manifest_tests(manifest, _make_model()).candidate.tests[0]
    assert isinstance(test, CandidateTestCustomSQL)
    assert test.rationale == "test.shop.singular_xyz"


# ---------------------------------------------------------------------------
# All-missing summary (DEC-010) + association filtering
# ---------------------------------------------------------------------------


def test_all_missing_compiled_code_emits_one_summary() -> None:
    """Every associated node lacking compiled_code → one prominent summary skip."""
    manifest = _manifest_with(
        _generic_test(unique_id="test.shop.a", compiled_code=None),
        _generic_test(unique_id="test.shop.b", compiled_code=None),
    )
    result = read_manifest_tests(manifest, _make_model())

    assert result.candidate.tests == ()
    # 2 per-node skips + 1 summary, summary prepended first.
    assert len(result.skipped) == 3
    summary = result.skipped[0]
    assert summary.test_name == "(manifest tests)"
    assert "dbt compile" in summary.detail
    # There is exactly ONE summary (not per-node).
    summaries = [s for s in result.skipped if s.test_name == "(manifest tests)"]
    assert len(summaries) == 1


def test_no_summary_when_some_nodes_have_compiled_code() -> None:
    """A mix of present + absent compiled_code → per-node skip, NO summary."""
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.ok",
            compiled_code="select * from orders where amount < 0",
        ),
        _generic_test(unique_id="test.shop.missing", compiled_code=None),
    )
    result = read_manifest_tests(manifest, _make_model())

    assert len(result.candidate.tests) == 1
    assert all(s.test_name != "(manifest tests)" for s in result.skipped)
    assert len(result.skipped) == 1


def test_no_associated_tests_returns_empty_no_summary() -> None:
    """A model with zero associated test nodes → empty result, no summary."""
    manifest = _manifest_with(
        _generic_test(
            unique_id="test.other.x",
            compiled_code=None,
            attached_node="model.shop.customers",
        )
    )
    result = read_manifest_tests(manifest, _make_model())
    assert result.candidate.tests == ()
    assert result.skipped == ()


def test_tests_for_other_models_are_excluded() -> None:
    """Only nodes associated to the target model are ingested."""
    mine = _generic_test(
        unique_id="test.shop.mine",
        compiled_code="select * from orders where amount < 0",
        attached_node=_MODEL_UID,
    )
    theirs = _generic_test(
        unique_id="test.shop.theirs",
        compiled_code="select * from customers where amount < 0",
        attached_node="model.shop.customers",
    )
    result = read_manifest_tests(_manifest_with(mine, theirs), _make_model())
    assert len(result.candidate.tests) == 1
    test = result.candidate.tests[0]
    assert isinstance(test, CandidateTestCustomSQL)
    assert "customers" not in test.sql


def test_all_skip_reasons_are_within_the_closed_literal() -> None:
    """Every emitted skip reason is one of the 3 closed SkipReason values."""
    manifest = _manifest_with(
        _generic_test(unique_id="test.shop.no_code", compiled_code=None),
        _generic_test(
            unique_id="test.shop.agg",
            compiled_code="select avg(amount) from orders",
            macro="expect_column_mean_to_be_between",
        ),
    )
    result = read_manifest_tests(manifest, _make_model())
    for skip in result.skipped:
        assert isinstance(skip, SkippedTest)
        assert skip.reason in _VALID_SKIP_REASONS
    # Sanity: SkipReason is the closed 3-value set (guards a silent 4th).
    assert set(SkipReason.__args__) == _VALID_SKIP_REASONS  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Real committed dbt-compiled fixture (US-006)
# ---------------------------------------------------------------------------


def test_bridge_routes_real_expectations_fixture() -> None:
    """The bridge routes each of the fixture's 6 dbt-compiled test nodes.

    Routing reflects the actual US-002 helper behaviour, NOT the fixture's
    design-time labels: dbt-expectations wraps every macro (including
    ``expect_table_row_count_to_be_between``) in a row-returning
    ``validation_errors`` shell, so only the ``now()``-referencing recent-data
    body is skip-recorded (non-deterministic). The four other dbt-expectations
    nodes become ``custom_sql`` candidates the prune step then grades
    (always-passes / kept). The #267-US-005 ``no_orders_above_threshold``
    singular test compiles to a bare scalar ``COUNT(*)`` and is graduated to a
    fifth candidate by the count-of-rows path (see
    ``test_bridge_routes_scalar_count_singular_test`` for its own assertions).
    """
    manifest = load(_FIXTURE_DIR)
    model = manifest.get_model(_ORDERS_UID)

    result = read_manifest_tests(manifest, model)

    # 5 candidates (not_null, 2× between, row_count-wrapper, scalar-count
    # singular) + 1 skip (recent_data).
    assert len(result.candidate.tests) == 5
    assert len(result.skipped) == 1

    skip = result.skipped[0]
    assert skip.reason == "malformed-supported-test"
    assert "non-deterministic" in skip.detail.lower()

    # The four dbt-expectations candidates carry a macro-named rationale
    # (DEC-001 / DEC-015); the singular scalar-count node's rationale is its
    # node unique_id (no ``test_metadata``), so it is excluded here.
    macro_candidates = [
        test
        for test in result.candidate.tests
        if (test.rationale or "").startswith("dbt-expectations ")
    ]
    assert len(macro_candidates) == 4

    macros_seen: set[str] = set()
    for test in result.candidate.tests:
        assert isinstance(test, CandidateTestCustomSQL)
        assert test.type == "custom_sql"
        assert test.column is None
        assert test.sql.strip()
        assert test.rationale is not None
    for test in macro_candidates:
        macros_seen.add((test.rationale or "").split("(", 1)[0].split(" ", 1)[1])

    assert macros_seen == {
        "expect_column_values_to_not_be_null",
        "expect_column_values_to_be_between",
        "expect_table_row_count_to_be_between",
    }


def test_bridge_routes_scalar_count_singular_test() -> None:
    """The #267-US-005 singular test compiles to a bare scalar ``COUNT(*)`` and
    is graduated to a ``from_manifest`` candidate — NOT skip-recorded (DEC-008).

    ``tests/no_orders_above_threshold.sql`` has a single ``ref('orders')`` and no
    GROUP BY, so ``dbt compile`` produces a bare top-level
    ``SELECT count(*) FROM "dev"."main"."orders" WHERE amount > 1000`` body. The
    US-001 classifier (``is_prunable_count_scalar``) graduates it, the US-002
    ingest gate routes it to a candidate, and US-003 restructures it — so the
    count-of-rows manifest-ingest prune path turns it into a prunable
    ``CandidateTestCustomSQL`` instead of dropping it into ``skipped``.

    Its associated model must resolve to ``orders``: a singular test carries no
    ``attached_node``, so ``associate_test_model`` falls back to the single
    ``depends_on.nodes`` model entry — and the bridge only includes tests whose
    association equals the queried model, so the candidate's mere presence proves
    the association resolved.
    """
    manifest = load(_FIXTURE_DIR)
    model = manifest.get_model(_ORDERS_UID)

    result = read_manifest_tests(manifest, model)

    singular_uid = "test.signalforge_test_expectations.no_orders_above_threshold"

    # It is a candidate (associated to orders), never skip-recorded.
    assert all(s.test_name != singular_uid for s in result.skipped)
    matches = [
        test
        for test in result.candidate.tests
        if isinstance(test, CandidateTestCustomSQL) and test.rationale == singular_uid
    ]
    assert len(matches) == 1

    candidate = matches[0]
    assert candidate.type == "custom_sql"
    assert candidate.from_manifest is True
    assert candidate.column is None
    # Carries the real dbt-compiled bare-scalar-count body against the qualified
    # ``orders`` relation (the US-003 restructure wraps it into failing-rows
    # form at prune-compile time, downstream of this bridge).
    assert "count(*)" in candidate.sql.lower()
    assert '"orders"' in candidate.sql


# ---------------------------------------------------------------------------
# #268 US-001 — compiled_code size cap + dialect threading
# ---------------------------------------------------------------------------


def test_oversize_compiled_code_is_skip_recorded_with_an_existing_skip_reason(
    monkeypatch: Any,
) -> None:
    """A ``compiled_code`` body over the cap is skip-recorded, never parsed.

    ``read_manifest_tests`` takes an already-parsed ``Manifest``, so the 5 MB
    file-read cap never applies to it — a pathological body would reach sqlglot
    unbounded (#268 DEC-012(2)). The cap must fire BEFORE any gate call, and the
    skip must reuse the closed 3-value ``SkipReason`` (no 4th value).
    """
    calls: list[str] = []
    for gate in ("is_row_returning", "is_prunable_count_scalar", "is_deterministic_sql"):

        def _spy(sql: str, *, dialect: str = "bigquery", _name: str = gate) -> bool:
            calls.append(_name)
            raise AssertionError(f"{_name} must not be reached for an over-cap body")

        monkeypatch.setattr(reader_module, gate, _spy)

    body = "select * from t where x = '" + "a" * reader_module._COMPILED_CODE_SIZE_LIMIT_BYTES + "'"
    manifest = _manifest_with(
        _generic_test(unique_id="test.shop.huge", compiled_code=body, column_name="amount")
    )

    result = read_manifest_tests(manifest, _make_model())

    assert calls == []
    assert result.candidate.tests == ()
    assert len(result.skipped) == 1
    skip = result.skipped[0]
    assert skip.reason in _VALID_SKIP_REASONS
    assert skip.reason == "malformed-supported-test"
    assert "compiled_code" in skip.detail


def test_compiled_code_size_cap_constant_is_256_kib() -> None:
    """Pin the cap VALUE, not just the check's existence (#268 QG).

    The over-cap test derives its body from the live constant, so it stays
    green if the cap is silently RAISED — but raising it re-opens the
    unbounded-sqlglot-parse defence this cap exists to close (a 300 KB body
    would reach the parser). Mirrors the file-cap value pin
    (``test_read_test_files_real_cap_constant_is_5mb``).
    """
    assert reader_module._COMPILED_CODE_SIZE_LIMIT_BYTES == 262_144


def test_under_cap_compiled_code_still_becomes_a_candidate() -> None:
    """The cap must not swallow a realistic dbt-expectations body (negative pin)."""
    body = "select * from t where x = '" + "a" * 1_000 + "'"
    manifest = _manifest_with(
        _generic_test(unique_id="test.shop.ok", compiled_code=body, column_name="amount")
    )

    result = read_manifest_tests(manifest, _make_model())

    assert result.skipped == ()
    assert len(result.candidate.tests) == 1


def test_read_manifest_tests_threads_the_callers_dialect_into_every_gate(
    monkeypatch: Any,
) -> None:
    """Every sqlglot gate call must carry the caller's dialect, not the default.

    Pre-#268 the bridge called the gates with the ``"bigquery"`` default while
    the prune compiler passes ``dialect.name`` — two parses of the same body
    under different dialects can disagree, so the engine and the compiler could
    reach opposite verdicts (#268 DEC-013).
    """
    seen: dict[str, list[str]] = {}

    def _make_spy(name: str, verdict: bool):
        def _spy(sql: str, *, dialect: str = "bigquery") -> bool:
            seen.setdefault(name, []).append(dialect)
            return verdict

        return _spy

    # row-returning False + count-scalar True falls through to the determinism
    # gate, so all three gates are exercised in one pass.
    monkeypatch.setattr(reader_module, "is_row_returning", _make_spy("is_row_returning", False))
    monkeypatch.setattr(
        reader_module, "is_prunable_count_scalar", _make_spy("is_prunable_count_scalar", True)
    )
    monkeypatch.setattr(
        reader_module, "is_deterministic_sql", _make_spy("is_deterministic_sql", True)
    )

    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.dialect",
            compiled_code="select count(*) from t",
            column_name="amount",
        )
    )

    result = read_manifest_tests(manifest, _make_model(), dialect="snowflake")

    assert result.skipped == ()
    assert len(result.candidate.tests) == 1
    assert seen == {
        "is_row_returning": ["snowflake"],
        "is_prunable_count_scalar": ["snowflake"],
        "is_deterministic_sql": ["snowflake"],
    }


def test_read_manifest_tests_dialect_defaults_to_bigquery(monkeypatch: Any) -> None:
    """The new keyword-only ``dialect`` defaults to ``"bigquery"`` so existing
    callers keep today's behaviour byte-for-byte."""
    seen: list[str] = []
    real = reader_module.is_deterministic_sql

    def _spy(sql: str, *, dialect: str = "bigquery") -> bool:
        seen.append(dialect)
        return real(sql, dialect=dialect)

    monkeypatch.setattr(reader_module, "is_deterministic_sql", _spy)

    manifest = _manifest_with(
        _generic_test(
            unique_id="test.shop.default",
            compiled_code="select * from t where amount < 0",
            column_name="amount",
        )
    )
    read_manifest_tests(manifest, _make_model())

    assert seen == ["bigquery"]
