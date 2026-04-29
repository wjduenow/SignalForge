"""Typed exception hierarchy for the safety / PII layer.

Implements DEC-026 (10-class hierarchy rooted at :class:`SafetyError`) and
DEC-022 (user-supplied strings rendered via ``repr()`` so adversarial input —
embedded quotes, control chars, ANSI escapes — cannot smuggle special
characters into log viewers or error messages). Mirrors the style established
by :mod:`signalforge.warehouse.errors` and :mod:`signalforge.manifest.errors`:
every error carries a class-level ``default_remediation`` that the base
``__str__`` renders on a separate ``↳ Remediation:`` line.

The remediation pattern operationalises the README's "explainable diffs"
commitment at the safety layer's failure surface; every distinct failure mode
the safety machinery can produce gets a typed exception so the CLI / audit
layer can pattern-match without sniffing message text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar


def _format_value(v: object) -> str:
    """Quote a user-supplied value via ``repr()`` for safe inclusion in
    error messages (DEC-022).

    Embedding raw user input in error strings is a log-injection seam: a
    crafted pattern like ``"foo'\\nINFO: spoofed log line"`` (or an ANSI
    escape such as ``"\\x1b[31m"``) could pollute log viewers or stack
    traces. Routing every user-controlled value through ``repr()`` quotes
    the string, escapes control characters, and makes whitespace visible.
    """
    return repr(v)


class SafetyError(Exception):
    """Base class for all safety-layer errors.

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


class ConfigNotFoundError(SafetyError):
    """An explicit ``path=`` argument pointed at a missing safety config file.

    Raised only when the caller passed a path explicitly; the implicit
    default-discovery path (``<project_dir>/signalforge.yml``) is allowed to
    be absent and falls back to built-in defaults.
    """

    default_remediation: ClassVar[str] = (
        "Verify the path is correct, or pass path=None to fall back to "
        "<project_dir>/signalforge.yml or built-in defaults."
    )

    def __init__(self, path: Path, *, remediation: str | None = None) -> None:
        self.path = path
        message = f"Safety config not found at {_format_value(str(path))}."
        super().__init__(message, remediation=remediation)


class InvalidConfigError(SafetyError):
    """Parent for parse / schema failures in ``signalforge.yml``.

    Free-form message; subclasses (e.g. :class:`InvalidSamplingModeError`,
    :class:`InvalidPatternError`) refine it with structured fields. Catching
    ``InvalidConfigError`` covers every shape-of-config failure.
    """

    default_remediation: ClassVar[str] = (
        "Check signalforge.yml against the documented schema in docs/safety-ops.md."
    )


class InvalidSamplingModeError(InvalidConfigError):
    """The ``mode:`` field of the safety config is not one of the allowed
    sampling-mode literals (``schema-only`` / ``aggregate-only`` / ``sample``)."""

    default_remediation: ClassVar[str] = (
        "Set safety.mode to one of the documented sampling modes; see docs/safety-ops.md."
    )

    def __init__(
        self,
        value: Any,
        *,
        allowed: tuple[str, ...],
        remediation: str | None = None,
    ) -> None:
        self.value = value
        self.allowed = allowed
        message = f"Invalid sampling mode {_format_value(value)}; allowed values: {allowed}."
        if remediation is None:
            remediation = f"mode must be one of {allowed}; got {_format_value(value)}."
        super().__init__(message, remediation=remediation)


class InvalidPatternError(InvalidConfigError):
    """A redact / allowlist pattern failed validation.

    Patterns must be non-empty fnmatch globs and may not be the bare
    wildcards ``"*"`` / ``"?"`` (which would disable the safety layer
    silently).
    """

    default_remediation: ClassVar[str] = (
        "Patterns must be non-empty fnmatch globs and may not be the bare wildcards '*' or '?'."
    )

    def __init__(self, value: str, *, reason: str, remediation: str | None = None) -> None:
        self.value = value
        self.reason = reason
        message = f"Invalid pattern {_format_value(value)}: {_format_value(reason)}."
        super().__init__(message, remediation=remediation)


class ColumnNotInModelError(SafetyError):
    """The caller asked for a column the manifest model does not expose.

    Raised by safety-layer helpers that look up columns by name on a
    :class:`signalforge.manifest.Model` and find no match in
    ``manifest.nodes[model].columns``.
    """

    default_remediation: ClassVar[str] = (
        "Verify the column exists in manifest.nodes[model].columns."
    )

    def __init__(
        self,
        model_unique_id: str,
        column_name: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.model_unique_id = model_unique_id
        self.column_name = column_name
        message = (
            f"Column {_format_value(column_name)} is not declared on model "
            f"{_format_value(model_unique_id)}."
        )
        super().__init__(message, remediation=remediation)


class AuditWriteError(SafetyError):
    """Appending a record to the JSONL audit log failed (any I/O error).

    The audit log is fail-closed: when a write fails, the LLM call that
    triggered it is aborted rather than allowed to proceed without an
    audit trail.
    """

    default_remediation: ClassVar[str] = (
        "Check that <project_dir>/.signalforge/ exists and is writable; the "
        "audit log is fail-closed — the LLM call is aborted on write failure."
    )

    def __init__(
        self,
        path: Path,
        cause: BaseException | None = None,
        *,
        remediation: str | None = None,
    ) -> None:
        self.path = path
        self.cause = cause
        if cause is not None:
            message = (
                f"Audit log write failed at {_format_value(str(path))}: {_format_value(cause)!s}"
            )
        else:
            message = f"Audit log write failed at {_format_value(str(path))}."
        super().__init__(message, remediation=remediation)


class AuditRecordTooLargeError(SafetyError):
    """An audit JSONL record would exceed the POSIX atomic-append size cap.

    POSIX guarantees ``write(2)`` is atomic only for payloads up to ``PIPE_BUF``
    bytes (typically 4 KiB on Linux). The audit writer enforces a size cap
    to keep concurrent appends from interleaving partial records.
    """

    default_remediation: ClassVar[str] = (
        "Audit records must stay under the configured byte limit for atomic "
        "concurrent appends; reduce columns_sent or redactions count."
    )

    def __init__(self, size: int, limit: int, *, remediation: str | None = None) -> None:
        self.size = size
        self.limit = limit
        message = f"Audit record size {size} exceeds atomic-append limit {limit}."
        if remediation is None:
            remediation = (
                f"Audit records must stay under {limit} bytes for atomic "
                "concurrent appends; reduce columns_sent or redactions count."
            )
        super().__init__(message, remediation=remediation)


class PolicyValidationError(SafetyError):
    """Generic policy-shape validation failure.

    Raised when a :class:`SafetyPolicy` field has the wrong type or otherwise
    fails its post-construction invariants. The discriminating triple
    (``field``, ``value``, ``reason``) lets callers report the exact field
    that tripped without sniffing message text.
    """

    default_remediation: ClassVar[str] = (
        "Verify SafetyPolicy fields match the documented types in docs/safety-ops.md."
    )

    def __init__(
        self,
        field: str,
        value: Any,
        reason: str,
        *,
        remediation: str | None = None,
    ) -> None:
        self.field = field
        self.value = value
        self.reason = reason
        message = (
            f"Invalid SafetyPolicy field {_format_value(field)}="
            f"{_format_value(value)}: {_format_value(reason)}."
        )
        if remediation is None:
            remediation = f"Verify SafetyPolicy field {field!r} matches the documented type."
        super().__init__(message, remediation=remediation)


class UnknownConfigKeyError(SafetyError):
    """A typo'd or unsupported key was found under a known config scope.

    Raised by ``extra="forbid"`` validators on the safety config models
    (e.g. ``redacts:`` instead of ``redact:``). Surfacing typos loudly
    keeps the safety config from silently doing nothing.
    """

    default_remediation: ClassVar[str] = (
        "Remove or rename the unknown key; see docs/safety-ops.md for the supported schema."
    )

    def __init__(self, key: str, scope: str, *, remediation: str | None = None) -> None:
        self.key = key
        self.scope = scope
        message = f"Unknown config key {_format_value(key)} under scope {_format_value(scope)}."
        if remediation is None:
            remediation = (
                f"Remove or rename the unknown key {key!r} under {scope!r}; "
                "see docs/safety-ops.md for the supported schema."
            )
        super().__init__(message, remediation=remediation)


# Sorted alphabetically (verified by tests/safety/test_errors.py).
__all__ = [
    "AuditRecordTooLargeError",
    "AuditWriteError",
    "ColumnNotInModelError",
    "ConfigNotFoundError",
    "InvalidConfigError",
    "InvalidPatternError",
    "InvalidSamplingModeError",
    "PolicyValidationError",
    "SafetyError",
    "UnknownConfigKeyError",
]
