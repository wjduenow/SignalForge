"""Unit tests for the safety errors module (DEC-026, DEC-022).

Mirrors :mod:`tests.warehouse.test_errors` and :mod:`tests.manifest.test_errors`.
Every test is capable of failing: no ``assert True``-shaped placeholders
(``testing-signal.md``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.safety import errors as errors_module
from signalforge.safety.errors import (
    AuditRecordTooLargeError,
    AuditWriteError,
    ColumnNotInModelError,
    ConfigNotFoundError,
    InvalidConfigError,
    InvalidPatternError,
    InvalidSamplingModeError,
    PolicyValidationError,
    SafetyError,
    UnknownConfigKeyError,
    _format_value,
)

# Subclasses (excluding the base) — kept in alphabetical order.
_SUBCLASSES: tuple[type[SafetyError], ...] = (
    AuditRecordTooLargeError,
    AuditWriteError,
    ColumnNotInModelError,
    ConfigNotFoundError,
    InvalidConfigError,
    InvalidPatternError,
    InvalidSamplingModeError,
    PolicyValidationError,
    UnknownConfigKeyError,
)


@pytest.mark.unit
@pytest.mark.safety
def test_safety_error_renders_message_and_remediation() -> None:
    """Base ``__str__`` includes both the message and the
    ``↳ Remediation:`` marker line."""
    rendered = str(SafetyError("boom", remediation="fix it"))
    assert "boom" in rendered
    assert "↳ Remediation: fix it" in rendered


@pytest.mark.unit
@pytest.mark.safety
def test_safety_error_default_remediation_used_when_remediation_kwarg_omitted() -> None:
    """When ``remediation=`` is omitted, the class-level default is used."""

    class _Sub(SafetyError):
        default_remediation = "subclass default hint"

    err = _Sub("something went wrong")
    rendered = str(err)
    assert "subclass default hint" in rendered
    assert err.remediation == "subclass default hint"


@pytest.mark.unit
@pytest.mark.safety
def test_safety_error_explicit_remediation_overrides_default() -> None:
    """An explicit ``remediation=`` kwarg overrides the class default."""

    class _Sub(SafetyError):
        default_remediation = "subclass default hint"

    err = _Sub("something went wrong", remediation="custom")
    rendered = str(err)
    assert "custom" in rendered
    assert "subclass default hint" not in rendered


@pytest.mark.unit
@pytest.mark.safety
@pytest.mark.parametrize("cls", _SUBCLASSES, ids=lambda c: c.__name__)
def test_each_subclass_has_default_remediation(cls: type[SafetyError]) -> None:
    """Every concrete subclass declares a non-empty ``default_remediation``."""
    remediation = cls.default_remediation
    assert isinstance(remediation, str)
    assert remediation.strip(), f"{cls.__name__}.default_remediation must be non-empty"


@pytest.mark.unit
@pytest.mark.safety
@pytest.mark.parametrize("cls", _SUBCLASSES, ids=lambda c: c.__name__)
def test_each_subclass_inherits_from_safety_error(cls: type[SafetyError]) -> None:
    """Every concrete subclass is a subclass of :class:`SafetyError`."""
    assert issubclass(cls, SafetyError)


@pytest.mark.unit
@pytest.mark.safety
def test_invalid_sampling_mode_error_renders_value_and_allowed() -> None:
    """``InvalidSamplingModeError`` renders both the bad value and the
    allowed tuple in its rendered output."""
    err = InvalidSamplingModeError(
        value="phantom",
        allowed=("schema-only", "aggregate-only", "sample"),
    )
    rendered = str(err)
    assert repr("phantom") in rendered
    # The allowed tuple's contents appear in the remediation.
    assert "schema-only" in rendered
    assert "aggregate-only" in rendered
    assert "sample" in rendered


@pytest.mark.unit
@pytest.mark.safety
@pytest.mark.error
def test_invalid_pattern_error_quotes_user_input_with_control_chars() -> None:
    """DEC-022: control characters in user-supplied patterns are rendered via
    ``repr()`` so they cannot smuggle ANSI escapes into log viewers."""
    adversarial = "\x1b[31m"
    err = InvalidPatternError(value=adversarial, reason="empty")
    rendered = str(err)
    # repr() of the value MUST appear verbatim somewhere in the message; the
    # raw escape sequence must NOT appear unescaped (we check via the repr
    # form which contains the literal backslash-x escape).
    assert repr(adversarial) in rendered
    # Sanity: attribute is preserved for programmatic access.
    assert err.value == adversarial
    assert err.reason == "empty"


@pytest.mark.unit
@pytest.mark.safety
def test_audit_write_error_carries_path_and_cause() -> None:
    """``AuditWriteError`` exposes both ``path`` and ``cause`` attributes;
    ``cause`` defaults to ``None`` when omitted."""
    p = Path("/tmp/.signalforge/audit.jsonl")
    err = AuditWriteError(path=p)
    assert err.path == p
    assert err.cause is None

    cause = OSError("disk full")
    err2 = AuditWriteError(path=p, cause=cause)
    assert err2.path == p
    assert err2.cause is cause


@pytest.mark.unit
@pytest.mark.safety
def test_audit_write_error_str_includes_cause_repr_when_present() -> None:
    """When a ``cause`` is supplied, its ``repr()`` appears in the rendered
    message so log viewers can see what failed underneath."""
    p = Path("/tmp/.signalforge/audit.jsonl")
    cause = OSError("disk full")
    err = AuditWriteError(path=p, cause=cause)
    rendered = str(err)
    assert repr(cause) in rendered
    assert repr(str(p)) in rendered


@pytest.mark.unit
@pytest.mark.safety
def test_audit_record_too_large_error_includes_size_and_limit() -> None:
    """The rendered message contains both the actual size and the limit, and
    the remediation substitutes the limit value (not the literal ``{limit}``)."""
    err = AuditRecordTooLargeError(size=8192, limit=4096)
    rendered = str(err)
    assert "8192" in rendered
    assert "4096" in rendered
    # The format-string placeholder must have been substituted away.
    assert "{limit}" not in rendered
    assert err.size == 8192
    assert err.limit == 4096


@pytest.mark.unit
@pytest.mark.safety
def test_unknown_config_key_error_includes_key_and_scope() -> None:
    """The rendered message contains both the unknown key and its scope."""
    err = UnknownConfigKeyError(key="redacts", scope="safety")
    rendered = str(err)
    assert repr("redacts") in rendered
    assert repr("safety") in rendered
    assert err.key == "redacts"
    assert err.scope == "safety"


@pytest.mark.unit
@pytest.mark.safety
def test_invalid_sampling_mode_extends_invalid_config_error() -> None:
    """``InvalidSamplingModeError`` is catchable via ``except InvalidConfigError``."""
    caught: InvalidConfigError | None = None
    try:
        raise InvalidSamplingModeError(
            value="phantom",
            allowed=("schema-only", "aggregate-only", "sample"),
        )
    except InvalidConfigError as exc:
        caught = exc
    assert isinstance(caught, InvalidSamplingModeError)


@pytest.mark.unit
@pytest.mark.safety
def test_invalid_pattern_extends_invalid_config_error() -> None:
    """``InvalidPatternError`` is catchable via ``except InvalidConfigError``."""
    caught: InvalidConfigError | None = None
    try:
        raise InvalidPatternError(value="*", reason="bare wildcard")
    except InvalidConfigError as exc:
        caught = exc
    assert isinstance(caught, InvalidPatternError)


@pytest.mark.unit
@pytest.mark.safety
def test_format_value_helper_uses_repr() -> None:
    """``_format_value(v)`` delegates to ``repr(v)`` — DEC-022."""
    s = "foo'bar"
    assert _format_value(s) == repr(s)
    # And for non-strings too: the helper must be a thin ``repr()`` wrapper.
    assert _format_value(42) == repr(42)
    assert _format_value(None) == repr(None)


@pytest.mark.unit
@pytest.mark.safety
def test_module_all_lists_all_classes() -> None:
    """``__all__`` lists all 10 class names (1 base + 9 concrete subclasses)."""
    expected = {
        "SafetyError",
        "ConfigNotFoundError",
        "InvalidConfigError",
        "InvalidSamplingModeError",
        "InvalidPatternError",
        "ColumnNotInModelError",
        "AuditWriteError",
        "AuditRecordTooLargeError",
        "PolicyValidationError",
        "UnknownConfigKeyError",
    }
    assert set(errors_module.__all__) == expected
    assert len(errors_module.__all__) == 10
