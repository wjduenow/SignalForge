"""Maintainer-only live smoke for raw :func:`call_llm` against Gemini (#137 US-008).

Gated by ``@pytest.mark.gemini`` (excluded from default CI by
:file:`pyproject.toml`'s ``addopts``) AND a runtime env-var skip
(``SF_RUN_GEMINI=1`` + ``GOOGLE_API_KEY``). The marker is the primary
gate against accidental collection in CI; the runtime ``pytest.skip``
makes a missing-env-var ``pytest -m gemini`` run skip cleanly with a
clear reason rather than fail loudly on an auth error.

Belt-and-suspenders gating mirrors the precedent in
:file:`tests/cli/test_e2e_bigquery_smoke.py` and the
``snowflake``-marked live tests under
:file:`tests/warehouse/test_snowflake_*_live.py`.

Cost economy: uses ``gemini-2.5-flash`` (cheapest of the three SKUs in
:mod:`signalforge.llm.pricing`) and a minimal ``max_tokens=64`` budget.

Shape-only assertions: LLM output bytes are not deterministic enough
to pin (see :file:`.claude/rules/testing-signal.md` § "End-to-end
gated tests"). What this proves end-to-end:

* ``provider="gemini"`` resolves the :class:`GeminiProvider` and
  ``strategy.make_client()`` builds a working SDK client.
* :func:`signalforge.llm.client.call_llm` reaches Gemini and returns
  a parseable :class:`LLMResult`.
* The provider's capability flags
  (``supports_prompt_caching=False`` / ``supports_token_count=False``)
  produce ``cache_*_input_tokens == 0`` and bypass the pre-send count
  gate (DEC-008 of #135 / DEC-003 of #137).
"""

from __future__ import annotations

import os

import pytest

from signalforge.llm import LLMResult, call_llm

pytestmark = pytest.mark.gemini


def _skip_reason() -> str | None:
    """Return a clear skip-reason string when env vars are missing.

    Belt-and-suspenders pattern from
    :file:`.claude/rules/testing-signal.md` § "Belt-and-suspenders
    gating": the marker keeps the test out of default CI; this helper
    keeps a maintainer's ``pytest -m gemini`` run from surfacing an
    auth error when ``GOOGLE_API_KEY`` is unset.
    """
    if os.environ.get("SF_RUN_GEMINI") != "1":
        return "SF_RUN_GEMINI=1 not set"
    if not os.environ.get("GOOGLE_API_KEY", "").strip():
        return "GOOGLE_API_KEY env var not set"
    return None


def test_call_llm_gemini_round_trips_against_real_api() -> None:
    """One real Gemini round-trip through :func:`call_llm`; shape-only.

    Drives the provider-neutral orchestrator with ``provider="gemini"``
    and ``client=None`` so ``strategy.make_client()`` (the DEC-006 of
    #135 seam) builds the real SDK client. Asserts only the shape of
    the returned :class:`LLMResult` — non-empty text, positive
    ``input_tokens``, zero cache-token fields (DEC-003 of #137 —
    ``supports_prompt_caching=False``).
    """
    reason = _skip_reason()
    if reason:
        pytest.skip(reason)

    result = call_llm(
        system="You are a helpful assistant. Respond with a single word.",
        cached_block="Vocabulary: greet, dismiss.",
        dynamic_block='Say "hello".',
        model="gemini-2.5-flash",
        max_tokens=64,
        prompt_version="gemini-live-smoke-v1",
        provider="gemini",
        # client=None ⇒ strategy.make_client() builds the real SDK client
        # (DEC-006 of #135).
        client=None,
    )

    assert isinstance(result, LLMResult)
    # Non-empty text blocks — proves the response shape decoded cleanly.
    assert result.text_blocks
    assert any(block.strip() for block in result.text_blocks)
    # Positive input tokens — proves the usage-metadata extraction works.
    assert result.input_tokens > 0
    # Capability flags False/False ⇒ no cache accounting (DEC-003 of #137).
    assert result.cache_creation_input_tokens == 0
    assert result.cache_read_input_tokens == 0
    # Provenance fields survive the round-trip.
    assert result.model == "gemini-2.5-flash"
    assert result.prompt_version == "gemini-live-smoke-v1"
