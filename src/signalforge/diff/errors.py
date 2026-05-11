"""Diff-renderer typed exception hierarchy.

Implements US-001 of issue #8 (DEC-002, DEC-006, DEC-009): a seven-class
hierarchy rooted at :class:`DiffError` that mirrors the rendering convention
established by :mod:`signalforge.safety.errors`,
:mod:`signalforge.draft.errors`, :mod:`signalforge.prune.errors`, and
:mod:`signalforge.grade.errors`. Every error carries a class-level
``default_remediation`` that the base ``__str__`` renders on a separate
``↳ Remediation:`` line, and every user-supplied string flowing into an
error message routes through :func:`_format_value` (``repr()``-based,
ANSI-safe — DEC-022 of #6).

The remediation pattern operationalises the README's "explainable diffs"
commitment at the diff-renderer's failure surface; every distinct failure
mode the renderer can produce gets a typed exception so the orchestrator /
CLI can pattern-match on type rather than sniffing message text.

The seven classes:

1. :class:`DiffError` — base.
2. :class:`DiffCandidateModelMismatchError` — DEC-002 boundary check
   (``candidate.name`` vs. ``model.name``).
3. :class:`DiffPruneResultModelMismatchError` — DEC-002 boundary check
   (``prune_result.model_unique_id`` vs. ``model.unique_id``).
4. :class:`DiffGradingReportModelMismatchError` — DEC-002 boundary check
   (``grading_report.model_unique_id`` vs. ``model.unique_id``; only when
   the optional report is provided).
5. :class:`DiffInputTooLargeError` — DEC-006 (existing-schema YAML byte
   cap; refused before any ``yaml.safe_load`` to defend against
   billion-laughs / deep-nesting attacks).
6. :class:`DiffSidecarRecordTooLargeError` — DEC-009 (sidecar JSON
   payload byte cap; refused before any ``os.open``).
7. :class:`DiffSidecarWriteError` — fail-closed wrapper around OS
   errors raised inside the sidecar writer (mirrors the safety / draft /
   prune / grade audit-write seam).

See ``plans/super/8-diff-renderer.md`` for the full design.
"""

from __future__ import annotations

from typing import ClassVar


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages (DEC-022 of #6).

    Embedding raw user input in error strings is a log-injection seam: a
    crafted ``model_name`` like ``"model.shop.\\x1b[31mevil"`` (or a value
    containing ``"foo'\\nINFO: spoofed log line"``) could pollute log
    viewers or stack traces. Routing every user-controlled value through
    ``repr()`` quotes the string, escapes control characters, and makes
    whitespace visible.
    """
    return repr(v)


class DiffError(Exception):
    """Base class for all diff-renderer errors.

    Subclasses set a class-level ``default_remediation`` string; instances
    may override it via the ``remediation=`` keyword argument. ``__str__``
    renders the message and the remediation on separate lines so log
    output and CLI output both read cleanly.
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


class DiffCandidateModelMismatchError(DiffError):
    """The supplied :class:`~signalforge.draft.CandidateSchema`'s ``name``
    does not match the ``Model.name`` under render.

    DEC-002 boundary check, raised at ``render_diff`` entry BEFORE any
    rendering work begins. Mirrors the precedent from
    :class:`signalforge.grade.engine` (the ``prune_result.model_unique_id``
    boundary check) — a candidate built from a different model would
    silently drive the wrong column-set into the unified diff and the
    operator would never see a loud signal that the inputs were stale.
    """

    default_remediation: ClassVar[str] = (
        "Verify that the `candidate` argument was produced for the same `model` "
        "passed to `render_diff`. The mismatch usually surfaces when an old "
        "draft is reused after the model was renamed; re-run the drafter with "
        "the current model to refresh the candidate."
    )

    def __init__(
        self,
        candidate_name: str,
        model_name: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.candidate_name = candidate_name
        self.model_name = model_name
        message = (
            f"Candidate name {_format_value(candidate_name)} does not match "
            f"model name {_format_value(model_name)}."
        )
        super().__init__(message, remediation=remediation)


class DiffPruneResultModelMismatchError(DiffError):
    """The supplied :class:`~signalforge.prune.PruneResult`'s
    ``model_unique_id`` does not match the ``Model.unique_id`` under render.

    DEC-002 boundary check, raised at ``render_diff`` entry BEFORE any
    rendering work. Mirrors :mod:`signalforge.grade.engine`'s entry-time
    boundary check verbatim — a stale prune result would feed misleading
    kept/dropped tallies into the rendered diff and the per-test "why"
    column would point at the wrong model.
    """

    default_remediation: ClassVar[str] = (
        "Verify that the `prune_result` argument was produced by `prune_tests` "
        "for the same `model` passed to `render_diff`. Re-running `prune_tests` "
        "with the current model is the supported recovery path."
    )

    def __init__(
        self,
        prune_id: str,
        model_id: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.prune_id = prune_id
        self.model_id = model_id
        message = (
            f"Prune-result model_unique_id {_format_value(prune_id)} does not "
            f"match model unique_id {_format_value(model_id)}."
        )
        super().__init__(message, remediation=remediation)


class DiffGradingReportModelMismatchError(DiffError):
    """The (optional) :class:`~signalforge.grade.GradingReport`'s
    ``model_unique_id`` does not match the ``Model.unique_id`` under render.

    DEC-002 boundary check, raised at ``render_diff`` entry BEFORE any
    rendering work — but only when the caller actually supplied a
    ``grading_report``; the argument is optional, and its absence is not
    an error. A mismatched grading report would join scored criteria onto
    the wrong artifacts and the rendered "flagged" tier would be
    nonsensical.
    """

    default_remediation: ClassVar[str] = (
        "Verify that the `grading_report` argument was produced by "
        "`grade_artifacts` for the same `model` passed to `render_diff`. "
        "Re-running `grade_artifacts` with the current model is the supported "
        "recovery path; alternatively, omit the `grading_report` argument to "
        "render a kept/dropped diff without per-criterion scores."
    )

    def __init__(
        self,
        grade_id: str,
        model_id: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.grade_id = grade_id
        self.model_id = model_id
        message = (
            f"Grading-report model_unique_id {_format_value(grade_id)} does not "
            f"match model unique_id {_format_value(model_id)}."
        )
        super().__init__(message, remediation=remediation)


class DiffInputTooLargeError(DiffError):
    """The ``existing_schema`` YAML payload exceeds the byte cap enforced
    BEFORE any ``yaml.safe_load`` call.

    DEC-006: ``yaml.safe_load`` is safe against arbitrary-code execution
    but is NOT safe against pathological payloads. Billion-laughs (deeply
    nested anchor expansion) and arbitrary deep-nesting attacks can
    consume gigabytes of memory before the parser yields. The cap is
    checked on the raw byte length (``len(existing_schema.encode("utf-8"))``)
    so the parser never sees a hostile payload.

    Raised BEFORE any parser call, so a hostile schema cannot reach the
    YAML deserialiser.
    """

    default_remediation: ClassVar[str] = (
        "The existing schema.yml exceeded the configured byte safety cap "
        "(default 10 MB) applied before yaml.safe_load. Inspect the file for "
        "accidental bloat (large embedded docs, copy-pasted dumps) or for an "
        "attempted billion-laughs / deeply-nested-anchor payload. Trim the "
        "schema or split it into multiple files."
    )

    def __init__(self, size: int, limit: int, *, remediation: str | None = None) -> None:
        self.size = size
        self.limit = limit
        message = f"Existing schema.yml size {size} exceeds parse-safety limit {limit}."
        if remediation is None:
            remediation = (
                f"The existing schema.yml exceeded the {limit}-byte safety cap "
                "applied before yaml.safe_load (DEC-006). Trim oversized "
                "embedded content or split the schema across multiple files."
            )
        super().__init__(message, remediation=remediation)


class DiffSidecarRecordTooLargeError(DiffError):
    """The diff sidecar JSON payload exceeds the per-document size cap.

    DEC-009: the diff sidecar is a single-document JSON file written via
    ``O_WRONLY | O_CREAT | O_TRUNC | 0o600`` and is ~10× the grade
    sidecar's cap (1 MB → 10 MB) because diff text is naturally larger
    than evidence-only payloads. Cap is checked on the encoded byte
    length BEFORE any ``os.open`` so an oversize payload leaves no
    on-disk artefact.

    Raised BEFORE the file is opened — mirrors safety's
    :class:`signalforge.safety.errors.AuditRecordTooLargeError`, draft's
    :class:`signalforge.draft.errors.LLMResponseAuditRecordTooLargeError`,
    prune's :class:`signalforge.prune.errors.PruneAuditRecordTooLargeError`,
    and grade's
    :class:`signalforge.grade.errors.GradeAuditRecordTooLargeError`.
    """

    default_remediation: ClassVar[str] = (
        "The diff sidecar payload exceeded the configured byte cap. Common "
        "causes: an unusually wide model (1000+ columns), a large embedded "
        "existing-schema YAML, or an exceptionally long unified-diff body. "
        "Reduce the candidate scope, split the model, or omit the sidecar "
        "by passing `sidecar_path=None`."
    )

    def __init__(self, size: int, limit: int, *, remediation: str | None = None) -> None:
        self.size = size
        self.limit = limit
        message = f"Diff sidecar record size {size} exceeds size limit {limit}."
        if remediation is None:
            remediation = (
                f"The diff sidecar payload exceeded its {limit}-byte cap "
                "(DEC-009). Trim oversized fields (existing schema, unified "
                "diff body) or omit the sidecar via `sidecar_path=None`."
            )
        super().__init__(message, remediation=remediation)


class DiffSidecarWriteError(DiffError):
    """The fail-closed diff-sidecar writer could not durably persist the
    rendered report to disk.

    Mirrors safety's :class:`signalforge.safety.errors.AuditWriteError`,
    draft's :class:`signalforge.draft.errors.LLMResponseAuditWriteError`,
    prune's :class:`signalforge.prune.errors.PruneAuditWriteError`, and
    grade's :class:`signalforge.grade.errors.GradeAuditWriteError`: the
    writer catches **no** exceptions internally; any ``OSError`` /
    ``PermissionError`` / encoding failure / ``fsync`` failure / symlink
    containment failure propagates out via this class. The orchestrator
    must NOT return a :class:`signalforge.diff.DiffReport` whose sidecar
    didn't durably hit disk when the caller asked for one — silently
    succeeding when the receipt was lost is exactly the failure mode the
    fail-closed pattern exists to prevent.
    """

    default_remediation: ClassVar[str] = (
        "Verify the target sidecar path is writable (permissions / disk "
        "space / SELinux contexts) and that no symlink in the path escapes "
        "the project directory. The diff render is intentionally aborted "
        "when the sidecar write fails — re-running after fixing the "
        "underlying I/O issue is the supported recovery path."
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
        # Chain the underlying I/O cause so ``raise DiffSidecarWriteError(...)``
        # exposes ``exc.__cause__`` for callers that branch on the OS-level
        # detail (mirrors ``raise X from cause``).
        self.__cause__ = cause


# Sorted alphabetically (mirrors safety / draft / prune / grade / warehouse
# error modules).
__all__ = [
    "DiffCandidateModelMismatchError",
    "DiffError",
    "DiffGradingReportModelMismatchError",
    "DiffInputTooLargeError",
    "DiffPruneResultModelMismatchError",
    "DiffSidecarRecordTooLargeError",
    "DiffSidecarWriteError",
]
