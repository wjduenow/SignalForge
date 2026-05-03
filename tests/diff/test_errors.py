"""Tests for ``signalforge.diff.errors`` (US-001 of #8; DEC-002, DEC-006,
DEC-009).

Covers the seven-class typed exception hierarchy: every distinct
diff-renderer failure mode (the three boundary checks at orchestrator
entry, the existing-schema YAML byte cap, the sidecar size cap, and the
fail-closed sidecar-write seam) gets a typed exception so the orchestrator
/ CLI can pattern-match on type rather than sniffing message text.

Each subclass renders message + ``↳ Remediation:`` and quotes user-supplied
strings via :func:`repr` so adversarial input — embedded quotes, control
chars, ANSI escapes — cannot smuggle special characters into log viewers
or error messages. Mirrors the precedent from
:mod:`signalforge.safety.errors`, :mod:`signalforge.draft.errors`,
:mod:`signalforge.prune.errors`, and :mod:`signalforge.grade.errors`.
"""

from __future__ import annotations

import pytest

from signalforge.diff.errors import (
    DiffCandidateModelMismatchError,
    DiffError,
    DiffGradingReportModelMismatchError,
    DiffInputTooLargeError,
    DiffPruneResultModelMismatchError,
    DiffSidecarRecordTooLargeError,
    DiffSidecarWriteError,
)


def test_diff_error_renders_message_and_remediation() -> None:
    """Base ``__str__`` renders message and remediation on separate lines."""
    rendered = str(DiffError("the diff renderer hit an internal error"))
    assert "the diff renderer hit an internal error" in rendered
    assert "↳ Remediation:" in rendered


def test_diff_error_accepts_explicit_remediation() -> None:
    """An explicit ``remediation=`` kwarg overrides the class-level default."""
    exc = DiffError("boom", remediation="run signalforge --rebuild")
    assert exc.remediation == "run signalforge --rebuild"
    rendered = str(exc)
    assert "run signalforge --rebuild" in rendered
    assert "↳ Remediation:" in rendered


def test_diff_errors_smoke_import() -> None:
    """Smoke: the documented public-acceptance import line in US-001 works.

    The per-name imports at the top of this module are the actual smoke
    test; this body asserts the subclass-of-``Exception`` relationship to
    give pytest something to fail against if the imports degrade.
    """
    for cls in (
        DiffError,
        DiffCandidateModelMismatchError,
        DiffPruneResultModelMismatchError,
        DiffGradingReportModelMismatchError,
        DiffInputTooLargeError,
        DiffSidecarRecordTooLargeError,
        DiffSidecarWriteError,
    ):
        assert issubclass(cls, Exception)


@pytest.mark.parametrize(
    "exc",
    [
        DiffCandidateModelMismatchError("candidate_a", "model_b"),
        DiffPruneResultModelMismatchError("model.shop.a", "model.shop.b"),
        DiffGradingReportModelMismatchError("model.shop.a", "model.shop.b"),
        DiffInputTooLargeError(size=2_000_000, limit=1_000_000),
        DiffSidecarRecordTooLargeError(size=11_000_000, limit=10_000_000),
        DiffSidecarWriteError("fsync failed", cause=OSError("disk full")),
    ],
)
def test_subclasses_have_default_remediation(exc: DiffError) -> None:
    """Each concrete subclass exposes a non-empty remediation string when
    no explicit ``remediation=`` kwarg is supplied.

    The remediation is either the class-level ``default_remediation``
    verbatim or a templated form derived from it (e.g.
    :class:`DiffInputTooLargeError` formats the limit number into the
    remediation at construction time — matches the safety / draft /
    prune / grade precedent for the same shape of error).
    """
    # Class-level default is concrete, not the base sentinel.
    assert "(no remediation set" not in type(exc).default_remediation
    assert type(exc).default_remediation.strip() != ""
    # Instance remediation is non-empty.
    assert exc.remediation.strip() != ""
    assert "(no remediation set" not in exc.remediation
    # __str__ renders both message and remediation.
    rendered = str(exc)
    assert "↳ Remediation:" in rendered
    assert exc.message in rendered


@pytest.mark.parametrize(
    "exc",
    [
        DiffCandidateModelMismatchError("a", "b"),
        DiffPruneResultModelMismatchError("a", "b"),
        DiffGradingReportModelMismatchError("a", "b"),
        DiffInputTooLargeError(size=2_000_000, limit=1_000_000),
        DiffSidecarRecordTooLargeError(size=11_000_000, limit=10_000_000),
        DiffSidecarWriteError("fsync failed", cause=OSError("disk full")),
    ],
)
def test_subclasses_repr_round_trips(exc: DiffError) -> None:
    """``repr(exc)`` returns a non-empty string that includes the class
    name. The default ``Exception.__repr__`` shape is ``ClassName(msg)``;
    we don't pin the exact form, only that it's identifiable in tracebacks.
    """
    rendered = repr(exc)
    assert rendered != ""
    assert type(exc).__name__ in rendered


def test_candidate_mismatch_quotes_both_names_via_repr() -> None:
    """``DiffCandidateModelMismatchError("a", "b").__str__()`` quotes both
    names via ``repr()`` — the rendered form contains ``'a'`` and ``'b'``
    rather than the bare ``a`` / ``b``. This is the US-001 acceptance
    criterion."""
    exc = DiffCandidateModelMismatchError("a", "b")
    rendered = str(exc)
    assert "'a'" in rendered
    assert "'b'" in rendered
    # Fields are exposed on the instance for callers that branch on them.
    assert exc.candidate_name == "a"
    assert exc.model_name == "b"


def test_candidate_mismatch_repr_quotes_ansi_escape() -> None:
    """A model_name containing an ANSI escape MUST NOT render as a raw
    escape sequence (DEC-022 of #6 — log-injection defence).

    The value is rendered via ``repr()`` so ``\\x1b`` shows as the literal
    four characters ``\\x1b`` rather than the actual ESC byte. Mirrors
    :mod:`signalforge.grade.errors`'
    ``test_grade_prompt_envelope_breach_repr_quotes_artifact_id``.
    """
    exc = DiffCandidateModelMismatchError("safe", "\x1b[31mevil")
    rendered = str(exc)
    # The raw ANSI escape (single byte 0x1b) MUST NOT appear in output.
    assert "\x1b" not in rendered
    # repr()-quoted form: the four literal chars ``\x1b`` MUST appear.
    assert "\\x1b" in rendered
    # The exception still exposes the original (un-quoted) value so
    # callers can branch on it.
    assert exc.model_name == "\x1b[31mevil"


def test_prune_result_mismatch_exposes_fields() -> None:
    """``DiffPruneResultModelMismatchError`` exposes ``prune_id`` and
    ``model_id`` as separate fields and renders both via ``repr()`` in the
    message."""
    exc = DiffPruneResultModelMismatchError("model.shop.a", "model.shop.b")
    assert exc.prune_id == "model.shop.a"
    assert exc.model_id == "model.shop.b"
    rendered = str(exc)
    assert "'model.shop.a'" in rendered
    assert "'model.shop.b'" in rendered


def test_grading_report_mismatch_exposes_fields() -> None:
    """``DiffGradingReportModelMismatchError`` exposes ``grade_id`` and
    ``model_id`` as separate fields and renders both via ``repr()`` in the
    message."""
    exc = DiffGradingReportModelMismatchError("model.shop.a", "model.shop.b")
    assert exc.grade_id == "model.shop.a"
    assert exc.model_id == "model.shop.b"
    rendered = str(exc)
    assert "'model.shop.a'" in rendered
    assert "'model.shop.b'" in rendered


def test_input_too_large_carries_size_and_limit() -> None:
    """``DiffInputTooLargeError`` exposes both numbers on the instance and
    renders both into the message so the operator can see the gap at a
    glance."""
    exc = DiffInputTooLargeError(size=2_000_000, limit=1_000_000)
    assert exc.size == 2_000_000
    assert exc.limit == 1_000_000
    rendered = str(exc)
    assert "2000000" in rendered
    assert "1000000" in rendered


def test_sidecar_too_large_carries_size_and_limit() -> None:
    """``DiffSidecarRecordTooLargeError`` exposes both numbers on the
    instance and renders both into the message."""
    exc = DiffSidecarRecordTooLargeError(size=11_000_000, limit=10_000_000)
    assert exc.size == 11_000_000
    assert exc.limit == 10_000_000
    rendered = str(exc)
    assert "11000000" in rendered
    assert "10000000" in rendered


def test_sidecar_write_error_carries_cause() -> None:
    """``DiffSidecarWriteError`` carries the underlying I/O error as
    ``__cause__`` so ``except ... as exc: exc.__cause__`` works for callers
    that need to log the OS-level detail. Mirrors safety / draft / prune /
    grade audit-write errors."""
    cause = OSError("disk full")
    exc = DiffSidecarWriteError("fsync failed", cause=cause)
    assert exc.__cause__ is cause
    assert exc.cause is cause


def test_subclass_inheritance_chain() -> None:
    """Every diff error subclasses :class:`DiffError`, which itself
    subclasses ``Exception`` directly — same per-layer pattern as
    ``SafetyError``, ``DraftError``, ``PruneError``, ``GradeError``,
    ``WarehouseError``, ``ManifestError``."""
    assert issubclass(DiffCandidateModelMismatchError, DiffError)
    assert issubclass(DiffPruneResultModelMismatchError, DiffError)
    assert issubclass(DiffGradingReportModelMismatchError, DiffError)
    assert issubclass(DiffInputTooLargeError, DiffError)
    assert issubclass(DiffSidecarRecordTooLargeError, DiffError)
    assert issubclass(DiffSidecarWriteError, DiffError)
    assert issubclass(DiffError, Exception)
    # DiffError is a *direct* subclass of Exception — sibling layers follow
    # the same flat-one-level pattern.
    assert DiffError.__bases__ == (Exception,)


def test_signalforge_diff_subpackage_imports() -> None:
    """The ``signalforge.diff`` subpackage itself imports successfully
    (US-001 acceptance criterion — subpackage importable).

    The public surface ships only the error hierarchy in US-001; later
    stories (US-002 result models, US-003 config, US-010 orchestrator)
    expand ``__all__``.
    """
    import signalforge.diff

    assert "DiffError" in signalforge.diff.__all__
    assert "DiffCandidateModelMismatchError" in signalforge.diff.__all__
    assert "DiffPruneResultModelMismatchError" in signalforge.diff.__all__
    assert "DiffGradingReportModelMismatchError" in signalforge.diff.__all__
    assert "DiffInputTooLargeError" in signalforge.diff.__all__
    assert "DiffSidecarRecordTooLargeError" in signalforge.diff.__all__
    assert "DiffSidecarWriteError" in signalforge.diff.__all__
