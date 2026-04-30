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

- `signalforge.llm` — the centralized SDK seam. One function,
  `call_anthropic`, owns retry policy, prompt-cache pre-send checks,
  exception translation, and the `LLMResult` value object. No other
  module imports the `anthropic` SDK.
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
| `CandidateTest`         | type      | Discriminated union over `not_null` / `unique` / `accepted_values` / `relationships` test variants. Discriminator: `type`. |
| `DraftConfig`           | model     | Config-shaped (`extra="forbid"`) Pydantic model mirroring the `llm:` block of `signalforge.yml`.                       |
| `load_draft_config`     | function  | `load_draft_config(project_dir, path=None) -> DraftConfig`. Mirrors `load_safety_config`.                              |
| `LLMResponseEvent`      | model     | One JSONL audit record per LLM response. Fields are documented in [§4](#response-audit).                                |
| `DraftError`            | exception | Base class for every failure surface in this layer.                                                                    |
| `LLMOutputError`        | exception | Base for parse-time failures (JSON / validation / anchor contract). Carries the bad-JSON envelope.                     |

### `signalforge.llm.__all__`

| Name                     | Kind      | Description                                                                                                          |
| ------------------------ | --------- | -------------------------------------------------------------------------------------------------------------------- |
| `call_anthropic`         | function  | The single Anthropic `messages.create` seam. Owns retry policy + cache pre-send check. Returns `LLMResult`.          |
| `LLMResult`              | model     | Frozen result shape: `text_blocks`, `response_text`, token counts (input/output/cache_creation/cache_read), `model`, `prompt_version`, `raw_message`. |
| `LLMError`               | exception | Base class for everything in `signalforge.llm.errors`.                                                               |
| `LLMHelperError`         | exception | Umbrella for SDK-call failures. Subclasses cover the retry-taxonomy branches.                                        |
| `LLMAuthError`           | exception | 401 / 403 from the Anthropic API. No retry.                                                                          |
| `LLMRateLimitError`      | exception | 429 retry budget exhausted. Carries `attempts`.                                                                      |
| `LLMServerError`         | exception | 5xx retry budget exhausted.                                                                                          |
| `LLMConnectionError`     | exception | Connection / transport retry budget exhausted.                                                                       |
| `LLMCacheTooSmallError`  | exception | Pre-send: cached block is below the model's minimum cacheable size.                                                  |
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

## Cache behaviour

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
block and raises:

- `LLMCacheTooSmallError` if the block is below the model's minimum
  cacheable size (1024 tokens for Sonnet/Opus, 2048 for Haiku).
  Anthropic silently treats a sub-minimum cache marker as a no-op:
  the request still succeeds but the cache entry is never created,
  costing the input-token premium with none of the discount. Failing
  loud is the only way to surface this.
- `LLMCacheTooLargeError` if the block exceeds the SignalForge cap
  of 8000 input tokens. The cap is a SignalForge-imposed safeguard
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
ANTHROPIC_API_KEY=sk-... pytest -m anthropic
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
  model: claude-sonnet-4-6
  cheap_model: claude-haiku-4-5-20251001
  max_output_tokens: 4096
  cache_ttl: 5m              # one of "5m" | "1h"
  max_retries_429: 3
  max_retries_5xx: 1
  max_retries_conn: 1
```

Field-by-field:

- **`model`** — the Anthropic model id used by every `call_anthropic`
  invocation. Default `claude-sonnet-4-6`. Any string the SDK accepts
  is allowed.
- **`cheap_model`** — informational; not selected automatically.
  The CLI (#9) flips on `--cheap` to swap `model` for this value.
  Default `claude-haiku-4-5-20251001`.
- **`max_output_tokens`** — Anthropic `max_tokens` ceiling. Must be
  positive (validator).
- **`cache_ttl`** — `Literal["5m", "1h"]`. `"1h"` opts into the
  `extended-cache-ttl-2025-04-11` beta header at the LLM seam.
- **`max_retries_429` / `max_retries_5xx` / `max_retries_conn`** — see
  [§7 Retry taxonomy](#retry-taxonomy).

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
| `LLMCacheTooSmallError`        | Pre-send: cached block below the model's minimum (1024 / 2048 tokens).                     | DEC-024  |
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
