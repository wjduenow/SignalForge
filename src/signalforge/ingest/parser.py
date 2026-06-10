"""Pure dbt test-entry parser (US-003).

Maps a single dbt ``schema.yml`` test entry — a bare string or a single-key
dict — plus its owning column name to either a supported
:class:`~signalforge.draft.CandidateTest` or a structured
:class:`~signalforge.ingest.models.SkippedTest`.

The four supported types are ``not_null`` / ``unique`` (parameterless) and
``accepted_values`` / ``relationships`` (parameterised). Everything else is
*skipped + recorded*, never silently dropped (DEC-003):

* a recognised-but-unmodelled bare string → ``unsupported-test-type``;
* a namespaced / project-defined test (``dbt_utils.*``, ``dbt_expectations.*``,
  any custom generic) → ``custom-or-generic-test``;
* a supported type whose required args are missing or empty →
  ``malformed-supported-test``.

Compatibility surface (DEC-006): args are read both **inline**
(``{accepted_values: {values: [...]}}``) and nested under ``arguments:``
(the dbt 1.8+ shape, ``{accepted_values: {arguments: {values: [...]}}}``).
Interleaved config keys (``config``, ``severity``, ``where`` …) are ignored,
never mistaken for args.

``relationships.to`` is best-effort-unwrapped from ``ref()`` / ``source()``
to a bare model name (DEC-009) via a bounded regex — NO Jinja engine, NO new
dependency. A ``to`` string matching no pattern is carried verbatim.

This module is a **pure mapping** consumed by the ingest reader (a later
story); it is NOT part of the public ``signalforge.ingest`` surface, takes no
I/O, and emits ZERO logs (``.claude/rules/manifest-readers.md`` rule #4).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from signalforge.draft import CandidateTest
from signalforge.draft.models import (
    CandidateTestAcceptedValues,
    CandidateTestCustomSQL,
    CandidateTestNotNull,
    CandidateTestRelationships,
    CandidateTestRowCountBetween,
    CandidateTestUnique,
    CandidateTestUniqueCombination,
)
from signalforge.ingest.models import SkippedTest
from signalforge.manifest.errors import (
    AmbiguousRefError,
    RefNotFoundError,
    SourceNotFoundError,
    TemplateResolutionError,
)
from signalforge.manifest.template import (
    _EXPR_RE,
    _THIS_RE,
    _resolve_ref_args,
    resolve_template_refs,
)
from signalforge.manifest.template import (
    _REF_RE as _TEMPLATE_REF_RE,
)

if TYPE_CHECKING:
    from signalforge.manifest.models import Manifest, Model

# Config keys that dbt allows interleaved with test args; never treated as
# args and never a skip cause when present.
_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "config",
        "severity",
        "where",
        "name",
        "tags",
        "error_if",
        "warn_if",
        "store_failures",
        "limit",
    }
)

# The dbt-expectations macro name SignalForge promotes to the
# `row_count_between` candidate variant (#169, DEC-007 / DEC-008). All
# other `dbt_expectations.*` / `dbt_utils.*` macros fall through to the
# generic custom-or-generic skip below; only this one specific macro is
# recognised as a first-class supported test.
_ROW_COUNT_BETWEEN_NAME = "dbt_expectations.expect_table_row_count_to_be_between"

# The dbt_utils macro name SignalForge promotes to the
# `unique_combination` candidate variant (#170, DEC-002 / DEC-008). All
# other `dbt_utils.*` macros fall through to the generic
# custom-or-generic skip below; only this one specific macro is
# recognised as a first-class supported test.
_UNIQUE_COMBINATION_NAME = "dbt_utils.unique_combination_of_columns"

# Matches ref('m') / ref("pkg", "m") / source('s', 't'); captures the quoted
# positional args. Bounded — no nesting, no Jinja semantics.
_REF_RE = re.compile(r"^\s*ref\s*\(\s*(.+?)\s*\)\s*$", re.DOTALL)
_SOURCE_RE = re.compile(r"^\s*source\s*\(\s*(.+?)\s*\)\s*$", re.DOTALL)
_QUOTED_ARG_RE = re.compile(r"""['"]([^'"]*)['"]""")


def _unwrap_ref_or_source(to: str) -> str:
    """Best-effort unwrap of ``ref()`` / ``source()`` to a target name (DEC-009).

    ``ref('m')`` → ``"m"``; ``ref("pkg", "m")`` → ``"m"`` (last positional);
    ``source('s', 't')`` → ``"s.t"``. A string matching neither pattern is
    returned verbatim.
    """
    ref_match = _REF_RE.match(to)
    if ref_match is not None:
        args = _QUOTED_ARG_RE.findall(ref_match.group(1))
        if args:
            return args[-1]
        return to

    source_match = _SOURCE_RE.match(to)
    if source_match is not None:
        args = _QUOTED_ARG_RE.findall(source_match.group(1))
        if len(args) >= 2:
            return f"{args[0]}.{args[1]}"
        if len(args) == 1:
            return args[0]
        return to

    return to


def _extract_args(body: Any) -> dict[str, Any]:
    """Return the arg mapping for a single-key test dict body.

    Reads args nested under ``arguments:`` (dbt 1.8+) when that key is a
    mapping; otherwise reads inline args. Config keys are stripped so a
    downstream ``required-arg`` check sees only real args. A non-dict body
    yields ``{}``. The structural ``arguments`` key itself is never returned
    as an arg: when it is present but not a mapping, it is dropped and the
    inline args (if any) are returned instead.
    """
    if not isinstance(body, dict):
        return {}
    nested = body.get("arguments")
    if isinstance(nested, dict):
        source: dict[str, Any] = nested
    else:
        # Inline args; drop the structural ``arguments`` key itself.
        source = {k: v for k, v in body.items() if k != "arguments"}
    return {k: v for k, v in source.items() if k not in _CONFIG_KEYS}


def parse_test_entry(
    entry: str | dict[str, Any], *, column: str | None
) -> CandidateTest | SkippedTest:
    """Map one dbt test entry to a ``CandidateTest`` or a ``SkippedTest``.

    ``entry`` is a bare string (``"not_null"``) or a single-key dict whose key
    is the test name (``{accepted_values: {values: [...]}}``). ``column`` is
    the owning column's name, or ``None`` for a model-level test.

    Pure: no I/O, no logging, deterministic for a given input.
    """
    if isinstance(entry, str):
        return _parse_named_test(entry, body=None, column=column)

    if isinstance(entry, dict):
        if len(entry) != 1:
            # A test entry is a single-key dict by dbt's grammar; anything
            # else is not a shape we model.
            name = next(iter(entry), "<empty>") if entry else "<empty>"
            return SkippedTest(
                test_name=str(name),
                column=column,
                reason="custom-or-generic-test",
                detail="test entry is not a single-key mapping",
            )
        (name, body) = next(iter(entry.items()))
        return _parse_named_test(str(name), body=body, column=column)

    return SkippedTest(
        test_name=str(entry),
        column=column,
        reason="custom-or-generic-test",
        detail="test entry is neither a string nor a mapping",
    )


def _model_level_supported_skip(name: str) -> SkippedTest:
    """A supported test type used at model level cannot be represented.

    The four supported ``CandidateTest`` subtypes all require a non-empty
    ``column``. dbt does not place these at model level, but a hand-edited
    schema.yml could; route it to a structured skip rather than letting a
    Pydantic ``ValidationError`` escape ``read_schema``.
    """
    return SkippedTest(
        test_name=name,
        column=None,
        reason="malformed-supported-test",
        detail="supported test types must be column-scoped; model-level is not representable",
    )


def _parse_named_test(name: str, *, body: Any, column: str | None) -> CandidateTest | SkippedTest:
    """Dispatch on the (already-extracted) test name."""
    if name in ("not_null", "unique"):
        # Parameterless; any body is config-only and ignored.
        if column is None:
            return _model_level_supported_skip(name)
        if name == "not_null":
            return CandidateTestNotNull(column=column)
        return CandidateTestUnique(column=column)

    if name == "accepted_values":
        if column is None:
            return _model_level_supported_skip(name)
        return _parse_accepted_values(body=body, column=column)

    if name == "relationships":
        if column is None:
            return _model_level_supported_skip(name)
        return _parse_relationships(body=body, column=column)

    if name == _ROW_COUNT_BETWEEN_NAME:
        # `dbt_expectations.expect_table_row_count_to_be_between` (#169,
        # DEC-007). The variant is model-level only — a column-scoped usage
        # is not representable (DEC-008). Different sibling macros (e.g.
        # `expect_table_row_count_to_equal`) fall through to the namespaced
        # custom-or-generic skip below; only this one specific macro is
        # promoted to a supported variant.
        return _parse_row_count_between(body=body, column=column)

    if name == _UNIQUE_COMBINATION_NAME:
        # `dbt_utils.unique_combination_of_columns` (#170, DEC-002 / DEC-008).
        # The variant is model-level only — a column-scoped usage is not
        # representable. Different sibling `dbt_utils.*` macros fall through
        # to the namespaced custom-or-generic skip below; only this one
        # specific macro is promoted to a supported variant.
        return _parse_unique_combination(body=body, column=column)

    # A namespaced or project-defined test: dbt_utils.*, dbt_expectations.*,
    # any custom generic. Distinct from a bare unsupported string.
    if isinstance(body, dict) or "." in name:
        return SkippedTest(
            test_name=name,
            column=column,
            reason="custom-or-generic-test",
            detail="not one of the four supported test types",
        )
    return SkippedTest(
        test_name=name,
        column=column,
        reason="unsupported-test-type",
        detail="not one of the four supported test types",
    )


def _parse_accepted_values(*, body: Any, column: str | None) -> CandidateTest | SkippedTest:
    args = _extract_args(body)
    raw_values = args.get("values")
    if not isinstance(raw_values, (list, tuple)) or len(raw_values) == 0:
        return SkippedTest(
            test_name="accepted_values",
            column=column,
            reason="malformed-supported-test",
            detail="accepted_values requires a non-empty 'values' list",
        )
    return CandidateTestAcceptedValues(
        column=column if column is not None else "",
        values=tuple(str(v) for v in raw_values),
    )


def _parse_row_count_between(*, body: Any, column: str | None) -> CandidateTest | SkippedTest:
    """Map a ``dbt_expectations.expect_table_row_count_to_be_between`` entry.

    Inbound mapping (DEC-008): ``min_value`` → ``minimum``, ``max_value``
    → ``maximum``, ``where`` → ``where``. The diff emitter (US-010)
    handles the OUTBOUND mapping.

    Skip routes (DEC-007), all with ``reason="malformed-supported-test"``:
    column-scoped usage (variant is model-level only); both bounds
    missing/None; either bound non-int or negative; ``minimum > maximum``;
    ``where`` set to a non-string. Recognition of a *different* sibling
    macro (e.g. ``expect_table_row_count_to_equal``) does not reach this
    helper — it falls through to the generic custom-or-generic skip in
    :func:`_parse_named_test`. The closed 3-value
    :data:`~signalforge.ingest.models.SkipReason` is preserved (DEC-011).
    """
    if column is not None:
        # The variant is model-level only (``column = None`` on the model).
        # A column-scoped usage in YAML is not representable — route to
        # malformed-supported-test with an explicit diagnostic rather than
        # letting a Pydantic ValidationError escape ``read_schema``.
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=column,
            reason="malformed-supported-test",
            detail=(
                "expect_table_row_count_to_be_between is a model-level test; "
                "column-scoped usage is not representable"
            ),
        )

    # ``where`` is part of this macro's arguments, not a dbt-side test-config
    # passthrough — so we cannot reuse ``_extract_args`` here (which would
    # strip ``where`` via ``_CONFIG_KEYS``). Read the body directly: a non-
    # dict body has no args; a body with an ``arguments:`` mapping pulls
    # from there (dbt 1.8+ shape); else inline.
    if not isinstance(body, dict):
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=None,
            reason="malformed-supported-test",
            detail=(
                "expect_table_row_count_to_be_between requires at least one "
                "of (min_value, max_value)"
            ),
        )
    nested = body.get("arguments")
    source: dict[str, Any] = nested if isinstance(nested, dict) else body
    raw_min = source.get("min_value")
    raw_max = source.get("max_value")
    raw_where = source.get("where")

    # At least one bound must be set — an unbounded row-count test carries no
    # signal. (Mirrors the model-level invariant on CandidateTestRowCountBetween.)
    if raw_min is None and raw_max is None:
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=None,
            reason="malformed-supported-test",
            detail=(
                "expect_table_row_count_to_be_between requires at least one "
                "of (min_value, max_value)"
            ),
        )

    # Strict int check: ``isinstance(True, int) is True`` would otherwise
    # silently coerce a bool to 0/1, which is not what the operator wrote.
    if raw_min is not None and (isinstance(raw_min, bool) or not isinstance(raw_min, int)):
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=None,
            reason="malformed-supported-test",
            detail="min_value must be a non-negative integer",
        )
    if raw_max is not None and (isinstance(raw_max, bool) or not isinstance(raw_max, int)):
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=None,
            reason="malformed-supported-test",
            detail="max_value must be a non-negative integer",
        )
    if raw_min is not None and raw_min < 0:
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=None,
            reason="malformed-supported-test",
            detail="min_value must be a non-negative integer",
        )
    if raw_max is not None and raw_max < 0:
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=None,
            reason="malformed-supported-test",
            detail="max_value must be a non-negative integer",
        )
    if raw_min is not None and raw_max is not None and raw_min > raw_max:
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=None,
            reason="malformed-supported-test",
            detail=(f"min_value ({raw_min}) must be <= max_value ({raw_max})"),
        )
    if raw_where is not None and (not isinstance(raw_where, str) or not raw_where.strip()):
        return SkippedTest(
            test_name=_ROW_COUNT_BETWEEN_NAME,
            column=None,
            reason="malformed-supported-test",
            detail="where must be a non-empty string when set",
        )

    return CandidateTestRowCountBetween(
        minimum=raw_min,
        maximum=raw_max,
        where=raw_where,
    )


def _parse_unique_combination(*, body: Any, column: str | None) -> CandidateTest | SkippedTest:
    """Map a ``dbt_utils.unique_combination_of_columns`` entry (#170).

    Inbound mapping (DEC-002 / DEC-008): YAML ``combination_of_columns: list[str]``
    → Pydantic ``columns: tuple[str, ...]``; optional ``where: str`` → ``where``.
    The diff emitter (US-006) handles the OUTBOUND mapping back to the
    macro shape. Don't push the dbt_utils name into the internal model —
    the mapping seams are these two functions only.

    Skip routes (DEC-008), all with ``reason="malformed-supported-test"``:
    column-scoped usage (variant is model-level only); non-dict body;
    missing ``combination_of_columns`` key; non-list value under the key;
    empty list; ``len < 2`` (single-column is just ``unique``); non-string
    items; duplicate items; non-empty non-string or whitespace-only
    ``where``. Different sibling ``dbt_utils.*`` macros do not reach this
    helper — they fall through to the generic custom-or-generic skip in
    :func:`_parse_named_test`. The closed 3-value
    :data:`~signalforge.ingest.models.SkipReason` is preserved.
    """
    if column is not None:
        # The variant is model-level only (``column = None`` on the model).
        # A column-scoped usage in YAML is not representable — route to
        # malformed-supported-test with an explicit diagnostic rather than
        # letting a Pydantic ValidationError escape ``read_schema``.
        return SkippedTest(
            test_name=_UNIQUE_COMBINATION_NAME,
            column=column,
            reason="malformed-supported-test",
            detail=(
                "unique_combination_of_columns is a model-level test; "
                "column-scoped usage is not representable"
            ),
        )

    # ``where`` is part of this macro's arguments, not a dbt-side test-config
    # passthrough — so we cannot reuse ``_extract_args`` here (which would
    # strip ``where`` via ``_CONFIG_KEYS``). Read the body directly: a non-
    # dict body has no args; a body with an ``arguments:`` mapping pulls
    # from there (dbt 1.8+ shape); else inline. Stripped config keys (per
    # ``.claude/rules/ingest-layer.md`` § "dbt syntax tolerance") are
    # tolerated alongside args because we only look up specific argument
    # keys by name — anything else (severity / tags / name / …) is
    # silently ignored.
    if not isinstance(body, dict):
        return SkippedTest(
            test_name=_UNIQUE_COMBINATION_NAME,
            column=None,
            reason="malformed-supported-test",
            detail=(
                "unique_combination_of_columns requires a 'combination_of_columns' list "
                "of at least two distinct column names"
            ),
        )
    nested = body.get("arguments")
    source: dict[str, Any] = nested if isinstance(nested, dict) else body
    raw_columns = source.get("combination_of_columns")
    raw_where = source.get("where")

    if raw_columns is None:
        return SkippedTest(
            test_name=_UNIQUE_COMBINATION_NAME,
            column=None,
            reason="malformed-supported-test",
            detail=(
                "unique_combination_of_columns requires a 'combination_of_columns' list "
                "of at least two distinct column names"
            ),
        )

    # The YAML grammar carries `combination_of_columns` as a list of strings.
    # A bare string (a common operator typo) is rejected loudly rather than
    # silently coerced to a one-element list.
    if not isinstance(raw_columns, list):
        return SkippedTest(
            test_name=_UNIQUE_COMBINATION_NAME,
            column=None,
            reason="malformed-supported-test",
            detail="combination_of_columns must be a list of column-name strings",
        )

    if len(raw_columns) == 0:
        return SkippedTest(
            test_name=_UNIQUE_COMBINATION_NAME,
            column=None,
            reason="malformed-supported-test",
            detail=(
                "combination_of_columns must contain at least two entries "
                "(got an empty list) — a single-column variant is just `unique`"
            ),
        )

    if len(raw_columns) < 2:
        return SkippedTest(
            test_name=_UNIQUE_COMBINATION_NAME,
            column=None,
            reason="malformed-supported-test",
            detail=(
                "combination_of_columns must contain at least two entries "
                f"(got {len(raw_columns)}) — a single-column variant is just `unique`"
            ),
        )

    # Every entry must be a string identifier. ``isinstance(True, int) is True``
    # in Python — a bool would otherwise propagate to ``str(True) == "True"``
    # and end up as a column name; reject up-front. Mirror the bool-as-int
    # guard from ``_parse_row_count_between``.
    for item in raw_columns:
        if isinstance(item, bool) or not isinstance(item, str):
            return SkippedTest(
                test_name=_UNIQUE_COMBINATION_NAME,
                column=None,
                reason="malformed-supported-test",
                detail="combination_of_columns entries must all be column-name strings",
            )

    if len(set(raw_columns)) != len(raw_columns):
        return SkippedTest(
            test_name=_UNIQUE_COMBINATION_NAME,
            column=None,
            reason="malformed-supported-test",
            detail=(
                f"combination_of_columns must not contain duplicates (got {list(raw_columns)!r}) — "
                "a duplicate column compiles to a uniqueness test that always "
                "trivially has the same value in two positions"
            ),
        )

    if raw_where is not None and (not isinstance(raw_where, str) or not raw_where.strip()):
        return SkippedTest(
            test_name=_UNIQUE_COMBINATION_NAME,
            column=None,
            reason="malformed-supported-test",
            detail="where must be a non-empty string when set",
        )

    return CandidateTestUniqueCombination(
        columns=tuple(raw_columns),
        where=raw_where,
    )


def _parse_relationships(*, body: Any, column: str | None) -> CandidateTest | SkippedTest:
    args = _extract_args(body)
    raw_to = args.get("to")
    raw_field = args.get("field")
    if not isinstance(raw_to, str) or not raw_to or not isinstance(raw_field, str) or not raw_field:
        return SkippedTest(
            test_name="relationships",
            column=column,
            reason="malformed-supported-test",
            detail="relationships requires both 'to' and 'field'",
        )
    return CandidateTestRelationships(
        column=column if column is not None else "",
        to=_unwrap_ref_or_source(raw_to),
        field=raw_field,
    )


def _references_qualified_name(resolved_sql: str, target: str) -> bool:
    """Word-boundary match for ``target`` in ``resolved_sql`` (post-substitution).

    A raw ``target in resolved_sql`` substring check false-matches when the
    qualified name appears inside a string literal / comment, or as a fragment
    of a longer dotted identifier (e.g. target ``proj.ds.orders`` inside
    ``proj.ds.orders_archive``). The boundary assertions ``(?<![\\w.])`` /
    ``(?![\\w.])`` require the match to be flanked by neither a word char nor a
    dot, so only a standalone occurrence of the qualified name counts. Full
    FROM/JOIN parsing is overkill; a bounded boundary check is proportionate.
    """
    pattern = r"(?<![\w.])" + re.escape(target) + r"(?![\w.])"
    return re.search(pattern, resolved_sql) is not None


def _raw_sql_references_target(sql: str, *, model: Model, manifest: Manifest) -> bool:
    """Bounded heuristic: does the RAW ``sql`` plausibly reference ``model``?

    Used when :func:`resolve_template_refs` raises because *some* ``ref()`` /
    ``source()`` in the body is unresolvable/ambiguous — we must not discard a
    test that ALSO targets this model just because a sibling reference is
    unknown. Reuses the template module's expression / ``this`` / ``ref``
    regexes (NO new Jinja engine, mirroring ``_unwrap_ref_or_source``) and
    resolves each ``{{ ... }}`` expression *individually*:

    * ``{{ this }}`` → references this model (the target);
    * a ``ref()`` whose own resolution succeeds AND yields the target's
      qualified name → references this model.

    A ``{{ source(...) }}`` is intentionally NOT checked: the target of a
    singular test is always a *model* (``Model.resolve_this()`` → the model's
    own table), and a source resolves to a source table — it can never equal
    the target, so a source reference can never make this model the target.
    Sources are still tolerated as *siblings*: they simply don't associate.

    A per-expression resolution that itself raises (the unresolvable sibling)
    is swallowed — it just means *that* expression is not the target. We return
    ``True`` as soon as any expression resolves to the target; ``False`` if none
    do. Deterministic, pure, no I/O.
    """
    target = model.resolve_this().qualified_name
    for match in _EXPR_RE.finditer(sql):
        body = match.group(1).strip()
        if _THIS_RE.match(body):
            return True
        ref_match = _TEMPLATE_REF_RE.match(body)
        if ref_match is None:
            continue  # source() / unsupported expr — never the model target
        try:
            if _resolve_ref_args(ref_match.group(1), manifest=manifest) == target:
                return True
        except Exception:  # noqa: BLE001 — sibling ref unresolvable; not the target
            # This particular ref() can't be resolved (the very condition that
            # brought us here). It is therefore not the target reference; keep
            # scanning the remaining expressions.
            continue
    return False


def classify_singular_test(
    sql: str,
    *,
    file_name: str,
    model: Model,
    manifest: Manifest,
) -> CandidateTestCustomSQL | SkippedTest | None:
    """Classify a dbt singular-test ``.sql`` file against ``model`` (US-013).

    A singular test is a standalone ``.sql`` file under ``tests/`` whose body
    is a failing-rows SELECT (passes when zero rows return). This maps it to
    one of three dispositions (DEC-013):

    * **Associated** → a :class:`CandidateTestCustomSQL` (``column=None`` —
      singular tests are model-level — ``sql`` = the raw file body), when the
      SQL's resolved dbt references include ``model``. The prune stage runs
      the test verbatim.
    * **Unrelated** → ``None``, when the SQL resolves cleanly but references
      some *other* model. Per DEC-013 these are simply *not included* in the
      target model's candidate — they are NOT skip-recorded (a test for a
      different model is not a defect of this model's ingest).
    * **Skip** → a :class:`SkippedTest` with ``reason="malformed-supported-test"``
      (the closed 3-value :data:`~signalforge.ingest.models.SkipReason` is NOT
      extended), when the SQL carries Jinja the bounded resolver cannot
      evaluate (``{% ... %}`` blocks, ``{{ var(...) }}`` / ``{{ env_var(...) }}``,
      macro calls) or an unresolved ``{{ ... }}``.

    Association reuses :func:`signalforge.manifest.template.resolve_template_refs`
    to resolve ``ref()`` / ``source()`` / ``this`` — no regex is duplicated
    here. When the whole-body resolve raises because *some* reference is
    unknown/ambiguous, we fall back to a bounded per-expression heuristic
    (:func:`_raw_sql_references_target`): if the RAW SQL still plausibly
    references *this* model (``{{ this }}``, or a ``ref()`` / ``source()`` that
    resolves to this model) we associate and carry the RAW unresolved SQL into
    a :class:`CandidateTestCustomSQL` — the prune compiler re-resolves it and
    routes ``RefNotFoundError`` / ``SourceNotFoundError`` → requires-future-data
    and ``AmbiguousRefError`` → kept-without-evidence (US-019), so the test is
    *deferred*, not lost. Only when the target is genuinely NOT referenced do we
    treat the file as *unrelated* (``None``), not skip-recorded. ``{{ this }}``
    is not expected in a standalone singular test, but if present it resolves to
    ``model`` and associates.

    Args:
        sql: The raw ``.sql`` file body.
        file_name: The file's name, for the skip ``detail`` diagnostic.
        model: The manifest model the caller is ingesting tests for.
        manifest: The manifest, used to resolve ``ref()`` / ``source()``.

    Returns:
        A :class:`CandidateTestCustomSQL` (associated), ``None`` (unrelated, not
        recorded), or a :class:`SkippedTest` (unsupported Jinja).

    Pure: no I/O, no logging, deterministic for a given input.
    """
    target = model.resolve_this().qualified_name
    try:
        resolved = resolve_template_refs(sql, model, manifest)
    except TemplateResolutionError:
        # UnsupportedJinjaError is a TemplateResolutionError subclass, so this
        # one branch covers both the unsupported-Jinja and unresolved-``{{ }}``
        # cases. The closed 3-value SkipReason is preserved (DEC-013): a
        # singular test we cannot statically resolve is "malformed".
        return SkippedTest(
            test_name=file_name,
            column=None,
            reason="malformed-supported-test",
            detail="singular .sql test contains Jinja the bounded resolver cannot evaluate",
        )
    except (RefNotFoundError, AmbiguousRefError, SourceNotFoundError):
        # The SQL is well-formed Jinja but at least one ref()/source() is
        # absent from (or ambiguous in) the manifest, so the whole-body resolve
        # could not complete. Do NOT discard yet: a body that references THIS
        # model AND a sibling unknown model still targets us. Use the bounded
        # per-expression heuristic; if this model is referenced, carry the RAW
        # (unresolved) SQL so the prune compiler routes it (requires-future-data
        # / kept-without-evidence). Otherwise it is genuinely unrelated → None.
        if _raw_sql_references_target(sql, model=model, manifest=manifest):
            return CandidateTestCustomSQL(column=None, sql=sql, rationale=None)
        return None

    # The SQL resolved cleanly. Associate iff its resolved references include
    # the target model's qualified name; otherwise it is a test for a different
    # model and is silently not included. Use a word-boundary match so the
    # target name appearing inside a string literal / comment, or as a fragment
    # of a longer dotted identifier (``my_project.orders_archive``), does NOT
    # false-match — only a standalone occurrence of the qualified name counts.
    if not _references_qualified_name(resolved, target):
        return None

    return CandidateTestCustomSQL(column=None, sql=sql, rationale=None)


__all__ = ("classify_singular_test", "parse_test_entry")
