"""Tests for ``signalforge.safety.policy`` (US-005).

Covers :class:`SafetyPolicy`, :func:`_resolve_redact_patterns`, and
:func:`_compute_policy_hash`. Traces to DEC-007 (built-ins +
override semantics), DEC-014 (``policy_hash``), DEC-017
(``@model_validator``), DEC-018 (``with_mode``), DEC-021 (sample-mode
warning), DEC-023 (pattern-injection rejection), DEC-024
(case-insensitive mode load).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from signalforge.safety.errors import (
    InvalidConfigError,
    InvalidPatternError,
    InvalidSamplingModeError,
    UnknownConfigKeyError,
)
from signalforge.safety.models import SamplingMode
from signalforge.safety.policy import (
    DEFAULT_AUDIT_PATH,
    DEFAULT_REDACT_PATTERNS,
    SafetyPolicy,
    _compute_policy_hash,
    _resolve_redact_patterns,
)

pytestmark = pytest.mark.safety


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_safety_policy_no_args_is_schema_only() -> None:
    """DEC-012(a) regression: default mode must be schema-only."""
    assert SafetyPolicy().mode is SamplingMode.SCHEMA_ONLY


def test_safety_policy_default_redact_patterns_are_six_builtins() -> None:
    policy = SafetyPolicy()
    assert policy.redact_patterns == DEFAULT_REDACT_PATTERNS
    assert len(policy.redact_patterns) == 6


def test_safety_policy_default_sample_size_is_100() -> None:
    assert SafetyPolicy().sample_size == 100


def test_safety_policy_default_audit_path() -> None:
    assert SafetyPolicy().audit_path == Path(".signalforge/audit.jsonl")
    assert DEFAULT_AUDIT_PATH == Path(".signalforge/audit.jsonl")  # noqa: SIM300


# ---------------------------------------------------------------------------
# extra="forbid"
# ---------------------------------------------------------------------------


def test_safety_policy_extra_forbid_rejects_typo() -> None:
    """DEC-015: typos must fail loud at the config-shaped surface."""
    with pytest.raises(ValidationError):
        SafetyPolicy.model_validate({"redacts": {"extend": ["*foo"]}})


# ---------------------------------------------------------------------------
# redact: extend / replace
# ---------------------------------------------------------------------------


def test_safety_policy_redact_extend_appends_to_builtins() -> None:
    policy = SafetyPolicy.model_validate({"redact": {"extend": ["*custom"]}})
    assert policy.redact_patterns[-1] == "*custom"
    for builtin in DEFAULT_REDACT_PATTERNS:
        assert builtin in policy.redact_patterns
    assert len(policy.redact_patterns) == len(DEFAULT_REDACT_PATTERNS) + 1


def test_safety_policy_redact_replace_substitutes() -> None:
    policy = SafetyPolicy.model_validate({"redact": {"replace": ["*specific"]}})
    assert policy.redact_patterns == ("*specific",)


def test_safety_policy_redact_extend_and_replace_simultaneously_errors() -> None:
    with pytest.raises(InvalidConfigError):
        SafetyPolicy.model_validate({"redact": {"extend": ["*x"], "replace": ["*y"]}})


def test_safety_policy_redact_replace_empty_warns_once_and_returns_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        policy = SafetyPolicy.model_validate({"redact": {"replace": []}})
    assert policy.redact_patterns == ()
    warning_records = [
        r for r in caplog.records if r.name == "signalforge.safety" and r.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    assert "redact.replace=[]" in warning_records[0].getMessage()


def test_safety_policy_redact_unknown_key_raises_unknown_config_key_error() -> None:
    with pytest.raises(UnknownConfigKeyError):
        SafetyPolicy.model_validate({"redact": {"phantom": ["x"]}})


# ---------------------------------------------------------------------------
# Mode case-insensitive load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "schema-only",
        "Schema-Only",
        "SCHEMA-ONLY",
        "schema_only",
        "SCHEMA_ONLY",
        "Schema_Only",
    ],
)
def test_safety_policy_mode_case_insensitive_load(raw: str) -> None:
    policy = SafetyPolicy.model_validate({"mode": raw})
    assert policy.mode is SamplingMode.SCHEMA_ONLY


def test_safety_policy_mode_unknown_raises_invalid_sampling_mode_error() -> None:
    with pytest.raises(InvalidSamplingModeError):
        SafetyPolicy.model_validate({"mode": "phantom"})


@pytest.mark.parametrize("bad_value", [42, 3.14, None, [], {}, ("schema-only",), object()])
def test_safety_policy_mode_non_string_non_enum_raises_invalid_sampling_mode_error(
    bad_value: Any,
) -> None:
    """Regression: the ``@field_validator(mode="before")`` previously fell
    through to Pydantic's generic ``ValidationError`` when given non-string,
    non-enum input (e.g. ``mode=42``). Now every invalid type raises the
    typed ``InvalidSamplingModeError`` so the safety-layer error hierarchy
    stays homogeneous. Caught by Quality-Gate review."""
    with pytest.raises(InvalidSamplingModeError):
        SafetyPolicy.model_validate({"mode": bad_value})


# ---------------------------------------------------------------------------
# Pattern-injection rejection (DEC-023)
# ---------------------------------------------------------------------------


def test_safety_policy_pattern_empty_raises_invalid_pattern_error() -> None:
    with pytest.raises(InvalidPatternError):
        SafetyPolicy(redact_patterns=("",))


def test_safety_policy_pattern_star_alone_raises_invalid_pattern_error() -> None:
    with pytest.raises(InvalidPatternError):
        SafetyPolicy(redact_patterns=("*",))


def test_safety_policy_pattern_question_mark_alone_raises_invalid_pattern_error() -> None:
    with pytest.raises(InvalidPatternError):
        SafetyPolicy(redact_patterns=("?",))


def test_safety_policy_pattern_mixed_invalid_first_raises() -> None:
    """Empty pattern must be caught even when not at index 0."""
    with pytest.raises(InvalidPatternError):
        SafetyPolicy(redact_patterns=("foo", ""))


# ---------------------------------------------------------------------------
# with_mode (DEC-018)
# ---------------------------------------------------------------------------


def test_safety_policy_with_mode_returns_new_frozen_policy() -> None:
    policy = SafetyPolicy()
    overridden = policy.with_mode(SamplingMode.SAMPLE)
    assert overridden is not policy
    assert overridden.mode is SamplingMode.SAMPLE
    assert policy.mode is SamplingMode.SCHEMA_ONLY


def test_safety_policy_with_mode_preserves_other_fields() -> None:
    policy = SafetyPolicy(
        redact_patterns=("*custom",),
        sample_size=42,
        audit_path=Path("custom/audit.jsonl"),
    )
    overridden = policy.with_mode(SamplingMode.AGGREGATE_ONLY)
    assert overridden.redact_patterns == ("*custom",)
    assert overridden.sample_size == 42
    assert overridden.audit_path == Path("custom/audit.jsonl")
    assert overridden.mode is SamplingMode.AGGREGATE_ONLY


# ---------------------------------------------------------------------------
# Sample-mode warning (DEC-021)
# ---------------------------------------------------------------------------


def test_safety_policy_sample_mode_emits_warning_on_construction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        SafetyPolicy(mode=SamplingMode.SAMPLE)
    warning_records = [
        r for r in caplog.records if r.name == "signalforge.safety" and r.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    assert "Sample mode enabled" in warning_records[0].getMessage()


def test_safety_policy_schema_only_mode_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        SafetyPolicy(mode=SamplingMode.SCHEMA_ONLY)
    warning_records = [
        r for r in caplog.records if r.name == "signalforge.safety" and r.levelno == logging.WARNING
    ]
    assert warning_records == []


def test_safety_policy_with_mode_sample_emits_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: ``with_mode(SAMPLE)`` MUST re-trigger the validator chain.

    Pydantic v2's ``model_copy(update=...)`` skips ``@model_validator(mode="after")``
    by default — that path was the original implementation and silently
    enabled sample mode without WARNING when the CLI's ``--mode sample`` flag
    flowed through. Caught by Quality-Gate review; ``with_mode`` now goes
    through ``model_validate`` so the WARNING fires every time sample mode is
    enabled, regardless of whether construction was direct or via override.
    """
    policy = SafetyPolicy()  # SCHEMA_ONLY, no warning
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        overridden = policy.with_mode(SamplingMode.SAMPLE)
    assert overridden.mode is SamplingMode.SAMPLE
    warning_records = [
        r for r in caplog.records if r.name == "signalforge.safety" and r.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    assert "Sample mode enabled" in warning_records[0].getMessage()


def test_safety_policy_with_mode_schema_only_emits_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Symmetric check: switching FROM sample mode (or staying off it) must
    not spuriously fire the warning."""
    policy = SafetyPolicy(mode=SamplingMode.SAMPLE)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        overridden = policy.with_mode(SamplingMode.SCHEMA_ONLY)
    assert overridden.mode is SamplingMode.SCHEMA_ONLY
    warning_records = [
        r for r in caplog.records if r.name == "signalforge.safety" and r.levelno == logging.WARNING
    ]
    assert warning_records == []


# ---------------------------------------------------------------------------
# Frozen
# ---------------------------------------------------------------------------


def test_safety_policy_is_frozen() -> None:
    policy = SafetyPolicy()
    with pytest.raises(ValidationError):
        policy.mode = SamplingMode.SAMPLE  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _compute_policy_hash
# ---------------------------------------------------------------------------


def test_compute_policy_hash_deterministic_for_equal_policies() -> None:
    policy_a = SafetyPolicy()
    policy_b = SafetyPolicy()
    assert _compute_policy_hash(policy_a) == _compute_policy_hash(policy_b)
    assert _compute_policy_hash(policy_a) == _compute_policy_hash(policy_a)


def test_compute_policy_hash_differs_for_semantically_different_policies() -> None:
    policy_a = SafetyPolicy()
    policy_b = SafetyPolicy(mode=SamplingMode.AGGREGATE_ONLY)
    assert _compute_policy_hash(policy_a) != _compute_policy_hash(policy_b)


def test_compute_policy_hash_differs_when_redact_patterns_differ() -> None:
    policy_a = SafetyPolicy()
    policy_b = SafetyPolicy(redact_patterns=("*foo",))
    assert _compute_policy_hash(policy_a) != _compute_policy_hash(policy_b)


def test_compute_policy_hash_returns_16_hex_chars() -> None:
    digest = _compute_policy_hash(SafetyPolicy())
    assert len(digest) == 16
    assert re.fullmatch(r"[0-9a-f]{16}", digest) is not None


# ---------------------------------------------------------------------------
# _resolve_redact_patterns
# ---------------------------------------------------------------------------


def test_resolve_redact_patterns_none_returns_defaults() -> None:
    assert _resolve_redact_patterns(None) == DEFAULT_REDACT_PATTERNS


def test_resolve_redact_patterns_empty_dict_returns_defaults() -> None:
    assert _resolve_redact_patterns({}) == DEFAULT_REDACT_PATTERNS


def test_resolve_redact_patterns_extend_with_non_list_raises() -> None:
    with pytest.raises(InvalidConfigError):
        _resolve_redact_patterns({"extend": "*foo"})


def test_resolve_redact_patterns_replace_with_non_list_raises() -> None:
    with pytest.raises(InvalidConfigError):
        _resolve_redact_patterns({"replace": "*foo"})


# ---------------------------------------------------------------------------
# __all__
# ---------------------------------------------------------------------------


def test_module_all_lists_documented_names() -> None:
    from signalforge.safety import policy as policy_module

    assert set(policy_module.__all__) == {
        "DEFAULT_REDACT_PATTERNS",
        "DEFAULT_AUDIT_PATH",
        "SafetyPolicy",
    }
