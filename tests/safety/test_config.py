"""Tests for ``signalforge.safety.config`` (US-006).

Covers :func:`load_safety_config` — full DEC-016 resolution / error contract
plus DEC-013 ``audit_path`` traversal hardening. The 24 tests below exercise
every documented branch of the loader, including the symlink-hardened
``_path_safety.canonicalise_path`` copy.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from signalforge.safety.config import load_safety_config
from signalforge.safety.errors import (
    ConfigNotFoundError,
    InvalidConfigError,
    InvalidSamplingModeError,
    SafetyError,
)
from signalforge.safety.models import SamplingMode
from signalforge.safety.policy import DEFAULT_REDACT_PATTERNS

pytestmark = pytest.mark.safety


_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "safety"


def _copy_fixture(fixture_name: str, dest_dir: Path) -> Path:
    """Copy a fixture YAML into ``dest_dir/signalforge.yml``."""
    src = _FIXTURES_DIR / fixture_name
    dest = dest_dir / "signalforge.yml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# Defaults / no file / empty file
# ---------------------------------------------------------------------------


def test_load_safety_config_no_file_returns_defaults(tmp_path: Path) -> None:
    """DEC-012(b): missing default config falls back to schema-only."""
    assert load_safety_config(tmp_path).mode is SamplingMode.SCHEMA_ONLY


def test_load_safety_config_no_file_default_redact_patterns(tmp_path: Path) -> None:
    assert load_safety_config(tmp_path).redact_patterns == DEFAULT_REDACT_PATTERNS


def test_load_safety_config_empty_file_returns_defaults(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_bytes(b"")
    policy = load_safety_config(tmp_path)
    assert policy.mode is SamplingMode.SCHEMA_ONLY
    assert policy.redact_patterns == DEFAULT_REDACT_PATTERNS


def test_load_safety_config_whitespace_only_file_returns_defaults(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_text("   \n  \n", encoding="utf-8")
    policy = load_safety_config(tmp_path)
    assert policy.mode is SamplingMode.SCHEMA_ONLY


def test_load_safety_config_yaml_with_only_comments_returns_defaults(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_text("# just a comment\n", encoding="utf-8")
    policy = load_safety_config(tmp_path)
    assert policy.mode is SamplingMode.SCHEMA_ONLY


def test_load_safety_config_missing_safety_key_returns_defaults(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_text("llm:\n  model: claude-opus-4-7\n", encoding="utf-8")
    policy = load_safety_config(tmp_path)
    assert policy.mode is SamplingMode.SCHEMA_ONLY
    assert policy.redact_patterns == DEFAULT_REDACT_PATTERNS


def test_load_safety_config_unknown_top_level_key_ignored(tmp_path: Path) -> None:
    """DEC-025 namespace: unknown top-level keys (other than ``safety:``)
    are silently ignored — they belong to other reserved top-level scopes.
    """
    _copy_fixture("signalforge_unknown_top_level.yml", tmp_path)
    policy = load_safety_config(tmp_path)
    assert policy.mode is SamplingMode.SCHEMA_ONLY


# ---------------------------------------------------------------------------
# Path resolution / explicit path
# ---------------------------------------------------------------------------


def test_load_safety_config_explicit_path_missing_raises(tmp_path: Path) -> None:
    missing = tmp_path / "missing.yml"
    with pytest.raises(ConfigNotFoundError):
        load_safety_config(tmp_path, path=missing)


def test_load_safety_config_implicit_path_missing_returns_defaults(tmp_path: Path) -> None:
    """Companion to test #1 — explicit name for the implicit-default branch."""
    assert load_safety_config(tmp_path, path=None).mode is SamplingMode.SCHEMA_ONLY


# ---------------------------------------------------------------------------
# Malformed / wrong-shape input
# ---------------------------------------------------------------------------


def test_load_safety_config_malformed_yaml_raises_invalid_config(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_text(": : :\n", encoding="utf-8")
    with pytest.raises(InvalidConfigError):
        load_safety_config(tmp_path)


def test_load_safety_config_non_mapping_top_level_raises_invalid_config(
    tmp_path: Path,
) -> None:
    (tmp_path / "signalforge.yml").write_text(
        "- a list at top level\n- like this\n", encoding="utf-8"
    )
    with pytest.raises(InvalidConfigError):
        load_safety_config(tmp_path)


# ---------------------------------------------------------------------------
# Fixture-driven happy paths
# ---------------------------------------------------------------------------


def test_load_safety_config_minimal_fixture(tmp_path: Path) -> None:
    _copy_fixture("signalforge_minimal.yml", tmp_path)
    policy = load_safety_config(tmp_path)
    assert policy.mode is SamplingMode.SCHEMA_ONLY
    assert policy.redact_patterns == DEFAULT_REDACT_PATTERNS


def test_load_safety_config_extend_fixture(tmp_path: Path) -> None:
    _copy_fixture("signalforge_extend.yml", tmp_path)
    policy = load_safety_config(tmp_path)
    assert policy.redact_patterns[-1] == "*custom_*"
    for builtin in DEFAULT_REDACT_PATTERNS:
        assert builtin in policy.redact_patterns


def test_load_safety_config_replace_empty_fixture_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _copy_fixture("signalforge_replace_empty.yml", tmp_path)
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        policy = load_safety_config(tmp_path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert policy.redact_patterns == ()


# ---------------------------------------------------------------------------
# Fixture-driven error paths
# ---------------------------------------------------------------------------


def test_load_safety_config_extend_replace_conflict_fixture_raises(tmp_path: Path) -> None:
    _copy_fixture("signalforge_extend_replace_conflict.yml", tmp_path)
    with pytest.raises(InvalidConfigError):
        load_safety_config(tmp_path)


def test_load_safety_config_unknown_mode_fixture_raises(tmp_path: Path) -> None:
    _copy_fixture("signalforge_unknown_mode.yml", tmp_path)
    with pytest.raises(InvalidSamplingModeError):
        load_safety_config(tmp_path)


def test_load_safety_config_typo_fixture_raises(tmp_path: Path) -> None:
    """The ``redacts:`` typo (rather than ``redact:``) under ``safety:`` —
    the loader must surface a typed safety-layer error. The exact subclass
    depends on whether the validator can map it to
    :class:`UnknownConfigKeyError` or whether it falls through to the
    Pydantic generic path; either way it must inherit from
    :class:`SafetyError`.
    """
    _copy_fixture("signalforge_typo.yml", tmp_path)
    with pytest.raises(SafetyError):
        load_safety_config(tmp_path)


# ---------------------------------------------------------------------------
# audit_path traversal hardening (DEC-013)
# ---------------------------------------------------------------------------


def test_load_safety_config_audit_path_with_dotdot_raises(tmp_path: Path) -> None:
    _copy_fixture("signalforge_audit_path_traversal.yml", tmp_path)
    with pytest.raises(InvalidConfigError):
        load_safety_config(tmp_path)


def test_load_safety_config_audit_path_outside_project_raises(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_text(
        "safety:\n  audit_path: /tmp/escape.jsonl\n", encoding="utf-8"
    )
    with pytest.raises(SafetyError):
        load_safety_config(tmp_path)


def test_load_safety_config_audit_path_relative_resolves_inside_project(
    tmp_path: Path,
) -> None:
    (tmp_path / "signalforge.yml").write_text(
        "safety:\n  audit_path: .signalforge/audit.jsonl\n", encoding="utf-8"
    )
    policy = load_safety_config(tmp_path)
    expected = (tmp_path / ".signalforge" / "audit.jsonl").resolve()
    assert policy.audit_path == expected
    assert policy.audit_path.is_absolute()
    assert policy.audit_path.is_relative_to(tmp_path.resolve())


def test_load_safety_config_audit_path_symlink_to_outside_raises(tmp_path: Path) -> None:
    """A symlink at ``<project_dir>/.signalforge/escape.jsonl`` pointing at
    ``/tmp/escape.jsonl`` must be rejected — the resolved path falls outside
    the project tree, so the containment check fires.
    """
    sigdir = tmp_path / ".signalforge"
    sigdir.mkdir()
    target = Path("/tmp") / f"sf_escape_{tmp_path.name}.jsonl"
    link = sigdir / "escape.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation not supported on this platform")
    (tmp_path / "signalforge.yml").write_text(
        "safety:\n  audit_path: .signalforge/escape.jsonl\n", encoding="utf-8"
    )
    with pytest.raises(SafetyError):
        load_safety_config(tmp_path)


# ---------------------------------------------------------------------------
# YAML-driven redact resolution + safety
# ---------------------------------------------------------------------------


def test_load_safety_config_extend_resolution_via_yaml(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_text(
        'safety:\n  redact:\n    extend: ["*foo"]\n', encoding="utf-8"
    )
    policy = load_safety_config(tmp_path)
    assert policy.redact_patterns[-1] == "*foo"
    for builtin in DEFAULT_REDACT_PATTERNS:
        assert builtin in policy.redact_patterns


def test_load_safety_config_replace_empty_resolution_via_yaml(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "signalforge.yml").write_text(
        "safety:\n  redact:\n    replace: []\n", encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="signalforge.safety"):
        policy = load_safety_config(tmp_path)
    assert policy.redact_patterns == ()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_load_safety_config_uses_yaml_safe_load_not_load(tmp_path: Path) -> None:
    """Defensive regression: ``yaml.safe_load`` rejects arbitrary Python
    object construction tags. If the loader ever regressed to ``yaml.load``
    this fixture would silently invoke ``os.system("ls")`` instead of
    raising. We expect :class:`InvalidConfigError`.
    """
    (tmp_path / "signalforge.yml").write_text(
        'safety: !!python/object/apply:os.system ["ls"]\n', encoding="utf-8"
    )
    with pytest.raises(InvalidConfigError):
        load_safety_config(tmp_path)
