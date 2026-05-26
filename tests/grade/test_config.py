"""Tests for ``signalforge.grade.config`` (US-004).

Exercises every locked invariant of :func:`load_grade_config` and
:class:`GradeConfig`:

* Resolution order: explicit ``path`` > ``<project_dir>/signalforge.yml``
  ``grade:`` block > defaults.
* Defaults match DEC-023..DEC-027 verbatim (regression guard against an
  accidental field-default tweak).
* ``extra="forbid"`` on the inner :class:`GradeConfig` rejects typos
  loud (mirrors ``safety-layer.md`` DEC-015 / ``llm-drafter.md``
  DEC-027 / ``prune-engine.md`` DEC-020).
* ``extra="ignore"`` on the outer :class:`_GradeConfigFile` silently
  tolerates sibling stage namespaces (``safety:``, ``llm:``, ``prune:``).
* Numeric range validators (positive, non-negative, ``[0.0, 1.0]``)
  fire when the YAML supplies an out-of-range knob.
* The optional rubric override path: well-formed → tuple of
  :class:`Criterion`; duplicate ids → re-raises through Pydantic as
  ``GradeConfigError``.

Each test is capable of failing if its target is broken (per
``docs/rules/testing-signal.md``); no ``assert True``-shaped no-ops.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from signalforge.grade.config import GradeConfig, load_grade_config
from signalforge.grade.errors import GradeConfigError
from signalforge.grade.rubric import Criterion

# ----- Resolution order -----


def test_load_grade_config_missing_file_returns_defaults_when_path_is_none(
    tmp_path: Path,
) -> None:
    """No ``signalforge.yml`` in ``project_dir`` → defaults silently
    (the typical fresh-project case)."""
    cfg = load_grade_config(tmp_path)
    assert cfg == GradeConfig()


def test_load_grade_config_explicit_missing_path_raises_grade_config_error(
    tmp_path: Path,
) -> None:
    """An explicit ``path`` that does not exist must fail loud — silent
    no-op would mask a typo in the operator's CLI flag."""
    missing = tmp_path / "does-not-exist.yml"
    with pytest.raises(GradeConfigError) as exc_info:
        load_grade_config(tmp_path, missing)
    rendered = str(exc_info.value)
    assert "Remediation" in rendered


def test_load_grade_config_empty_file_returns_defaults(tmp_path: Path) -> None:
    """Empty file → defaults (parses to ``None`` via ``yaml.safe_load``)."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("", encoding="utf-8")
    cfg = load_grade_config(tmp_path)
    assert cfg == GradeConfig()


def test_load_grade_config_comments_only_returns_defaults(tmp_path: Path) -> None:
    """A YAML file containing only comments parses to ``None`` after
    strip; treated identically to an empty file."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("# just a comment\n# another\n", encoding="utf-8")
    cfg = load_grade_config(tmp_path)
    assert cfg == GradeConfig()


def test_load_grade_config_top_level_not_mapping_raises(tmp_path: Path) -> None:
    """A YAML sequence at top level is a structural error — we expect a
    mapping with stage keys."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("- 1\n- 2\n- 3\n", encoding="utf-8")
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_load_grade_config_invalid_yaml_raises(tmp_path: Path) -> None:
    """Syntactically broken YAML → :class:`GradeConfigError` (the
    underlying ``yaml.YAMLError`` is preserved on ``__cause__``)."""
    config_path = tmp_path / "signalforge.yml"
    # Unbalanced quotes after the colon — yaml.safe_load raises.
    config_path.write_text(': "bad\n', encoding="utf-8")
    with pytest.raises(GradeConfigError) as exc_info:
        load_grade_config(tmp_path)
    assert exc_info.value.__cause__ is not None


# ----- Sibling namespaces silently ignored -----


def test_load_grade_config_unknown_top_level_key_silently_ignored(
    tmp_path: Path,
) -> None:
    """Sibling stage blocks (``safety:``, ``llm:``, ``prune:``) without
    a ``grade:`` key → defaults; the loader doesn't know or care about
    other stages."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "safety:\n  mode: schema-only\nllm:\n  model: claude-haiku-4-5\nprune:\n  scope: full\n",
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg == GradeConfig()


def test_load_grade_config_extra_field_at_top_level_silently_ignored(
    tmp_path: Path,
) -> None:
    """Even an unknown top-level key (not a documented sibling stage) is
    silently tolerated by the outer ``extra="ignore"`` wrapper. The
    grade block parses normally."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "weather_service:\n  endpoint: https://example.com\ngrade:\n  model: claude-opus-4-7\n",
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg.model == "claude-opus-4-7"


def test_load_grade_config_grade_key_present_but_null_returns_defaults(
    tmp_path: Path,
) -> None:
    """``grade:`` with no body parses to ``None`` — same as missing."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("grade:\n", encoding="utf-8")
    cfg = load_grade_config(tmp_path)
    assert cfg == GradeConfig()


def test_load_grade_config_grade_block_not_mapping_raises(tmp_path: Path) -> None:
    """``grade:`` with a non-mapping value (sequence, scalar) → loud
    fail. The strict inner shape is a mapping of knobs."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text("grade:\n  - 1\n  - 2\n", encoding="utf-8")
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


# ----- Typos in inner block fail loud (extra="forbid") -----


def test_load_grade_config_typo_in_grade_block_fails_loud(tmp_path: Path) -> None:
    """``mdoel:`` instead of ``model:`` must surface — the strict
    ``extra="forbid"`` on :class:`GradeConfig` is the silent-no-op
    defence."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  mdoel: claude-sonnet-4-6\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError) as exc_info:
        load_grade_config(tmp_path)
    rendered = str(exc_info.value)
    # Pydantic's ValidationError message names the offending key or
    # cites the ``extra_forbidden`` discriminator; either is acceptable
    # evidence the typo was caught at the right seam.
    assert "mdoel" in rendered or "extra_forbidden" in rendered


def test_load_grade_config_unknown_field_in_inner_block_fails_loud(
    tmp_path: Path,
) -> None:
    """An unrecognised inner field (not in the locked DEC-023..DEC-027
    set) → loud fail via ``extra="forbid"``."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  weight: 0.5\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


# ----- Defaults match DEC-023..DEC-027 verbatim -----


def test_grade_config_defaults_match_dec_023_to_027() -> None:
    """Regression guard: every locked default must match the plan. A
    drift here is a behaviour change masquerading as a refactor."""
    cfg = GradeConfig()
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.cache_ttl == "1h"
    assert cfg.max_output_tokens == 256
    assert cfg.max_retries_429 == 3
    assert cfg.max_retries_5xx == 1
    assert cfg.max_retries_conn == 1
    assert cfg.total_budget_seconds == 300
    assert cfg.min_pass_rate == 0.7
    assert cfg.min_mean_score == 0.5
    assert cfg.rubric is None
    assert cfg.fail_on_below_threshold is False


# ----- Numeric validators -----


def test_grade_config_min_pass_rate_above_one_rejected(tmp_path: Path) -> None:
    """``min_pass_rate`` is a ``[0.0, 1.0]`` float — out-of-range
    raises through Pydantic and the loader wraps as
    :class:`GradeConfigError`."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  min_pass_rate: 1.5\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_grade_config_min_mean_score_below_zero_rejected(tmp_path: Path) -> None:
    """Same range guard for ``min_mean_score`` (the negative-side
    boundary)."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  min_mean_score: -0.1\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_grade_config_max_output_tokens_negative_rejected(tmp_path: Path) -> None:
    """``max_output_tokens`` must be positive — zero or negative is a
    silent no-op (the LLM would refuse to emit output)."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  max_output_tokens: -1\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_grade_config_total_budget_seconds_zero_rejected(tmp_path: Path) -> None:
    """A zero total budget would route every criterion to the degraded
    path before any LLM call; refuse at config-load time."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  total_budget_seconds: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_grade_config_max_retries_negative_rejected(tmp_path: Path) -> None:
    """Retries are non-negative; ``-1`` would silently become "no
    retries" if not validated."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  max_retries_429: -1\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_grade_config_max_retries_zero_accepted(tmp_path: Path) -> None:
    """Zero retries IS a valid (if aggressive) config — non-negative
    means the lower bound is inclusive."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  max_retries_429: 0\n  max_retries_5xx: 0\n  max_retries_conn: 0\n",
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg.max_retries_429 == 0
    assert cfg.max_retries_5xx == 0
    assert cfg.max_retries_conn == 0


def test_grade_config_empty_model_string_rejected(tmp_path: Path) -> None:
    """An empty / whitespace-only model id is a silent-no-op vector."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        'grade:\n  model: "   "\n',
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


# ----- cache_ttl Literal -----


def test_grade_config_cache_ttl_unsupported_value_rejected(tmp_path: Path) -> None:
    """``cache_ttl`` is ``Literal["5m", "1h"]``; ``"30m"`` must
    fail loud rather than silently default."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        'grade:\n  cache_ttl: "30m"\n',
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_grade_config_cache_ttl_5m_accepted(tmp_path: Path) -> None:
    """Both ``Literal`` values are valid; ``"5m"`` should round-trip."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        'grade:\n  cache_ttl: "5m"\n',
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg.cache_ttl == "5m"


# ----- Rubric override -----


def test_grade_config_rubric_override_replaces_default(tmp_path: Path) -> None:
    """A YAML rubric of two well-formed criteria → ``cfg.rubric`` is
    the parsed tuple of :class:`Criterion`. This is the wholesale-
    replacement contract: the override is the rubric, not a merge."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n"
        "  rubric:\n"
        "    - id: c1\n"
        "      criterion: First criterion text.\n"
        "    - id: c2\n"
        "      criterion: Second criterion text.\n",
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg.rubric is not None
    assert len(cfg.rubric) == 2
    assert all(isinstance(c, Criterion) for c in cfg.rubric)
    assert cfg.rubric[0].id == "c1"
    assert cfg.rubric[1].id == "c2"
    assert cfg.rubric[0].criterion == "First criterion text."


def test_grade_config_rubric_with_duplicate_ids_raises_validator(
    tmp_path: Path,
) -> None:
    """Duplicate ``id`` values across the rubric must surface — the
    parser's anchor contract leans on uniqueness, and the diff renderer
    cannot disambiguate two ``GradingResult`` rows sharing an id."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n"
        "  rubric:\n"
        "    - id: same\n"
        "      criterion: First.\n"
        "    - id: same\n"
        "      criterion: Second.\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_grade_config_rubric_empty_list_rejected(tmp_path: Path) -> None:
    """An empty rubric ``[]`` would make every grade run a silent
    no-op; ``validate_rubric`` rejects it."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  rubric: []\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_grade_config_rubric_criterion_extra_field_rejected(tmp_path: Path) -> None:
    """:class:`Criterion` itself is ``extra="forbid"`` (DEC-017): a
    ``weight: 1.0`` typo in the rubric YAML must fail loud at config
    load — silent ignore is the failure mode this layer prevents."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  rubric:\n    - id: c1\n      criterion: First.\n      weight: 1.0\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


# ----- Explicit-path resolution -----


def test_load_grade_config_explicit_path_takes_precedence(tmp_path: Path) -> None:
    """When both ``<project_dir>/signalforge.yml`` AND an explicit
    ``path`` exist, the explicit path wins."""
    project_default = tmp_path / "signalforge.yml"
    project_default.write_text("grade:\n  model: from-project-default\n", encoding="utf-8")
    explicit = tmp_path / "alt.yml"
    explicit.write_text("grade:\n  model: from-explicit-path\n", encoding="utf-8")
    cfg = load_grade_config(tmp_path, explicit)
    assert cfg.model == "from-explicit-path"


def test_load_grade_config_doc_example_round_trips(tmp_path: Path) -> None:
    """The example YAML in docs/grade-ops.md round-trips through
    load_grade_config without errors."""
    fixture = Path(__file__).parent.parent / "fixtures" / "grade" / "example_config.yml"
    target = tmp_path / "signalforge.yml"
    target.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    config = load_grade_config(tmp_path)
    # Assert all fields are populated (smoke).
    assert config.model == "claude-sonnet-4-6"
    assert config.cache_ttl == "1h"
    assert config.max_output_tokens == 256
    assert config.total_budget_seconds == 300
    assert config.min_pass_rate == 0.7
    assert config.min_mean_score == 0.5
    assert config.fail_on_below_threshold is False


def test_load_grade_config_full_well_formed_block(tmp_path: Path) -> None:
    """End-to-end happy path: every field set to a non-default value
    round-trips through the loader."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n"
        "  model: claude-haiku-4-5\n"
        '  cache_ttl: "5m"\n'
        "  max_output_tokens: 512\n"
        "  max_retries_429: 5\n"
        "  max_retries_5xx: 2\n"
        "  max_retries_conn: 3\n"
        "  total_budget_seconds: 60\n"
        "  min_pass_rate: 0.8\n"
        "  min_mean_score: 0.6\n"
        "  fail_on_below_threshold: true\n",
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg.model == "claude-haiku-4-5"
    assert cfg.cache_ttl == "5m"
    assert cfg.max_output_tokens == 512
    assert cfg.max_retries_429 == 5
    assert cfg.max_retries_5xx == 2
    assert cfg.max_retries_conn == 3
    assert cfg.total_budget_seconds == 60
    assert cfg.min_pass_rate == 0.8
    assert cfg.min_mean_score == 0.6
    assert cfg.fail_on_below_threshold is True
    assert cfg.rubric is None
