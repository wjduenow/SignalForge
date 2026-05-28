"""Provider-neutral ``--estimate`` tests (US-005 of issue #136).

Two load-bearing invariants this file pins (DEC-003, DEC-007, DEC-012,
DEC-013):

1. **Anthropic byte-identity (DEC-013).** The pre-refactor inline
   ``client.messages.count_tokens(...)`` calls in
   :mod:`signalforge.cli._estimate` were generalised in US-005 to
   dispatch through
   :meth:`signalforge.llm.providers.LLMProvider.estimate_input_tokens`.
   The Anthropic implementation must continue to produce the same
   rendered estimate stdout for the same canned token counts — the
   golden ``tests/fixtures/estimate/anthropic_byte_identity_golden.txt``
   pins the bytes. A refactor that changes the rendered shape (or the
   USD math) for the Anthropic path breaks this test loudly.

2. **OpenAI ``--estimate`` works end-to-end.** With
   ``draft.provider: openai`` + ``grade.provider: openai`` and an
   ``OpenAIProvider`` registered, the engine produces an
   :class:`signalforge.cli._estimate.EstimateReport` with non-zero
   token counts and non-zero USD figures. The token count is the
   local ``tiktoken`` figure (no API call), and the pricing math
   uses the OpenAI SKU table added in #136 US-004.

The two tests are deliberately co-located so a future refactor that
breaks either path surfaces both regressions in one file.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from signalforge.cli._estimate import estimate, render
from signalforge.draft.config import DraftConfig
from signalforge.grade.config import GradeConfig
from signalforge.grade.rubric import DEFAULT_RUBRIC
from signalforge.prune.config import PruneConfig
from signalforge.warehouse import BigQueryAdapter
from tests.cli.test_estimate_engine import _make_manifest, _make_model
from tests.llm._fake import FakeAnthropicClient, FakeCountTokensResponse
from tests.warehouse._fake import FakeBigQueryClient

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "estimate"
_GOLDEN_PATH = _FIXTURES / "anthropic_byte_identity_golden.txt"


# ---------------------------------------------------------------------------
# DEC-013 — Anthropic byte-identity floor
# ---------------------------------------------------------------------------


def test_estimate_anthropic_byte_identity_golden() -> None:
    """The Anthropic ``--estimate`` rendered output is byte-identical to
    the golden captured BEFORE the US-005 strategy-dispatch refactor.

    Inputs:
      * default :class:`DraftConfig` / :class:`GradeConfig` /
        :class:`PruneConfig` (Anthropic provider, ``claude-sonnet-4-6``,
        default rubric of 4 criteria).
      * Two-column ``model.shop.customers`` model (``id`` + ``email``).
      * Canned token counts: 1000 for the drafter; 500 for each of the
        4 grade criteria (queued on the fake client).
      * Canned warehouse dry-run bytes: 10,000.

    Captured 2026-05-28 from the pre-refactor engine; reproduced
    verbatim by the post-refactor strategy-dispatch path. A drift in
    the rendered shape (column widths, label wording, decimal
    precision) OR the USD math (price table, output-token estimate,
    artifact-count formula) breaks this test. The fix is to align the
    refactor with the golden, NOT to regenerate the golden — that is
    the entire point of DEC-013.

    If the price table is intentionally refreshed (a deliberate
    decision committed alongside a ``PRICE_TABLE_VERSION`` bump per
    ``python-build.md`` § "5-surface parity"), regenerate the golden
    in the same commit so the relationship between "what the operator
    sees" and "what the provider charges" remains explicit.
    """
    model = _make_model()
    mf = _make_manifest(model)
    draft_config = DraftConfig()  # provider="anthropic" default
    grade_config = GradeConfig()  # provider="anthropic" default
    prune_config = PruneConfig()
    fc = FakeBigQueryClient(project="fake_project")
    adapter = BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=100_000_000,
        client=fc,
    )
    fa = FakeAnthropicClient(project="fake_project")
    fa.expect_count_tokens(
        matching=lambda kw: True,
        returns=FakeCountTokensResponse(input_tokens=1000),
    )
    for _ in range(len(DEFAULT_RUBRIC)):
        fa.expect_count_tokens(
            matching=lambda kw: True,
            returns=FakeCountTokensResponse(input_tokens=500),
        )
    fc.expect_dry_run(sql_matching=r"SELECT", returns_bytes=10_000)

    report = estimate(
        model,
        mf,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fa,
    )
    rendered = render(report)

    golden = _GOLDEN_PATH.read_text(encoding="utf-8")
    assert rendered == golden, (
        "Anthropic --estimate rendered output drifted from the captured "
        f"golden. Diff against {_GOLDEN_PATH}. DEC-013 of #136 mandates "
        "byte-identity across the US-005 strategy refactor; align the "
        "implementation to the golden rather than regenerating the golden, "
        "unless this commit also intentionally bumps PRICE_TABLE_VERSION."
    )


# ---------------------------------------------------------------------------
# OpenAI provider-aware estimate (DEC-003 / DEC-007 / DEC-012)
# ---------------------------------------------------------------------------


def test_estimate_openai_provider_produces_nonzero_tokens_and_usd() -> None:
    """``--estimate`` with ``draft.provider: openai`` + ``grade.provider:
    openai`` produces a report with non-zero token counts AND non-zero
    USD figures.

    OpenAI counts tokens locally via ``tiktoken`` (DEC-012); the
    engine does NOT consult ``anthropic_client`` for the OpenAI path
    (``client=None`` is the canonical CLI shape after the US-005
    refactor of :mod:`signalforge.cli.generate`). The Anthropic test
    fake is passed in only to satisfy the engine's positional
    parameter; the fake's ``count_tokens`` is NOT consumed — pinned by
    ``len(fake.count_calls) == 0`` at the end.

    Pricing math uses the ``gpt-4o`` row from
    :data:`signalforge.llm.pricing.PRICES` (#136 US-004 / DEC-007);
    even with tiny token counts the per-MTok rates produce strictly
    positive USD figures.
    """
    model = _make_model()
    mf = _make_manifest(model)
    draft_config = DraftConfig.model_validate(
        {**DraftConfig().model_dump(), "provider": "openai", "model": "gpt-4o"}
    )
    grade_config = GradeConfig.model_validate(
        {**GradeConfig().model_dump(), "provider": "openai", "model": "gpt-4o"}
    )
    prune_config = PruneConfig()
    fc = FakeBigQueryClient(project="fake_project")
    adapter = BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=100_000_000,
        client=fc,
    )
    fc.expect_dry_run(sql_matching=r"SELECT", returns_bytes=10_000)

    # OpenAI counts locally; no Anthropic client is needed. The CLI
    # passes ``None`` for the OpenAI path (see :mod:`signalforge.cli.generate`),
    # which the engine forwards verbatim through the strategy.
    report = estimate(
        model,
        mf,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        None,
    )

    # Token counts are strictly positive (the rendered drafter prompt
    # is non-trivial; tiktoken returns a non-zero count for any
    # non-empty text). Engineered determinism: ``len(tiktoken.encode(x))
    # > 0`` for any non-empty ``x``.
    assert report.draft_input_tokens > 0
    assert all(c.total_input_tokens > 0 for c in report.grade_per_criterion)

    # USD math: the OpenAI ``gpt-4o`` row charges $2.50/MTok input and
    # $10/MTok output (#136 US-004 DEC-007); any positive token count
    # therefore produces a strictly positive USD figure.
    assert report.draft_usd > 0
    assert report.grade_usd > 0
    assert report.total_llm_usd > 0

    # The rendered output names the OpenAI models in the prelude and
    # carries the same totals + warehouse sections.
    rendered = render(report)
    assert "drafter: gpt-4o" in rendered
    assert "grader:  gpt-4o" in rendered
    assert "Total estimated LLM cost: $" in rendered
    # The renderer's USD prefix on the totals line includes the
    # computed value; assert the literal "$0.0000" placeholder is NOT
    # the rendered total (i.e. the figure rounded to 4 decimals is
    # non-zero).
    assert "Total estimated LLM cost: $0.0000" not in rendered


def test_estimate_openai_provider_ignores_threaded_client() -> None:
    """The OpenAI ``estimate_input_tokens`` impl ignores the ``client``
    kwarg — it counts via local ``tiktoken``.

    Drives the engine with a :class:`FakeAnthropicClient` AS the
    ``anthropic_client`` positional (a deliberately wrong-shape
    client for OpenAI) and asserts:
      * the engine completes successfully,
      * ``len(fake.count_calls) == 0`` — the Anthropic surface is
        never touched on the OpenAI path.

    This is the load-bearing proof that the strategy dispatch in
    ``_count_draft_tokens`` / ``_count_grade_criterion_tokens`` routes
    by ``config.provider``, NOT by inspecting the client type. A
    regression that fell through to the old hard-coded
    ``client.messages.count_tokens(...)`` call would consume a queued
    expectation from the fake (and fail loudly when no expectation is
    queued).
    """
    model = _make_model()
    mf = _make_manifest(model)
    draft_config = DraftConfig.model_validate(
        {**DraftConfig().model_dump(), "provider": "openai", "model": "gpt-4o"}
    )
    grade_config = GradeConfig.model_validate(
        {**GradeConfig().model_dump(), "provider": "openai", "model": "gpt-4o"}
    )
    prune_config = PruneConfig()
    fc = FakeBigQueryClient(project="fake_project")
    adapter = BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=100_000_000,
        client=fc,
    )
    fc.expect_dry_run(sql_matching=r"SELECT", returns_bytes=2048)

    # No ``expect_count_tokens`` queued on the Anthropic fake; a
    # regression that called through would raise loudly inside
    # ``FakeAnthropicClient.messages.count_tokens``.
    fa = FakeAnthropicClient(project="fake_project")

    report = estimate(
        model,
        mf,
        draft_config,
        grade_config,
        prune_config,
        adapter,
        fa,
    )

    assert len(fa.count_calls) == 0
    assert report.draft_input_tokens > 0
    assert report.total_llm_usd > 0


def test_estimate_openai_uses_count_openai_tokens_for_local_count() -> None:
    """The OpenAI estimate path delegates to
    :func:`signalforge.llm._openai_client._count_openai_tokens` (and
    therefore ``tiktoken``) — not to any Anthropic SDK surface.

    Patches the underlying ``_count_openai_tokens`` helper and asserts
    every dispatch reaches it. The patch returns a sentinel positive
    value so the engine's pricing math still produces non-zero USD
    figures (degenerate inputs would short-circuit the test rather
    than exercising the call surface).

    Without this, a refactor that re-pointed ``OpenAIProvider.estimate_input_tokens``
    at a stub (or stubbed it out for "v0.2") would pass the
    nonzero-USD test above silently — every count still comes from
    somewhere, just not necessarily from tiktoken.
    """
    model = _make_model()
    mf = _make_manifest(model)
    draft_config = DraftConfig.model_validate(
        {**DraftConfig().model_dump(), "provider": "openai", "model": "gpt-4o"}
    )
    grade_config = GradeConfig.model_validate(
        {**GradeConfig().model_dump(), "provider": "openai", "model": "gpt-4o"}
    )
    prune_config = PruneConfig()
    fc = FakeBigQueryClient(project="fake_project")
    adapter = BigQueryAdapter(
        project="fake_project",
        location="US",
        max_bytes_billed=100_000_000,
        client=fc,
    )
    fc.expect_dry_run(sql_matching=r"SELECT", returns_bytes=2048)

    sentinel = 4242
    with patch(
        "signalforge.llm._openai_client._count_openai_tokens",
        return_value=sentinel,
    ) as mocked:
        report = estimate(
            model,
            mf,
            draft_config,
            grade_config,
            prune_config,
            adapter,
            None,
        )

    # 1 drafter call + N per-criterion calls, all routed through
    # tiktoken.
    n_criteria = len(DEFAULT_RUBRIC)
    assert mocked.call_count == 1 + n_criteria
    # The drafter token count IS the sentinel (one call, one value).
    assert report.draft_input_tokens == sentinel
    # Per-criterion input tokens are sentinel * artifact_count; the
    # multiplication itself is engine logic, but the underlying
    # ``input_tokens_per_call`` is exactly the sentinel.
    for crit in report.grade_per_criterion:
        assert crit.input_tokens_per_call == sentinel
    # And every mocked call was issued for ``gpt-4o``.
    for call in mocked.call_args_list:
        assert call.args[0] == "gpt-4o"


def test_fakenocache_provider_estimate_input_tokens_returns_word_count() -> None:
    """:class:`tests.llm._fake_provider.FakeNoCacheProvider` answers
    ``estimate_input_tokens`` with ``len(text.split())``.

    The neutrality test in ``tests/grade/test_provider_neutrality.py``
    instantiates the fake provider via :class:`LLMProvider`'s ABC, so
    the impl must exist (an abstract method without an override would
    raise :class:`TypeError` at instantiation time). This test pins
    the trivial answer shape so the neutrality test's "shape only"
    assertions stay deterministic.
    """
    from tests.llm._fake_provider import FakeNoCacheProvider

    provider = FakeNoCacheProvider()
    assert provider.estimate_input_tokens("any-model", "one two three four") == 4
    assert provider.estimate_input_tokens("any-model", "") == 0
    # The ``client`` kwarg is accepted but ignored.
    assert provider.estimate_input_tokens("any-model", "hello world", client=object()) == 2
