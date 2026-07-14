"""#268 US-007 — the UNGATED sqlglot parse-guard over the ingested-rewrite fixtures.

The #268 relation rewrite is the only place SignalForge edits **foreign** SQL: dbt
rendered the ``compiled_code``, not us, and we splice the model's relation out of
it and the materialised temp table in. A splice that produced *syntactically*
broken SQL would surface as a warehouse ``QuerySyntaxError`` →
``kept-without-evidence`` — a silent loss of every ingested verdict, on a path no
snapshot can catch (a snapshot happily pins invalid SQL byte-for-byte; the #121 /
#171 lesson).

So the fixtures under ``tests/fixtures/prune/compiled_sql/ingested/`` carry, per
case, dbt's body (``<name>.in.sql``) and the rewritten body the production splice
emits (``<name>.out.sql``), and this module gates them on four axes:

1. **Drift** — re-deriving ``.out.sql`` from ``.in.sql`` through the production
   helpers (``plan_relation_rewrite`` → ``_build_ingested_rewrite``) must be
   byte-equal to the committed fixture.
2. **Validity** — every ``.out.sql`` parses in its dialect under ``sqlglot``.
3. **Integrity** — the DEC-004 post-condition (``verify_relation_rewrite``): zero
   residual source relations, exactly *N* temp relations.
4. **Coverage** — a ``>= _MIN_FIXTURES`` floor plus an orphan check, so an empty
   or half-deleted glob can never vacuously pass (``testing-signal.md``).

**Ungated, by design** (``prune-engine.md`` § "Databricks compiler dialect",
DEC-002 of #223): ``sqlglot`` is a base runtime dep and there is no offline
execution fake for a rewritten dbt body, so this guard is the *only* automated
validity gate. Gating it behind a marker CI never runs would leave the rewrite
uncertified.

**It is necessary, not sufficient.** A sqlglot parse certifies **SYNTAX**, not
that a live warehouse **ACCEPTS** the SQL — #226's live Databricks run found three
bugs that the entire offline tier (snapshots, parse-guard, fakes) passed clean.
US-008's gated BigQuery live cert is the merge gate (#268 DEC-016).

The fixture bodies for the three ``dbt_expectations`` cases are dbt's REAL
``compiled_code`` from ``tests/fixtures/dbt_project_expectations`` — every byte
of dbt's blank-line runs, indentation and macro whitespace preserved — with only
the relation re-quoted into each shipped dialect's form (the committed project is
DuckDB-compiled and SignalForge ships no DuckDB adapter).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlglot
import sqlglot.errors

from signalforge.ingest._compiled_sql import plan_relation_rewrite, verify_relation_rewrite
from signalforge.prune.compiler import _build_ingested_rewrite
from signalforge.warehouse.models import (
    BIGQUERY_DIALECT,
    DATABRICKS_DIALECT,
    SNOWFLAKE_DIALECT,
    Dialect,
    TableRef,
)

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "prune" / "compiled_sql" / "ingested"

#: Floor on the committed case count. A glob that silently returned nothing would
#: otherwise make every parametrized assertion below vacuous.
_MIN_FIXTURES = 9

_DIALECTS: dict[str, Dialect] = {
    "bigquery": BIGQUERY_DIALECT,
    "snowflake": SNOWFLAKE_DIALECT,
    "databricks": DATABRICKS_DIALECT,
}


def _load_index() -> list[dict[str, object]]:
    payload = json.loads((_FIXTURE_DIR / "index.json").read_text())
    assert isinstance(payload, list)
    return payload


_INDEX = _load_index()
_CASE_NAMES = [str(case["name"]) for case in _INDEX]


def _case(name: str) -> dict[str, object]:
    return next(case for case in _INDEX if case["name"] == name)


def _relation(case: dict[str, object]) -> tuple[str, ...]:
    parts = case["relation"]
    assert isinstance(parts, list)
    return tuple(str(part) for part in parts)


def _temp(case: dict[str, object]) -> tuple[str, ...]:
    parts = case["temp"]
    assert isinstance(parts, list)
    return tuple(str(part) for part in parts)


def _temp_ref(case: dict[str, object]) -> TableRef:
    parts = _temp(case)
    if len(parts) == 3:
        return TableRef(project=parts[0], dataset=parts[1], name=parts[2])
    return TableRef(project=None, dataset=parts[0], name=parts[1])


def _dialect(case: dict[str, object]) -> Dialect:
    return _DIALECTS[str(case["dialect"])]


def _read(case: dict[str, object], suffix: str) -> str:
    return (_FIXTURE_DIR / f"{case['name']}.{suffix}.sql").read_text()


# ---------------------------------------------------------------------------
# Coverage floor — the guard cannot vacuously pass.
# ---------------------------------------------------------------------------


def test_fixture_count_floor_and_no_orphans() -> None:
    """A ``>= N`` floor plus a two-way orphan check (``testing-signal.md``).

    Without the floor, deleting the fixture directory would turn every
    parametrized case below into zero collected tests and the guard would report
    green while certifying nothing. Without the orphan check, a fixture added to
    disk but not to ``index.json`` would never be parsed.
    """
    outputs = sorted(path.name.removesuffix(".out.sql") for path in _FIXTURE_DIR.glob("*.out.sql"))
    assert len(outputs) >= _MIN_FIXTURES
    assert outputs == sorted(_CASE_NAMES)
    # Enumerate the INPUT fixtures too — an unindexed ``.in.sql`` (a fixture added
    # to disk but never wired into ``index.json``) would otherwise pass unnoticed.
    inputs = sorted(path.name.removesuffix(".in.sql") for path in _FIXTURE_DIR.glob("*.in.sql"))
    assert inputs == sorted(_CASE_NAMES)

    # Both self-join shapes are present: a partial rewrite there would join a
    # SAMPLE against PRODUCTION, the sharpest hazard in the epic.
    assert any(case["expected_spans"] == 2 for case in _INDEX)
    # Every shipped compiler dialect that can host an ingested rewrite is covered.
    assert {str(case["dialect"]) for case in _INDEX} == set(_DIALECTS)


# ---------------------------------------------------------------------------
# Drift — the committed ``.out.sql`` IS what production emits today.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _CASE_NAMES)
def test_committed_rewrite_matches_the_production_splice(name: str) -> None:
    """Re-derive the rewrite from dbt's body through the production helpers and
    require byte-equality with the committed fixture.

    This is what keeps the parse-guard honest: without it the fixtures would drift
    into a museum of SQL nobody emits, and the parse below would certify bytes the
    engine never dispatches.
    """
    case = _case(name)
    dialect = _dialect(case)
    plan = plan_relation_rewrite(_read(case, "in"), relation=_relation(case), dialect=dialect.name)
    assert plan.samplable, plan.reason
    assert len(plan.spans) == case["expected_spans"]

    rewritten = _build_ingested_rewrite(
        _read(case, "in"),
        spans=plan.spans,
        table_ref=_temp_ref(case),
        dialect=dialect,
    )
    assert rewritten == _read(case, "out")


# ---------------------------------------------------------------------------
# Validity — the rewritten SQL parses. THE point of this module.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _CASE_NAMES)
def test_rewritten_sql_parses_in_its_dialect(name: str) -> None:
    """``sqlglot.parse_one(<rewritten>, dialect=<d>)`` succeeds.

    Syntax only — a live warehouse can still reject SQL sqlglot parses happily
    (#226 found exactly that, three times). US-008 is the merge gate.
    """
    case = _case(name)
    tree = sqlglot.parse_one(_read(case, "out"), dialect=str(case["dialect"]))
    assert tree is not None


# ---------------------------------------------------------------------------
# Integrity — DEC-004: zero residual source, exactly N temp.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _CASE_NAMES)
def test_rewritten_sql_binds_only_the_temp_relation(name: str) -> None:
    """Every fixture satisfies the DEC-004 post-condition, and the source
    relation's quoted form does not survive anywhere in the raw bytes — not in a
    comment, not in a string literal.

    A count of rewrites is NOT an integrity proof (AR-B1); the post-condition is
    proved on the rewritten AST.
    """
    case = _case(name)
    rewritten = _read(case, "out")
    assert verify_relation_rewrite(
        rewritten,
        source=_relation(case),
        temp=_temp(case),
        expected_n=int(str(case["expected_spans"])),
        dialect=str(case["dialect"]),
    )

    dialect = _dialect(case)
    quote = dialect.quote_char
    quoted_source = ".".join(f"{quote}{part}{quote}" for part in _relation(case))
    assert quoted_source not in rewritten


# ---------------------------------------------------------------------------
# Planted-violation self-checks — the guard genuinely fires.
# ---------------------------------------------------------------------------


def test_parse_guard_rejects_malformed_sql() -> None:
    """The planted-violation self-check ``testing-signal.md`` requires: prove the
    parse step raises on broken SQL.

    Without it, a refactor that swallowed ``ParseError`` (or parsed a constant
    instead of the fixture) would silently disable the guard at the exact moment a
    real regression needed catching.
    """
    with pytest.raises(sqlglot.errors.ParseError):
        sqlglot.parse_one("select from from where", dialect="bigquery")


def test_integrity_gate_rejects_a_planted_partial_rewrite() -> None:
    """The sharpest hazard, planted: corrupt the self-join fixture so only ONE of
    its two relations was rewritten. The result still PARSES — it is a perfectly
    valid join of a sample against production — so only the DEC-004 integrity gate
    can catch it. It must return ``False``.
    """
    case = _case("bigquery_self_join")
    source = _relation(case)
    temp = _temp(case)
    dialect = _dialect(case)
    quote = dialect.quote_char
    quoted_source = ".".join(f"{quote}{part}{quote}" for part in source)
    quoted_temp = f"{quote}{'.'.join(temp)}{quote}"

    partial = _read(case, "out").replace(quoted_temp, quoted_source, 1)

    # It parses — a partial rewrite is syntactically flawless.
    assert sqlglot.parse_one(partial, dialect=dialect.name) is not None
    # And the integrity gate is what refuses it.
    assert not verify_relation_rewrite(
        partial,
        source=source,
        temp=temp,
        expected_n=2,
        dialect=dialect.name,
    )
