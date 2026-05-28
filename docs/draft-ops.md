# LLM draft pipeline — operations guide

Operational reference for users of `signalforge.draft` and
`signalforge.llm`. Companion to [`docs/safety-ops.md`](safety-ops.md),
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md), and
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md), and to the
design record in
[`plans/super/5-llm-draft-pipeline.md`](../plans/super/5-llm-draft-pipeline.md).

## Overview

The draft pipeline turns one dbt model into one `CandidateSchema` —
the typed value the prune layer (#6) consumes — by issuing one LLM
call. It sits **after** the safety layer (which produces the
`LLMRequest`) and **before** prune / grade / diff render
(#6 / #7 / #8). Two subpackages share the work (DEC-001):

- `signalforge.llm` — the centralized, provider-neutral LLM seam. One
  function, `call_llm`, owns the retry loop, backoff math, prompt-cache
  pre-send checks, and the `LLMResult` value object; a pluggable
  `LLMProvider` strategy (resolved from a process-level registry) owns
  the vendor-specific request build, response extraction, and
  exception classification (issue #135). The default provider is
  `anthropic`; its SDK noise (type-stub gaps, lazy exception-class
  import) stays confined to `signalforge.llm._anthropic_client`. No
  other module imports the `anthropic` SDK.
- `signalforge.draft` — the orchestration layer on top of that seam.
  Owns the prompt builder, the JSON + anchor-contract parser, the
  fail-closed response-audit JSONL writer, and the `draft_schema` /
  `draft_from_request` entry points.

The seam split keeps the SDK noise (type-stub gaps, retry plumbing,
exception-class lazy-import) confined to one subpackage; the rest of
the layer stays SDK-agnostic and pyright-clean.

## Public API surface

### `signalforge.draft.__all__`

| Name                    | Kind      | Description                                                                                                            |
| ----------------------- | --------- | ---------------------------------------------------------------------------------------------------------------------- |
| `draft_schema`          | function  | `draft_schema(model, adapter, policy, manifest, *, config) -> DraftOutcome`. End-to-end entry point: builds the safety-layer `LLMRequest`, renders the prompt, calls the LLM, parses, audits. |
| `draft_from_request`    | function  | `draft_from_request(request, model, manifest, *, config, audit_path) -> DraftOutcome`. Same pipeline minus the safety-layer step; takes a pre-built `LLMRequest`. |
| `DraftOutcome`          | model     | Frozen Pydantic value object: `(candidate, request, result)`. The thing every downstream stage receives.               |
| `CandidateSchema`       | model     | The parsed schema the LLM produced: `(name, description, columns, tests, …)`. Frozen, `extra="ignore"` for read-back tolerance. |
| `CandidateColumn`       | model     | One column on a `CandidateSchema`: `(name, description, rationale, tests, meta)`.                                      |
| `CandidateTest`         | type      | Discriminated union over `not_null` / `unique` / `accepted_values` / `relationships` / `custom_sql` test variants. Discriminator: `type`. |
| `CandidateTestCustomSQL`| model     | The fifth variant (issue #116): a custom singular SQL business-rule test. Carries `sql` (a failing-rows SELECT), optional `column`, optional `rationale`. See [§ Custom business-rule tests](#custom-business-rule-tests-custom_sql). |
| `DraftConfig`           | model     | Config-shaped (`extra="forbid"`) Pydantic model mirroring the `llm:` block of `signalforge.yml`.                       |
| `load_draft_config`     | function  | `load_draft_config(project_dir, path=None) -> DraftConfig`. Mirrors `load_safety_config`.                              |
| `LLMResponseEvent`      | model     | One JSONL audit record per LLM response. Fields are documented in [§4](#response-audit).                                |
| `DraftError`            | exception | Base class for every failure surface in this layer.                                                                    |
| `LLMOutputError`        | exception | Base for parse-time failures (JSON / validation / anchor contract). Carries the bad-JSON envelope.                     |

### `signalforge.llm.__all__`

| Name                     | Kind      | Description                                                                                                          |
| ------------------------ | --------- | -------------------------------------------------------------------------------------------------------------------- |
| `call_llm`               | function  | The single provider-neutral LLM seam. Owns retry policy + cache pre-send check; selects an `LLMProvider` strategy by name (default `"anthropic"`). Returns `LLMResult`. |
| `LLMResult`              | model     | Frozen result shape: `text_blocks`, `response_text`, token counts (input/output/cache_creation/cache_read), `model`, `prompt_version`, `raw_message`. |
| `LLMError`               | exception | Base class for everything in `signalforge.llm.errors`.                                                               |
| `LLMHelperError`         | exception | Umbrella for SDK-call failures. Subclasses cover the retry-taxonomy branches.                                        |
| `LLMAuthError`           | exception | 401 / 403 from the Anthropic API. No retry.                                                                          |
| `LLMRateLimitError`      | exception | 429 retry budget exhausted. Carries `attempts`.                                                                      |
| `LLMServerError`         | exception | 5xx retry budget exhausted.                                                                                          |
| `LLMConnectionError`     | exception | Connection / transport retry budget exhausted.                                                                       |
| `LLMCacheTooLargeError`  | exception | Pre-send: cached block exceeds the SignalForge cap (8000 input tokens).                                              |

`LLMResponseFormatError` is exported from
`signalforge.llm.errors` but not from the top-level `__all__`; reach
for it via `from signalforge.llm.errors import LLMResponseFormatError`.

## `DraftOutcome` shape

```python
class DraftOutcome(BaseModel):
    candidate: CandidateSchema
    request: LLMRequest
    result: LLMResult
```

Three fields, each load-bearing for a different downstream stage:

- **`candidate`** — the parsed `CandidateSchema` ready for the prune
  step (#6). The prune layer iterates `candidate.columns[*].tests` and
  `candidate.tests` to decide what to run against the warehouse.
- **`request`** — the safety-layer `LLMRequest` that was sent to the
  LLM. The audit log ties the request to a durable receipt; keeping
  the typed object on the outcome lets prune cross-check
  `columns_sent` / `redactions` without re-running the safety layer.
- **`result`** — the typed `LLMResult` with token usage,
  `prompt_version`, and `raw_message`. The grader (#7) reads
  `result.prompt_version` for incident-response queries; the diff
  renderer (#8) uses `result.cache_creation_input_tokens` /
  `result.cache_read_input_tokens` to surface cache economics in the
  per-run summary.

`DraftOutcome` is `frozen=True, extra="ignore"`: downstream stages
hold an outcome without worrying about post-construction mutation,
and forward-compat field additions don't break older readers.

## Response audit

> **Consumer guide.** For cross-stage joins, `jq` / pandas worked examples,
> the forward-compat policy, and the redaction surface, see
> [`docs/audits.md`](audits.md). This section is the draft-layer
> production contract.

Path: `audit_path.with_name("llm_responses.jsonl")` — adjacent to the
safety layer's `audit.jsonl`. Both audit streams share a parent
directory so the privacy boundary is uniform (DEC-006).

One JSONL record per successful LLM call. The writer mirrors
`signalforge.safety.audit` exactly: serialise → size-check (BEFORE any
file open) → `mkdir -p` parent at `0o700` → `os.open` with
`O_APPEND | O_CREAT | 0o600` → single `os.write` → `os.fsync` → close.

`LLMResponseEvent` fields:

| Field                          | Type                  | Meaning                                                                                                                          |
| ------------------------------ | --------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `timestamp`                    | ISO 8601 datetime     | UTC timestamp of the response.                                                                                                   |
| `model_unique_id`              | string                | dbt unique_id of the drafted model.                                                                                              |
| `prompt_version`               | 16 hex chars          | Deterministic blake2b digest of the prompt template content. See [§9](#prompt_version-cross-reference).                          |
| `response_text_hash`           | 16 hex chars          | blake2b digest of the LLM's raw response text. Reviewers correlate to a captured response by re-hashing the cleartext.           |
| `parsed_schema_hash`           | 16 hex chars          | blake2b digest of the canonicalised parsed `CandidateSchema` (sorted keys via `json.dumps`).                                     |
| `sent_sql_hash`                | 16 hex chars          | blake2b digest of the model SQL placed in the `<MODEL_SQL>` envelope. Detects prompt drift between runs.                          |
| `cache_creation_input_tokens`  | integer               | Tokens charged at 1.25× input pricing for cache writes.                                                                          |
| `cache_read_input_tokens`      | integer               | Tokens charged at 0.1× input pricing for cache reads.                                                                            |
| `input_tokens`                 | integer               | Total input tokens billed.                                                                                                       |
| `output_tokens`                | integer               | Total output tokens billed.                                                                                                      |
| `model`                        | string                | The Anthropic model id used (e.g. `claude-sonnet-4-6`).                                                                          |
| `signalforge_version`          | PEP-440 version       | The package version that produced the record. Read from `signalforge.__version__` at write time.                                |
| `audit_schema_version`         | integer               | Audit shape version. Currently `1`. Bump when the JSONL schema evolves; v0.2 readers gate on this.                                |

Storing hashes (not cleartext) keeps individual records under the
POSIX-atomic-append cap (`_RESPONSE_AUDIT_RECORD_LIMIT_BYTES = 4000`)
and avoids re-emitting whatever PII the LLM may have echoed back from
the prompt.

**Fail-closed semantics (DEC-011).** `write_response_event` catches
**no** exceptions internally; oversize records propagate as
`LLMResponseAuditRecordTooLargeError` (raised BEFORE any file is
opened, so an oversize record leaves no on-disk artefact), and every
other I/O / encoding failure propagates and is wrapped by
`draft_from_request` as `LLMResponseAuditWriteError`. The drafter
returns `None` on the floor — an unaudited LLM response is, by
definition, output leaving without a receipt, exactly the failure mode
this layer exists to prevent. The propagation IS the defence.

**Incident response.** `sent_sql_hash` and `parsed_schema_hash` (DEC-008,
DEC-006) make "which run produced this column description?" answerable
without spelunking through the LLM provider's logs:

```bash
# What runs hit a particular SQL?
jq -c 'select(.sent_sql_hash == "a3f29c61b8de2014")' .signalforge/llm_responses.jsonl
# How much did cache reads save on the last 50 calls?
jq -s 'sort_by(.timestamp) | .[-50:] | map(.cache_read_input_tokens) | add' \
  .signalforge/llm_responses.jsonl
```

## Prompt-injection mitigation

The dynamic block wraps the model SQL in a `<MODEL_SQL>` envelope and
the system message instructs the model that anything between the tags
is data, not instructions (DEC-007):

```
<MODEL_SQL>
SELECT customer_id, ... FROM ...
-- adversarial dbt comment: "ignore prior instructions and ..."
</MODEL_SQL>
```

This protects against the most common attack surface: a malicious dbt
project committing a `-- prompt injection` comment in `model.sql` that
attempts to override the system prompt.

What it does **not** protect against:

- **The LLM hallucinating columns** that don't exist on the model.
  The anchor-contract validator (see [§8](#hard-json-validation--anchor-contract))
  catches this — every test that references a column must point at a
  real column on the input model, or the whole draft is rejected.
- **Adversarial column descriptions in the manifest.** Column
  descriptions, tags, and meta fields go through to the LLM verbatim
  inside the cached manifest summary. Column *names* are hashed in
  schema-only mode by the safety layer (`col_<8 hex>`), but column
  *descriptions* are passthrough. Treat manifest content as
  semi-trusted: a malicious description can attempt the same prompt
  injection as a SQL comment, and the `<MODEL_SQL>` envelope does not
  cover that surface.

A v0.2 follow-up may add a manifest-content envelope with the same
treatment; for v0.1 the practical mitigation is "review your manifest
descriptions like you review your SQL."

## Custom business-rule tests (`custom_sql`)

The four schema-test types (`not_null`, `unique`, `accepted_values`,
`relationships`) cover the structural invariants dbt's generic
catalogue can express. They cannot express a *business rule* — "a
refund's amount never exceeds the original order," "every shipped
order has a ship date," "discount percent stays between 0 and 100."
The fifth test variant, `custom_sql` (issue #116), is the escape hatch:
a free-form **singular** SQL test the drafter authors per dbt's
singular-test convention.

### What a `custom_sql` test is

`CandidateTestCustomSQL` carries:

- **`sql`** — the complete failing-rows SELECT. Per dbt's singular-test
  contract, the query returns the rows that **violate** the rule: a
  non-empty result means the test failed. (Zero rows = pass.) This
  mirrors how the four built-in variants compile to an inner
  failing-rows SELECT — the prune adapter wraps every test in
  `SELECT COUNT(*) AS failures FROM (<sql>) AS t`.
- **`column`** — optional. A non-empty string scopes the rule to one
  column (and the test renders as `test.column.<col>.custom_sql` in the
  diff / grade artifact ids); `null` (the default) marks a model-level
  business-rule assertion.
- **`rationale`** — optional one-line "why," surfaced in the diff.

Like the four built-ins, `custom_sql` **can** be excluded via
`DraftConfig.exclude_tests` — it is a member of `VALID_TEST_TYPES`
(US-021). When `"custom_sql"` is in `exclude_tests`,
`_render_system_prompt` omits its JSON-shape illustration and drops it
from the `### SCOPE` line, so a cooperative LLM never proposes one; if
the LLM defies that, the parser's anchor-contract check rejects the
`custom_sql` candidate (defence in depth — prompt + parser). Otherwise
the system prompt appends the `custom_sql` JSON-shape illustration after
the (possibly filtered) four standard entries.

### Authoring rules via `meta.signalforge.business_rules`

You steer the drafter toward specific rules by declaring them in your
dbt model's `meta`. The drafter reads `meta.signalforge.business_rules`
at **both** the column level (`columns[*].meta.signalforge`) and the
model level (`config.meta.signalforge`). The value accepts two shapes:

- **A single natural-language string:**

  ```yaml
  models:
    - name: dim_customers
      config:
        meta:
          signalforge:
            business_rules: "lifetime_value must never be negative"
      columns:
        - name: discount_pct
          meta:
            signalforge:
              business_rules: "discount_pct stays between 0 and 100 inclusive"
  ```

- **A list of rules:**

  ```yaml
  config:
    meta:
      signalforge:
        business_rules:
          - "total_amount equals the sum of line-item amounts"
          - "every order with status='shipped' has a non-null ship_date"
  ```

When any rules are present, the drafter renders a `## BUSINESS RULES`
section into the prompt's data block (model-level rules first, then
per-column rules, columns sorted by name for byte-stable prompts;
duplicate rule strings are de-duplicated) and instructs the LLM to
draft **one `custom_sql` test per stated rule**, translating each
natural-language rule into a failing-rows SELECT.

Business-rule reading is **best-effort, never fail-loud.** A
`business_rules` value that isn't a `str` or `list` (a number, a
`dict`, `None`) yields no rules — the drafter still runs, and the
inferred-fallback path below covers the gap. Whitespace-only strings
collapse to nothing, so an empty `meta` value emits no section.

### Inferred fallback

You do **not** have to declare any rules. When no
`meta.signalforge.business_rules` are present, the system prompt still
permits the LLM to **infer** `custom_sql` tests from the model SQL and
the column profile *where a clear, checkable invariant exists* — e.g.
a `COALESCE`'d column that should never be null after the coalesce, or
a derived `_pct` column that the SELECT computes as a bounded ratio.
Declared rules are the high-precision path; inference is the
zero-config path. Both produce the same `custom_sql` variant.

### Jinja support inside `custom_sql.sql`

The drafted SQL **may** reference the model under draft and its
neighbours via the standard dbt-Jinja ref helpers:

- `{{ this }}` — the model under draft (resolves to its qualified
  warehouse table name at prune time).
- `{{ ref('<model>') }}` — a neighbour model.
- `{{ source('<src>', '<table>') }}` — a source table.

These three are resolved by a **bounded** resolver at prune time
(`signalforge.manifest.resolve_template_refs`) — there is no
Jinja *engine*. **Control-flow Jinja is unsupported:** `{% if %}` /
`{% for %}` blocks, `var()` / `env_var()` calls, and macro invocations
cannot be resolved. A `custom_sql` test carrying unsupported Jinja is
not dropped — the prune layer routes it to `kept-without-evidence` so a
reviewer sees it (see [`docs/prune-ops.md`](prune-ops.md#custom_sql-evaluation)).

### Worked example

A `dim_customers` model declares one model-level rule:

```yaml
models:
  - name: dim_customers
    config:
      meta:
        signalforge:
          business_rules: "total_amount must never be negative"
```

The drafter emits a `custom_sql` candidate roughly like:

```json
{
  "type": "custom_sql",
  "column": "total_amount",
  "sql": "select * from {{ this }} where total_amount < 0",
  "rationale": "total_amount is a non-negative monetary aggregate"
}
```

This candidate flows into the prune layer, which resolves `{{ this }}`
to the warehouse table, samples (single-table), and counts failing
rows. If the warehouse has zero negative-total rows the test is dropped
as `always-passes` (no signal); if it finds any, the test is `kept`
and surfaces as a proposed standalone `tests/dim_customers__total_amount_custom_sql_<hash>.sql`
file in the diff (see
[`docs/diff-ops.md`](diff-ops.md#sidecar-json-schema) and
[`docs/cli-ops.md`](cli-ops.md#signalforge-generate-model)).

## Cache behaviour

Prompt caching is a **provider capability** (issue #135): the seam
emits a `cache_control` marker, the `extended-cache-ttl-2025-04-11`
beta header, and the pre-send `count_tokens` gate only when the
selected `LLMProvider` reports `supports_prompt_caching` /
`supports_token_count`. A provider that supports neither simply reports
0 cache tokens and skips the marker. The default `anthropic` provider
supports both, so the behaviour below is unchanged.

Default `cache_ttl="5m"`; opt in to `"1h"` via `DraftConfig.cache_ttl`
(DEC-005, DEC-009). The `extended-cache-ttl-2025-04-11` beta header
is auto-set when `cache_ttl="1h"`; sending it for `"5m"` is at best
ignored, at worst a deprecation flag, so the seam only sets it when
required.

**What's cached.** The cached block contains the system message + a
manifest summary covering the model under draft + its direct
`refs` / `depends_on` neighbours only — **not** the full manifest
(DEC-009). The dynamic block (model SQL + sampled rows / aggregates)
sits outside the cache marker so each per-model call gets a fresh
billing.

**Hard cap and pre-send check.** Before any `messages.create` call,
the seam issues a `client.messages.count_tokens` against the cached
block and:

- **Drops** the `cache_control` marker from the request when the
  cached block is below the model's minimum cacheable size (1024
  tokens for Sonnet/Opus, 2048 for Haiku) and logs an `INFO` line.
  Anthropic silently treats a sub-minimum cache marker as a no-op,
  so the call would still succeed; dropping the marker explicitly
  avoids paying the count-tokens cost twice and silences the
  dual-zero cache-anomaly WARNING further down. Callers whose cached
  block is naturally below the minimum (e.g. the grade layer's
  compact rubric) get a clean run. _(Behaviour changed under issue
  #10 — previously raised `LLMCacheTooSmallError`.)_
- Raises `LLMCacheTooLargeError` if the block exceeds the SignalForge
  cap of 8000 input tokens. The cap is a SignalForge-imposed safeguard
  against accidental prompt bloat: a summary above the cap signals
  the prompt builder has drifted (e.g. embedded the full project
  manifest), and we'd rather fail loud than silently bloat every
  request.

Both errors fire **before** any `messages.create` call, so an oversize
or undersize block never leaves a billable footprint.

**Cache economics.** `DraftOutcome.result.cache_creation_input_tokens`
captures cache writes (charged at 1.25× input pricing), and
`cache_read_input_tokens` captures cache reads (charged at 0.1× input
pricing). Break-even is approximately 2 reads per write — a CLI run
that drafts ≥3 sibling models against the same neighbour subgraph
will recoup the cache write cost.

If the cached block was above the minimum (the pre-send check would
have raised otherwise) yet the response reports
`cache_creation_input_tokens == 0`, the seam emits a `WARNING` —
this can happen on Anthropic load-balancer rerouting or partial cache
miss, and surfaces so the operator knows the discount didn't land.

## Retry taxonomy

Exponential backoff with bounded ±25% jitter. Each retry emits a
`WARNING` log carrying `attempt`, `delay`, `error_class`, `model`
(DEC-004):

```
delay = 2**attempt * _rand_uniform(0.75, 1.25)
```

| Branch                  | Default budget | Carries on exhaustion       | Notes                                                          |
| ----------------------- | -------------- | --------------------------- | -------------------------------------------------------------- |
| 429 (rate limit)        | 3 retries      | `LLMRateLimitError`         | Tunable via `DraftConfig.max_retries_429`.                     |
| 5xx (server)            | 1 retry        | `LLMServerError`            | Tunable via `DraftConfig.max_retries_5xx`.                     |
| Connection / transport  | 1 retry        | `LLMConnectionError`        | Tunable via `DraftConfig.max_retries_conn`.                    |
| 401 / 403 (auth)        | 0 retries      | `LLMAuthError`              | Short-circuits — retrying won't fix a missing/invalid API key. |
| 4xx other (400/404/422) | 0 retries      | `LLMHelperError`            | The request is malformed in some way the SDK didn't reject locally. |

Module-level aliases `_sleep` (default `time.sleep`) and
`_rand_uniform` (default `random.uniform`) let tests reassign for
deterministic backoff without monkey-patching `asyncio.sleep` or
`random` globally. The clauditor pattern is the model — see the
`tests/llm/` directory for the deterministic-stand-in pattern.

To dial down for batch CLI mode (post-#9), set the retry knobs in
`signalforge.yml`:

```yaml
llm:
  max_retries_429: 1
  max_retries_5xx: 0
  max_retries_conn: 0
```

## Hard JSON validation + anchor-contract

The parser validates two layers of the LLM's response (DEC-003):

1. **JSON shape.** Bad JSON raises `LLMOutputJSONError` with
   `parse_position=(line, col)` derived from the
   `json.JSONDecodeError`, plus a ±80-char `excerpt` window with a
   `⟨HERE⟩` sentinel at the offending offset. Pydantic-validation
   failures (wrong field types, missing required fields) raise
   `LLMOutputValidationError` with the underlying `ValidationError`
   on `cause`.
2. **Anchor contract.** Every column referenced by a candidate test
   must exist on the input model; tests across the whole draft must
   not duplicate `(test_type, column)` parameterless pairs; per-column
   tests must reference the parent column. Violations raise
   `LLMOutputAnchorContractError` with all collected violations on
   `violations: tuple[str, ...]`.

Whole-draft fail-loud: a single anchor-contract violation rejects the
entire candidate. Partial schemas never escape the parser. The full
response text stays on `LLMOutputError.raw_text` for the audit /
incident workflow (US-012); `__str__` truncates to 4 KB to keep log
lines readable.

```python
try:
    outcome = draft_schema(model, adapter, policy, manifest, config=config)
except LLMOutputAnchorContractError as exc:
    for v in exc.violations:
        print(f"  - {v}")
    # exc.raw_text preserved for forensic replay
    raise
```

## `prompt_version` cross-reference

`prompt_version` is a deterministic 16-hex-char blake2b digest of the
prompt template content. Surfaces at four points (DEC-022, DEC-015):

- `LLMResult.prompt_version` — the value object the seam returns.
- `LLMResponseEvent.prompt_version` — the audit JSONL record.
- Every `LLMOutputError`-shaped exception's envelope.
- A `DEBUG: prompt_version=...` log emitted on success
  (`signalforge.draft.schema._LOGGER`).

Use this for incident-response queries. Two records with identical
`prompt_version` came from the same prompt template; a mismatch
indicates a template change between runs and is the first thing to
check when comparing draft quality across versions:

```bash
jq -r '.prompt_version' .signalforge/llm_responses.jsonl | sort -u
```

## Real-API smoke test

```bash
ANTHROPIC_API_KEY=sk-... pytest -m anthropic --no-cov
```

The `anthropic` marker is excluded from the default CI run via
`addopts = -m 'not anthropic'`. Tests under that marker need a real
`ANTHROPIC_API_KEY` env var; without one they are skipped at collection.

The current smoke test (US-015) is a "wire test" that proves SDK auth
+ transport but stops at the cache-size pre-send check — the smoke
fixture's cached block is below the model minimum (intentionally
small). The full pipeline runs once the smoke fixture grows past the
1024 / 2048 minimums; for v0.1 the wire test is sufficient evidence
that the SDK seam authenticates and transports correctly.

Default count: 608 passed, 7 deselected. The deselected count is the
seven `@pytest.mark.anthropic` tests across the LLM and draft
subpackages.

## Configuration

Top-level namespace key for this layer is `llm:` (DEC-027). Other
top-level keys (`safety:`, `prune:`, `grade:`, …) are reserved for
other stages and silently ignored by the draft loader.

```yaml
# signalforge.yml
llm:
  provider: anthropic        # registry-validated; only "anthropic" registered today
  model: claude-sonnet-4-6
  cheap_model: claude-haiku-4-5-20251001
  max_output_tokens: 4096
  cache_ttl: 5m              # one of "5m" | "1h"
  max_retries_429: 3
  max_retries_5xx: 1
  max_retries_conn: 1
  exclude_tests: []          # subset of [not_null, unique, accepted_values, relationships, custom_sql]
```

Field-by-field:

- **`provider`** — the LLM provider strategy name (issue #135 DEC-007),
  resolved against the `signalforge.llm.providers` registry and threaded
  into `call_llm` from `draft_schema`. Default `"anthropic"`. An unknown
  value fails loud at config-load, listing the registered provider
  names. Deliberately a registry-validated `str`, not a `Literal` — the
  provider registry is a forward-looking plugin point (#136 OpenAI /
  #137 Gemini register more providers); today only `anthropic` is
  registered.
- **`model`** — the model id used by every `call_llm` invocation.
  Default `claude-sonnet-4-6`. Any string the SDK accepts is allowed.
- **`cheap_model`** — informational; not selected automatically.
  The CLI (#9) flips on `--cheap` to swap `model` for this value.
  Default `claude-haiku-4-5-20251001`.
- **`max_output_tokens`** — Anthropic `max_tokens` ceiling. Must be
  positive (validator).
- **`cache_ttl`** — `Literal["5m", "1h"]`. `"1h"` opts into the
  `extended-cache-ttl-2025-04-11` beta header at the LLM seam.
- **`max_retries_429` / `max_retries_5xx` / `max_retries_conn`** — see
  [§7 Retry taxonomy](#retry-taxonomy).
- **`exclude_tests`** — list of dbt test types the drafter must not
  propose (issue #54; extended to `custom_sql` in US-021 of #116).
  Each entry must be one of the five `VALID_TEST_TYPES` — `not_null`,
  `unique`, `accepted_values`, `relationships`, `custom_sql`; an
  unknown value fails loud at config-load. Default `[]` (all five
  allowed). When non-empty the system prompt's test catalogue +
  `### SCOPE` line drop the excluded types (including `custom_sql`'s
  JSON-shape illustration) AND the parser rejects any defiant LLM
  output via `LLMOutputAnchorContractError`. Excluding every type is
  a config error (the drafter has nothing to propose). The prompt
  version hash rotates per exclusion set so Anthropic's prompt cache
  invalidates correctly. Useful for teams that find `accepted_values`
  noisy on enum columns, want to defer `relationships` to a manual
  review pass, or want to suppress `custom_sql` business-rule drafting
  entirely (issue #116); see
  [§ Custom business-rule tests](#custom-business-rule-tests-custom_sql).

`DraftConfig` uses `extra="forbid"` (DEC-011, mirroring
`safety-layer.md` DEC-015). Typos like `mdoel:` instead of `model:`
fail loud rather than silently no-op. The outer file wrapper uses
`extra="ignore"` so reserved top-level keys (`safety:` etc.) don't
trip the strict validator.

If `signalforge.yml` is missing entirely (or the `llm:` key is absent),
`load_draft_config(project_dir)` returns the built-in defaults
silently — same behaviour as `load_safety_config`.

## Error hierarchy

### `signalforge.llm.errors`

| Class                          | When raised                                                                                | DEC      |
| ------------------------------ | ------------------------------------------------------------------------------------------ | -------- |
| `LLMError`                     | Base class; never raised directly.                                                         | DEC-022  |
| `LLMHelperError`               | Umbrella for SDK-call failures. Subclasses cover the retry-taxonomy branches.              | DEC-004  |
| `LLMAuthError`                 | 401 / 403 from Anthropic. No retry.                                                        | DEC-004  |
| `LLMRateLimitError`            | 429 retry budget exhausted. Carries `attempts`.                                            | DEC-004  |
| `LLMServerError`               | 5xx retry budget exhausted.                                                                | DEC-004  |
| `LLMConnectionError`           | Connection / transport retry budget exhausted.                                             | DEC-004  |
| `LLMResponseFormatError`       | SDK returned 200 but the response object is missing a required attribute (`content`, `usage`). | DEC-004  |
| `LLMCacheTooLargeError`        | Pre-send: cached block above the SignalForge 8000-token cap.                               | DEC-009  |

### `signalforge.draft.errors`

| Class                                  | When raised                                                                            | DEC      |
| -------------------------------------- | -------------------------------------------------------------------------------------- | -------- |
| `DraftError`                           | Base class; never raised directly.                                                     | DEC-022  |
| `LLMOutputError`                       | Base for parse-time failures. Carries the bad-JSON envelope.                           | DEC-003  |
| `LLMOutputJSONError`                   | LLM response was not valid JSON. Auto-derives `parse_position` from the `JSONDecodeError`. | DEC-003  |
| `LLMOutputValidationError`             | Response parsed as JSON but didn't match the `CandidateSchema` shape.                  | DEC-003  |
| `LLMOutputAnchorContractError`         | Response cited columns / duplicates / parent-column mismatches that violate the anchor contract. | DEC-003  |
| `DraftConfigNotFoundError`             | Explicit `path=` to `load_draft_config` pointed at a missing file.                     | DEC-011  |
| `DraftConfigInvalidError`              | `signalforge.yml` `llm:` block failed schema validation.                               | DEC-011  |
| `LLMResponseAuditRecordTooLargeError`  | Audit record exceeded the POSIX-atomic-append size cap (4000 bytes).                   | DEC-006  |
| `LLMResponseAuditWriteError`           | Fail-closed audit-write failure. Wraps the underlying exception on `cause`.            | DEC-011  |

Every error carries a class-level `default_remediation` that the base
`__str__` renders on a separate `↳ Remediation:` line. The
remediation pattern operationalises Architectural Commitment #5
(explainable diffs) at the LLM layer's failure surface; pattern-match
on type rather than sniffing message text.

## References

- Design record: [`plans/super/5-llm-draft-pipeline.md`](../plans/super/5-llm-draft-pipeline.md).
- Safety-layer counterpart (the layer the draft pipeline mirrors most
  patterns from): [`docs/safety-ops.md`](safety-ops.md) and
  [`.claude/rules/safety-layer.md`](../.claude/rules/safety-layer.md).
- Manifest reader conventions
  (frozen / `extra="ignore"` / drift detector pattern):
  [`.claude/rules/manifest-readers.md`](../.claude/rules/manifest-readers.md).
- Warehouse adapter conventions (the adapter seam pattern this layer
  mirrors for the SDK shim):
  [`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md).

Cross-reference DECs: DEC-001 (subpackage split), DEC-003 (typed
`CandidateSchema` + anchor contract), DEC-004 (retry taxonomy),
DEC-005 / DEC-009 (cache TTL + 8000-token cap), DEC-006 (response
audit), DEC-007 (`<MODEL_SQL>` envelope), DEC-008 (`sent_sql_hash`),
DEC-011 (fail-closed audit + propagation IS the defence), DEC-013
(AST scan for `LLMResponseEvent` construction), DEC-015 (lazy-format
JSON in loggers), DEC-016 (`LLMResult` provenance fields), DEC-017
(default `model` / `cheap_model` / etc.), DEC-022 (`prompt_version`),
DEC-024 (pre-send `count_tokens`), DEC-027 (`llm:` namespace).
