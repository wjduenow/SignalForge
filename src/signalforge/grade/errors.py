"""Grader-layer typed exception hierarchy.

Implements DEC-028 (nine-class hierarchy rooted at :class:`GradeError`) and
mirrors the rendering convention established by
:mod:`signalforge.prune.errors` and :mod:`signalforge.draft.errors`: every
error carries a class-level ``default_remediation`` that the base
``__str__`` renders on a separate ``↳ Remediation:`` line, and every
user-supplied string flowing into an error message routes through
:func:`_format_value` (``repr()``-based, ANSI-safe — DEC-022 of #6).

The remediation pattern operationalises the README's "explainable diffs"
commitment at the grader's failure surface; every distinct failure mode the
grader can produce gets a typed exception so the orchestrator / CLI / diff
renderer can pattern-match on type rather than sniffing message text.

The nine classes (DEC-028):

1. :class:`GradeError` — base.
2. :class:`GradeConfigError` — config load / parse / validation failures.
3. :class:`GradeRubricError` — rubric YAML structure invalid (duplicate
   criterion ids, bare strings, missing required keys).
4. :class:`GradeLLMError` — wraps :class:`signalforge.llm.LLMError`; the
   one-level adapter used by the grader's exception ladder.
5. :class:`GradeBudgetExceededError` — ``total_budget_seconds`` tripped.
6. :class:`GradePromptEnvelopeBreachError` — ``</ARTIFACT>`` detected in
   artefact payload (the grader's prompt-injection defence — refuse to
   render rather than risk the envelope being closed early).
7. :class:`GradeOutputError` — LLM response invalid (carries
   ``violation_type`` field; the literal taxonomy is locked in US-006).
8. :class:`GradeAuditWriteError` — fail-closed audit propagation.
9. :class:`GradeAuditRecordTooLargeError` — size cap exceeded before
   file open.

See ``plans/super/7-quality-grader.md`` for the full design.
"""

from __future__ import annotations

from typing import ClassVar, Literal

GradeOutputViolationType = Literal[
    "json_parse",
    "missing_required_field",
    "missing_criterion_id",
    "criterion_id_mismatch",
    "score_out_of_range",
    "score_not_a_number",
    "passed_not_a_bool",
    "unknown_artifact_id",
    "ambiguous_artifact_id",
]
"""Locked taxonomy for :attr:`GradeOutputError.violation_type` (US-006).

Mirrors the drafter's anchor-contract precedent (#5 DEC-003 / DEC-022) —
the violation discriminator is a finite ``Literal`` so audit-log
consumers / orchestrator branches can pattern-match exhaustively rather
than sniffing message text. The first seven entries are produced by
:func:`signalforge.grade.parser.parse_grade_response`; the trailing two
(``unknown_artifact_id``, ``ambiguous_artifact_id``) are produced by
:func:`signalforge.grade.prompts.extract_artifact_text` (US-005) and are
preserved here verbatim — adding a tenth literal in v0.2 requires
updating production, the drift-detector strict mirror, and the
docs/grade-ops.md table in the same change.
"""


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages (DEC-022 of #6).

    Embedding raw user input in error strings is a log-injection seam: a
    crafted criterion_id like ``"crit.\\x1b[31mevil"`` (or a value
    containing ``"foo'\\nINFO: spoofed log line"``) could pollute log
    viewers or stack traces. Routing every user-controlled value through
    ``repr()`` quotes the string, escapes control characters, and makes
    whitespace visible.
    """
    return repr(v)


class GradeError(Exception):
    """Base class for all grader-layer errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    renders the message and the remediation on separate lines so log output
    and CLI output both read cleanly.
    """

    default_remediation: ClassVar[str] = "(no remediation set — this is the base class)"

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.remediation = (
            remediation if remediation is not None else type(self).default_remediation
        )

    def __str__(self) -> str:
        return f"{self.message}\n  ↳ Remediation: {self.remediation}"


class GradeConfigError(GradeError):
    """The ``signalforge.yml`` ``grade:`` block failed parse / schema
    validation.

    Wraps either a YAML parse failure, a wrong-shape top level, or a
    Pydantic ``ValidationError`` from the grader's config model (which
    uses ``extra="forbid"`` per ``safety-layer.md`` DEC-015 so typos like
    ``mdoel:`` instead of ``model:`` fail loud rather than silently
    no-op).
    """

    default_remediation: ClassVar[str] = (
        "Inspect the `grade:` block of signalforge.yml — likely a typo in a "
        "key (config-shaped models use extra='forbid'), an out-of-range "
        "numeric knob (e.g. non-positive `total_budget_seconds`), or a "
        "missing required field. See docs/grade-ops.md for the field "
        "reference once US-010 lands."
    )


class GradeRubricError(GradeError):
    """The rubric YAML failed structural validation.

    Raised when the rubric file is malformed in a way that no Pydantic
    schema check can catch generically: duplicate ``criterion_id`` values,
    bare-string entries where a mapping is required, missing required
    rubric-level metadata, or unsupported rubric-schema version. Loaded
    rubrics are content-hashed (``rubric_hash`` in :class:`GradeEvent`)
    so a structural failure here is the loud-fail path before any LLM
    call is issued.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the rubric YAML referenced by `grade.rubric_path`. Common "
        "causes: duplicate `criterion_id`, a criterion entry given as a bare "
        "string instead of a mapping, an unsupported `rubric_schema_version`. "
        "Each criterion must be a mapping with at least `id`, `description`, "
        "and `weight`. See docs/grade-ops.md for the rubric schema reference "
        "once US-003 lands."
    )


class GradeLLMError(GradeError):
    """One-level adapter wrapping :class:`signalforge.llm.LLMError`.

    The grader reuses the centralised :func:`signalforge.llm.call_anthropic`
    seam (#5 DEC-012) for its judge calls. When that seam raises an
    :class:`signalforge.llm.LLMError` subclass, the grader's exception
    ladder wraps it once into :class:`GradeLLMError` so callers that
    branch on "any grader error" (``except GradeError``) catch LLM
    failures without having to import the LLM error hierarchy directly.

    The original error is preserved as ``__cause__`` (and exposed as the
    ``cause`` attribute) so callers that need the underlying LLM-layer
    detail — retry exhaustion vs. auth failure vs. rate-limit — can
    pattern-match on the wrapped exception's type.
    """

    default_remediation: ClassVar[str] = (
        "Inspect the wrapped LLMError (exposed via the `cause` attribute / "
        "`__cause__`). Common causes: ANTHROPIC_API_KEY missing or invalid, "
        "rate-limit retries exhausted, the configured model id is "
        "unrecognised. The grader's `total_budget_seconds` does NOT cover "
        "LLM-layer retries — see signalforge.llm for the retry taxonomy."
    )

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException,
        remediation: str | None = None,
    ) -> None:
        self.cause = cause
        super().__init__(message, remediation=remediation)
        # Chain the underlying LLM-layer cause so ``raise GradeLLMError(...)``
        # exposes ``exc.__cause__`` for callers that branch on the
        # vendor-level detail (mirrors ``raise X from cause``).
        self.__cause__ = cause


class GradeBudgetExceededError(GradeError):
    """The grader run exceeded its ``total_budget_seconds`` budget.

    Mirrors the prune layer's budget semantics (``prune-engine.md``
    DEC-011): when the total wall-clock budget trips, the orchestrator
    routes any un-evaluated ``(artefact, criterion)`` pair to a degraded
    :class:`signalforge.grade.models.GradingResult` (DEC-015) rather than
    silently dropping it. This typed error surfaces from
    :func:`signalforge.grade.grade_artifacts` only when the budget trips
    *before* any criterion has been graded (a hard "the run did nothing"
    failure); a partial run completes normally with a
    :class:`signalforge.grade.models.GradingReport` whose
    ``aggregate_complete`` flag is ``False``.
    """

    default_remediation: ClassVar[str] = (
        "Total grade budget exceeded. Either raise `total_budget_seconds` in "
        "signalforge.yml, narrow the candidate set, or reduce the rubric's "
        "criterion count. Un-evaluated criteria ship with `score=None` and "
        "`aggregate_complete=False` so no signal is silently dropped."
    )


class GradePromptEnvelopeBreachError(GradeError):
    """The artefact payload contained the literal ``</ARTIFACT>`` close
    tag, which would terminate the prompt envelope early.

    Mirrors the drafter's ``PromptEnvelopeBreachError`` (#5 DEC-007): the
    ``<ARTIFACT>...</ARTIFACT>`` envelope is the grader's only LLM-prompt
    defence; the system message instructs the judge to treat anything
    between the tags as data, not instructions. A payload containing the
    close tag could let downstream content escape the fence and flip the
    judge's verdict. Refuse to render the prompt rather than ship a
    degraded envelope.
    """

    default_remediation: ClassVar[str] = (
        "The artefact payload contains a literal `</ARTIFACT>` close tag, "
        "which would close the prompt envelope early. Inspect the candidate "
        "artefact (column doc, test rationale, model description) and remove "
        "the literal tag. The envelope is the grader's only prompt-injection "
        "defence — refusing to render is the correct fail-loud behaviour."
    )

    def __init__(self, artifact_id: str, *, remediation: str | None = None) -> None:
        self.artifact_id = artifact_id
        message = (
            f"Artefact {_format_value(artifact_id)} contains a literal "
            "`</ARTIFACT>` close tag; refusing to render the judge prompt."
        )
        super().__init__(message, remediation=remediation)


class GradeOutputError(GradeError):
    """The LLM-judge response failed parse / anchor-contract validation.

    Carries a ``violation_type`` discriminator so the orchestrator can
    branch on the specific failure mode without sniffing message text.
    The literal taxonomy (``"criterion_id_mismatch"``,
    ``"missing_criterion_id"``, ``"score_out_of_range"``,
    ``"json_parse"``, ...) is locked in US-006 when the parser lands;
    this scaffold accepts any ``str`` so US-001 doesn't pre-bake a
    decision the parser story still owns.

    Per #5 DEC-013 (re-stated in ``llm-drafter.md``), a bad-LLM-response
    drop does NOT write a response audit — the parse failure raises
    *before* the audit-write seam, so the JSONL stays empty for that
    call. The grader inherits this contract.
    """

    default_remediation: ClassVar[str] = (
        "The LLM-judge response failed validation. Inspect the "
        "`violation_type` field on the exception to localise the failure "
        "mode (e.g. `criterion_id_mismatch` — the judge returned a different "
        "criterion id than the one sent; `score_out_of_range` — the score "
        "was outside [0.0, 1.0]; `json_parse` — the response wasn't valid "
        "JSON). Re-running typically resolves transient JSON failures; "
        "structural mismatches usually point at a prompt-template "
        "regression."
    )

    def __init__(
        self,
        message: str,
        *,
        violation_type: GradeOutputViolationType,
        remediation: str | None = None,
    ) -> None:
        self.violation_type: GradeOutputViolationType = violation_type
        super().__init__(message, remediation=remediation)


class GradeAuditWriteError(GradeError):
    """The fail-closed grade-audit writer could not durably persist a
    per-criterion receipt.

    Mirrors safety's :class:`signalforge.safety.errors.AuditWriteError`,
    draft's :class:`signalforge.draft.errors.LLMResponseAuditWriteError`,
    and prune's :class:`signalforge.prune.errors.PruneAuditWriteError`:
    the writer catches **no** exceptions internally; any ``OSError`` /
    ``PermissionError`` / encoding failure / ``fsync`` failure propagates
    out via this class. The orchestrator must NOT return a
    :class:`signalforge.grade.models.GradingReport` whose criterion audit
    didn't durably hit disk — an unaudited grade decision is, by
    definition, a kept/rejected verdict without a receipt, exactly the
    failure mode the audit exists to prevent.
    """

    default_remediation: ClassVar[str] = (
        "Verify the grade-audit path (<project>/.signalforge/grade.jsonl) is "
        "writable (permissions / disk space / SELinux contexts) and re-run. "
        "The grade run is intentionally aborted when the audit write fails — "
        "re-running after fixing the underlying I/O issue is the supported "
        "recovery path."
    )

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException,
        remediation: str | None = None,
    ) -> None:
        self.cause = cause
        super().__init__(message, remediation=remediation)
        # Chain the underlying I/O cause so ``raise GradeAuditWriteError(...)``
        # exposes ``exc.__cause__`` for callers that branch on the OS-level
        # detail (mirrors ``raise X from cause``).
        self.__cause__ = cause


class GradeAuditRecordTooLargeError(GradeError):
    """A grade-audit JSONL record would exceed the POSIX atomic-append
    size cap.

    POSIX guarantees ``write(2)`` is atomic only for payloads up to
    ``PIPE_BUF`` bytes (typically 4 KiB on Linux). The grade-audit writer
    enforces a size cap to keep concurrent appends from interleaving
    partial records. Mirrors safety's
    :class:`signalforge.safety.errors.AuditRecordTooLargeError`, draft's
    :class:`signalforge.draft.errors.LLMResponseAuditRecordTooLargeError`,
    and prune's
    :class:`signalforge.prune.errors.PruneAuditRecordTooLargeError`.

    Raised BEFORE any file is opened, so an oversize record leaves no
    on-disk artefact — the orchestrator sees the typed error and aborts
    the grade run rather than emitting a partial receipt.
    """

    default_remediation: ClassVar[str] = (
        "Grade-audit records must stay under the configured byte limit for "
        "atomic concurrent appends; the criterion payload was unusually "
        "large. Common causes: an oversized `reasoning` field in the LLM "
        "response or an over-long `one_line_why` summary. Note: 4 KB is the "
        "POSIX-atomic-append guarantee on Linux — exceeding it makes "
        "concurrent writers unsafe."
    )

    def __init__(self, size: int, limit: int, *, remediation: str | None = None) -> None:
        self.size = size
        self.limit = limit
        message = f"Grade audit record size {size} exceeds atomic-append limit {limit}."
        if remediation is None:
            remediation = (
                f"Grade-audit records must stay under {limit} bytes for "
                "atomic concurrent appends; reduce the reasoning payload or "
                "trim the one_line_why summary."
            )
        super().__init__(message, remediation=remediation)


# Sorted alphabetically (mirrors safety / draft / prune / warehouse error modules).
__all__ = [
    "GradeAuditRecordTooLargeError",
    "GradeAuditWriteError",
    "GradeBudgetExceededError",
    "GradeConfigError",
    "GradeError",
    "GradeLLMError",
    "GradeOutputError",
    "GradeOutputViolationType",
    "GradePromptEnvelopeBreachError",
    "GradeRubricError",
]
