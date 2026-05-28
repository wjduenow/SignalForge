# `--estimate` — operations guide

Operational reference for `signalforge generate --estimate`, the
pre-flight cost-preview path. Companion to
[`docs/cli-ops.md`](cli-ops.md) (where `--estimate` is registered as a
`generate` flag), [`docs/draft-ops.md`](draft-ops.md) (the LLM seam the
estimate counts tokens against), and
[`docs/grade-ops.md`](grade-ops.md) (the per-criterion grading fan-out
the estimate projects).

## What it does

`signalforge generate <model> --estimate` runs the full pipeline
prelude (manifest load, safety policy resolve, draft + grade + diff
config load, warehouse profile load, adapter construction) so that any
typo in `--profiles-dir` / `signalforge.yml` surfaces BEFORE the
estimate is computed (DEC-009 of
[`plans/super/36-estimate-cost-preview.md`](../plans/super/36-estimate-cost-preview.md)).
It then issues a small set of cheap calls to project the cost of the
billable pipeline that `signalforge generate` *without* `--estimate`
would perform:

- **Drafter half** — one token-count for the drafter prompt plus the
  per-criterion judge token-counts (`1 + len(rubric)` calls). The
  full `messages.create` LLM call is never invoked.
- **Warehouse half** — one BigQuery `dryRun` (or the warehouse's
  equivalent — see
  [`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md)) to
  project the bytes the prune step will scan, multiplied by the
  `3.5 tests/column` heuristic (DEC-012 of
  [`plans/super/36-estimate-cost-preview.md`](../plans/super/36-estimate-cost-preview.md)).

Output goes to stdout as plain text with three sections (Draft /
Grade / Warehouse) followed by totals and a footer listing the
price-table version
(`signalforge.llm.pricing.PRICE_TABLE_VERSION`).

`--estimate` reports a **billing ceiling** — actual scans usually
come in lower because cache hits, sampled rows, and shorter LLM
responses all trim the projected numbers. Treat the figure as a
calibration signal, not a billing guarantee (mirrors the
planner-estimate caveats in
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md) for
Snowflake `EXPLAIN`).

## Provider-aware token counting (issue #136 US-005)

The drafter and grader token counts are computed via the
`LLMProvider.estimate_input_tokens(model, text) -> int` ABC method
(issue #135's provider-neutral seam, extended in issue #136 DEC-003).
Each registered provider supplies its own implementation:

- **Anthropic (default)** — calls the SDK's
  `client.messages.count_tokens(...)` (one real round-trip per
  count). `ANTHROPIC_API_KEY` is required because this is a live API
  call, not a local computation (DEC-006 of #36).
- **OpenAI** — uses `tiktoken` locally (BPE tokeniser, no extra API
  round-trip — DEC-012 of #136). Resolves the model id via
  `tiktoken.encoding_for_model(model)` with a graceful `cl100k_base`
  fallback for unknown ids.

Anthropic stdout is byte-identical before and after the #136 refactor
— pinned via a snapshot test in `tests/cli/test_estimate.py` per
DEC-013. Selecting a different provider routes the token count
through that provider's strategy method without touching the
Anthropic path.

## OpenAI provider — `[openai]` install extra

Selecting `grade.provider: openai` and/or `llm.provider: openai` in
`signalforge.yml` requires the `openai` install extra so both the SDK
and `tiktoken` are available:

```bash
pip install signalforge-dbt[openai]
# or, in a contributor checkout
uv sync --dev   # the dev group already pulls openai + tiktoken
```

`tiktoken` is OpenAI's local BPE tokeniser (MIT-licensed, no native
build; wheels for CPython 3.11–3.13). It runs entirely client-side —
no API round-trip per count — which is the main reason the OpenAI
estimate path is meaningfully faster than the Anthropic one on
multi-criterion grading runs.

### Registered OpenAI pricing SKUs

`signalforge.llm.pricing._PRICES_MUTABLE` ships four OpenAI SKUs
(issue #136 US-004):

| Model id | Notes |
|---|---|
| `gpt-4o` | Default judge model for the OpenAI provider (DEC-004). |
| `gpt-4o-mini` | Budget tier — cheapest OpenAI SKU registered. |
| `gpt-4.1` | Newer flagship variant. |
| `gpt-4-turbo` | Back-compat for projects pinned to the prior generation. |

Each SKU carries `input_per_mtok` and `output_per_mtok` rates; the
cache fields are `0.0` because OpenAI's Chat Completions surface does
not expose Anthropic-style prompt caching (see
[`docs/grade-ops.md` § OpenAI provider](grade-ops.md#openai-provider)
and [`docs/draft-ops.md` § OpenAI provider](draft-ops.md#openai-provider)
for the no-cache cost note).

### `EstimateUnknownModelError` for unknown SKUs

Setting `grade.model` or `llm.model` to an id that is **not** in the
pricing table raises `EstimateUnknownModelError` from the
`--estimate` path at config-load resolution time (the live draft /
grade calls themselves still run; only `--estimate` requires a
pricing row). Common cases:

- A model id that hasn't been added to `_PRICES_MUTABLE` yet (file an
  issue with the public pricing for the SKU).
- A typo (e.g. `gpt-4o-min` instead of `gpt-4o-mini`).

Maps to CLI exit-code tier 2 (`INPUT`) — see
[`docs/cli-ops.md`](cli-ops.md) for the full exit-code taxonomy.

## Maintainer-only live smoke tests

Three `@pytest.mark.openai` gated tests exercise the OpenAI half of
the estimate path against the real API (DEC-008 of #136):

```bash
SF_RUN_OPENAI=1 OPENAI_API_KEY=sk-... uv run pytest -m openai --no-cov
```

The marker is excluded from the default CI run via
`addopts -m 'not openai'`. Both env vars are required (each missing
var produces a clear skip reason naming the var). Mirrors the
`@pytest.mark.anthropic` precedent:

```bash
ANTHROPIC_API_KEY=sk-... uv run pytest -m anthropic --no-cov
```

The Anthropic suite also includes the byte-identity snapshot for the
estimate stdout that DEC-013 of #136 pins as the refactor floor.

## References

- Design records:
  [`plans/super/36-estimate-cost-preview.md`](../plans/super/36-estimate-cost-preview.md)
  (the original `--estimate` design),
  [`plans/super/135-provider-neutral-llm-seam.md`](../plans/super/135-provider-neutral-llm-seam.md)
  (the provider-neutral seam),
  [`plans/super/136-openai-grading-provider.md`](../plans/super/136-openai-grading-provider.md)
  (the OpenAI provider + tiktoken estimate path).
- CLI flag reference: [`docs/cli-ops.md`](cli-ops.md) `--estimate`.
- Per-provider config / cost notes:
  [`docs/draft-ops.md`](draft-ops.md) and
  [`docs/grade-ops.md`](grade-ops.md) (OpenAI provider sections).
- Warehouse-side estimate (BigQuery `dryRun`, Snowflake `EXPLAIN
  USING JSON`):
  [`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md).
