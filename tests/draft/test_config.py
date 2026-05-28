"""Tests for ``signalforge.draft.config`` (US-009).

Covers :class:`DraftConfig` defaults / validators and
:func:`load_draft_config`'s full resolution / error contract. Mirrors
:mod:`tests.safety.test_config`. Every test is capable of failing per
``testing-signal.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from signalforge.draft.config import DraftConfig, load_draft_config
from signalforge.draft.errors import DraftConfigInvalidError, DraftConfigNotFoundError

pytestmark = [pytest.mark.unit, pytest.mark.draft]


_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "draft"


def _copy_fixture(fixture_name: str, dest_dir: Path) -> Path:
    """Copy a fixture YAML into ``dest_dir/signalforge.yml``."""
    src = _FIXTURES_DIR / fixture_name
    dest = dest_dir / "signalforge.yml"
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# DraftConfig — defaults and field validators
# ---------------------------------------------------------------------------


def test_draft_config_defaults_match_dec_017() -> None:
    """DEC-017: every default field value matches the spec."""
    cfg = DraftConfig()
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.cheap_model == "claude-haiku-4-5-20251001"
    assert cfg.max_output_tokens == 4096
    assert cfg.cache_ttl == "5m"
    assert cfg.max_retries_429 == 3
    assert cfg.max_retries_5xx == 1
    assert cfg.max_retries_conn == 1


def test_draft_config_is_frozen() -> None:
    """DEC-011 / safety-layer DEC-015: config-shaped models are immutable."""
    cfg = DraftConfig()
    with pytest.raises(ValidationError):
        cfg.model = "claude-opus-4-7"  # type: ignore[misc]


def test_draft_config_extra_forbid_rejects_typo() -> None:
    """DEC-011: ``extra="forbid"`` so a YAML typo like ``mdoel:`` (instead
    of ``model:``) raises rather than silently no-ops. Loaded via
    :func:`load_draft_config` which wraps the underlying ``ValidationError``
    as :class:`DraftConfigInvalidError` carrying ``cause``."""
    with pytest.raises(DraftConfigInvalidError) as excinfo:
        load_draft_config(
            _FIXTURES_DIR.parent.parent.parent,  # any real project_dir
            path=_FIXTURES_DIR / "signalforge_llm_typo.yml",
        )
    # The wrapped Pydantic ValidationError is preserved on `cause`.
    assert excinfo.value.cause is not None
    # The bad key surfaces in the error so users can find it.
    assert "mdoel" in str(excinfo.value)


@pytest.mark.parametrize("bad_value", [0, -1])
def test_draft_config_max_output_tokens_rejects_zero_and_negative(bad_value: int) -> None:
    """DEC-017: ``max_output_tokens`` must be positive."""
    with pytest.raises(ValidationError) as excinfo:
        DraftConfig(max_output_tokens=bad_value)
    assert "max_output_tokens" in str(excinfo.value)


def test_draft_config_cache_ttl_rejects_unknown() -> None:
    """DEC-017: ``cache_ttl`` is ``Literal["5m", "1h"]``; ``"30m"`` rejected."""
    with pytest.raises(ValidationError):
        DraftConfig(cache_ttl="30m")  # type: ignore[arg-type]


def test_draft_config_provider_defaults_to_anthropic() -> None:
    """DEC-007 of #135: ``provider`` defaults to the registered ``"anthropic"``."""
    assert DraftConfig().provider == "anthropic"


def test_draft_config_provider_accepts_registered_name() -> None:
    """DEC-007: a registered provider name is accepted by the validator."""
    cfg = DraftConfig(provider="anthropic")
    assert cfg.provider == "anthropic"


def test_draft_config_provider_rejects_unknown_with_available_keys() -> None:
    """DEC-007: an unknown provider fails loud with a typed
    :class:`UnknownProviderError` that names the registered providers.

    The validator delegates to
    :func:`signalforge.llm.providers.provider_for`, which raises the typed
    error directly — Pydantic does NOT wrap it into a ``ValidationError``
    (it isn't a ``ValueError`` / ``TypeError`` / ``AssertionError``)."""
    from signalforge.llm.errors import UnknownProviderError

    with pytest.raises(UnknownProviderError) as excinfo:
        DraftConfig(provider="bogus")
    assert excinfo.value.name == "bogus"
    # Available-keys remediation: the registered providers are listed.
    assert "anthropic" in str(excinfo.value)
    assert "bogus" in str(excinfo.value)


# ---------------------------------------------------------------------------
# load_draft_config — resolution / defaults
# ---------------------------------------------------------------------------


def test_load_draft_config_no_file_returns_defaults(tmp_path: Path) -> None:
    """Implicit-default path missing → built-in defaults."""
    cfg = load_draft_config(tmp_path)
    assert cfg == DraftConfig()


def test_load_draft_config_empty_file_returns_defaults(tmp_path: Path) -> None:
    """Empty ``signalforge.yml`` → defaults silently."""
    (tmp_path / "signalforge.yml").write_bytes(b"")
    assert load_draft_config(tmp_path) == DraftConfig()


def test_load_draft_config_whitespace_only_file_returns_defaults(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_text("   \n  \n", encoding="utf-8")
    assert load_draft_config(tmp_path) == DraftConfig()


def test_load_draft_config_yaml_with_only_comments_returns_defaults(tmp_path: Path) -> None:
    (tmp_path / "signalforge.yml").write_text("# just a comment\n", encoding="utf-8")
    assert load_draft_config(tmp_path) == DraftConfig()


def test_load_draft_config_minimal_yaml_round_trips(tmp_path: Path) -> None:
    """The minimal fixture sets only ``model:`` — every other field defaults."""
    _copy_fixture("signalforge_llm_minimal.yml", tmp_path)
    cfg = load_draft_config(tmp_path)
    assert cfg.model == "claude-haiku-4-5-20251001"
    # Untouched fields retain DEC-017 defaults.
    assert cfg.max_output_tokens == 4096
    assert cfg.cache_ttl == "5m"
    assert cfg.max_retries_429 == 3


def test_load_draft_config_full_yaml_round_trips(tmp_path: Path) -> None:
    """The full fixture sets multiple overrides — they all flow through."""
    _copy_fixture("signalforge_llm_full.yml", tmp_path)
    cfg = load_draft_config(tmp_path)
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.max_output_tokens == 8192
    assert cfg.cache_ttl == "1h"
    assert cfg.max_retries_429 == 5
    # Fields not in the fixture remain at their DEC-017 defaults.
    assert cfg.max_retries_5xx == 1
    assert cfg.max_retries_conn == 1


def test_load_draft_config_missing_llm_key_returns_defaults(tmp_path: Path) -> None:
    """DEC-027 namespace: a ``signalforge.yml`` containing only ``safety:``
    (or any other reserved top-level key) yields built-in defaults — the
    ``llm:`` block is simply absent."""
    (tmp_path / "signalforge.yml").write_text("safety: {}\n", encoding="utf-8")
    assert load_draft_config(tmp_path) == DraftConfig()


def test_load_draft_config_provider_round_trips_from_yaml(tmp_path: Path) -> None:
    """DEC-007: the ``provider`` knob round-trips from the ``llm:`` block."""
    (tmp_path / "signalforge.yml").write_text("llm:\n  provider: anthropic\n", encoding="utf-8")
    cfg = load_draft_config(tmp_path)
    assert cfg.provider == "anthropic"


def test_load_draft_config_unknown_provider_fails_loud(tmp_path: Path) -> None:
    """DEC-007: an unknown ``provider`` in ``signalforge.yml`` fails loud with
    the typed :class:`UnknownProviderError` naming the registered providers.

    The validator's :class:`UnknownProviderError` is NOT a Pydantic
    ``ValidationError``, so it propagates raw through ``load_draft_config``
    rather than being re-wrapped as ``DraftConfigInvalidError``."""
    from signalforge.llm.errors import UnknownProviderError

    (tmp_path / "signalforge.yml").write_text("llm:\n  provider: bogus\n", encoding="utf-8")
    with pytest.raises(UnknownProviderError) as excinfo:
        load_draft_config(tmp_path)
    assert "anthropic" in str(excinfo.value)


def test_load_draft_config_explicit_path_miss_raises(tmp_path: Path) -> None:
    """Explicit-path miss → :class:`DraftConfigNotFoundError`."""
    missing = tmp_path / "does_not_exist.yml"
    with pytest.raises(DraftConfigNotFoundError) as excinfo:
        load_draft_config(tmp_path, path=missing)
    assert excinfo.value.path == missing


def test_load_draft_config_unknown_top_level_key_ignored(tmp_path: Path) -> None:
    """DEC-027 namespace: unknown top-level keys (e.g., ``prune:``) coexist
    with ``llm:`` and are silently ignored — they're reserved for other
    stages, not this loader's concern."""
    (tmp_path / "signalforge.yml").write_text(
        "llm:\n  model: claude-opus-4-7\nprune:\n  threshold: 0.5\n",
        encoding="utf-8",
    )
    cfg = load_draft_config(tmp_path)
    assert cfg.model == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# load_draft_config — malformed input
# ---------------------------------------------------------------------------


def test_load_draft_config_malformed_yaml_raises(tmp_path: Path) -> None:
    """Unparseable YAML → :class:`DraftConfigInvalidError` with ``cause``."""
    (tmp_path / "signalforge.yml").write_text(": : :\n", encoding="utf-8")
    with pytest.raises(DraftConfigInvalidError) as excinfo:
        load_draft_config(tmp_path)
    assert excinfo.value.cause is not None


def test_load_draft_config_non_mapping_top_level_raises(tmp_path: Path) -> None:
    """A YAML list at the top level → :class:`DraftConfigInvalidError`."""
    (tmp_path / "signalforge.yml").write_text(
        "- a list at top level\n- like this\n", encoding="utf-8"
    )
    with pytest.raises(DraftConfigInvalidError):
        load_draft_config(tmp_path)


def test_load_draft_config_llm_block_not_mapping_raises(tmp_path: Path) -> None:
    """``llm:`` set to a scalar / list (rather than a mapping) → invalid."""
    (tmp_path / "signalforge.yml").write_text("llm: not-a-mapping\n", encoding="utf-8")
    with pytest.raises(DraftConfigInvalidError):
        load_draft_config(tmp_path)


def test_load_draft_config_uses_yaml_safe_load(tmp_path: Path) -> None:
    """Defensive regression: ``yaml.safe_load`` rejects arbitrary Python
    object construction tags. If the loader ever regressed to ``yaml.load``
    this fixture would silently invoke ``os.system("ls")`` instead of
    raising :class:`DraftConfigInvalidError`."""
    (tmp_path / "signalforge.yml").write_text(
        'llm: !!python/object/apply:os.system ["ls"]\n', encoding="utf-8"
    )
    with pytest.raises(DraftConfigInvalidError):
        load_draft_config(tmp_path)
