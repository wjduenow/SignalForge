"""Prune-layer typed exception hierarchy.

Implements DEC-006 (six-class hierarchy rooted at :class:`PruneError`) and
DEC-022 (user-supplied strings rendered via ``repr()`` so adversarial input —
embedded quotes, control chars, ANSI escapes — cannot smuggle special
characters into log viewers or error messages). Mirrors the style established
by :mod:`signalforge.safety.errors` and :mod:`signalforge.draft.errors`:
every error carries a class-level ``default_remediation`` that the base
``__str__`` renders on a separate ``↳ Remediation:`` line.

The remediation pattern operationalises the README's "explainable diffs"
commitment at the prune layer's failure surface; every distinct failure mode
the prune machinery can produce gets a typed exception so the orchestrator /
CLI / diff renderer can pattern-match on type rather than sniffing message
text.

DEC-006 deliberately omits a ``PruneCompilerError`` class. Compilation always
succeeds; failures like ``relationships(to: unknown)`` emit a structured
drop reason (``requires-future-data``) via the orchestrator rather than an
exception. Adding a compiler-error class would let callers paper over a
classified drop with a generic catch and lose signal.

See ``plans/super/6-prune-engine.md`` for the full design.
"""

from __future__ import annotations

from typing import ClassVar

from signalforge.errors import SignalForgeError


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages (DEC-022).

    Embedding raw user input in error strings is a log-injection seam: a
    crafted unique_id like ``"model.shop.\\x1b[31mevil"`` (or a value
    containing ``"foo'\\nINFO: spoofed log line"``) could pollute log
    viewers or stack traces. Routing every user-controlled value through
    ``repr()`` quotes the string, escapes control characters, and makes
    whitespace visible.
    """
    return repr(v)


class PruneError(SignalForgeError):
    """Base class for all prune-layer errors.

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


class PruneConfigError(PruneError):
    """The ``signalforge.yml`` ``prune:`` block failed parse / schema
    validation.

    Wraps either a YAML parse failure, a wrong-shape top level, or a
    Pydantic ``ValidationError`` from :class:`signalforge.prune.config.PruneConfig`
    (the config-shaped model uses ``extra="forbid"`` per ``safety-layer.md``
    DEC-015 so typos like ``scop:`` instead of ``scope:`` fail loud rather
    than silently no-op).
    """

    default_remediation: ClassVar[str] = (
        "Inspect the `prune:` block of signalforge.yml — likely a typo in a "
        "key (config-shaped models use extra='forbid'), an unknown `scope` "
        "value (must be 'sample' or 'full'), or a non-positive "
        "`test_timeout_seconds` / `total_budget_seconds`. See "
        "docs/prune-ops.md for the field reference."
    )


class PruneTrustedModelNotFoundError(PruneConfigError):
    """``prune.trusted_models`` references a ``unique_id`` that does not
    appear in the loaded :class:`signalforge.manifest.Manifest`.

    Raised at ``prune_tests(...)`` entry — DEC-008 validates trusted-models
    at orchestrator entry rather than at config load time, because the
    manifest isn't loaded yet when ``load_prune_config`` runs. Surfacing
    the typo loudly keeps a misspelled trusted-models entry from silently
    losing its "treat clean-data failure as a real failure" semantics.
    """

    default_remediation: ClassVar[str] = (
        "Verify the unique_id matches a model in manifest.json (e.g. "
        "`model.<project>.<name>`). Trusted-models is validated at "
        "prune_tests() entry, NOT at config load — typos surface here, "
        "before any warehouse call is issued."
    )

    def __init__(self, unique_id: str, *, remediation: str | None = None) -> None:
        self.unique_id = unique_id
        message = f"Trusted model {_format_value(unique_id)} is not present in the loaded manifest."
        super().__init__(message, remediation=remediation)


class PruneTimeoutError(PruneError):
    """The prune run exceeded its ``total_budget_seconds`` budget.

    Wraps adapter cancellation when the orchestrator pulls the plug on an
    in-flight test. DEC-011: the orchestrator catches this internally and
    routes the in-flight + every remaining un-started test to
    ``kept-without-evidence`` with ``why="total prune budget exceeded
    before evaluation"``. Callers of :func:`signalforge.prune.prune_tests`
    do NOT see this exception; it is an internal control-flow signal.
    """

    default_remediation: ClassVar[str] = (
        "Total prune budget exceeded. Either raise `total_budget_seconds` in "
        "signalforge.yml, narrow the candidate set, or scope the run with a "
        "`partition_filter`. Tests not yet evaluated will ship as "
        "`kept-without-evidence` so no signal is silently dropped."
    )


class PruneAuditWriteError(PruneError):
    """The fail-closed prune-audit writer (DEC-016) could not durably
    persist the per-decision receipt.

    Mirrors safety's :class:`signalforge.safety.errors.AuditWriteError` and
    draft's :class:`signalforge.draft.errors.LLMResponseAuditWriteError`:
    the writer catches **no** exceptions internally; any ``OSError`` /
    ``PermissionError`` / encoding failure / ``fsync`` failure propagates
    out via this class. The orchestrator must NOT return a
    :class:`signalforge.prune.models.PruneResult` whose decision audit
    didn't durably hit disk — an unaudited prune decision is, by definition,
    a kept/dropped artefact without a receipt, exactly the failure mode the
    audit exists to prevent.
    """

    default_remediation: ClassVar[str] = (
        "Verify the prune-audit path (<project>/.signalforge/prune.jsonl) is "
        "writable (permissions / disk space / SELinux contexts) and re-run. "
        "The prune run is intentionally aborted when the audit write fails — "
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
        # Chain the underlying I/O cause so ``raise PruneAuditWriteError(...)``
        # exposes ``exc.__cause__`` for callers that branch on the OS-level
        # detail (mirrors ``raise X from cause``).
        self.__cause__ = cause


class PruneAuditRecordTooLargeError(PruneError):
    """A prune-audit JSONL record would exceed the POSIX atomic-append size cap.

    POSIX guarantees ``write(2)`` is atomic only for payloads up to
    ``PIPE_BUF`` bytes (typically 4 KiB on Linux). The prune-audit writer
    (DEC-016) enforces a size cap to keep concurrent appends from
    interleaving partial records. Mirrors safety's
    :class:`signalforge.safety.errors.AuditRecordTooLargeError` and draft's
    :class:`signalforge.draft.errors.LLMResponseAuditRecordTooLargeError`.

    Raised BEFORE any file is opened, so an oversize record leaves no
    on-disk artefact — the orchestrator sees the typed error and aborts
    the prune run rather than emitting a partial receipt.
    """

    default_remediation: ClassVar[str] = (
        "Prune-audit records must stay under the configured byte limit for "
        "atomic concurrent appends; the decision payload was unusually "
        "large. Consider tightening `capture_failure_rows` in signalforge.yml "
        "or trimming `compiled_sql` size by simplifying the candidate test. "
        "Note: 4 KB is the POSIX-atomic-append guarantee on Linux — exceeding "
        "it makes concurrent writers unsafe."
    )

    def __init__(self, size: int, limit: int, *, remediation: str | None = None) -> None:
        self.size = size
        self.limit = limit
        message = f"Prune audit record size {size} exceeds atomic-append limit {limit}."
        if remediation is None:
            remediation = (
                f"Prune-audit records must stay under {limit} bytes for "
                "atomic concurrent appends; reduce capture_failure_rows or "
                "trim the compiled_sql payload."
            )
        super().__init__(message, remediation=remediation)


# Sorted alphabetically (mirrors safety/draft/warehouse error modules).
__all__ = [
    "PruneAuditRecordTooLargeError",
    "PruneAuditWriteError",
    "PruneConfigError",
    "PruneError",
    "PruneTimeoutError",
    "PruneTrustedModelNotFoundError",
]
