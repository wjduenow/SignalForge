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
``.claude/rules/testing-signal.md``); no ``assert True``-shaped no-ops.
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


# ----- Grade-cache master switch (#189 US-005 / DEC-016) -----


def test_grade_config_cache_enabled_defaults_true() -> None:
    """:attr:`GradeConfig.cache_enabled` defaults to ``True`` (#189 DEC-016).

    The grade cache ships on-by-default so operators get the wall-clock
    win without an opt-in step; ``signalforge generate --no-cache`` (US-007)
    or ``grade.cache_enabled: false`` in ``signalforge.yml`` are the
    explicit opt-outs.
    """
    cfg = GradeConfig()
    assert cfg.cache_enabled is True


def test_grade_config_cache_enabled_accepts_explicit_false() -> None:
    """An explicit ``cache_enabled=False`` parses cleanly (#189 DEC-016).

    The engine surgery in US-006 reads this field at orchestrator entry to
    short-circuit both lookup AND write — a regression here would silently
    re-enable the cache on a run the operator asked to bypass it.
    """
    cfg = GradeConfig(cache_enabled=False)
    assert cfg.cache_enabled is False


def test_grade_config_typo_cache_enable_missing_d_fails_loud() -> None:
    """``cache_enable`` (missing the trailing ``d``) MUST fail loud (#189 DEC-016).

    ``GradeConfig`` is ``extra="forbid"`` — a typo on a security-adjacent
    knob (silently leaving the cache enabled when the operator meant to
    disable it) is exactly the silent-no-op failure mode the strict
    validator exists to prevent.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        # Deliberate typo: missing 'd' on the kwarg, exercising the
        # ``extra="forbid"`` defence at runtime. The pyright suppression
        # is load-bearing — without it, the test couldn't express the
        # runtime contract.
        GradeConfig(cache_enable=False)  # pyright: ignore[reportCallIssue]


# ----- Defaults match DEC-023..DEC-027 verbatim -----


def test_grade_config_defaults_match_dec_023_to_027() -> None:
    """Regression guard: every locked default must match the plan. A
    drift here is a behaviour change masquerading as a refactor."""
    cfg = GradeConfig()
    # #187 US-002 / DEC-004: ``model`` now defaults to the sentinel that
    # resolves to the calling provider's default judge model. With the
    # default provider (``anthropic``) that is ``claude-sonnet-4-6`` — the
    # #187 calibration gate kept Sonnet the default (Haiku is opt-in).
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.cache_ttl == "1h"
    # #187 DEC-004: raised from 256 to avoid one-line gemini-flash truncation.
    assert cfg.max_output_tokens == 1024
    # #202 US-008 / DEC-209: raised 3 -> 6 as belt-and-braces over the
    # header-honoring rate limiter (the primary 429 fix).
    assert cfg.max_retries_429 == 6
    assert cfg.max_retries_5xx == 1
    assert cfg.max_retries_conn == 1
    # #198 DEC-001: total_budget_seconds is now an OPTIONAL absolute hard
    # ceiling; None (the new default) routes the engine to the scaled formula.
    assert cfg.total_budget_seconds is None
    # #198 DEC-001: scaled-budget terms (always on).
    assert cfg.budget_base_seconds == 60
    assert cfg.budget_per_pair_seconds == 20.0
    # #198 DEC-002: three opt-in soft ceilings, all default off (None).
    assert cfg.max_grade_calls is None
    assert cfg.max_grade_cost_usd is None
    assert cfg.max_grade_tokens is None
    assert cfg.max_concurrent_calls == 10
    # #202 US-005/US-006: always-on sweep knobs + the fail-loud completeness
    # contract. Pinned so default drift on the new #202 knobs fails loud here.
    assert cfg.sweep_max_rounds == 3
    assert cfg.sweep_cooldown_seconds == 2.0
    assert cfg.sweep_budget_seconds == 300
    assert cfg.require_complete is True
    assert cfg.min_pass_rate == 0.7
    assert cfg.min_mean_score == 0.5
    assert cfg.rubric is None
    assert cfg.fail_on_below_threshold is False
    assert cfg.provider == "anthropic"
    # #189 DEC-016: grade-cache master switch defaults on.
    assert cfg.cache_enabled is True


# ----- Provider validator (issue #135 DEC-007) -----


def test_grade_config_provider_defaults_to_anthropic() -> None:
    """DEC-007 of #135: ``provider`` defaults to the registered ``"anthropic"``."""
    assert GradeConfig().provider == "anthropic"


def test_grade_config_provider_accepts_registered_name() -> None:
    """DEC-007: a registered provider name is accepted by the validator."""
    assert GradeConfig(provider="anthropic").provider == "anthropic"


def test_grade_config_provider_accepts_openai() -> None:
    """US-002 of #136: after ``OpenAIProvider`` is registered at import time,
    ``GradeConfig(provider="openai", model="gpt-4o")`` validates without
    error (DEC-005 of #136 — both stages accept ``provider: openai``)."""
    cfg = GradeConfig(provider="openai", model="gpt-4o")
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o"


def test_grade_config_provider_accepts_gemini() -> None:
    """#137 US-002: ``GeminiProvider`` registers under ``"gemini"`` at import
    time so ``GradeConfig(provider="gemini", model="gemini-2.5-flash")``
    validates cleanly. The registry membership IS the validation surface."""
    cfg = GradeConfig(provider="gemini", model="gemini-2.5-flash")
    assert cfg.provider == "gemini"
    assert cfg.model == "gemini-2.5-flash"


def test_grade_config_provider_rejects_unknown_with_available_keys() -> None:
    """DEC-007: an unknown provider fails loud with a typed
    :class:`UnknownProviderError` naming the registered providers.

    ``UnknownProviderError`` is not a Pydantic ``ValidationError``, so it
    propagates raw from the validator rather than being wrapped."""
    from signalforge.llm.errors import UnknownProviderError

    with pytest.raises(UnknownProviderError) as excinfo:
        GradeConfig(provider="bogus")
    assert excinfo.value.name == "bogus"
    assert "anthropic" in str(excinfo.value)
    assert "bogus" in str(excinfo.value)


def test_load_grade_config_provider_round_trips_from_yaml(tmp_path: Path) -> None:
    """DEC-007: the ``provider`` knob round-trips from the ``grade:`` block."""
    (tmp_path / "signalforge.yml").write_text("grade:\n  provider: anthropic\n", encoding="utf-8")
    cfg = load_grade_config(tmp_path)
    assert cfg.provider == "anthropic"


def test_load_grade_config_unknown_provider_fails_loud(tmp_path: Path) -> None:
    """DEC-007: an unknown ``provider`` in ``signalforge.yml`` fails loud with
    the typed :class:`UnknownProviderError` naming the registered providers.

    The typed error propagates raw through ``load_grade_config`` rather than
    being re-wrapped as ``GradeConfigError`` (it is not a ``ValidationError``)."""
    from signalforge.llm.errors import UnknownProviderError

    (tmp_path / "signalforge.yml").write_text("grade:\n  provider: bogus\n", encoding="utf-8")
    with pytest.raises(UnknownProviderError) as excinfo:
        load_grade_config(tmp_path)
    assert "anthropic" in str(excinfo.value)


# ----- Per-provider fast-model resolution (#187 US-002 / DEC-004) -----


def test_grade_config_model_resolves_anthropic_default() -> None:
    """The sentinel ``model=None`` (default) resolves to the anthropic
    default judge model (``claude-sonnet-4-6``) via
    :data:`PROVIDER_DEFAULT_MODELS`. Haiku is an explicit opt-in — the
    #187 calibration gate found it grades stricter than Sonnet."""
    assert GradeConfig().model == "claude-sonnet-4-6"


def test_grade_config_model_resolves_openai_fast_default() -> None:
    """With ``provider="openai"`` and no explicit model, resolution
    yields the openai fast model."""
    assert GradeConfig(provider="openai").model == "gpt-4o-mini"


def test_grade_config_model_resolves_gemini_fast_default() -> None:
    """With ``provider="gemini"`` and no explicit model, resolution
    yields the gemini fast model."""
    assert GradeConfig(provider="gemini").model == "gemini-2.5-flash"


def test_grade_config_explicit_model_is_honoured_over_default() -> None:
    """An explicit ``model:`` always wins over the per-provider default."""
    assert GradeConfig(model="claude-sonnet-4-6").model == "claude-sonnet-4-6"


def test_grade_config_resolved_model_is_never_none() -> None:
    """After construction on the happy path, ``model`` is a concrete
    string — the sentinel never leaks out."""
    cfg = GradeConfig()
    assert isinstance(cfg.model, str)
    assert cfg.model.strip() != ""


def test_grade_config_unknown_provider_not_masked_by_resolution() -> None:
    """An unknown provider must still raise the typed provider error —
    the model-resolution before-validator declines to inject (the
    provider isn't in the fast-model table) so the provider
    field-validator surfaces :class:`UnknownProviderError` rather than a
    masked ``KeyError`` (#187 US-002 / DEC-004)."""
    from signalforge.llm.errors import UnknownProviderError

    with pytest.raises(UnknownProviderError) as excinfo:
        GradeConfig(provider="bogus")
    assert excinfo.value.name == "bogus"


# ----- Model<->provider compatibility validator (#187 US-002 / DEC-006) -----


def test_grade_config_provider_model_mismatch_rejected() -> None:
    """A ``claude-`` model under ``provider="openai"`` is an operator
    mistake — reject at config-load via the SKU-prefix compat check."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(provider="openai", model="claude-sonnet-4-6")


def test_grade_config_provider_model_match_accepted() -> None:
    """A ``gpt-`` model under ``provider="openai"`` passes the compat
    check (the prefix matches the provider)."""
    cfg = GradeConfig(provider="openai", model="gpt-4o")
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o"


def test_grade_config_whitespace_model_still_rejected() -> None:
    """A whitespace-only explicit model must still trip the non-empty
    guard — the sentinel resolution does not relax that defence."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(model="   ")


def test_grade_config_max_output_tokens_default_is_1024() -> None:
    """#187 DEC-004: the per-criterion cap default is raised to 1024."""
    assert GradeConfig().max_output_tokens == 1024


# ----- load_grade_config fast-model resolution + compat (#187 US-002) -----


def test_load_grade_config_block_without_model_resolves_fast_model(
    tmp_path: Path,
) -> None:
    """A ``grade:`` block that omits ``model:`` resolves the provider's
    fast model at load time."""
    (tmp_path / "signalforge.yml").write_text(
        "grade:\n  provider: openai\n",
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg.provider == "openai"
    assert cfg.model == "gpt-4o-mini"


def test_load_grade_config_block_with_model_honours_it(tmp_path: Path) -> None:
    """An explicit ``model:`` in the ``grade:`` block is honoured."""
    (tmp_path / "signalforge.yml").write_text(
        "grade:\n  model: claude-sonnet-4-6\n",
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg.model == "claude-sonnet-4-6"


def test_load_grade_config_provider_model_mismatch_raises(tmp_path: Path) -> None:
    """A mismatched provider/model in ``signalforge.yml`` surfaces as
    :class:`GradeConfigError` at the loader boundary (the underlying
    ``ValidationError`` is wrapped)."""
    (tmp_path / "signalforge.yml").write_text(
        "grade:\n  provider: openai\n  model: claude-sonnet-4-6\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


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
    path before any LLM call; refuse at config-load time. (#198 DEC-001:
    the field is now optional, but a *present* value must still be positive.)"""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  total_budget_seconds: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


# ----- #198 DEC-001/DEC-002: scaled-budget + soft-ceiling validators -----


def test_grade_config_total_budget_seconds_none_accepted() -> None:
    """#198 DEC-001: ``None`` is the new default and means "use the scaled
    formula" — the allow-None-or-positive validator passes it through."""
    cfg = GradeConfig(total_budget_seconds=None)
    assert cfg.total_budget_seconds is None


def test_grade_config_total_budget_seconds_explicit_int_accepted() -> None:
    """#198 DEC-001: an explicit int still validates — it acts as an absolute
    hard cap (``min(scaled, total_budget_seconds)``), preserving v0.1 pinned
    configs."""
    cfg = GradeConfig(total_budget_seconds=600)
    assert cfg.total_budget_seconds == 600


def test_grade_config_budget_base_seconds_zero_rejected() -> None:
    """#198 DEC-001: ``budget_base_seconds`` is an always-on scaled-budget
    term; a non-positive value would size the wall-clock backstop to ~0 and
    degrade every pair. Fail loud."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(budget_base_seconds=0)


def test_grade_config_budget_per_pair_seconds_zero_rejected() -> None:
    """#198 DEC-001: ``budget_per_pair_seconds`` must be positive (the
    per-wave wall allowance can't be zero)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(budget_per_pair_seconds=0.0)


def test_grade_config_budget_per_pair_seconds_negative_rejected() -> None:
    """#198 DEC-001: a negative per-wave allowance is rejected."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(budget_per_pair_seconds=-1.0)


def test_grade_config_max_grade_calls_zero_rejected() -> None:
    """#198 DEC-002: the opt-in soft ceilings accept ``None`` but reject a
    present ``<= 0`` value (a zero ceiling would trip immediately and degrade
    the whole run)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(max_grade_calls=0)


def test_grade_config_max_grade_cost_usd_zero_rejected() -> None:
    """#198 DEC-002: a present ``max_grade_cost_usd`` must be positive."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(max_grade_cost_usd=0.0)


def test_grade_config_max_grade_tokens_zero_rejected() -> None:
    """#198 DEC-002: a present ``max_grade_tokens`` must be positive."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(max_grade_tokens=0)


def test_grade_config_optional_positive_fields_reject_negative() -> None:
    """#198 DEC-001/002: the optional-positive validator rejects a present
    NEGATIVE value (not just zero) for every field it guards. Zero-rejection
    alone would still pass if a future refactor slipped from ``<= 0`` to
    ``== 0``/``!= 0`` while letting negatives through; pin negatives too."""
    from pydantic import ValidationError

    for kwargs in (
        {"total_budget_seconds": -1},
        {"budget_base_seconds": -1},
        {"max_grade_calls": -1},
        {"max_grade_cost_usd": -0.01},
        {"max_grade_tokens": -1},
    ):
        with pytest.raises(ValidationError):
            GradeConfig(**kwargs)  # pyright: ignore[reportArgumentType]


def test_grade_config_float_fields_reject_non_finite() -> None:
    """#198 (PR review): the float-bearing knobs reject NaN / +/-inf.

    ``yaml.safe_load`` parses ``.nan`` / ``.inf``, and Pydantic floats allow
    them by default. ``nan <= 0`` / ``inf <= 0`` are both ``False``, so without
    an explicit finiteness guard a NaN ``budget_per_pair_seconds`` would slip
    through and later crash ``math.ceil(nan)`` in ``_compute_effective_budget``;
    ``max_grade_cost_usd: .inf`` would silently make the cost ceiling a no-op."""
    from pydantic import ValidationError

    for kwargs in (
        {"budget_per_pair_seconds": float("nan")},
        {"budget_per_pair_seconds": float("inf")},
        {"budget_per_pair_seconds": float("-inf")},
        {"max_grade_cost_usd": float("nan")},
        {"max_grade_cost_usd": float("inf")},
    ):
        with pytest.raises(ValidationError):
            GradeConfig(**kwargs)  # pyright: ignore[reportArgumentType]


def test_grade_config_soft_ceilings_accept_none() -> None:
    """#198 DEC-002: all three opt-in soft ceilings accept ``None`` (off) —
    the explicit-None path mirrors the default."""
    cfg = GradeConfig(max_grade_calls=None, max_grade_cost_usd=None, max_grade_tokens=None)
    assert cfg.max_grade_calls is None
    assert cfg.max_grade_cost_usd is None
    assert cfg.max_grade_tokens is None


def test_grade_config_soft_ceilings_accept_positive() -> None:
    """#198 DEC-002: a present positive value for each soft ceiling
    validates."""
    cfg = GradeConfig(max_grade_calls=50, max_grade_cost_usd=1.25, max_grade_tokens=500_000)
    assert cfg.max_grade_calls == 50
    assert cfg.max_grade_cost_usd == 1.25
    assert cfg.max_grade_tokens == 500_000


def test_grade_config_typo_max_grade_cal_fails_loud() -> None:
    """#198 / safety-layer.md DEC-015: ``extra="forbid"`` still rejects a typo
    on a new ceiling key (``max_grade_cal`` missing ``ls``) rather than
    silently leaving the ceiling off."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GradeConfig(max_grade_cal=5)  # pyright: ignore[reportCallIssue]


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
    """The committed example fixture (tests/fixtures/grade/example_config.yml)
    round-trips through load_grade_config without errors. The fixture pins
    EXPLICIT model/token values (not the #187 resolved defaults) to exercise
    the explicit-override path — `claude-sonnet-4-6` under the default
    `anthropic` provider is accepted by the model↔provider compat validator
    (matching `claude-` prefix)."""
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
    # #198: the fixture carries the two always-on scaled-budget terms; pin
    # fixture<->loader parity explicitly (a fixture typo that happened to match
    # another valid key would otherwise pass via the defaults test alone). The
    # three opt-in ceilings are commented out in the fixture, so they load None.
    assert config.budget_base_seconds == 60
    assert config.budget_per_pair_seconds == 20.0
    assert config.max_grade_calls is None
    assert config.max_grade_cost_usd is None
    assert config.max_grade_tokens is None


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


# ----- max_concurrent_calls range validator (issue #186 DEC-003) -----


def test_grade_config_max_concurrent_calls_default_is_ten() -> None:
    """Default ``max_concurrent_calls`` matches DEC-003 of #186."""
    assert GradeConfig().max_concurrent_calls == 10


def test_grade_config_max_concurrent_calls_field_range_lower_bound_accepted() -> None:
    """``max_concurrent_calls=1`` is the v0.1-sequential-equivalent
    floor and must validate."""
    cfg = GradeConfig(max_concurrent_calls=1)
    assert cfg.max_concurrent_calls == 1


def test_grade_config_max_concurrent_calls_field_range_upper_bound_accepted() -> None:
    """``max_concurrent_calls=100`` is the documented ceiling and must
    validate (closed interval [1, 100])."""
    cfg = GradeConfig(max_concurrent_calls=100)
    assert cfg.max_concurrent_calls == 100


def test_grade_config_max_concurrent_calls_zero_rejected() -> None:
    """``max_concurrent_calls=0`` would dispatch nothing (semaphore
    acquire deadlock); reject loud."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        GradeConfig(max_concurrent_calls=0)
    # The message is locked verbatim by DEC-003 of #186.
    assert "must be in the closed interval [1, 100]" in str(excinfo.value)


def test_grade_config_max_concurrent_calls_above_ceiling_rejected() -> None:
    """``max_concurrent_calls=101`` exceeds the documented ceiling and
    must fail loud rather than silently invite rate-limit storms."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as excinfo:
        GradeConfig(max_concurrent_calls=101)
    assert "must be in the closed interval [1, 100]" in str(excinfo.value)


def test_load_grade_config_max_concurrent_calls_out_of_range_wraps_as_grade_config_error(
    tmp_path: Path,
) -> None:
    """Out-of-range values supplied via ``signalforge.yml`` route through
    :func:`load_grade_config` and wrap as :class:`GradeConfigError`
    (the standard loader-side wrapping; mirrors every other numeric
    validator's loader-side test)."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  max_concurrent_calls: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(GradeConfigError):
        load_grade_config(tmp_path)


def test_load_grade_config_max_concurrent_calls_round_trips_from_yaml(
    tmp_path: Path,
) -> None:
    """A valid in-range override round-trips through the loader."""
    config_path = tmp_path / "signalforge.yml"
    config_path.write_text(
        "grade:\n  max_concurrent_calls: 25\n",
        encoding="utf-8",
    )
    cfg = load_grade_config(tmp_path)
    assert cfg.max_concurrent_calls == 25
