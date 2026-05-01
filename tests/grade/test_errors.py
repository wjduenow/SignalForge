"""Tests for ``signalforge.grade.errors`` (US-001, DEC-028, DEC-022 of #6).

Covers the nine-class typed exception hierarchy: every distinct grader-layer
failure mode (config load, rubric structure, wrapped LLM error, total-budget
timeout, prompt-envelope breach, parser violation, fail-closed audit-write,
audit-record size cap) gets a typed exception so the orchestrator / CLI can
pattern-match on type rather than sniffing message text.

Each subclass renders message + ``↳ Remediation:`` and quotes user-supplied
strings via :func:`repr` so adversarial input — embedded quotes, control
chars, ANSI escapes — cannot smuggle special characters into log viewers
or error messages.
"""

from __future__ import annotations

import pytest

from signalforge.grade.errors import (
    GradeAuditRecordTooLargeError,
    GradeAuditWriteError,
    GradeBudgetExceededError,
    GradeConfigError,
    GradeError,
    GradeLLMError,
    GradeOutputError,
    GradePromptEnvelopeBreachError,
    GradeRubricError,
)


def test_grade_error_renders_message_and_remediation() -> None:
    """Base ``__str__`` renders message and remediation on separate lines."""
    rendered = str(GradeError("the judge response was empty"))
    assert "the judge response was empty" in rendered
    assert "↳ Remediation:" in rendered


def test_grade_errors_smoke_import() -> None:
    """Smoke: the documented public-acceptance import line in US-001 works."""
    # The import at module top level is the actual smoke test; this body
    # simply asserts the subclass-of-Exception relationship to give pytest
    # something to fail against if the imports degrade.
    for cls in (
        GradeError,
        GradeConfigError,
        GradeRubricError,
        GradeLLMError,
        GradeBudgetExceededError,
        GradePromptEnvelopeBreachError,
        GradeOutputError,
        GradeAuditWriteError,
        GradeAuditRecordTooLargeError,
    ):
        assert issubclass(cls, Exception)


@pytest.mark.parametrize(
    "exc",
    [
        GradeConfigError("invalid grade config"),
        GradeRubricError("duplicate criterion id"),
        GradeLLMError("LLM call failed", cause=RuntimeError("upstream")),
        GradeBudgetExceededError("total grade budget exceeded"),
        GradePromptEnvelopeBreachError(artifact_id="model.shop.customers#col.email"),
        GradeOutputError("score out of range", violation_type="score_out_of_range"),
        GradeAuditWriteError("fsync failed", cause=OSError("disk full")),
        GradeAuditRecordTooLargeError(size=5000, limit=4000),
    ],
)
def test_subclasses_have_default_remediation(exc: GradeError) -> None:
    """Each concrete subclass exposes a non-empty remediation string when
    no explicit ``remediation=`` kwarg is supplied.

    The remediation is either the class-level ``default_remediation``
    verbatim or a templated form derived from it (e.g.
    :class:`GradeAuditRecordTooLargeError` formats the limit number into
    the remediation at construction time — matches the safety / draft /
    prune precedent for the same shape of error).
    """
    # Class-level default is set to something concrete, not the base sentinel.
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
        GradeConfigError("invalid grade config"),
        GradeRubricError("duplicate criterion id"),
        GradeLLMError("LLM call failed", cause=RuntimeError("upstream")),
        GradeBudgetExceededError("total grade budget exceeded"),
        GradePromptEnvelopeBreachError(artifact_id="model.shop.customers#col.email"),
        GradeOutputError("score out of range", violation_type="score_out_of_range"),
        GradeAuditWriteError("fsync failed", cause=OSError("disk full")),
        GradeAuditRecordTooLargeError(size=5000, limit=4000),
    ],
)
def test_subclasses_repr_round_trips(exc: GradeError) -> None:
    """``repr(exc)`` returns a non-empty string that includes the class
    name. The default ``Exception.__repr__`` shape is ``ClassName(msg)``;
    we don't pin the exact form, only that it's identifiable in tracebacks.
    """
    rendered = repr(exc)
    assert rendered != ""
    assert type(exc).__name__ in rendered


def test_grade_prompt_envelope_breach_repr_quotes_artifact_id() -> None:
    """An artifact_id containing an ANSI escape MUST NOT render as a raw
    escape sequence (DEC-022 of #6 — log-injection defence).

    The value is rendered via ``repr()`` so ``\\x1b`` shows as the literal
    four characters ``\\x1b`` rather than the actual ESC byte. Mirrors the
    prune layer's ``test_prune_trusted_model_not_found_repr_quotes_unique_id``.
    """
    exc = GradePromptEnvelopeBreachError(artifact_id="model.shop.\x1b[31mevil")
    rendered = str(exc)
    # The raw ANSI escape (single byte 0x1b) MUST NOT appear in output.
    assert "\x1b" not in rendered
    # repr()-quoted form: the four literal chars ``\x1b`` MUST appear.
    assert "\\x1b" in rendered
    # The exception still exposes the original (un-quoted) artifact_id field
    # so callers can branch on it.
    assert exc.artifact_id == "model.shop.\x1b[31mevil"


def test_grade_llm_error_carries_cause() -> None:
    """``GradeLLMError`` carries the underlying LLM-layer error as
    ``__cause__`` so ``except ... as exc: exc.__cause__`` works for
    callers that need the vendor-level detail."""
    cause = RuntimeError("rate-limit retries exhausted")
    exc = GradeLLMError("LLM call failed", cause=cause)
    assert exc.__cause__ is cause
    assert exc.cause is cause


def test_grade_audit_write_error_carries_cause() -> None:
    """``GradeAuditWriteError`` carries the underlying I/O error as
    ``__cause__`` so ``except ... as exc: exc.__cause__`` works for
    callers that need to log the OS-level detail."""
    cause = OSError("disk full")
    exc = GradeAuditWriteError("fsync failed", cause=cause)
    assert exc.__cause__ is cause
    assert exc.cause is cause


def test_grade_audit_record_too_large_error_carries_size_and_limit() -> None:
    """``GradeAuditRecordTooLargeError`` exposes both numbers on the
    instance and renders both into the message so the operator can see
    the gap at a glance."""
    exc = GradeAuditRecordTooLargeError(size=5000, limit=4000)
    assert exc.size == 5000
    assert exc.limit == 4000
    rendered = str(exc)
    assert "5000" in rendered
    assert "4000" in rendered


def test_grade_output_error_carries_violation_type() -> None:
    """``GradeOutputError.violation_type`` is exposed on the instance for
    callers that branch on the parser-failure-mode discriminator. The
    literal taxonomy (``"criterion_id_mismatch"``, ``"score_out_of_range"``,
    ``"json_parse"``, ...) is locked in US-006 — US-001 accepts any
    ``str``."""
    exc = GradeOutputError("score 1.5 outside [0.0, 1.0]", violation_type="score_out_of_range")
    assert exc.violation_type == "score_out_of_range"
    rendered = str(exc)
    assert "score 1.5" in rendered


def test_subclass_inheritance_chain() -> None:
    """Every grade error subclasses :class:`GradeError`, which itself
    subclasses ``Exception`` directly — same per-layer pattern as
    ``SafetyError``, ``DraftError``, ``PruneError``, ``WarehouseError``,
    ``ManifestError``."""
    assert issubclass(GradeConfigError, GradeError)
    assert issubclass(GradeRubricError, GradeError)
    assert issubclass(GradeLLMError, GradeError)
    assert issubclass(GradeBudgetExceededError, GradeError)
    assert issubclass(GradePromptEnvelopeBreachError, GradeError)
    assert issubclass(GradeOutputError, GradeError)
    assert issubclass(GradeAuditWriteError, GradeError)
    assert issubclass(GradeAuditRecordTooLargeError, GradeError)
    assert issubclass(GradeError, Exception)
    # GradeError is a *direct* subclass of Exception — sibling layers
    # follow the same flat-one-level pattern.
    assert GradeError.__bases__ == (Exception,)


def test_signalforge_grade_subpackage_imports() -> None:
    """The ``signalforge.grade`` subpackage itself imports successfully
    (US-001 acceptance criterion — subpackage importable).

    The public re-exports landed in US-008; this test now asserts that
    the documented surface (matching the ``__all__`` declared in
    :file:`signalforge/grade/__init__.py`) is non-empty and contains the
    error hierarchy the original US-001 stub committed to.
    """
    import signalforge.grade

    # US-008 ships the full public surface — error hierarchy + typed
    # value objects + orchestrator + config loader. Sentinel a few
    # load-bearing names to fail loud if the surface accidentally
    # contracts; the full list is enforced by the per-name imports
    # above.
    assert "GradeError" in signalforge.grade.__all__
    assert "grade_artifacts" in signalforge.grade.__all__
    assert "GradingReport" in signalforge.grade.__all__
    assert "DEFAULT_RUBRIC" in signalforge.grade.__all__
    assert len(signalforge.grade.__all__) > 0
