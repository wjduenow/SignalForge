"""Real-API smoke test for the LLM draft pipeline (US-015).

Gated by the ``anthropic`` marker — excluded from default CI; requires
``ANTHROPIC_API_KEY``. Drafts a tiny synthetic dbt model against the real
Anthropic API and asserts the round-trip plumbing works.

Path chosen: **A** (wire test).

Why not Path C (full round-trip)? The pre-send token-count check
(:class:`signalforge.llm.errors.LLMCacheTooSmallError`, DEC-024) rejects
cached blocks below the model minimum (1024 tokens for Sonnet, 2048 for
Haiku). This smoke fixture's cached block is intentionally tiny — one
model, four columns, no neighbours — so the manifest-summary block
clocks in around ~100 tokens, far below either floor. Padding the
manifest to push the cached block above 1024 would exercise more of the
pipeline but obscure the no-neighbours code path that's the actual
fixture-shape we want regression-protected.

So this test connects to the real Anthropic API, issues the
``count_tokens`` call (which DOES hit the network and prove the
SDK/auth/transport works), and asserts that the seam fails loud with
:class:`LLMCacheTooSmallError` BEFORE any ``messages.create`` is sent.
That's the v0.1 wire test: real SDK, real network, real credential —
but the actual draft call is short-circuited by the pre-send check so
the test costs effectively nothing per run.

When the real-API smoke fixture grows (e.g. when the regenerate.sh
script captures a fresh ``candidate_schema_v1.json``), this test can be
upgraded to Path C without changing its marker or invocation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from signalforge.draft.config import DraftConfig
from signalforge.draft.schema import draft_schema
from signalforge.llm.errors import LLMCacheTooSmallError
from signalforge.manifest.loader import load
from signalforge.safety.models import SamplingMode
from signalforge.safety.policy import SafetyPolicy
from tests.safety._fake_adapter import FakeAdapter

pytestmark = pytest.mark.anthropic


_FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "draft"
_SMOKE_MANIFEST = _FIXTURE_DIR / "smoke_manifest.json"


def test_haiku_drafts_candidate_schema_for_tiny_model(tmp_path: Path) -> None:
    """Wire-test the LLM draft pipeline against the real Anthropic API.

    The cached manifest summary for ``simple_orders`` sits at ~100
    tokens — well below Sonnet's 1024-token minimum and Haiku's
    2048-token minimum. The pre-send token-count check fails loud
    with :class:`LLMCacheTooSmallError` before ``messages.create``
    is invoked.

    What this proves:

    * ``ANTHROPIC_API_KEY`` is present and valid (the SDK constructs
      a real client; auth would surface as :class:`LLMAuthError`,
      not :class:`LLMCacheTooSmallError`).
    * The shim's ``count_tokens`` call reaches Anthropic and the
      response shape matches our expectations.
    * The full ``draft_schema`` orchestration —
      :func:`signalforge.safety.request.build_llm_request` →
      :func:`signalforge.draft.prompts.render_prompt` →
      :func:`signalforge.llm.client.call_anthropic` — wires up
      cleanly under realistic conditions.
    * The no-neighbours code path of ``_render_manifest_summary``
      (a model with empty ``depends_on.nodes`` and ``refs``) renders
      without raising.

    What this does NOT prove (deferred until the smoke fixture grows
    above the cache minimum):

    * ``messages.create`` round-trip / candidate-schema generation.
    * Response parsing + anchor-contract enforcement.
    * Response-audit JSONL writing.
    """
    if "ANTHROPIC_API_KEY" not in os.environ:
        pytest.skip("ANTHROPIC_API_KEY not set")

    manifest = load(_FIXTURE_DIR, manifest_path=_SMOKE_MANIFEST)
    model = manifest.get_model("model.sf_smoke.simple_orders")

    policy = SafetyPolicy(
        mode=SamplingMode.SCHEMA_ONLY,
        audit_path=tmp_path / "safety_audit.jsonl",
    )
    config = DraftConfig(model="claude-haiku-4-5-20251001", cache_ttl="5m")
    adapter = FakeAdapter()

    # Path A: the cached block is far below Haiku's 2048-token minimum,
    # so the LLM seam's pre-send check raises before ``messages.create``
    # is invoked. The ``count_tokens`` call still hits the real API,
    # which is what proves the wire is up.
    with pytest.raises(LLMCacheTooSmallError) as exc_info:
        draft_schema(model, adapter, policy, manifest, config=config)

    assert exc_info.value.model == config.model
    assert exc_info.value.min_tokens == 2048
    assert exc_info.value.cached_block_tokens < exc_info.value.min_tokens
