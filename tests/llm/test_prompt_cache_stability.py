"""Cache-stability snapshot for the LLM-drafter prompt (US-014 / DEC-019).

The cached block produced by :func:`signalforge.draft.prompts.render_prompt`
is the prefix Anthropic's prompt cache keys on. Any byte-level change to
that block invalidates every cached prefix in flight — a silent cost
regression. The :data:`signalforge.draft.prompts._PROMPT_VERSION` hash
covers the *templates*; this test covers the *rendered output* for the
canonical fixture (the load-bearing part for cache stability).

On mismatch, the assertion message includes a :func:`difflib.unified_diff`
so the regression is reviewable in PR.

Pinned ``_PROMPT_VERSION``: tracked by :data:`_EXPECTED_PROMPT_VERSION`
below — the constant is the source of truth, this docstring deliberately
does not hard-code a hash so it can't drift. Latest known rotations:

- ``1c55806467984090`` — original pin from #5 / DEC-019.
- ``c7d15d59f78bab2d`` — rotated under #10 when an explicit JSON-shape
  example was added to the system prompt (DEC-025 of #10).
- ``8a0d81994275b803`` — rotated under #10 review feedback when the
  JSON example's outer ```json fence was removed (the prompt instructs
  "do not wrap in markdown fences" so showing a fenced example was a
  foot-gun; CodeRabbit/Copilot both flagged it).
- ``2563a71c5e31f0db`` — current. Rotated under #54 when the system
  prompt became parameterised by ``DraftConfig.exclude_tests`` (the
  test catalogue + SCOPE line are now templated). The default render
  (no exclusions) is semantically identical to the prior text but
  has a different line-wrap in the SCOPE paragraph because the enum
  list is now rendered via :func:`str.format` rather than a literal.

If this rotates again, update both :data:`_EXPECTED_PROMPT_VERSION` and
:data:`_CACHED_BLOCK_GOLDEN` in lockstep — the rotation is the signal
that the templates changed.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from signalforge.draft.prompts import render_prompt
from signalforge.manifest.models import Manifest
from signalforge.safety import LLMRequest, SamplingMode

pytestmark = pytest.mark.llm


_FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "draft"
    / "manifest_one_model_with_neighbours.json"
)


_EXPECTED_PROMPT_VERSION: str = "2563a71c5e31f0db"


# Captured once via ``render_prompt`` against the canonical fixture below.
# Any byte-level change to the cached block — model description text,
# column rendering, neighbour ordering — will rotate this snapshot. The
# golden value is intentionally inline (not a fixture file) so reviewers
# see the diff in the PR rather than chasing a separate file.
_CACHED_BLOCK_GOLDEN: str = """\
## Model under draft

Name: fct_orders
Description: Order fact table joined to the customer dimension.

Columns:
- amount (NUMERIC)
- customer_id (STRING)
- order_id (STRING)
- ordered_at (TIMESTAMP)

## Neighbouring models

### dim_customers

Description: Customer dimension keyed by customer_id.

Columns:
- country_code (STRING): ISO 3166-1 alpha-2 country code.
- created_at (TIMESTAMP): Customer record creation timestamp.
- customer_id (STRING): Primary key for a customer.
- customer_name (STRING): Display name for the customer.

### stg_orders

Description: Staged orders pulled from the raw orders source.

Columns:
- amount_cents (INT64): Order total in cents.
- customer_id (STRING): Foreign key to dim_customers.customer_id.
- order_id (STRING): Surrogate key from the source orders table.
- ordered_at (TIMESTAMP): Wall-clock timestamp the order was placed.
"""


def _build_canonical_request() -> LLMRequest:
    """Construct the canonical request used to pin the cached block.

    Schema-only mode, four columns matching the ``fct_orders`` model in
    the fixture, no aggregates / sampled_rows / redactions. Constructed
    directly (not via ``build_llm_request``) because this is the
    cache-stability snapshot — the safety layer's audit-write seam is
    exercised elsewhere.
    """
    return LLMRequest(
        model_unique_id="model.sf_demo.fct_orders",
        mode=SamplingMode.SCHEMA_ONLY,
        columns_sent=("order_id", "customer_id", "amount", "ordered_at"),
        redactions=(),
        schema=(
            ("order_id", "STRING"),
            ("customer_id", "STRING"),
            ("amount", "FLOAT64"),
            ("ordered_at", "TIMESTAMP"),
        ),
    )


def test_prompt_version_pinned_to_us_010_value() -> None:
    """The :data:`_PROMPT_VERSION` hash is pinned by US-010. Any template
    edit rotates the hash; updating this constant without also rotating
    :data:`_CACHED_BLOCK_GOLDEN` would silently desync the snapshot.
    """
    manifest = Manifest.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    model = manifest.nodes["model.sf_demo.fct_orders"]
    request = _build_canonical_request()
    _system, _cached, _dynamic, prompt_version = render_prompt(model, request, manifest)
    assert prompt_version == _EXPECTED_PROMPT_VERSION, (
        f"_PROMPT_VERSION rotated: expected {_EXPECTED_PROMPT_VERSION!r}, "
        f"got {prompt_version!r}. If this is intentional (a template change), "
        "update _EXPECTED_PROMPT_VERSION AND re-capture _CACHED_BLOCK_GOLDEN "
        "in lockstep — the cached snapshot below will also fail until you do."
    )


def test_cached_block_byte_stable_against_golden() -> None:
    """Byte-equality assertion between the rendered cached block and the
    inline golden constant. On mismatch, prints a unified diff so the
    regression is reviewable in PR.

    Cache cost regressions are silent in production: a one-character
    change to the cached block invalidates every cached prefix and rebills
    each call at full input-token rate. This test is the only thing
    standing between an inadvertent prompt edit and a cost spike.
    """
    manifest = Manifest.model_validate_json(_FIXTURE_PATH.read_text(encoding="utf-8"))
    model = manifest.nodes["model.sf_demo.fct_orders"]
    request = _build_canonical_request()
    _system, cached, _dynamic, _prompt_version = render_prompt(model, request, manifest)

    if cached != _CACHED_BLOCK_GOLDEN:
        diff = "".join(
            difflib.unified_diff(
                _CACHED_BLOCK_GOLDEN.splitlines(keepends=True),
                cached.splitlines(keepends=True),
                fromfile="_CACHED_BLOCK_GOLDEN",
                tofile="render_prompt(...).cached",
                n=3,
            )
        )
        pytest.fail(
            "Cached block drifted from the pinned golden snapshot.\n"
            "If this is intentional, update _CACHED_BLOCK_GOLDEN to the "
            "new render and verify _PROMPT_VERSION rotated in lockstep.\n\n"
            f"Unified diff:\n{diff}"
        )
