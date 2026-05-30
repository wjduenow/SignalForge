# LLM usage & providers

Where SignalForge calls an LLM, which providers are supported, and
how to pick one. The deep-dive companion to the brief mention in
[the README](../README.md) and the per-stage ops references
([`draft-ops.md`](draft-ops.md), [`grade-ops.md`](grade-ops.md),
[`cost-estimate-ops.md`](cost-estimate-ops.md)).

> Issue [#134](https://github.com/wjduenow/SignalForge/issues/134)
> shipped the pluggable provider epic
> ([#135](https://github.com/wjduenow/SignalForge/issues/135) +
> [#136](https://github.com/wjduenow/SignalForge/issues/136) +
> [#137](https://github.com/wjduenow/SignalForge/issues/137)). Before
> #134, the LLM seam was Anthropic-only.

## Where LLMs are (and aren't) used

SignalForge is a five-stage pipeline; exactly **two** stages issue
LLM calls:

| Stage | Module | LLM calls per `signalforge generate <model>` |
|---|---|---|
| **Manifest loader** | `signalforge.manifest` | 0 — deterministic JSON parse. |
| **Safety layer** | `signalforge.safety` | 0 — redacts PII before the drafter ever runs. |
| **Drafter** | `signalforge.draft` | **1** — drafts `schema.yml` + tests + docs from the model SQL. |
| **Prune engine** | `signalforge.prune` | 0 — compiles candidate tests to SQL, runs them against the warehouse. |
| **Grader** | `signalforge.grade` | **N × M** — one judge call per `(artifact × rubric criterion)`. Default 4 criteria; ~12 artifacts on a typical staging model → ~48 calls. |
| **Diff renderer** | `signalforge.diff` | 0 — renders the kept/dropped/flagged tier table + unified diff. |
| **Ingest layer** | `signalforge.ingest` | 0 — reads existing `schema.yml` / `tests/*.sql` for `prune-existing`. |

`signalforge prune-existing` issues **zero** LLM calls — it skips the
drafter and the grader entirely and runs warehouse-only pruning over
your already-authored tests.

`signalforge lint` issues zero LLM calls and makes no warehouse
calls — it loads `signalforge.yml` and the dbt manifest and reports
typos / missing keys offline.

`signalforge generate --estimate` issues a small number of cheap
calls per provider to project cost (one `count_tokens` per prompt,
plus a warehouse `dryRun`); the full `messages.create` call is never
invoked. See [`cost-estimate-ops.md`](cost-estimate-ops.md).

### Two independent provider knobs

The drafter and grader resolve their providers separately:

```yaml
# signalforge.yml
llm:
  provider: anthropic       # drafter — one call per `generate` run
grade:
  provider: gemini          # grader — N × M calls per `generate` run
```

Common pattern: **Anthropic drafter, Gemini grader** — the drafter
call benefits from Anthropic's prompt caching (the cached manifest
summary is read across siblings within a `--select` batch), while the
grader fan-out runs against Gemini's cheaper per-token rates. See
[Choosing a provider](#choosing-a-provider) below.

## The provider-neutral seam

Issue [#135](https://github.com/wjduenow/SignalForge/issues/135)
replaced the Anthropic-bound `call_anthropic` helper with a
provider-neutral `call_llm` orchestrator. The shape:

```text
signalforge.llm
├── client.py            — call_llm (retry loop, backoff, budgets, logs, LLMResult assembly)
├── providers.py         — LLMProvider ABC + register_provider + provider_for
├── _anthropic_client.py — SDK shim; every `# pyright: ignore` for anthropic confined here
├── _openai_client.py    — SDK shim; every `# pyright: ignore` for openai + tiktoken confined here
├── _gemini_client.py    — SDK shim; every `# pyright: ignore` for google-genai confined here
└── cost/                — pricing table + `rollup_audit_dir` for post-run USD tallies
```

`call_llm` owns the generic machinery — retry loop with
`(2 ** attempt) * uniform(0.75, 1.25)` backoff, per-class budgets,
WARNING/INFO logs, `LLMResult` assembly. It dispatches the
vendor-specific bits (request shape, response parsing, exception
classification, token counting) to an `LLMProvider` strategy
resolved from the registry. Capability flags
(`supports_prompt_caching`, `supports_token_count`) govern whether
the orchestrator attaches a `cache_control` marker or runs the
pre-send `count_tokens` gate.

Adding a fourth provider is a one-file shim + a `LLMProvider`
subclass + `register_provider("<name>", <class>)`. The drafter and
grader pick it up automatically; no edits to `call_llm`, no edits to
`DraftConfig`/`GradeConfig` (provider is a registry-validated `str`,
not a `Literal`). See [Adding a provider](#adding-a-provider) below.

## Capability matrix

| Capability | Anthropic | OpenAI | Gemini |
|---|---|---|---|
| **Install** | base (`pip install signalforge-dbt`) | `pip install signalforge-dbt[openai]` | `pip install signalforge-dbt[gemini]` |
| **Env var** | `ANTHROPIC_API_KEY` | `OPENAI_API_KEY` | `GOOGLE_API_KEY` |
| **Drafter (`llm.provider`)** | ✅ default | ✅ | ✅ |
| **Grader (`grade.provider`)** | ✅ default | ✅ | ✅ |
| **`--estimate` integration** | ✅ live `messages.count_tokens` | ✅ local `tiktoken` | ✅ live `models.count_tokens` |
| **Prompt caching** | ✅ `cache_control` (5m / 1h tiers) | ❌ no Chat Completions caching tier | ❌ explicit caching deferred |
| **Server-side JSON mode** | n/a (Anthropic parser tolerant) | ✅ `response_format={"type":"json_object"}` | ✅ `response_mime_type="application/json"` |
| **Pre-send `count_tokens` gate** | ✅ | ❌ (no SDK token-count API) | ❌ (deferred — Gemini has the API but we don't gate on it for cache parity) |
| **`cache_ttl` config** | honoured (`"5m"` / `"1h"`) | silently ignored | silently ignored |
| **Default model** | `claude-sonnet-4-6` (drafter + grader) | `gpt-4o` | drafter unset; grader `gemini-2.5-flash` |
| **Live smoke marker** | `@pytest.mark.anthropic` | `@pytest.mark.openai` | `@pytest.mark.gemini` |
| **Live smoke env** | `ANTHROPIC_API_KEY` | `SF_RUN_OPENAI=1` + `OPENAI_API_KEY` | `SF_RUN_GEMINI=1` + `GOOGLE_API_KEY` |

A `❌` on prompt caching does **not** mean the provider is unusable —
it means every drafter call ships the full system + cached_block
without a read discount. For a one-call-per-`generate` drafter this
is modest; for the multi-call grader, budget per-call input-token
spend at full rates.

## Supported providers

### Anthropic (default)

The shipped default for both stages. No extra install; set
`ANTHROPIC_API_KEY` and SignalForge runs out of the box.

- **Default models:** `claude-sonnet-4-6` (drafter + grader),
  `claude-haiku-4-5-20251001` (drafter `cheap_model`).
- **Prompt caching:** active. Drafter caches the manifest summary
  block; grader caches the rubric criterion list. `cache_ttl: 1h`
  opts into the `extended-cache-ttl-2025-04-11` beta header. The
  drafter's cached prefix is amortised across siblings within a
  single `--select` batch but **NOT** across process boundaries —
  the cache lives in Anthropic's infrastructure.
- **`--estimate`:** one live `messages.count_tokens` round-trip per
  prompt. Reports a billing ceiling.
- **Pricing SKUs:** `claude-sonnet-4-6`, `claude-opus-4-7`,
  `claude-haiku-4-5` (4-tier rate: input / output / cache-write 5m /
  cache-read).
- **Reference:** [`docs/draft-ops.md`](draft-ops.md) for the drafter
  configuration block, retry taxonomy, and prompt-injection
  envelope. [`docs/grade-ops.md`](grade-ops.md) for the grader's
  per-criterion fan-out and the `<ARTIFACT>` envelope.

### OpenAI

Registered by issue
[#136](https://github.com/wjduenow/SignalForge/issues/136). Select
via `llm.provider: openai` and/or `grade.provider: openai`.

```yaml
# signalforge.yml
llm:
  provider: openai
  model: gpt-4o            # default; any model id the SDK accepts is allowed
  max_output_tokens: 4096
grade:
  provider: openai
  model: gpt-4o
```

- **Install:** `pip install signalforge-dbt[openai]` — pulls
  `openai>=1.40,<3.0` plus `tiktoken` for local `--estimate` token
  counting.
- **Env var:** `OPENAI_API_KEY`.
- **Prompt caching:** none. `OpenAIProvider.supports_prompt_caching`
  is `False`; the orchestrator skips the `cache_control` marker and
  the pre-send `count_tokens` gate. `cache_ttl` in `signalforge.yml`
  is accepted but silently ignored. The grader's 48-call fan-out
  ships the full system + rubric block on every call.
- **Server-side JSON:** active. `OpenAIProvider.build_create_kwargs`
  attaches `response_format={"type": "json_object"}`; the tolerant
  `extract_json_payload` parser remains as defence-in-depth.
- **`--estimate`:** local `tiktoken` (no extra API round-trip per
  count). `tiktoken.encoding_for_model(model)` with a graceful
  `cl100k_base` fallback for unknown ids.
- **Pricing SKUs:** `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4-turbo`
  (cache fields zero — no discount tier).
- **`.messages.create` adapter:** OpenAI's SDK exposes
  `client.chat.completions.create(...)`; the SignalForge shim wraps
  it in a `_OpenAIClientAdapter.messages` namespace so the
  orchestrator's vendor-neutral call shape (`llm_client.messages.create(...)`)
  works unchanged.
- **Reference:** [`docs/draft-ops.md` § OpenAI provider](draft-ops.md#openai-provider)
  · [`docs/grade-ops.md` § OpenAI provider](grade-ops.md#openai-provider)
  · [`docs/cost-estimate-ops.md` § OpenAI provider](cost-estimate-ops.md#openai-provider--openai-install-extra).

### Google Gemini

Registered by issue
[#137](https://github.com/wjduenow/SignalForge/issues/137). Select
via `llm.provider: gemini` and/or `grade.provider: gemini`.

```yaml
# signalforge.yml
llm:
  provider: gemini
  model: gemini-2.5-flash    # mid-tier; gemini-2.5-pro and gemini-2.0-flash are also registered
  max_output_tokens: 4096    # see Gemini truncation note below
grade:
  provider: gemini
  model: gemini-2.5-flash
  max_output_tokens: 4096
```

- **Install:** `pip install signalforge-dbt[gemini]` — pulls
  `google-genai>=0.5,<1`.
- **Env var:** `GOOGLE_API_KEY` (read by the SDK; SignalForge never
  logs it).
- **Prompt caching:** none. v0.3 ships without Anthropic-style prompt
  caching (`supports_prompt_caching=False`). Explicit Gemini context
  caching is a tracked follow-up.
- **Server-side JSON:** active.
  `GeminiProvider.build_create_kwargs` sets
  `response_mime_type="application/json"` on the
  `GenerateContentConfig`.
- **`--estimate`:** native `client.models.count_tokens(...)` — one
  extra API round-trip per estimate call. Distinct from OpenAI's
  local `tiktoken` path because Gemini has no equivalent client-side
  BPE.
- **Pricing SKUs:** `gemini-2.5-pro` (flagship, base ≤200K-context
  tier), `gemini-2.5-flash` (mid-tier; default judge),
  `gemini-2.0-flash` (budget). Cache fields zero.
- **`.messages.create` adapter:** `google-genai`'s native surface is
  `client.models.generate_content(...)`; the SignalForge shim wraps
  it in a `_GeminiClientAdapter.messages` namespace so the
  orchestrator's call shape is unchanged. The SDK ships as a
  namespace package (`from google import genai`), confined by an AST
  scan to `_gemini_client.py` only.
- **Truncation / non-clean finish_reason handling:**
  `LLMProvider.is_clean_completion(response)` (issue
  [#155](https://github.com/wjduenow/SignalForge/issues/155))
  raises `LLMResponseFormatError` when Gemini's `finish_reason` is
  anything but `STOP` — including `MAX_TOKENS` with partial text,
  `SAFETY`, `RECITATION`, `OTHER`. The grader wraps the result as
  `GradeLLMError` and degrades the affected pair to
  `score=None, passed=False, reasoning="call failed: GradeLLMError: <inner finish_reason message>"`.
  The aggregate `GradingReport.aggregate_complete=False` flags the
  partial report.
- **Recommended `max_output_tokens` floor: 4096.** Gemini's
  reasoning style is verbose; smaller ceilings observed truncating
  mid-string (issue [#155](https://github.com/wjduenow/SignalForge/issues/155)
  DEC-008, issue [#158](https://github.com/wjduenow/SignalForge/issues/158)).
  Treat the figure as fixture-scale-dependent: validate with a full
  run and bump if any pair degrades.
- **Reference:** [`docs/draft-ops.md` § Gemini provider](draft-ops.md#gemini-provider)
  · [`docs/grade-ops.md` § Gemini provider](grade-ops.md#gemini-provider)
  · [`docs/cost-estimate-ops.md`](cost-estimate-ops.md) for Gemini's
  `count_tokens` integration.

## Prompt caching — what it is and why providers differ

The capability matrix marks Anthropic with a ✅ for prompt caching
and OpenAI / Gemini with a ❌. That's the most consequential row in
the table for anyone budgeting a real workload, so it earns its own
section.

### What prompt caching is (business framing)

Every LLM call bills you for **every input token, every time** —
even if 90% of the prompt is boilerplate you sent five seconds ago
on the previous call. Prompt caching is the provider's offer: tell
me which chunk of the prompt is the *stable prefix*, I'll fingerprint
it on my side, and on subsequent calls within a TTL window I'll
charge you a steep discount on those tokens instead of the full
input rate.

Anthropic's published rates for `claude-sonnet-4-6` (SignalForge's
default; the figures live in
[`signalforge/llm/pricing.py`](https://github.com/wjduenow/SignalForge/blob/dev/src/signalforge/llm/pricing.py)):

| Token class | Rate (USD / Mtok) | vs. full input |
|---|---|---|
| Full input | $3.00 | baseline |
| Cache **write** (first call seeds the cache) | $3.75 | 25% premium |
| Cache **read** (later calls within TTL hit the cache) | $0.30 | 90% discount |

You pay a small premium once to seed the cache, then 10¢ on the
dollar for every read. Break-even is roughly one follow-up call;
from the 2nd call onward you're saving money.

### Where this matters in SignalForge

- **Drafter** — one LLM call per `signalforge generate` invocation.
  The cached prefix is the system prompt + the manifest summary.
  Inside a single `--select` batch of 20 models, calls 2–20 hit the
  cache. Modest but real savings.
- **Grader** — ~48 LLM calls on a typical model (~12 artifacts × 4
  rubric criteria; see
  [`grade-ops.md` § One LLM call per artifact × criterion](grade-ops.md)).
  The cached prefix is the rubric criterion list. Once per run the
  rubric is "written" to the cache; the other ~47 calls "read" it
  at 90% off. This is where prompt caching pays for itself
  loudest.

### Why OpenAI isn't supported

OpenAI's Chat Completions API has an automatic prompt-caching
feature, but it's deliberately opaque:

1. **No marker, no control surface.** Anthropic exposes an inline
   `cache_control` marker on the block you want cached; OpenAI's
   backend pattern-matches recent prompts and applies discounts
   silently. SignalForge cannot *steer* the cache toward the
   system + cached-block prefix that matters.
2. **No public per-MTok rate.** Anthropic publishes a cache-write
   premium and a cache-read discount, so
   `signalforge.llm.pricing.PRICES` can model both. OpenAI's
   discount tier isn't published in the same shape, so
   `--estimate` cannot project caching savings honestly.
3. **No `cache_creation_input_tokens` / `cache_read_input_tokens`
   in the usage response.** Anthropic returns both fields per call,
   which feeds the dual-zero cache-anomaly WARNING and the audit
   JSONL's reproducibility hashes. OpenAI's usage shape carries no
   equivalent — even *measuring* whether a cache hit fired after
   the fact is awkward.

So `OpenAIProvider.supports_prompt_caching = False` is the honest
posture: we can't steer the cache, we can't price the cache, and we
can't audit the cache. Whatever automatic discount OpenAI applies
on their side, the operator gets — SignalForge just doesn't model
it. `cache_ttl` in `signalforge.yml` is accepted and silently
ignored for OpenAI.

### Why Gemini isn't supported (yet)

Gemini ships **context caching** — a real, usable feature — but its
shape is fundamentally different from Anthropic's inline marker:

1. **Separate API call.** You call `CachedContent.create(...)`
   before the generation call to upload the chunk to a named cache
   resource, get back a handle, and reference the handle on every
   subsequent `generate_content(...)`. Anthropic's `cache_control`
   marker is a single field inside the existing
   `messages.create(...)` call.
2. **Separate pricing dimension.** Gemini bills cache **storage**
   per hour while the cached resource lives, plus a per-token
   discount on cached reads. The cost model is "rent the cache
   slot, get cheaper reads" — fundamentally different from
   Anthropic's "pay a write premium once, get cheap reads."
3. **Minimum payload + lifecycle.** Gemini's cached content has a
   minimum size and an explicit TTL the operator manages; an
   abandoned cache keeps billing storage until it expires.

Wiring Gemini context caching correctly into SignalForge means a
separate "warm the cache" code path, a TTL strategy, an
`LLMResult.usage` shape that includes Gemini's distinct cache
fields, and a `pricing.py` schema extension carrying the storage
rate alongside the existing per-MTok rates. None of that was in
scope for issue
[#137](https://github.com/wjduenow/SignalForge/issues/137) (which
landed Gemini as a third provider proving the seam holds). It's a
tracked follow-up — `GeminiProvider.supports_prompt_caching = False`
today; `cache_ttl` is accepted and silently ignored.

The asymmetry isn't "Gemini is worse than Anthropic"; it's
"Anthropic's caching maps onto the existing seam in one config line
(`cache_ttl: 5m | 1h`), and Gemini's caching needs a code path we
haven't built yet."

### The practical bottom line

On the grader (the high-call-count surface where caching matters
most), the math typically nets out:

- **Anthropic** — 1 cache-write rubric + ~47 cache-read rubrics at
  10¢-on-the-dollar input tokens.
- **Gemini `gemini-2.5-flash`** — 48 full-input rubrics, but each
  input token costs ~10× less than `claude-sonnet-4-6`.
- **OpenAI `gpt-4o-mini`** — 48 full-input rubrics, input tokens
  ~20× cheaper than `claude-sonnet-4-6`.

Cheaper-per-token providers usually still come out ahead on
absolute dollars even without caching — they're just leaving
optimization on the table that Anthropic doesn't. If Gemini
context caching lands as a follow-up, Gemini becomes substantially
cheaper still.

## Choosing a provider

Three dimensions to weigh:

1. **Per-token cost.** At PR-prep prices
   ([`signalforge/llm/pricing.py`](https://github.com/wjduenow/SignalForge/blob/dev/src/signalforge/llm/pricing.py)),
   the cheapest grader SKU is `gpt-4o-mini`
   ($0.15 / $0.60 per Mtok in/out), followed by `gemini-2.0-flash`
   ($0.10 / $0.40 per Mtok in/out — even cheaper, but a budget
   model). `claude-sonnet-4-6` at $3 / $15 per Mtok costs ~20× more
   per token, partly offset by prompt caching on the cached
   prefix.
2. **Caching impact.** Anthropic's prompt-cache discount (5m or 1h
   TTL) is meaningful for the drafter's cached manifest block AND
   for the grader's cached rubric block when grading multiple
   artifacts per criterion. Neither OpenAI nor Gemini exposes a
   comparable discount today — every call pays the full input rate.
3. **Quality variance.** Gemini's verbose reasoning needs the 4096
   `max_output_tokens` floor to avoid mid-string truncation
   (issue #155); OpenAI's tighter `response_format` JSON mode is the
   simplest route to a clean parse but has shown lower judge
   evidence quality on some fixtures. Anthropic's judge model
   carries the longest production exposure (the v0.1–v0.2 e2e
   smokes all ran Anthropic).

**Practical patterns:**

- **Single-provider Anthropic** (the default). Simplest setup, best
  caching, highest unit cost. Right answer when you don't want to
  manage two API keys.
- **Anthropic drafter + Gemini grader.** Drafter benefits from
  caching across `--select` siblings; grader fan-out runs at the
  cheaper per-token rate. The shipped end-to-end smoke fixture
  exercises this configuration (issue
  [#155](https://github.com/wjduenow/SignalForge/issues/155)).
- **OpenAI both stages.** Right answer when you're already
  standardised on OpenAI billing and don't want to manage an
  Anthropic key. `gpt-4o` + `gpt-4o-mini` are inexpensive and
  reliable; no caching discount but no truncation risk either.
- **Gemini both stages.** Cheapest per-token; needs the 4096
  `max_output_tokens` floor and a tolerance for occasional
  `aggregate_complete=False` reports when the safety filter or
  truncation fires.

## Cost & token accounting

`signalforge generate --estimate` projects per-stage cost before the
billable run. Each provider supplies its own token counter via
`LLMProvider.estimate_input_tokens(model, text, *, system="", client=None)`:

- **Anthropic** — live `messages.count_tokens`; `system` is passed
  as its own kwarg so the system envelope is counted server-side.
- **OpenAI** — local `tiktoken` (no API round-trip);
  `tiktoken.encoding_for_model(model)` with `cl100k_base` fallback.
- **Gemini** — native `models.count_tokens`; `system + text`
  concatenated into one `contents` entry (Gemini doesn't
  distinguish a system envelope).

`signalforge.llm.cost.rollup_audit_dir(project_dir) -> CostReport`
walks `.signalforge/llm_responses.jsonl` and `.signalforge/grade.jsonl`
after a run and computes per-provider per-model USD against the
frozen `signalforge.llm.pricing.PRICES` table. The CLI wrapper is
`scripts/measure_e2e_cost.py`. See
[`docs/cost-estimate-ops.md`](cost-estimate-ops.md) for the full
contract, including the `<unavailable: <ErrorClass>>` degrade when a
supplementary surface fails.

## Reliability & error handling

### Retry taxonomy (drafter + grader)

Every provider classifies SDK exceptions through
`LLMProvider.classify_exception(exc) -> ExceptionCategory` (one of
`AUTH`, `RATE_LIMIT`, `SERVER_ERROR`, `CONNECTION`, `NO_RETRY`).
`call_llm` runs the same generic retry loop regardless of provider:
429 × 3, 5xx × 1, connection × 1, 401/403 no-retry-but-hint, 4xx
no-retry. Each retry emits one `WARNING` with `attempt` / `delay`
/ `error_class` / `model`. Per-class budgets are configurable per
stage (`DraftConfig.max_retries_429` /
`GradeConfig.max_retries_429`).

### Conservative degrade (grader)

A grade pair that exhausts retries, hits a non-clean `finish_reason`,
or trips its per-pair budget routes through the conservative
`score=None, passed=False, reasoning="<failure reason>"` degrade —
never aborts the whole run. The aggregate
`pass_rate` / `mean_score` are computed over the **scored** subset
only; `aggregate_complete: bool` flags partial reports. The whole
run aborts **only** if the audit JSONL writer itself fails. See
[`docs/grade-ops.md` § Conservative score-and-degrade taxonomy](grade-ops.md).

### Fail-loud (drafter)

The drafter has no equivalent degrade path — a single failing LLM
call is a hard failure. The retry loop runs; on exhaustion, the
CLI exits at tier 3 (Anthropic / external dependency failure) with
a typed error message and no traceback. See
[`docs/draft-ops.md` § Retry taxonomy](draft-ops.md#retry-taxonomy).

### Prompt-injection envelopes

User-controlled content (model SQL for the drafter, drafted artifact
text for the grader) is wrapped in named fences:

- Drafter: `<MODEL_SQL>...</MODEL_SQL>` and
  `<BUSINESS_RULE id="N">...</BUSINESS_RULE>` (the latter for
  `meta.signalforge.business_rules`, see
  [`docs/draft-ops.md` § Custom business-rule tests](draft-ops.md#custom-business-rule-tests-custom_sql)).
- Grader: `<ARTIFACT>...</ARTIFACT>`.

A payload containing the literal closing tag raises
`PromptEnvelopeBreachError` / `GradePromptEnvelopeBreachError`
BEFORE the LLM call is issued — a fail-loud pre-flight scan over
every payload at orchestrator entry. The defence is a boring
substring match; no whitespace / case normalisation.

## Audit & reproducibility

Every LLM call lands a structured record on disk. Both writers are
fail-closed: the call only "succeeds" once the audit byte hits disk
via `os.write` + `os.fsync`. An audit-write failure aborts the run
with a typed `LLMResponseAuditWriteError` / `GradeAuditWriteError`
(CLI tier 3).

| File | Layer | Shape |
|---|---|---|
| `.signalforge/audit.jsonl` | safety | One record per `build_llm_request` — what data went to the LLM (columns sent, redactions applied, sampling mode). |
| `.signalforge/llm_responses.jsonl` | drafter | One record per drafter call — `sent_sql_hash`, `parsed_schema_hash`, `response_text_hash`, `prompt_version`, cache token usage, model id, `signalforge_version`. |
| `.signalforge/grade.jsonl` | grader | One record per `(artifact × criterion)` pair — `rubric_hash`, `prompt_version_template`, `criterion_prompt_hash`, `response_text_hash`, scored / degraded state. |
| `.signalforge/grade.json` | grader | End-of-run sidecar; `GradingReport` with per-criterion scores + the `aggregate_complete` flag. |
| `.signalforge/diff.json` | diff | Sidecar with the kept/dropped/flagged tier table + unified diff + reproducibility hashes. |

The reproducibility hash fields make per-run output bytewise
verifiable: same input → same hashes → same decisions. See
[`docs/audits.md`](audits.md) for the per-file schemas and
correlation patterns.

## Adding a provider

The seam is designed for plug-in extension. A new vendor needs four
artifacts:

1. **SDK shim** at `src/signalforge/llm/_<vendor>_client.py`. Every
   `# pyright: ignore` / `# type: ignore` for the vendor SDK is
   confined here. Expose a `_<Vendor>ClientProtocol` duck-typed at
   `messages.create` and (optionally) `messages.count_tokens`. AST
   scan in `tests/test_audit_completeness.py` enforces the
   confinement.
2. **`LLMProvider` subclass** at `src/signalforge/llm/providers.py`.
   Implement `make_client`, `build_create_kwargs`,
   `build_count_tokens_kwargs`, `extract_text_blocks`,
   `extract_usage`, `classify_exception`, `is_clean_completion`,
   `unclean_finish_reason_message`, `estimate_input_tokens`, plus
   the capability flags. The orchestrator (`call_llm`) gates
   behaviour on those flags — never name-branch.
3. **`register_provider("<name>", <class>)`** so
   `DraftConfig.provider` / `GradeConfig.provider` validators
   accept the new key. The provider config field is a
   registry-validated `str`, not a `Literal`, so no churn in two
   places.
4. **Pricing SKUs** in `signalforge.llm.pricing._PRICES_MUTABLE` for
   `--estimate` integration, and a `[<vendor>]` extra in
   `pyproject.toml` so users opt in to the SDK weight.

The shipped Anthropic / OpenAI / Gemini providers are the
worked-examples — each landed as a self-contained slice without
touching `call_llm`. See `llm-drafter.md` (the rules file) for the
load-bearing conventions (capability-gated behaviour, server-side
JSON modes where available, namespace-package SDK considerations).

## Reference

- [`docs/draft-ops.md`](draft-ops.md) — drafter configuration,
  retry taxonomy, prompt-injection envelopes, per-provider sections.
- [`docs/grade-ops.md`](grade-ops.md) — grader configuration,
  conservative degrade taxonomy, per-criterion fan-out,
  per-provider sections.
- [`docs/cost-estimate-ops.md`](cost-estimate-ops.md) —
  `--estimate` semantics, per-provider token counters, pricing
  table, `EstimateUnknownModelError`.
- [`docs/audits.md`](audits.md) — fail-closed audit JSONLs and
  sidecars across every stage.
- [`docs/safety-ops.md`](safety-ops.md) — what data goes to the
  LLM (PII redaction, sampling modes).
- Issues:
  [#134 epic](https://github.com/wjduenow/SignalForge/issues/134) ·
  [#135 provider-neutral seam](https://github.com/wjduenow/SignalForge/issues/135) ·
  [#136 OpenAI](https://github.com/wjduenow/SignalForge/issues/136) ·
  [#137 Gemini](https://github.com/wjduenow/SignalForge/issues/137) ·
  [#155 Gemini truncation + per-provider e2e gap](https://github.com/wjduenow/SignalForge/issues/155) ·
  [#158 Gemini grader `MAX_TOKENS` floor](https://github.com/wjduenow/SignalForge/issues/158).
