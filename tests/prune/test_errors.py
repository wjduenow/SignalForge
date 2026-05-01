"""Tests for ``signalforge.prune.errors`` (US-006, DEC-006, DEC-022).

Covers the six-class typed exception hierarchy: every distinct prune-layer
failure mode (config load, trusted-models manifest mismatch, total-budget
timeout, fail-closed audit-write, audit-record size cap) gets a typed
exception so the orchestrator / CLI can pattern-match on type rather than
sniffing message text.

Each subclass renders message + ``↳ Remediation:`` and quotes user-supplied
strings via :func:`repr` so adversarial input — embedded quotes, control
chars, ANSI escapes — cannot smuggle special characters into log viewers
or error messages.
"""

from __future__ import annotations

import pytest

from signalforge.errors import SignalForgeError
from signalforge.prune.errors import (
    PruneAuditRecordTooLargeError,
    PruneAuditWriteError,
    PruneConfigError,
    PruneError,
    PruneTimeoutError,
    PruneTrustedModelNotFoundError,
)


def test_prune_error_renders_message_and_remediation() -> None:
    """Base ``__str__`` renders message and remediation on separate lines."""
    rendered = str(PruneError("the SQL was malformed"))
    assert "the SQL was malformed" in rendered
    assert "↳ Remediation:" in rendered


@pytest.mark.parametrize(
    "exc",
    [
        PruneConfigError("invalid prune config"),
        PruneTrustedModelNotFoundError(unique_id="model.shop.customers"),
        PruneTimeoutError("total budget exceeded"),
        PruneAuditWriteError("fsync failed", cause=OSError("disk full")),
        PruneAuditRecordTooLargeError(size=5000, limit=4000),
    ],
)
def test_subclasses_have_default_remediation(exc: PruneError) -> None:
    """Each concrete subclass exposes a non-empty remediation string when
    no explicit ``remediation=`` kwarg is supplied.

    The remediation is either the class-level ``default_remediation``
    verbatim or a templated form derived from it (e.g.
    :class:`PruneAuditRecordTooLargeError` formats the limit number into
    the remediation at construction time — matches the safety / draft
    precedent for the same shape of error).
    """
    # Class-level default is set to something concrete, not the base sentinel.
    assert "(no remediation set" not in type(exc).default_remediation
    assert type(exc).default_remediation.strip() != ""
    # Instance remediation is non-empty (constructor either uses the class
    # default verbatim or substitutes a templated form derived from it).
    assert exc.remediation.strip() != ""
    assert "(no remediation set" not in exc.remediation


def test_prune_trusted_model_not_found_repr_quotes_unique_id() -> None:
    """A unique_id containing an ANSI escape MUST NOT render as a raw
    escape sequence (DEC-022 — log-injection defence).

    The value is rendered via ``repr()`` so ``\\x1b`` shows as the literal
    four characters ``\\x1b`` rather than the actual ESC byte.
    """
    exc = PruneTrustedModelNotFoundError(unique_id="model.shop.\x1b[31mevil")
    rendered = str(exc)
    # The raw ANSI escape (single byte 0x1b) MUST NOT appear in output.
    assert "\x1b" not in rendered
    # repr()-quoted form: the four literal chars ``\x1b`` MUST appear.
    assert "\\x1b" in rendered
    # The exception still exposes the original (un-quoted) unique_id field
    # so callers can branch on it.
    assert exc.unique_id == "model.shop.\x1b[31mevil"


def test_prune_audit_write_error_carries_cause() -> None:
    """``PruneAuditWriteError`` carries the underlying I/O error as
    ``__cause__`` so ``except ... as exc: exc.__cause__`` works for
    callers that need to log the OS-level detail."""
    cause = OSError("disk full")
    exc = PruneAuditWriteError("fsync failed", cause=cause)
    assert exc.__cause__ is cause
    # The attribute is also exposed for direct introspection.
    assert exc.cause is cause


def test_prune_audit_record_too_large_error_carries_size_and_limit() -> None:
    """``PruneAuditRecordTooLargeError`` exposes both numbers on the
    instance and renders both into the message so the operator can see
    the gap at a glance."""
    exc = PruneAuditRecordTooLargeError(size=5000, limit=4000)
    assert exc.size == 5000
    assert exc.limit == 4000
    rendered = str(exc)
    assert "5000" in rendered
    assert "4000" in rendered


def test_subclass_inheritance_chain() -> None:
    """Every prune error subclasses ``PruneError`` (and through it,
    :class:`signalforge.errors.SignalForgeError`).

    ``PruneTrustedModelNotFoundError`` is a :class:`PruneConfigError`
    subclass so callers that catch "any config-shaped failure" get
    trusted-models mismatches for free (DEC-006).
    """
    assert issubclass(PruneTrustedModelNotFoundError, PruneConfigError)
    assert issubclass(PruneConfigError, PruneError)
    assert issubclass(PruneTimeoutError, PruneError)
    assert issubclass(PruneAuditWriteError, PruneError)
    assert issubclass(PruneAuditRecordTooLargeError, PruneError)
    assert issubclass(PruneError, SignalForgeError)
