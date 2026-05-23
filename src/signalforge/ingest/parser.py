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
    CandidateTestUnique,
)
from signalforge.ingest.models import SkippedTest
from signalforge.manifest.errors import (
    AmbiguousRefError,
    RefNotFoundError,
    SourceNotFoundError,
    TemplateResolutionError,
)
from signalforge.manifest.template import resolve_template_refs

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
    here. A reference to a model the manifest does not contain (or an ambiguous
    ``ref()``) means the file does not reference *this* model, so it is treated
    as *unrelated* (``None``), not skip-recorded. ``{{ this }}`` is not expected
    in a standalone singular test, but if present it resolves to ``model`` and
    associates.

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
        # The SQL is well-formed Jinja but references a model/source absent
        # from (or ambiguous in) the manifest — it does not reference *this*
        # model, so it is unrelated, not a defect of this model's ingest.
        return None

    # The SQL resolved cleanly. Associate iff its resolved references include
    # the target model's qualified name; otherwise it is a test for a different
    # model and is silently not included.
    if target not in resolved:
        return None

    return CandidateTestCustomSQL(column=None, sql=sql, rationale=None)


__all__ = ("classify_singular_test", "parse_test_entry")
