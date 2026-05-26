"""Tests for ``signalforge.diff.config`` (US-003).

Exercises every locked invariant of :func:`load_diff_config` and
:class:`DiffConfig`:

* Resolution order: explicit ``path`` > ``<project_dir>/signalforge.yml``
  ``diff:`` block > defaults.
* Defaults match DEC-010 verbatim (regression guard against an
  accidental field-default tweak).
* ``extra="forbid"`` on the inner :class:`DiffConfig` rejects typos
  loud (mirrors ``safety-layer.md`` DEC-015 / ``llm-drafter.md``
  DEC-027 / ``prune-engine.md`` DEC-020 / ``grade-layer.md`` DEC-029).
* ``extra="ignore"`` on the outer :class:`_DiffConfigFile` silently
  tolerates sibling stage namespaces (``safety:``, ``llm:``, ``prune:``,
  ``grade:``).
* Numeric range validators (positive) fire when the YAML supplies an
  out-of-range knob.

Each test is capable of failing if its target is broken (per
``.claude/rules/testing-signal.md``); no ``assert True``-shaped no-ops.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from signalforge.diff.config import DiffConfig, load_diff_config
from signalforge.diff.errors import DiffError

# ----- Resolution order -----


def test_load_diff_config_missing_file_returns_defaults_when_path_is_none(
    tmp_path: Path,
) -> None:
    """No ``signalforge.yml`` in ``project_dir`` → defaults silently
    (the typical fresh-project case)."""
    cfg = load_diff_config(tmp_path)
    assert cfg == DiffConfig()


def test_load_diff_config_explicit_missing_path_raises_diff_error(
    tmp_path: Path,
) -> None:
    """An explicit ``path`` that does not exist must fail loud — silent
    no-op would mask a typo in the operator's CLI flag."""
    missing = tmp_path / "does-not-exist.yml"
    with pytest.raises(DiffError) as exc_info:
        load_diff_config(tmp_path, missing)
    rendered = str(exc_info.value)
    assert "Remediation" in rendered


def test_load_diff_config_explicit_path_overrides_project_dir(
    tmp_path: Path,
) -> None:
    """An explicit ``path`` is preferred over the
    ``<project_dir>/signalforge.yml`` candidate. The default file is
    intentionally populated with a different value to prove the
    explicit path won."""
    default_path = tmp_path / "signalforge.yml"
    default_path.write_text("diff:\n  context_lines: 7\n", encoding="utf-8")
    explicit_path = tmp_path / "alt.yml"
    explicit_path.write_text("diff:\n  context_lines: 9\n", encoding="utf-8")
    cfg = load_diff_config(tmp_path, explicit_path)
    assert cfg.context_lines == 9


def test_load_diff_config_empty_file_returns_defaults(tmp_path: Path) -> None:
    """Empty file → defaults (parses to ``None`` via ``yaml.safe_load``)."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("", encoding="utf-8")
    cfg = load_diff_config(tmp_path)
    assert cfg == DiffConfig()


def test_load_diff_config_comments_only_returns_defaults(tmp_path: Path) -> None:
    """A YAML file containing only comments parses to ``None`` after
    strip; treated identically to an empty file."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("# just a comment\n# another\n", encoding="utf-8")
    cfg = load_diff_config(tmp_path)
    assert cfg == DiffConfig()


def test_load_diff_config_top_level_not_mapping_raises(tmp_path: Path) -> None:
    """A YAML sequence at top level is a structural error — we expect a
    mapping with stage keys."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("- 1\n- 2\n- 3\n", encoding="utf-8")
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


def test_load_diff_config_invalid_yaml_raises(tmp_path: Path) -> None:
    """Syntactically broken YAML → :class:`DiffError` (the underlying
    ``yaml.YAMLError`` is preserved on ``__cause__``)."""
    config_path = tmp_path / "signalforge.yml"
    # Unbalanced quotes after the colon — yaml.safe_load raises.
    config_path.write_text(': "bad\n', encoding="utf-8")
    with pytest.raises(DiffError) as exc_info:
        load_diff_config(tmp_path)
    assert exc_info.value.__cause__ is not None


# ----- Sibling namespaces silently ignored -----


def test_load_diff_config_unknown_top_level_key_silently_ignored(
    tmp_path: Path,
) -> None:
    """Sibling stage blocks (``safety:``, ``llm:``, ``prune:``,
    ``grade:``) without a ``diff:`` key → defaults; the loader doesn't
    know or care about other stages."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "safety:\n"
        "  mode: schema-only\n"
        "llm:\n"
        "  model: claude-haiku-4-5\n"
        "prune:\n"
        "  scope: full\n"
        "grade:\n"
        "  model: claude-sonnet-4-6\n",
        encoding="utf-8",
    )
    cfg = load_diff_config(tmp_path)
    assert cfg == DiffConfig()


def test_load_diff_config_extra_field_at_top_level_silently_ignored(
    tmp_path: Path,
) -> None:
    """Even an unknown top-level key (not a documented sibling stage) is
    silently tolerated by the outer ``extra="ignore"`` wrapper. The
    diff block parses normally."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "weather_service:\n  endpoint: https://example.com\ndiff:\n  context_lines: 5\n",
        encoding="utf-8",
    )
    cfg = load_diff_config(tmp_path)
    assert cfg.context_lines == 5


def test_load_diff_config_diff_key_present_but_null_returns_defaults(
    tmp_path: Path,
) -> None:
    """``diff:`` with no body parses to ``None`` — same as missing."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("diff:\n", encoding="utf-8")
    cfg = load_diff_config(tmp_path)
    assert cfg == DiffConfig()


def test_load_diff_config_diff_block_not_mapping_raises(tmp_path: Path) -> None:
    """``diff:`` with a non-mapping value (sequence, scalar) → loud
    fail. The strict inner shape is a mapping of knobs."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("diff:\n  - 1\n  - 2\n", encoding="utf-8")
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


# ----- Typos in inner block fail loud (extra="forbid") -----


def test_load_diff_config_typo_in_diff_block_fails_loud(tmp_path: Path) -> None:
    """``contxt_lines:`` instead of ``context_lines:`` must surface —
    the strict ``extra="forbid"`` on :class:`DiffConfig` is the
    silent-no-op defence."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "diff:\n  contxt_lines: 5\n",
        encoding="utf-8",
    )
    with pytest.raises(DiffError) as exc_info:
        load_diff_config(tmp_path)
    rendered = str(exc_info.value)
    # Pydantic's ValidationError message names the offending key or
    # cites the ``extra_forbidden`` discriminator; either is acceptable
    # evidence the typo was caught at the right seam.
    assert "contxt_lines" in rendered or "extra_forbidden" in rendered


def test_load_diff_config_unknown_field_in_inner_block_fails_loud(
    tmp_path: Path,
) -> None:
    """An unrecognised inner field (not in the locked DEC-010 set) →
    loud fail via ``extra="forbid"``."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "diff:\n  weight: 0.5\n",
        encoding="utf-8",
    )
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


# ----- Defaults match DEC-010 verbatim -----


def test_diff_config_defaults_match_dec_010() -> None:
    """Regression guard: every locked default must match the plan. A
    drift here is a behaviour change masquerading as a refactor."""
    cfg = DiffConfig()
    assert cfg.context_lines == 3
    assert cfg.max_why_chars == 80
    assert cfg.narrow_terminal_threshold == 60
    assert cfg.markdown_max_diff_chars == 60_000
    assert cfg.existing_schema_size_limit_bytes == 10_485_760
    assert cfg.existing_schema_warn_at_bytes == 1_000_000
    assert cfg.sidecar_size_limit_bytes == 10_000_000
    assert cfg.render_kind == "ansi"
    assert cfg.respect_no_color_env is True


def test_diff_config_field_count_locked_at_nine() -> None:
    """DEC-010 locks the field surface at nine knobs. A new field
    landing without an updated DEC entry is a docs/code drift; this
    test catches it loudly."""
    assert len(DiffConfig.model_fields) == 9


# ----- Populated diff block round-trips every field -----


def test_load_diff_config_populates_all_nine_fields(tmp_path: Path) -> None:
    """A YAML ``diff:`` block specifying every knob must populate the
    typed config end-to-end. Catches any field missing from the model
    or accidentally renamed."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "diff:\n"
        "  context_lines: 5\n"
        "  max_why_chars: 100\n"
        "  narrow_terminal_threshold: 40\n"
        "  markdown_max_diff_chars: 30000\n"
        "  existing_schema_size_limit_bytes: 5242880\n"
        "  existing_schema_warn_at_bytes: 2000000\n"
        "  sidecar_size_limit_bytes: 5000000\n"
        "  render_kind: markdown\n"
        "  respect_no_color_env: false\n",
        encoding="utf-8",
    )
    cfg = load_diff_config(tmp_path)
    assert cfg.context_lines == 5
    assert cfg.max_why_chars == 100
    assert cfg.narrow_terminal_threshold == 40
    assert cfg.markdown_max_diff_chars == 30_000
    assert cfg.existing_schema_size_limit_bytes == 5_242_880
    assert cfg.existing_schema_warn_at_bytes == 2_000_000
    assert cfg.sidecar_size_limit_bytes == 5_000_000
    assert cfg.render_kind == "markdown"
    assert cfg.respect_no_color_env is False


# ----- Numeric validators -----


def test_diff_config_context_lines_zero_rejected(tmp_path: Path) -> None:
    """Zero context lines would render an empty diff body for any
    change — refuse at config-load time."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("diff:\n  context_lines: 0\n", encoding="utf-8")
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


def test_diff_config_max_why_chars_negative_rejected(tmp_path: Path) -> None:
    """Negative truncation cap is meaningless; refuse at config-load
    time."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("diff:\n  max_why_chars: -1\n", encoding="utf-8")
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


def test_diff_config_existing_schema_size_limit_zero_rejected(tmp_path: Path) -> None:
    """A zero byte cap would refuse every payload; refuse the config
    instead."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "diff:\n  existing_schema_size_limit_bytes: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


def test_diff_config_sidecar_size_limit_negative_rejected(tmp_path: Path) -> None:
    """Negative sidecar cap is meaningless; refuse at config-load time."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "diff:\n  sidecar_size_limit_bytes: -100\n",
        encoding="utf-8",
    )
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


def test_diff_config_warn_at_must_be_below_size_limit(tmp_path: Path) -> None:
    """The DEC-014 soft-warn fires only when a payload exceeds
    ``existing_schema_warn_at_bytes`` but stays below the hard cap.
    A config with ``warn_at >= size_limit`` makes the warning dead
    code; reject at config-load time. Mirrors the
    ``safety-layer.md`` DEC-018 pattern (validators must run on every
    factory, including ``signalforge.yml``)."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "diff:\n"
        "  existing_schema_size_limit_bytes: 1000000\n"
        "  existing_schema_warn_at_bytes: 5000000\n",
        encoding="utf-8",
    )
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


def test_diff_config_warn_at_equal_to_size_limit_rejected() -> None:
    """Strict less-than: warn-at == size-limit is also dead code."""
    with pytest.raises(ValidationError):
        DiffConfig(
            existing_schema_size_limit_bytes=1_000_000,
            existing_schema_warn_at_bytes=1_000_000,
        )


# ----- render_kind literal validator -----


def test_diff_config_render_kind_invalid_rejected(tmp_path: Path) -> None:
    """``render_kind`` is a closed ``Literal["ansi", "markdown",
    "json"]`` — an unknown value (e.g. ``html``) must fail loud."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("diff:\n  render_kind: html\n", encoding="utf-8")
    with pytest.raises(DiffError):
        load_diff_config(tmp_path)


def test_diff_config_render_kind_all_three_accepted(tmp_path: Path) -> None:
    """All three ``Literal`` values must round-trip cleanly. Catches an
    accidental narrowing of the literal."""
    for kind in ("ansi", "markdown", "json"):
        config_path = tmp_path / "signalforge.yml"
        config_path.write_text(
            f"diff:\n  render_kind: {kind}\n",
            encoding="utf-8",
        )
        cfg = load_diff_config(tmp_path)
        assert cfg.render_kind == kind


# ----- Frozen / immutability -----


def test_diff_config_is_frozen() -> None:
    """``frozen=True`` on the model_config must hold — a caller
    accidentally mutating the loaded config would silently drift the
    knob set across stages."""
    cfg = DiffConfig()
    with pytest.raises(Exception):  # noqa: B017 — Pydantic raises ValidationError
        cfg.context_lines = 99  # type: ignore[misc]


# ----- Read-error wrapping (post-QG fix) -----


def test_load_diff_config_directory_at_config_path_raises_diff_error(
    tmp_path: Path,
) -> None:
    """When the candidate ``signalforge.yml`` path is a directory rather
    than a regular file, the loader wraps the resulting ``IsADirectoryError``
    as :class:`DiffError` rather than letting the OS error escape.
    """
    # Create a directory at the config-file location.
    (tmp_path / "signalforge.yml").mkdir()
    with pytest.raises(DiffError) as exc_info:
        load_diff_config(tmp_path)
    # The original OS error is chained.
    assert exc_info.value.__cause__ is not None


# ----- Symlink-hardening (post-QG fix) -----


def test_load_diff_config_symlink_escaping_project_dir_raises_diff_error(
    tmp_path: Path,
) -> None:
    """A ``signalforge.yml`` symlinked to a target outside the project
    directory must be rejected at load time. Mirrors the
    orchestrator-level canonicalisation applied to ``output_path`` /
    ``sidecar_path``.
    """
    # Build two sibling directories: a project tree and an
    # outside-the-project config file.
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    real_config = outside_dir / "real.yml"
    real_config.write_text("diff:\n  context_lines: 5\n", encoding="utf-8")
    # Symlink ``<project>/signalforge.yml -> ../outside/real.yml``.
    link = project_dir / "signalforge.yml"
    link.symlink_to(real_config)

    with pytest.raises(DiffError):
        load_diff_config(project_dir)
