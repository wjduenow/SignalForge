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
    GradeCachePathError,
    GradeCacheReadError,
    GradeCacheRecordTooLargeError,
    GradeCacheWriteError,
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


# ---------------------------------------------------------------------------
# US-004: persistent-grade-cache typed errors (#189)
# ---------------------------------------------------------------------------
#
# Per DEC-017 of plans/super/189-no-grade-cache.md, four typed errors land
# in lockstep with the cache module (US-003). The exit-code mapping
# (`tests/cli/test_exit_codes.py`) is the runtime contract; the
# ``default_remediation`` strings are pinned here so a drive-by edit cannot
# silently weaken the operator-facing message.


# Locked default_remediation text for the four cache errors (DEC-017).
# Sentences are operator-actionable; word-choice / phrasing is part of
# the contract.
_EXPECTED_CACHE_REMEDIATIONS: dict[str, str] = {
    "GradeCacheReadError": (
        "The grade cache file is present but unreadable or unparseable. Delete "
        "the cache file or run `signalforge cache clear --grade` to drop the "
        "entire cache directory; the next grade run will re-grade and "
        "re-populate. Cache reads are best-effort — a read failure NEVER "
        "aborts the live grade."
    ),
    "GradeCacheWriteError": (
        "The grade cache write failed (disk full, permission denied, oversize "
        "record, or a concurrent-write conflict). The live grade run is NOT "
        "aborted — cache writes are fail-soft per DEC-005; the next run will "
        "re-grade this pair and attempt the cache write again. To suppress the "
        "warning, fix the underlying I/O issue or set `grade.cache_enabled: "
        "false` in signalforge.yml."
    ),
    "GradeCachePathError": (
        "The grade cache directory (`<project_dir>/.signalforge/grade-cache/`) "
        "resolved outside the project directory via a symlink. This is the "
        "symlink-containment gate refusing to read or write outside the "
        "project tree. Inspect the `.signalforge/grade-cache` path; remove "
        "any symlinks that point elsewhere, then re-run."
    ),
    "GradeCacheRecordTooLargeError": (
        "The grade cache record exceeded the 16 KB per-record budget. This is "
        "the cache-record cap, distinct from the 4 KB POSIX-atomic-append "
        "limit on audit JSONL records (cache files are not concurrent-append "
        "targets). Common cause: an unusually large `evidence` or `reasoning` "
        "field in the LLM response — the live grade still succeeds; only the "
        "cache write is skipped."
    ),
}


def test_grade_cache_read_error_is_grade_error_subclass() -> None:
    """``GradeCacheReadError`` subclasses :class:`GradeError` so every
    ``except GradeError`` arm in the engine catches it."""
    assert issubclass(GradeCacheReadError, GradeError)


def test_grade_cache_write_error_is_grade_error_subclass() -> None:
    """``GradeCacheWriteError`` subclasses :class:`GradeError`."""
    assert issubclass(GradeCacheWriteError, GradeError)


def test_grade_cache_path_error_is_grade_error_subclass() -> None:
    """``GradeCachePathError`` subclasses :class:`GradeError`."""
    assert issubclass(GradeCachePathError, GradeError)


def test_grade_cache_record_too_large_subclasses_write() -> None:
    """``GradeCacheRecordTooLargeError`` is a *subclass* of
    :class:`GradeCacheWriteError` (DEC-017): the orchestrator's fail-soft
    catch on Write also catches oversize, and the MRO walk in
    :func:`map_exception_to_exit_code` resolves the TooLarge subclass to
    Write's tier without an explicit entry in
    :data:`_EXCEPTION_TO_EXIT_CODE`."""
    assert issubclass(GradeCacheRecordTooLargeError, GradeCacheWriteError)
    assert issubclass(GradeCacheRecordTooLargeError, GradeError)


def test_grade_cache_read_error_carries_cause() -> None:
    """``GradeCacheReadError`` carries the underlying I/O / parse error
    as ``__cause__`` so callers can branch on the OS-level detail."""
    cause = OSError("permission denied")
    exc = GradeCacheReadError("could not read cache file", cause=cause)
    assert exc.__cause__ is cause
    assert exc.cause is cause


def test_grade_cache_write_error_carries_cause() -> None:
    """``GradeCacheWriteError`` carries the underlying I/O error as
    ``__cause__`` so the engine's fail-soft WARNING line can name the
    OS-level detail."""
    cause = OSError("disk full")
    exc = GradeCacheWriteError("fsync failed on cache write", cause=cause)
    assert exc.__cause__ is cause
    assert exc.cause is cause


def test_grade_cache_record_too_large_error_carries_size_and_limit() -> None:
    """``GradeCacheRecordTooLargeError`` exposes both numbers on the
    instance and renders both into the message (mirrors the
    audit-record-too-large precedent)."""
    exc = GradeCacheRecordTooLargeError(size=20_000, limit=16_000)
    assert exc.size == 20_000
    assert exc.limit == 16_000
    rendered = str(exc)
    assert "20000" in rendered
    assert "16000" in rendered


@pytest.mark.parametrize(
    ("exc_cls", "construct"),
    [
        (
            GradeCacheReadError,
            lambda: GradeCacheReadError(
                "cache file unreadable",
                cause=OSError("permission denied"),
            ),
        ),
        (
            GradeCacheWriteError,
            lambda: GradeCacheWriteError(
                "cache write failed",
                cause=OSError("disk full"),
            ),
        ),
        (
            GradeCachePathError,
            lambda: GradeCachePathError("cache path escapes project_dir"),
        ),
        (
            GradeCacheRecordTooLargeError,
            lambda: GradeCacheRecordTooLargeError(size=20_000, limit=16_000),
        ),
    ],
)
def test_default_remediation_text_locked_for_each_cache_error(
    exc_cls: type[GradeError],
    construct: object,
) -> None:
    """Pin the exact :attr:`default_remediation` text for each cache
    error so a future drive-by edit cannot silently weaken the
    operator-facing message (DEC-017).

    Each remediation is ≥2 sentences and names the specific operator
    action — e.g. the Read remediation names ``signalforge cache clear
    --grade``; the Path remediation names the canonicalisation gate; the
    Write remediation names the fail-soft posture (the run is NOT
    aborted, the next run will re-grade); the TooLarge remediation
    explains the 16KB cap is the cache-record budget (distinct from the
    audit-record 4KB POSIX-atomic-append guarantee).
    """
    remediation = exc_cls.default_remediation
    # Locked exact text — change in lockstep with the rule file / docs.
    assert remediation == _EXPECTED_CACHE_REMEDIATIONS[exc_cls.__name__], (
        f"{exc_cls.__name__}.default_remediation drifted from the locked "
        "text. Update _EXPECTED_CACHE_REMEDIATIONS AND the docs/grade-ops.md "
        "remediation table in lockstep."
    )
    # Floor invariants the assertion above also implies (kept as
    # explicit asserts so a future broad edit can't pass by replacing
    # the locked text with an even-weaker string of equivalent length).
    assert remediation.strip() != ""
    assert "(no remediation set" not in remediation
    # At least two sentences (the contract: ≥2 sentences naming the
    # specific operator action).
    assert remediation.count(".") >= 2, (
        f"{exc_cls.__name__}.default_remediation must be ≥2 sentences "
        "naming the specific operator action."
    )

    # Instance-level remediation matches class-level when no override
    # is supplied — same pattern as the sibling GradeError subclasses.
    instance = construct()  # type: ignore[operator]
    assert isinstance(instance, exc_cls)
    # TooLarge constructs its remediation from a template, so we only
    # assert the class-level default is the locked text; the instance
    # path is exercised by test_grade_cache_record_too_large_error_*.
    if exc_cls is not GradeCacheRecordTooLargeError:
        assert instance.remediation == remediation
