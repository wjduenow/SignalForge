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
from dataclasses import dataclass

from pydantic import ValidationError

from signalforge.draft.errors import (
    LLMOutputAnchorContractError,
    LLMOutputJSONError,
    LLMOutputValidationError,
)
from signalforge.draft.models import CandidateSchema


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


def _validate_anchor_contract(
    candidate: CandidateSchema,
    model_columns: frozenset[str],
    *,
    exclude_tests: frozenset[str] = frozenset(),
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
    """
    violations: list[str] = []

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
        if test.column not in model_columns:
            violations.append(f"model-level test references nonexistent column {test.column!r}")
        if test.type in exclude_tests:
            violations.append(
                f"model-level test type {test.type!r} is in exclude_tests "
                f"(excluded: {sorted(exclude_tests)})"
            )

    return tuple(violations)


def parse_draft_response(
    raw_text: str,
    model_columns: frozenset[str],
    *,
    llm_result_meta: _LLMResultMeta,
    exclude_tests: frozenset[str] = frozenset(),
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
    try:
        candidate = CandidateSchema.model_validate_json(raw_text)
    except ValidationError as exc:
        if _is_json_invalid_error(exc):
            # Recover (line, column) positional context by re-parsing
            # via json.loads — Pydantic does not expose the
            # JSONDecodeError instance directly. If that re-parse
            # somehow succeeds (race between Pydantic's parser and the
            # stdlib parser), fall through to the validation-error path
            # so we never lose the failure signal.
            try:
                json.loads(raw_text)
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
        candidate, model_columns, exclude_tests=exclude_tests
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
