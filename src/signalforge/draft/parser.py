"""Two-stage parser for the LLM's textual response (US-011).

Stage 1 — JSON parse + Pydantic validate the raw text into a
:class:`signalforge.draft.models.CandidateSchema`. JSON-shaped failures
are wrapped in :class:`signalforge.draft.errors.LLMOutputJSONError` (the
underlying :class:`json.JSONDecodeError` is preserved as ``cause`` and
provides a 1-indexed ``(line, column)`` parse position so the error
envelope's excerpt window centres on the offending byte). Non-JSON
validation failures (wrong shape, missing field, bad discriminator value)
are wrapped in :class:`LLMOutputValidationError`.

Stage 2 — Anchor-contract validator (DEC-003 / DEC-022). Runs only after
Stage 1 succeeds. Walks the candidate schema collecting **every**
violation rather than short-circuiting on the first; whole-draft fail-loud
is the contract so a reviewer can see the full picture in a single error
rather than iteratively re-running until each violation surfaces.

The anchor contract enforces:

* Each :class:`signalforge.draft.models.CandidateColumn` test must carry
  ``test.column == column.name`` (a column-scoped test that cites a
  *different* column would silently land under the wrong YAML key).
* Each test's ``column`` must reference a real column on the input model
  (the ``model_columns`` frozenset).
* Per column, at most one ``not_null`` test and at most one ``unique``
  test (parameterless tests cannot meaningfully duplicate; multiple
  ``accepted_values`` / ``relationships`` tests are allowed because they
  may carry different arguments).

All construction goes through :func:`parse_draft_response`. The internal
``_LLMResultMeta`` dataclass bundles the provenance fields the error
envelope demands (DEC-006 / DEC-007 — every bad-output error carries the
prompt version, model identifier, cache-hit flag, and token counts so the
response audit / CLI can render a forensically-useful incident report
without sniffing message text).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

import sqlglot
import sqlglot.errors
from pydantic import ValidationError
from sqlglot import exp
from sqlglot.expressions import DataType
from sqlglot.optimizer.annotate_types import TypeAnnotator, annotate_types

from signalforge._common.json_payload import extract_json_payload
from signalforge.draft.errors import (
    LLMOutputAnchorContractError,
    LLMOutputJSONError,
    LLMOutputValidationError,
)
from signalforge.draft.models import CandidateSchema

# Jinja substitution pattern used to neutralise ``{{ this }}`` /
# ``{{ ref(...) }}`` / ``{{ source(...) }}`` BEFORE handing the SQL body
# to sqlglot. The parser defence runs PRE-resolution (the prune compiler
# is what resolves Jinja later); sqlglot raises ``ParseError`` on raw
# ``{{ ... }}`` and we'd otherwise skip every Jinja-bearing custom_sql.
# We replace the entire ``{{ ... }}`` block with a single stable identifier
# placeholder so the surrounding SQL parses as a normal SELECT against a
# placeholder table — the comparison nodes we care about live in the
# WHERE clause and are unaffected.
_JINJA_PLACEHOLDER_RE = re.compile(r"\{\{[^}]*\}\}")
_JINJA_PLACEHOLDER_TOKEN = "__sf_jinja__"


@dataclass(frozen=True)
class _LLMResultMeta:
    """Provenance bundle attached to every parser-raised error envelope.

    Carries the fields :class:`signalforge.draft.errors.LLMOutputError`
    requires so a parser caller can supply the LLM-call provenance once
    and have it propagate uniformly into JSON / validation /
    anchor-contract errors. Frozen + private — the parser owns
    construction; tests reach it via dotted import where needed.
    """

    prompt_version: str
    model: str
    cache_hit: bool
    input_tokens: int
    output_tokens: int


def _is_json_invalid_error(exc: ValidationError) -> bool:
    """Return ``True`` when ``exc`` reports a Pydantic ``json_invalid``
    error.

    Pydantic v2 surfaces a fundamentally-broken JSON payload as a single
    error entry whose ``type`` field equals ``"json_invalid"``. We branch
    on this to distinguish *parse* failures (raise
    :class:`LLMOutputJSONError`, with positional context recoverable via
    :func:`json.loads`) from *shape* failures (raise
    :class:`LLMOutputValidationError`, where positional context doesn't
    apply).
    """
    return any(err.get("type") == "json_invalid" for err in exc.errors())


def _types_compatible(a: object, b: object) -> bool:
    """Bidirectional sqlglot ``COERCES_TO`` compatibility check (DEC-003).

    Two BigQuery types are compatible if they are equal OR either coerces
    to the other under sqlglot's dialect-agnostic ``COERCES_TO`` table.
    BigQuery accepts implicit coercion between numeric families
    (INT64↔FLOAT64, NUMERIC↔BIGNUMERIC) but not between numeric and
    STRING or DATE, which is exactly the failure mode the parser
    defence catches.

    ``a`` and ``b`` are typed ``object`` because sqlglot's ``DataType.Type``
    enum is unstable across versions and resists static typing — the helper
    operates on whatever instance the runtime annotator produces.
    """
    if a == b:
        return True
    coerces_to: dict[object, set[object]] = TypeAnnotator.COERCES_TO  # type: ignore[assignment]
    if b in coerces_to.get(a, set()):
        return True
    if a in coerces_to.get(b, set()):  # noqa: SIM103
        return True
    return False


def _check_custom_sql_type_coherence(
    sql: str,
    model_columns_by_type: Mapping[str, str | None],
    dialect_name: str,
) -> tuple[str, ...]:
    """Run sqlglot type annotation against a custom_sql body and return
    any direct ``Column <op> Column`` violations (DEC-003 / DEC-006).

    Skip-when-uncertain policy is load-bearing:

    * sqlglot ``ParseError`` (e.g. Jinja placeholders, malformed SQL) →
      return empty tuple; the warehouse adapter will catch real-SQL
      breakage downstream via ``kept-without-evidence`` routing.
    * Comparison node where at least one side is NOT a bare
      :class:`sqlglot.exp.Column` (``Cast`` / ``SafeCast`` / ``Coalesce``
      / function call / subquery / literal / NULL / window) → skip.
      The user has either coerced explicitly or invoked a function whose
      semantics we can't statically reason about.
    * Comparison node where either column is absent from the schema map
      or has ``data_type=None`` → skip. Existing degraded behaviour
      preserved (the layer cannot positively reject what it can't see).
    * Type that sqlglot ``DataType.build(..., dialect=...)`` fails to
      parse → skip that column; never raise out of the defence.

    Type incompatibility (e.g. INT64 vs STRING) is the ONLY case that
    appends a violation. Numeric-family coercions (INT64↔FLOAT64,
    NUMERIC↔BIGNUMERIC) stay accepted via :func:`_types_compatible`.
    """
    # Neutralise Jinja placeholders before parsing — the parser defence
    # runs pre-resolution and the LLM almost always references the model
    # via ``{{ this }}``. Replacing the whole ``{{ ... }}`` block with a
    # bare identifier keeps the surrounding SQL parseable without depending
    # on a Jinja engine.
    sanitized_sql = _JINJA_PLACEHOLDER_RE.sub(_JINJA_PLACEHOLDER_TOKEN, sql)
    try:
        parsed = sqlglot.parse_one(sanitized_sql, dialect=dialect_name)
    except sqlglot.errors.ParseError:
        return ()
    except sqlglot.errors.SqlglotError:
        # Any other sqlglot parse-time error: skip silently. Conservative-
        # bias matches the rule (manifest-readers.md / llm-drafter.md).
        return ()

    if parsed is None:
        return ()

    # Annotate types in-place. Pass an empty schema; we resolve column
    # types from `model_columns_by_type` directly (the schema-by-table
    # form requires `qualify`, which itself trips on `{{ this }}` and is
    # not robust to dialect-specific quoting). The annotator still types
    # literals / casts / function returns, which we use to skip non-Column
    # sides.
    try:
        annotated = annotate_types(parsed, dialect=dialect_name)
    except Exception:  # noqa: BLE001 — sqlglot's annotator raises a wide surface
        # Skip silently on annotation failure — the parser defence is
        # belt-and-braces; never block on its own internals.
        return ()

    violations: list[str] = []
    for node in annotated.walk():
        if not isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE)):
            continue
        left = node.left
        right = node.right
        # Both sides must be bare Column nodes — skip every other shape
        # (Cast / SafeCast / Coalesce / function call / subquery / literal
        # / NULL / window). The skip is the conservative-bias contract.
        if type(left) is not exp.Column or type(right) is not exp.Column:
            continue
        left_name = left.name
        right_name = right.name
        left_type_str = model_columns_by_type.get(left_name)
        right_type_str = model_columns_by_type.get(right_name)
        if left_type_str is None or right_type_str is None:
            continue
        try:
            left_dtype = DataType.build(left_type_str, dialect=dialect_name).this
            right_dtype = DataType.build(right_type_str, dialect=dialect_name).this
        except Exception:  # noqa: BLE001 — opaque vendor-type strings; skip
            continue
        if _types_compatible(left_dtype, right_dtype):
            continue
        # Render a stable operator token from the node class for the message.
        op_token = {
            exp.EQ: "=",
            exp.NEQ: "<>",
            exp.GT: ">",
            exp.LT: "<",
            exp.GTE: ">=",
            exp.LTE: "<=",
        }.get(type(node), type(node).__name__.lower())
        violations.append(
            f"custom_sql test references column {left_name!r} ({left_type_str}) "
            f"and {right_name!r} ({right_type_str}) in {op_token!r} comparison "
            f"— types incompatible"
        )

    return tuple(violations)


def _validate_anchor_contract(
    candidate: CandidateSchema,
    model_columns: frozenset[str],
    *,
    model_columns_by_type: Mapping[str, str | None] | None = None,
    dialect_name: str = "bigquery",
    exclude_tests: frozenset[str] = frozenset(),
    business_rules: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Walk ``candidate`` collecting every anchor-contract violation.

    Whole-draft fail-loud (DEC-022): never short-circuits on the first
    violation. Returns an empty tuple when the candidate is clean.

    ``exclude_tests`` (issue #54) is the operator-supplied set of test
    types the drafter must NOT propose. Any candidate test whose
    ``type`` is in this set adds a violation. The prompt-builder
    filters the test catalogue server-side too, so a well-behaved LLM
    won't emit excluded types; this check is the defence-in-depth
    backstop for a model that ignores the prompt.

    ``custom_sql`` (DEC-002) is the free-form business-rule variant and
    receives bespoke handling: its ``sql`` body must be non-empty (after
    strip), and it is EXEMPT from the parent-column-equality rule (a
    column-scoped custom_sql may reference other columns in its SQL — only
    membership of its declared ``column`` matters). When its ``column``
    is ``None`` it is a model-level assertion with no column checks. The
    SQL's Jinja / safety is NOT validated here — that is the
    resolver/compiler's job; this is structural validation only.

    Issue #159 — when ``model_columns_by_type`` is supplied (mapping
    column name → BigQuery-style ``data_type`` string or ``None``), each
    ``custom_sql`` body is additionally parsed with sqlglot and any
    direct ``Column <op> Column`` comparison whose operands have known
    incompatible types appends a violation. ``model_columns_by_type=None``
    OR every column's type being ``None`` makes this arm a no-op (the
    catalog.json merge in #159 / US-001 is what fills the type map; without
    it the structural checks above run unchanged). ``dialect_name`` is a
    string (not the typed :class:`signalforge.warehouse.models.Dialect`)
    so the parser doesn't import the warehouse layer (DEC-012 / DEC-013).

    Issue #163 US-002 — ``business_rules`` is the tuple of operator-declared
    rules (prefixed by :func:`signalforge.draft.prompts._read_business_rules`
    with ``(model) `` / ``(column X) ``). When non-empty AND ``"custom_sql"``
    is NOT in ``exclude_tests``, the validator enforces at-least-one-per-rule
    cardinality (DEC-002): a single violation is appended if the total count
    of ``custom_sql`` tests across model-level and column-level scopes falls
    short of ``len(business_rules)``. ``business_rules=()`` is a no-op (the
    inferred-fallback path stays open — DEC-008). The check appends to the
    collect-all violations list, never short-circuits.
    """
    violations: list[str] = []
    type_arm_active = model_columns_by_type is not None and any(
        v is not None for v in model_columns_by_type.values()
    )

    # Column-scoped tests: hallucinated-column check on the parent
    # CandidateColumn name itself + parent-column-match + nonexistent-
    # column check on each test + duplicate not_null/unique check +
    # excluded-test-type rejection.
    for column in candidate.columns:
        # The CandidateColumn name itself must reference a real column.
        # Without this check, an LLM could invent
        # ``CandidateColumn(name="hallucinated", tests=[NotNull(column="hallucinated")])``
        # and pass validation despite the system-prompt anchor contract.
        if column.name not in model_columns:
            violations.append(
                f"CandidateColumn references nonexistent column {column.name!r} "
                f"(available: {sorted(model_columns)})"
            )
        not_null_count = 0
        unique_count = 0
        for test in column.tests:
            if test.type == "custom_sql":
                # custom_sql is exempt from the parent-column-equality
                # rule: its SQL body may legitimately reference columns
                # other than the one it is filed under. Only structural
                # checks apply (non-empty sql + membership of the
                # declared column, when present).
                if not test.sql.strip():
                    violations.append(f"column={column.name!r} custom_sql test has empty sql")
                if test.column is not None and test.column not in model_columns:
                    violations.append(
                        f"custom_sql test references nonexistent column {test.column!r} "
                        f"(available: {sorted(model_columns)})"
                    )
                # Issue #159 — type-coherence defence (DEC-003).
                if type_arm_active and test.sql.strip():
                    assert model_columns_by_type is not None  # narrow for pyright
                    violations.extend(
                        _check_custom_sql_type_coherence(
                            test.sql, model_columns_by_type, dialect_name
                        )
                    )
            else:
                if test.column != column.name:
                    violations.append(
                        f"column test on column={column.name!r} references {test.column!r}"
                    )
                if test.column not in model_columns:
                    violations.append(
                        f"test references nonexistent column {test.column!r} "
                        f"(available: {sorted(model_columns)})"
                    )
            if test.type in exclude_tests:
                violations.append(
                    f"column={column.name!r} test type {test.type!r} is in exclude_tests "
                    f"(excluded: {sorted(exclude_tests)})"
                )
            if test.type == "not_null":
                not_null_count += 1
            elif test.type == "unique":
                unique_count += 1
        if not_null_count > 1:
            violations.append(f"column={column.name!r} has duplicate 'not_null' tests")
        if unique_count > 1:
            violations.append(f"column={column.name!r} has duplicate 'unique' tests")

    # Model-level tests: nonexistent-column check + excluded-test-type
    # rejection. The parent-column rule is column-scoped by definition.
    for test in candidate.tests:
        if test.type == "custom_sql":
            # Model-level custom_sql: non-empty sql is mandatory; a
            # declared column (when present) must exist. column=None is
            # the canonical model-level business-rule shape — valid.
            if not test.sql.strip():
                violations.append("model-level custom_sql test has empty sql")
            if test.column is not None and test.column not in model_columns:
                violations.append(
                    f"model-level custom_sql test references nonexistent column {test.column!r}"
                )
            # Issue #159 — type-coherence defence (DEC-003).
            if type_arm_active and test.sql.strip():
                assert model_columns_by_type is not None  # narrow for pyright
                violations.extend(
                    _check_custom_sql_type_coherence(test.sql, model_columns_by_type, dialect_name)
                )
        elif test.column not in model_columns:
            violations.append(f"model-level test references nonexistent column {test.column!r}")
        if test.type in exclude_tests:
            violations.append(
                f"model-level test type {test.type!r} is in exclude_tests "
                f"(excluded: {sorted(exclude_tests)})"
            )

    # Issue #163 US-002 — business-rules cardinality gate (DEC-002, DEC-006,
    # DEC-008). No-op when no rules are declared OR when ``custom_sql`` is
    # in ``exclude_tests`` (the operator forbids the only test type that
    # could satisfy the cardinality, so enforcing it would be incoherent).
    # At-least-one-per-rule: excess is allowed (legitimate multi-test
    # decomposition of a complex rule); under-coverage appends one
    # collect-all violation that names every declared rule verbatim.
    if business_rules and "custom_sql" not in exclude_tests:
        custom_sql_count = sum(1 for test in candidate.tests if test.type == "custom_sql") + sum(
            1 for column in candidate.columns for test in column.tests if test.type == "custom_sql"
        )
        if custom_sql_count < len(business_rules):
            declared = ", ".join(repr(rule) for rule in business_rules)
            violations.append(
                f"Expected ≥{len(business_rules)} custom_sql test(s) "
                f"(one per declared business rule), got {custom_sql_count}. "
                f"Declared rules: {declared}."
            )

    return tuple(violations)


def parse_draft_response(
    raw_text: str,
    model_columns: frozenset[str],
    *,
    llm_result_meta: _LLMResultMeta,
    exclude_tests: frozenset[str] = frozenset(),
    model_columns_by_type: Mapping[str, str | None] | None = None,
    dialect_name: str = "bigquery",
    business_rules: tuple[str, ...] = (),
) -> CandidateSchema:
    """Parse and validate the LLM's textual response.

    Returns a fully-validated :class:`CandidateSchema` whose anchor
    contract has been verified against ``model_columns``. Raises one of
    three :class:`signalforge.draft.errors.LLMOutputError` subclasses on
    failure; every error carries the full provenance envelope from
    ``llm_result_meta`` so the response audit / CLI does not need to
    sniff message text to render an incident report.
    """
    # Stage 1 — JSON parse + Pydantic validation.
    #
    # Extract the embedded JSON object first (issue #144): some models
    # (notably claude-sonnet-4-6 on the business-rules path) narrate a
    # prose preamble before the `{`, and the model does not support an
    # assistant-turn prefill to force JSON-only output. `extract_json_payload`
    # strips the preamble; on a response with no JSON it returns the text
    # unchanged so the error path below still fires with the right excerpt.
    # The error envelopes keep the ORIGINAL `raw_text` so incident reports
    # show exactly what the model emitted, preamble included.
    payload = extract_json_payload(raw_text)
    try:
        candidate = CandidateSchema.model_validate_json(payload)
    except ValidationError as exc:
        if _is_json_invalid_error(exc):
            # Recover (line, column) positional context by re-parsing
            # via json.loads — Pydantic does not expose the
            # JSONDecodeError instance directly. If that re-parse
            # somehow succeeds (race between Pydantic's parser and the
            # stdlib parser), fall through to the validation-error path
            # so we never lose the failure signal.
            try:
                json.loads(payload)
            except json.JSONDecodeError as decode_exc:
                raise LLMOutputJSONError(
                    "LLM response was not valid JSON.",
                    cause=decode_exc,
                    raw_text=raw_text,
                    prompt_version=llm_result_meta.prompt_version,
                    model=llm_result_meta.model,
                    cache_hit=llm_result_meta.cache_hit,
                    input_tokens=llm_result_meta.input_tokens,
                    output_tokens=llm_result_meta.output_tokens,
                ) from decode_exc
        raise LLMOutputValidationError(
            "LLM response did not match the CandidateSchema shape.",
            cause=exc,
            raw_text=raw_text,
            prompt_version=llm_result_meta.prompt_version,
            model=llm_result_meta.model,
            cache_hit=llm_result_meta.cache_hit,
            input_tokens=llm_result_meta.input_tokens,
            output_tokens=llm_result_meta.output_tokens,
        ) from exc

    # Stage 2 — Anchor-contract validation.
    violations = _validate_anchor_contract(
        candidate,
        model_columns,
        model_columns_by_type=model_columns_by_type,
        dialect_name=dialect_name,
        exclude_tests=exclude_tests,
        business_rules=business_rules,
    )
    if violations:
        raise LLMOutputAnchorContractError(
            f"LLM response violated the anchor contract ({len(violations)} violation(s)).",
            violations=violations,
            raw_text=raw_text,
            prompt_version=llm_result_meta.prompt_version,
            model=llm_result_meta.model,
            cache_hit=llm_result_meta.cache_hit,
            input_tokens=llm_result_meta.input_tokens,
            output_tokens=llm_result_meta.output_tokens,
        )

    return candidate


__all__ = ("parse_draft_response",)
