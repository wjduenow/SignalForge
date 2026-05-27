"""Unit tests for the provider-neutral LLM seam (US-001 of issue #135).

Covers the foundation types: the :class:`ExceptionCategory` enum, the
:class:`UsageMetrics` value object, the :class:`LLMProvider` ABC, and the
process-level registry (:func:`register_provider` / :func:`provider_for`).

No vendor behaviour is wired at this stage (US-002 registers
``AnthropicProvider``), so ``provider_for("anthropic")`` raises
:class:`UnknownProviderError` here — pinned below.

Every test is capable of failing: no ``assert True``-shaped placeholders
(``testing-signal.md``).
"""

from __future__ import annotations

from typing import Any

import pytest

from signalforge.llm.errors import UnknownProviderError
from signalforge.llm.providers import (
    ExceptionCategory,
    LLMProvider,
    UsageMetrics,
    provider_for,
    register_provider,
)


class _DummyProvider(LLMProvider):
    """Minimal concrete provider for registry tests — no real SDK behaviour."""

    name = "dummy"
    supports_prompt_caching = False
    supports_token_count = False

    def make_client(self) -> object:
        return object()

    def build_create_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        dynamic_block: str,
        model: str,
        max_tokens: int,
        cache_ttl: str,
        cache_marker_active: bool,
    ) -> dict[str, Any]:
        return {"model": model, "max_tokens": max_tokens}

    def build_count_tokens_kwargs(
        self,
        *,
        system: str,
        cached_block: str,
        model: str,
    ) -> dict[str, Any]:
        return {"model": model}

    def extract_text_blocks(self, response: object) -> tuple[str, ...]:
        return ()

    def extract_usage(self, response: object) -> UsageMetrics:
        return UsageMetrics(input_tokens=0, output_tokens=0)

    def classify_exception(self, exc: BaseException) -> ExceptionCategory:
        return ExceptionCategory.NO_RETRY


@pytest.fixture
def _isolate_registry() -> Any:
    """Snapshot + restore the process-level registry so registering a dummy
    provider in one test doesn't leak into another."""
    from signalforge.llm import providers as providers_module

    saved = dict(providers_module._REGISTRY)
    try:
        yield
    finally:
        providers_module._REGISTRY.clear()
        providers_module._REGISTRY.update(saved)


@pytest.mark.unit
@pytest.mark.llm
def test_exception_category_has_exactly_five_members() -> None:
    """The retry-taxonomy enum has exactly the five DEC-002 members — adding
    or dropping one is a contract change the orchestrator dispatch depends on."""
    members = {m.name for m in ExceptionCategory}
    assert members == {
        "AUTH",
        "RATE_LIMIT",
        "SERVER_ERROR",
        "CONNECTION",
        "NO_RETRY",
    }


@pytest.mark.unit
@pytest.mark.llm
def test_usage_metrics_defaults_cache_fields_to_zero() -> None:
    """``UsageMetrics`` defaults both cache-token fields to 0 (DEC-002), so a
    provider without prompt caching reports 0 rather than requiring the caller
    to pass them."""
    usage = UsageMetrics(input_tokens=120, output_tokens=45)
    assert usage.input_tokens == 120
    assert usage.output_tokens == 45
    assert usage.cache_creation_input_tokens == 0
    assert usage.cache_read_input_tokens == 0


@pytest.mark.unit
@pytest.mark.llm
def test_usage_metrics_is_frozen() -> None:
    """The value object is immutable post-construction (mirrors ``LLMResult``)."""
    from pydantic import ValidationError

    usage = UsageMetrics(input_tokens=1, output_tokens=1)
    with pytest.raises(ValidationError):
        usage.input_tokens = 999  # type: ignore[misc]


@pytest.mark.unit
@pytest.mark.llm
def test_registry_hit_returns_registered_provider(_isolate_registry: None) -> None:
    """A registered provider is retrievable by name (DEC-003)."""
    provider = _DummyProvider()
    register_provider(provider)
    assert provider_for("dummy") is provider


@pytest.mark.unit
@pytest.mark.llm
def test_registry_miss_raises_unknown_provider_error(_isolate_registry: None) -> None:
    """An unregistered name raises ``UnknownProviderError`` listing the
    available registered names (DEC-003)."""
    register_provider(_DummyProvider())
    with pytest.raises(UnknownProviderError) as excinfo:
        provider_for("nope")
    err = excinfo.value
    assert err.name == "nope"
    # The available-keys list names the one registered provider.
    assert "dummy" in err.available
    rendered = str(err)
    assert "nope" in rendered
    assert "dummy" in rendered
    assert "↳ Remediation:" in rendered


@pytest.mark.unit
@pytest.mark.llm
def test_anthropic_not_registered_at_this_stage(_isolate_registry: None) -> None:
    """US-001 wires NO provider; ``provider_for("anthropic")`` raises until
    US-002 registers ``AnthropicProvider``."""
    # Empty the registry to assert the start-empty contract regardless of what
    # later stories register at import time.
    from signalforge.llm import providers as providers_module

    providers_module._REGISTRY.clear()
    with pytest.raises(UnknownProviderError):
        provider_for("anthropic")


@pytest.mark.unit
@pytest.mark.llm
def test_register_provider_last_writer_wins(_isolate_registry: None) -> None:
    """Re-registering under the same name replaces the prior entry (DEC-003)."""
    first = _DummyProvider()
    second = _DummyProvider()
    register_provider(first)
    register_provider(second)
    assert provider_for("dummy") is second


@pytest.mark.unit
@pytest.mark.llm
def test_llm_provider_is_abstract() -> None:
    """``LLMProvider`` cannot be instantiated directly — it is an ABC with
    unimplemented abstract methods."""
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]
