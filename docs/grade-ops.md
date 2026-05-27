# Grade layer — operations guide

Operational reference for users of `signalforge.grade`. Companion to
[`docs/safety-ops.md`](safety-ops.md),
[`docs/draft-ops.md`](draft-ops.md),
[`docs/prune-ops.md`](prune-ops.md),
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md), and
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md), and to the
design record in
[`plans/super/7-quality-grader.md`](../plans/super/7-quality-grader.md).

The grade layer sits between the prune layer (#6) and the diff renderer
(#8). Every drafted artefact that survives prune — column descriptions,
column rationales, model description, model rationale, and per-test
rationales — is scored by an LLM-as-judge against a configurable rubric
through one entry point: `signalforge.grade.grade_artifacts`. The
orchestrator issues one LLM call per `(artifact, criterion)` pair,
parses each response, writes a fail-closed JSONL audit record per
decision, and at end-of-run persists a sidecar JSON `GradingReport`.

This is the load-bearing operationalisation of Architectural Commitment
\#2 in [`CLAUDE.md`](../CLAUDE.md) — **evaluation in the loop**:
SignalForge generates AND grades; competitors only generate.

## Default posture

The grader is **report-only by default**. A below-threshold rubric does
not fail the run; the operator's diff surfaces the verdict and the
operator decides. Operators that want hard-fail-on-threshold behaviour
opt in by setting `fail_on_below_threshold: true` in `signalforge.yml`
(see [Threshold-fail behaviour](#threshold-fail-behaviour) below).

The layer is fail-closed on the audit-write boundary: any I/O error
from `GradeEvent` JSONL persistence or from the end-of-run sidecar
write aborts the run as `GradeAuditWriteError` (DEC-006 / DEC-012,
mirrors safety's DEC-011, draft's DEC-006, and prune's DEC-016). The
layer is conservative on the per-pair boundary: an LLM retry-exhausted,
parser-failed, or budget-exceeded pair lands as a degraded
`GradingResult(score=None, passed=False, reasoning="…")` rather than
silently dropped (DEC-015) — the diff renderer flags partial aggregates
explicitly so operators don't mistake a degraded-path mean for a real
one.

User-facing tagline: **every drafted artefact that ships is scored;
every scored artefact has a durable receipt; partial runs surface as
partial, not silent.**

## Public API

Import from `signalforge.grade`. The 18 names exported by `__all__`:

### Orchestrator

- **`grade_artifacts(model, candidate, prune_result, *, rubric=None, config=None, audit_path=None, sidecar_path=None, client=None, project_dir=None) -> GradingReport`** — End-to-end orchestrator. Validates the rubric, scans every artefact payload for the prompt-envelope close tag, iterates every `(criterion, artefact)` pair, issues one LLM-judge call per pair, writes one JSONL audit record per decision, builds the aggregate report, writes the sidecar JSON. Mirrors `signalforge.draft.draft_schema` and `signalforge.prune.prune_tests` so the CLI / wrapper layers see one consistent end-to-end shape across pipeline stages. `project_dir` defaults to `Path.cwd()`. `audit_path` defaults to `<project_dir>/.signalforge/grade.jsonl`. `sidecar_path` defaults to `<project_dir>/.signalforge/grade.json`.

### Result shapes

- **`GradingReport`** — Aggregate verdict for one model. Frozen Pydantic model with fields `grade_schema_version: Literal[1]`, `signalforge_version: str`, `run_id: str`, `timestamp: datetime`, `duration_seconds: float`, `model_unique_id: str`, `rubric_hash: str`, `thresholds: tuple[float, float]`, `results: tuple[GradingResult, ...]`. Computed properties: `pass_rate`, `mean_score`, `aggregate_complete`, `passed` — all derived from `results` so a `GradingReport` reconstructed from the sidecar carries identical views to a freshly produced one. Custom `__repr__` collapses to identity + aggregate counts (DEC-022 of #6) so accidental `_LOGGER.warning("report: %s", report)` does not dump multi-paragraph evidence into log sinks.

- **`GradingResult`** — One verdict per `(artifact, criterion)` pair. Carries `artifact_id: str` (canonical dotted-path per DEC-009), `criterion_id: str`, `score: float | None` (the `None` sentinel is the DEC-015 degraded path), `passed: bool`, `evidence: str`, `reasoning: str`. Computed property `one_line_why` returns the first sentence of `reasoning` capped at 120 characters — the diff renderer (#8) consumes this directly so display logic stays in the data layer. Custom `__repr__` omits `evidence` and `reasoning` to protect against accidental log dumps.

### Configuration

- **`GradeConfig`** — User-facing knobs. Frozen Pydantic model with `extra="forbid"` (config-shaped per `safety-layer.md` DEC-015 — typos fail loud). Field reference: see [Configuration](#configuration-signalforgeyml-grade-block) below.

- **`load_grade_config(project_dir, path=None) -> GradeConfig`** — Loads the `grade:` block from `signalforge.yml`. Resolves to `<project_dir>/signalforge.yml` when `path` is `None`. Returns defaults when the file is missing, empty, or the `grade:` key is absent. Raises `GradeConfigError` on parse / schema failures. Mirrors `load_safety_config` / `load_draft_config` / `load_prune_config` so the CLI sees one calling convention across stages.

### Rubric shapes

- **`Criterion`** — One rubric entry: `id: str` plus `criterion: str` (the prompt text sent to the LLM-judge). Frozen, `extra="forbid"` (DEC-017) so a typo like `weight: 1.0` in user-authored rubric YAML fails loud. Both fields required, non-empty, non-whitespace-only.

- **`Rubric`** — `TypeAlias` over `tuple[Criterion, ...]` (DEC-011). Deliberately not a wrapper class; mirrors `PruneResult.decisions: tuple[PruneDecision, ...]`.

- **`DEFAULT_RUBRIC`** — `Final[Rubric]` carrying the four locked criteria from DEC-016 verbatim: `clarity`, `consistency`, `rationale`, `no-redundant`. The IDs and exact criterion text are load-bearing for `rubric_hash` reproducibility — every change is a hash change, which means audit records written under v0.1 are no longer reproducible. Bump `audit_schema_version` before changing any of this in v0.2.

- **`GradeThresholds`** — Per-rubric pass/fail thresholds. `min_pass_rate: float = 0.7`, `min_mean_score: float = 0.5` (defaults match DEC-016). Both bounded `[0.0, 1.0]` inclusive. The `GradingReport.thresholds` tuple carries the active values forward into the sidecar.

### Audit

- **`GradeEvent`** — One JSONL audit record per LLM-judge call. Constructed ONLY by `signalforge.grade.audit._build_grade_event` (US-009 AST scan target — sixth of the AST-gated event types). `extra="ignore"` for forward-compat read-back. See [Audit JSONL schema](#audit-jsonl-schema) for the field set.

### Discriminator literal

- **`GradeOutputViolationType`** — `Literal["json_parse", "missing_required_field", "missing_criterion_id", "criterion_id_mismatch", "score_out_of_range", "score_not_a_number", "passed_not_a_bool", "unknown_artifact_id", "ambiguous_artifact_id"]`. Closed taxonomy carried by `GradeOutputError.violation_type` so audit-log consumers / orchestrator branches can pattern-match exhaustively rather than sniff message text.

### Errors

`from signalforge.grade import errors`. Every exception subclasses
`GradeError` and carries a class-level `default_remediation` rendered
on a `↳ Remediation:` line by `__str__`.

- **`GradeError`** — Base class. Never raised directly.
- **`GradeConfigError`** — `signalforge.yml` `grade:` block failed parse or schema validation.
- **`GradeRubricError`** — Rubric YAML structurally invalid (duplicate `id`, empty rubric, malformed criterion entry).
- **`GradeLLMError`** — One-level adapter wrapping `signalforge.llm.LLMError`. The original error is preserved on `__cause__` and exposed via the `cause` attribute.
- **`GradeBudgetExceededError`** — `total_budget_seconds` tripped before any criterion was graded (a hard "the run did nothing" failure). A partial run completes normally with a `GradingReport` whose `aggregate_complete` flag is `False`.
- **`GradePromptEnvelopeBreachError`** — Artefact payload contained the literal `</ARTIFACT>` close tag. Refuses to render rather than ship a degraded envelope. Mirrors the drafter's `PromptEnvelopeBreachError` (#5 DEC-007).
- **`GradeOutputError`** — LLM-judge response failed parse or anchor-contract validation. Carries `violation_type: GradeOutputViolationType`.
- **`GradeAuditWriteError`** — Fail-closed audit-write failure (`OSError` / `PermissionError` / encoding / `fsync` / symlink containment). Aborts the run; original cause exposed via `.cause` and `__cause__`.
- **`GradeAuditRecordTooLargeError`** — Serialised JSONL line (or sidecar JSON document) exceeded the size cap. Raised BEFORE any file is opened so an oversize record leaves no on-disk artefact.

## Configuration: `signalforge.yml` `grade:` block

Top-level namespace is `grade:` (claimed per the convention from
`safety-layer.md` DEC-025 / `llm-drafter.md` DEC-027 / `prune-engine.md`
DEC-020 — every pipeline stage gets one top-level key). Sibling keys
(`safety:`, `llm:`, `prune:`, future `diff:` …) are reserved for other
stages and silently ignored by the grade loader.

The full schema (every knob, every default, all v0.1 types), mirroring
`tests/fixtures/grade/example_config.yml` (exercised by
`test_load_grade_config_doc_example_round_trips` so the example and the
loader cannot drift):

```yaml
# signalforge.yml — grade stage configuration (v0.1)
grade:
  provider: anthropic             # registry-validated; only "anthropic" registered today
  model: claude-sonnet-4-6        # model id (default)
  cache_ttl: 1h                   # Prompt-cache TTL ('5m' or '1h')
  max_output_tokens: 256          # Per-criterion JSON response cap
  max_retries_429: 3              # Rate-limit retry budget
  max_retries_5xx: 1
  max_retries_conn: 1
  total_budget_seconds: 300       # Wall-clock budget across the whole run
  min_pass_rate: 0.7              # Aggregate threshold: fraction of passed criteria
  min_mean_score: 0.5             # Aggregate threshold: mean score across criteria
  fail_on_below_threshold: false  # opt-in hard-fail; default report-only
  # rubric:                       # Optional override; omitted = use DEFAULT_RUBRIC
  #   - id: clarity
  #     criterion: "..."
```

A minimal `signalforge.yml` is just `grade: {}` (or no `grade:` key at
all) — every field has a locked default from DEC-023..DEC-027 and the
loader returns `GradeConfig()` silently. A customised example that
overrides the rubric:

```yaml
grade:
  model: claude-sonnet-4-6
  total_budget_seconds: 600
  min_pass_rate: 0.8
  rubric:
    - id: clarity
      criterion: >
        Is the column description clear, specific, and actionable?
    - id: precision
      criterion: >
        Does the description state exactly what is captured (units,
        timezone, encoding) without hand-waving?
    - id: jargon
      criterion: >
        Is the description free of acronyms or domain-jargon a
        downstream analyst could not look up in five seconds?
```

Field-by-field:

- **`provider`** — The LLM provider strategy name (issue #135 DEC-007), resolved against the `signalforge.llm.providers` registry and threaded into `call_llm` from the per-criterion judge call, independently of the drafter's `DraftConfig.provider`. Default `"anthropic"`. An unknown value fails loud at config-load, listing the registered provider names. Deliberately a registry-validated `str`, not a `Literal` — the provider registry is a forward-looking plugin point (#136 OpenAI / #137 Gemini register more providers); today only `anthropic` is registered.
- **`model`** — The model id used by every per-pair judge call. Default `claude-sonnet-4-6`. Mirrors `DraftConfig.model` default. Haiku 4.5 is documented as a v0.2 cost-conscious option but not exposed in v0.1.
- **`cache_ttl`** — `Literal["5m", "1h"]`. Default `"1h"` (vs. the drafter's `"5m"`) because 60 sequential per-criterion calls under retry backoff can stretch beyond a 5-minute window; `"1h"` gives margin at no extra cost (cache writes are one-shot regardless of TTL).
- **`max_output_tokens`** — Per-criterion judge response cap. Default `256`. The expected JSON response is ~150 tokens; 256 gives 2× safety. Independent of `DraftConfig.max_output_tokens`.
- **`max_retries_429` / `max_retries_5xx` / `max_retries_conn`** — Per-call retry budgets at the centralised, provider-neutral `signalforge.llm.call_llm` seam (#5 DEC-012; #135 DEC-005). Defaults `3 / 1 / 1` mirror `DraftConfig`; dial down for batch CLI mode where one retry-exhaustion is preferable to dozens of stalled calls.
- **`total_budget_seconds`** — Whole-run wall-clock budget. Default `300` (5 minutes — ~3× safety on 60 calls × 1s p50). Mirrors `PruneConfig.total_budget_seconds` semantics: when the budget trips, every remaining `(artefact, criterion)` pair lands as a degraded `GradingResult(score=None)` rather than silently dropped. **Crucially** the LLM-layer retry budget does NOT count against this — `total_budget_seconds` is a top-of-loop wall-clock check; an in-flight call is allowed to complete before the next iteration's check fires.
- **`min_pass_rate`** — Floor on the fraction of `(artefact, criterion)` pairs that scored `passed=True` for the rubric to count as passed overall. Default `0.7`. Bounded `[0.0, 1.0]`. Mirrors `GradeThresholds.min_pass_rate`.
- **`min_mean_score`** — Floor on the mean numeric score across non-null verdicts. Default `0.5`. Bounded `[0.0, 1.0]`. Mirrors `GradeThresholds.min_mean_score`.
- **`fail_on_below_threshold`** — Hard-fail switch for the aggregate threshold check. Default `false` — v0.1 ships report-only posture by default. When `true`, `grade_artifacts(...)` raises `GradeBelowThresholdError` once the aggregate `GradingReport.passed` is `False` (`pass_rate < min_pass_rate` and/or `mean_score < min_mean_score`). The raise lands AFTER the sidecar JSON is durably persisted so the operator has a complete `grade.json` for diagnosis. See [Threshold-fail behaviour](#threshold-fail-behaviour) below for the full ordering invariant. Graduated from v0.2 reservation to v0.1 wiring in #9 (US-002).
- **`rubric`** — Optional rubric override. `None` (the default) means the orchestrator falls back to `DEFAULT_RUBRIC`. When provided, must be a non-empty list of mappings, each with non-empty `id` and `criterion` strings; duplicate `id` values raise `GradeRubricError`. Override is **wholesale**, not merge.

Unknown keys under `grade:` raise `GradeConfigError` (Pydantic
`extra="forbid"`). Typos like `mdoel:` or `total_budget_secnds:` fail
loud at load time rather than silently no-op'ing.

## Threshold-fail behaviour

Default posture is report-only — `grade_artifacts(...)` always returns a
`GradingReport`, and the operator inspects `report.passed` /
`report.pass_rate` / `report.mean_score` to decide what to do with the
verdict. Setting `fail_on_below_threshold: true` in `signalforge.yml`
opts the run into hard-fail behaviour: when the aggregate report does
not meet **both** `min_pass_rate` AND `min_mean_score`,
`grade_artifacts(...)` raises `GradeBelowThresholdError` instead of
returning the report.

The raise lands **after** the fail-closed sidecar write
(`write_grading_report(...)`) and the per-pair JSONL audit are durably
on disk, and **before** `grade_artifacts(...)` returns. Order is
load-bearing (DEC-021):

1. Iterate every `(criterion, artefact)` pair → write one
   `grade.jsonl` line per decision.
2. Build the aggregate `GradingReport` from the per-pair results.
3. Write the `grade.json` sidecar (fail-closed; `O_TRUNC` overwrite).
4. Emit the single INFO log per invocation (`grade completed: …`).
5. **If** `fail_on_below_threshold=True` AND `report.passed=False`,
   raise `GradeBelowThresholdError` carrying `pass_rate`, `mean_score`,
   `min_pass_rate`, `min_mean_score`, `aggregate_complete`.
6. Otherwise return the report.

Pinned by `tests/grade/test_engine.py::test_grade_below_threshold_writes_sidecar_before_raising`
— a threshold-fail run leaves a complete `grade.json` on disk so the
operator can diagnose **why** the run fell below threshold (which
criterion failed, which artefact's score dragged the mean down, the
full evidence/reasoning text). Raising before the sidecar would defeat
the durable hand-off; the test catches the raise then asserts the
sidecar exists and round-trips through `GradingReport.model_validate_json`.

`GradeBelowThresholdError` carries the five aggregate fields so a
caller catching the error can render a diagnostic without reaching back
to the report:

```python
from signalforge.grade import GradeBelowThresholdError, grade_artifacts

try:
    report = grade_artifacts(
        model, candidate, prune_result,
        config=load_grade_config(project_dir),
        project_dir=project_dir,
    )
except GradeBelowThresholdError as exc:
    # Sidecar JSON is on disk at <project_dir>/.signalforge/grade.json.
    log.error(
        "grade below threshold: pass_rate=%.3f (min %.3f), mean_score=%.3f (min %.3f)",
        exc.pass_rate, exc.min_pass_rate, exc.mean_score, exc.min_mean_score,
    )
    sys.exit(2)
```

The CLI (#9) wires the raise into its `INPUT` exit-code tier (exit 2);
see [`docs/cli-ops.md`](cli-ops.md) for the full exit-code table once
US-009 lands.

## Decision matrix

Per-pair scoring is a `[0.0, 1.0]` float plus an explicit
`passed: bool`. The judge prompt (US-005) instructs the model to emit
both — the score is the granular signal, and `passed` is the model's
own pass/fail call against the criterion's intent. The diff renderer
(#8) rescales the float to a 0–5-star display at render time; the data
layer stays in clauditor shape (DEC-002).

| Verdict shape | `score` | `passed` | What it means | Display in diff (#8) |
|---|---|---|---|---|
| Strong pass | `0.8`–`1.0` | `True` | Artefact meets the criterion clearly. | 4–5 stars. |
| Weak pass | `0.5`–`0.79` | `True` | Meets the criterion with caveats; reasoning calls them out. | 2–3 stars. |
| Weak fail | `0.2`–`0.49` | `False` | Falls short; the artefact ships only because it survived prune. | 1–2 stars. |
| Strong fail | `0.0`–`0.19` | `False` | Material problem; reviewer should rewrite or remove. | 0–1 star. |
| Degraded | `null` | `False` | Could not evaluate (LLM retry exhausted, parser failed, total budget tripped). DEC-015 sentinel. | "—" (no stars); `aggregate_complete: false` flag fires. |

**Aggregate semantics (DEC-002 — clauditor's threshold-AND pattern).**
The `GradingReport.passed` computed field is `True` iff **both**:

- `pass_rate >= thresholds[0]` (default `0.7`) — `pass_rate` is the
  mean of `passed` over results with a non-null `score`.
- `mean_score >= thresholds[1]` (default `0.5`) — `mean_score` is the
  mean of `score` over results with a non-null score.

**AND, not OR.** A rubric where every criterion is a soft pass at
`score=0.55` averages well above the mean-score floor but might still
fail the pass-rate floor (if those `0.55` scores came in as
`passed=False`). The two thresholds defend two different failure modes:
`min_pass_rate` catches "a few catastrophic failures masked by many
soft passes"; `min_mean_score` catches "many tepid passes that cluster
just above the bool boundary."

**Degraded-path skip (DEC-015).** Both aggregate computations skip
results where `score is None`. A criterion's retry-exhaustion does not
silently lower the `pass_rate` of the criteria that did run
successfully — but `aggregate_complete` flips to `False`, so the diff
renderer flags the report as partial.

## Audit JSONL schema

> **Consumer guide.** For cross-stage joins (including grade JSONL ↔
> grade sidecar ↔ diff sidecar on `artifact_id`), `jq` / pandas worked
> examples, the forward-compat policy, and the redaction surface, see
> [`docs/audits.md`](audits.md). This section is the grade-layer
> production contract.

Every per-pair `GradingResult` produces exactly one JSONL record at
`audit_path` (default `<project_dir>/.signalforge/grade.jsonl`). One
record per line; atomic concurrent appends via
`O_APPEND | O_CREAT | 0o600` and a single `os.write` (looped on short
returns) followed by `os.fsync` (DEC-006). The fourth instance of the
convention across the codebase — mirrors `signalforge.safety.audit`,
`signalforge.draft.audit`, and `signalforge.prune.audit`.

`GradeEvent` fields (~19 total):

| Field                          | Type                              | Meaning                                                                                          |
| ------------------------------ | --------------------------------- | ------------------------------------------------------------------------------------------------ |
| `audit_schema_version`         | integer (`Literal[1]`)            | Audit shape version. Currently `1`. Bump only on shape change; `extra="ignore"` handles additions. |
| `signalforge_version`          | PEP-440 version string            | Package version that produced the record.                                                        |
| `run_id`                       | 32-hex-char string                | Single `uuid4().hex` per `grade_artifacts` invocation (DEC-020). Repeated on every JSONL record AND on the sidecar so JSONL → sidecar correlation never depends on timestamp ranges. |
| `timestamp`                    | ISO-8601 UTC datetime             | When the per-call decision was finalised. Distinct from the sidecar's `started_at`.              |
| `model_unique_id`              | string                            | dbt `unique_id` of the graded model.                                                             |
| `artifact_id`                  | string (canonical dotted-path)    | DEC-009 canonical shape — see [Artefact-id format](#artefact-id-format) below.                   |
| `criterion_id`                 | string                            | The `Criterion.id` the judge scored against. Stable across artefacts of one run.                 |
| `score`                        | float `[0.0, 1.0]` or `null`      | Numeric verdict. `null` is the DEC-015 degraded sentinel.                                        |
| `passed`                       | bool                              | The judge's own pass/fail call. `False` for every degraded record by construction.               |
| `evidence`                     | string                            | The judge's quoted-fragment evidence pulled from the artefact text. Empty for degraded records.  |
| `reasoning`                    | string                            | The judge's free-text rationale. Empty for degraded records other than the leading "call failed" / "grade budget exceeded" descriptor. |
| `rubric_hash`                  | 16 hex chars                      | `blake2b(canonical_rubric_json, digest_size=8).hexdigest()` (DEC-010). Carried on every record AND the sidecar. |
| `prompt_version_template`      | 16 hex chars                      | `blake2b-8` of the system prompt + cached rubric block + envelope tag. Constant across all criteria of one run. |
| `criterion_prompt_hash`        | 16 hex chars                      | `blake2b-8` of the per-criterion prompt fragment. Stable across artefacts of one run.            |
| `response_text_hash`           | 16 hex chars                      | `blake2b-8` of the raw LLM response text. Empty string for degraded records (no response text to hash). |
| `model`                        | string                            | The Anthropic model id used for the call (e.g. `claude-sonnet-4-6`).                             |
| `input_tokens`                 | integer                           | Total input tokens billed for the call. `0` for degraded records.                                |
| `output_tokens`                | integer                           | Total output tokens billed. `0` for degraded records.                                            |
| `cache_creation_input_tokens`  | integer                           | Tokens charged at 1.25× input pricing for cache writes. `0` on cache-read-only calls.            |
| `cache_read_input_tokens`      | integer                           | Tokens charged at 0.1× input pricing for cache reads.                                            |

### Artefact-id format

`artifact_id` is a canonical dotted-path string (DEC-009). Six shapes
the formatter emits — the same shapes the resolver in
`signalforge.grade.prompts.extract_artifact_text` consumes:

- `column.<col>.description` — column documentation.
- `column.<col>.rationale` — column rationale.
- `model.description` — model documentation.
- `model.rationale` — model rationale.
- `test.column.<col>.<test.type>` — column-scoped test (e.g. `test.column.email.not_null`).
- `test.model.<test.type>` (or `test.model.<test.type>.<args_hash>`) — model-level test. The `args_hash` (8-hex `blake2b-4` of the test's identifying args, sorted for argument-order invariance) appears only when two model-level tests share a `test.type`.

A drift in the formatter is caught downstream when the resolver
produces a string that doesn't match `^(column|test|model)\.`.

## Sidecar JSON schema

End-of-run, one JSON document per invocation at `sidecar_path` (default
`<project_dir>/.signalforge/grade.json`). Single-document overwrite via
`O_WRONLY | O_CREAT | O_TRUNC` (DEC-012); a re-run replaces the prior
sidecar atomically (subject to platform truncate semantics). The
sidecar size cap is 1 MiB (`_GRADE_SIDECAR_RECORD_LIMIT_BYTES`) — much
larger than the JSONL audit's 4 KiB `PIPE_BUF`-bound limit because
there is no concurrent-append contract.

The sidecar carries the same `GradingReport` shape the orchestrator
returns:

```json
{
  "grade_schema_version": 1,
  "signalforge_version": "0.1.0.dev0",
  "run_id": "a1b2c3d4e5f6478890aabbccddeeff00",
  "timestamp": "2026-05-01T17:42:13.123456Z",
  "duration_seconds": 12.473,
  "model_unique_id": "model.shop.dim_customers",
  "rubric_hash": "0123456789abcdef",
  "thresholds": [0.7, 0.5],
  "results": [
    {
      "artifact_id": "column.email.description",
      "criterion_id": "clarity",
      "score": 0.8,
      "passed": true,
      "evidence": "...",
      "reasoning": "..."
    }
  ],
  "pass_rate": 1.0,
  "mean_score": 0.8,
  "aggregate_complete": true,
  "passed": true
}
```

Aggregate computed fields (`pass_rate`, `mean_score`,
`aggregate_complete`, `passed`) round-trip through the JSON via
Pydantic's `@computed_field` serialisation — a sidecar reader gets the
same view as a freshly-produced `GradingReport`.

**`run_id` correlation (DEC-020).** Every JSONL record from this run
carries the same `run_id` as the sidecar's `run_id`. To pull every
per-pair record for a sidecar:

```bash
RUN_ID=$(jq -r '.run_id' .signalforge/grade.json)
jq -c "select(.run_id == \"$RUN_ID\")" .signalforge/grade.jsonl
```

**Partial-run signal.** JSONL exists but the sidecar is absent → the
run crashed mid-iteration (most likely an audit-write failure on a
later pair, which propagated as `GradeAuditWriteError`). Per-pair JSONL
records up to the crash point are durable receipts of the work that
DID complete. The crash was deliberate (DEC-006 fail-closed) — an
unaudited grade decision is, by definition, a verdict without a
receipt, exactly the failure mode the audit exists to prevent.

**Drift gates.** `tests/fixtures/grade/grade_event_v1.jsonl` and
`grade_report_v1.json` are the canonical schema fixtures;
`tests/grade/test_drift_detector.py` pairs each production model
(`extra="ignore"`) with a one-off `extra="forbid"` strict mirror and
validates against the fixture. Adding a field to `GradeEvent` /
`GradingReport` / `GradingResult` without updating the strict mirror
OR the fixture breaks the test loudly. Don't bypass.

## Reproducibility / hash fields

Three hash fields land on every `GradeEvent`, all 16-hex-char `blake2b`
with `digest_size=8`. The cross-stage hash domain is consistent — a
reviewer querying "what response text produced criterion X for
artefact Y on date Z" can compare bytes verbatim across draft and
grade JSONLs.

- **`rubric_hash`** — `blake2b-8` of the canonical rubric JSON (list of `{id, criterion}` mappings, sorted by `id`, dumped with `sort_keys=True, separators=(",", ":")`). Deterministic and order-invariant by construction — swapping two criteria in the rubric tuple does not change the digest. Carried on every event AND on the sidecar `GradingReport`. Same `rubric_hash` across all records in a run = same rubric; differs = rubric changed mid-run (which doesn't happen in v0.1 since the rubric is locked at orchestrator entry, but a reader can still verify).
- **`prompt_version_template`** — `blake2b-8` of the system prompt + cached rubric block + envelope-tag template. Constant across all criteria of one run; constant across runs of the same SignalForge version with the same rubric.
- **`criterion_prompt_hash`** — `blake2b-8` of the per-criterion prompt fragment. Stable across artefacts of one run; reading the same criterion's prompt fragment from a v0.2 deployment with a tweaked prompt template surfaces a hash drift even when the criterion text itself is unchanged.
- **`response_text_hash`** — `blake2b-8` of the raw LLM response text. Empty string for degraded records (no response text). Mirrors `LLMResponseEvent.response_text_hash` (#5).

To find every JSONL record produced by a specific rubric:

```bash
jq -c 'select(.rubric_hash == "0123456789abcdef")' .signalforge/grade.jsonl
```

To verify a sidecar's `rubric_hash` matches the rubric still on disk,
reload via `_canonical_rubric_hash(DEFAULT_RUBRIC)` from a Python
session and compare; a mismatch means the rubric drifted between the
graded run and the current code.

## Cost guidance (DEC-014)

The grader's per-criterion fan-out (DEC-004) is the most expensive
option of those evaluated in
[`plans/super/7-quality-grader.md`](../plans/super/7-quality-grader.md):
**one LLM call per `(artefact × criterion)` pair**. Be clear-eyed about
what this costs.

**Reference numbers, with the assumptions.** The default rubric has
**4 criteria**; a typical drafted dbt model has **~12 artefacts**
(column descriptions + column rationales + per-test rationales + model
description + model rationale). With the default `model:
claude-sonnet-4-6` and `cache_ttl: 1h`, a representative run costs:

- **~$0.18 per model on Sonnet 4.6** (4 criteria × 12 artefacts × ~600
  input tokens dynamic block + ~150 output tokens per call), pricing
  date 2026-05.
- vs. **~$0.05 per model batched** (Q4=A in the plan — single judge
  call covering all criteria for one artefact at once). The
  per-criterion fan-out is **~3.4× more expensive**.

**Why fan-out anyway?** Three load-bearing reasons (recorded in
DEC-004 of the plan):

1. **Per-criterion retry isolation.** One LLM call per criterion means
   one bad criterion (parser failure, retry-exhausted) does not
   fail-loud the whole report — the orchestrator routes that pair to
   the DEC-015 degraded path and the rest of the rubric still produces
   signal. Batched mode would force "all-or-nothing" parsing.
2. **Per-criterion prompt tuning headroom for v0.2.** Each criterion
   already has its own prompt seam; adding a v0.2 per-criterion prompt
   override is a one-line config addition. Batched mode would require
   a prompt-template overhaul.
3. **Trivial anchor contract.** Single criterion = no positional
   alignment problem in the response parser. Batched mode would have
   to validate a multi-criterion response array against the rubric
   ordering, exactly the kind of loose-contract surface the safety /
   draft layers' anchor contracts exist to avoid.

**Cost-control knobs.** Three levers operators can pull when the
default fan-out is too expensive for their use case:

- **`total_budget_seconds`** (default `300`) — Whole-run wall-clock
  cap. Tripping this routes every remaining pair to the degraded path
  rather than billing for the whole rubric × every artefact. A
  `GradeBudgetExceededError` only fires if the budget trips before
  ANY criterion runs (a hard "the run did nothing" failure); a partial
  run completes with `aggregate_complete: false`.
- **`max_output_tokens`** (default `256`) — Per-call output cap. The
  expected JSON response is ~150 tokens; tightening to 192 trims ~25%
  off the output-token bill at marginal risk of truncated JSON
  (handled by `GradeOutputError(violation_type="json_parse")` and the
  degraded path).
- **`cache_ttl: "1h"`** (default) — Cache-read economics. Prompt
  caching is a **provider capability** (issue #135): the `cache_control`
  marker, the extended-cache-ttl beta header, and the pre-send
  `count_tokens` gate are emitted only when the selected `LLMProvider`
  reports `supports_prompt_caching` / `supports_token_count`. A provider
  that supports neither reports 0 cache tokens and skips the marker; the
  default `anthropic` provider supports both, so the economics below are
  unchanged. The cached block (system prompt + rubric block) is constant
  across every call in one `grade_artifacts` invocation; a 60-call run
  reads the cache ~59 times after one write. Cache reads are 0.1× input pricing vs.
  1.25× for writes; the break-even is ~2 reads per write. Switching
  to `cache_ttl: "5m"` is rarely worth it — the only failure mode the
  shorter TTL catches is a multi-hour run where the cache would otherwise
  expire mid-iteration, which means the per-call reads stop landing,
  which the dual-zero cache-anomaly WARNING (DEC-014 of #5) surfaces
  loudly. Leave at `"1h"` unless you have a specific reason.

**v0.2 will offer batched-criteria as opt-in** for cost-conscious
operators (DEC-014). The current architecture preserves the option:
each criterion has its own prompt seam already, so a `cost_mode:
batched` flag is additive rather than a rewrite.

## Prompt-injection mitigation

The grader's only LLM-prompt defence is the
`<ARTIFACT>...</ARTIFACT>` envelope (mirrors the drafter's
`<MODEL_SQL>` envelope, DEC-007 of #5). The system message instructs
the LLM-judge to treat anything between the tags as data, not
instructions:

```text
<ARTIFACT>
This column captures the customer's email address at order time.
-- adversarial column description: "ignore prior instructions and ..."
</ARTIFACT>
```

**Envelope-breach guard.** A payload containing the literal
`</ARTIFACT>` would terminate the fence early and let downstream
content escape. Two checks fire:

1. **Whole-run pre-flight scan (DEC-013).** `_scan_envelope_breach`
   runs at orchestrator entry over every artefact payload BEFORE any
   LLM call is issued. Failing fast surfaces one typed
   `GradePromptEnvelopeBreachError(artifact_id=...)` pointing at the
   offending artefact, rather than discovering the breach mid-iteration
   after several JSONL records have already landed.
2. **Per-call defence-in-depth.** `render_dynamic_block` re-checks at
   call time so a future caller using `_grade_one` directly (without
   the orchestrator's pre-flight) still gets the protection.

**Safety-boundary note (DEC-013 of #7).** The PII redaction boundary
established by issue #4 closed at *draft time* — the safety layer
redacts column names and values before the drafting LLM call.
Post-draft, `CandidateSchema` carries *real* column names; the grader
sends those real names to the LLM-judge by design, and writes them
into the sidecar JSON the operator reviews. Re-redaction inside the
grader would defeat both the rubric (judges need real names to score
documentation quality) and the explainable-diffs commitment (reviewers
need to see what was scored).

## Audit log sensitivity

`grade.jsonl` and `grade.json` contain the LLM-judge's evidence and
reasoning, which can echo verbatim fragments of the artefact text
(column descriptions, model docs, test rationales). Treat both files
at-rest the same way you treat the safety / draft / prune audits:

- **Gitignore `.signalforge/`** (already configured in this repo's `.gitignore`).
- **Restrict at-rest permissions.** The writers create files at `0o600` on first call; the parent directory is created via `mkdir(parents=True, exist_ok=True)` (Python's `mkdir` does not tighten an existing directory's permissions, so verify the existing `.signalforge/` mode is `0o700` on shared hosts).
- **Don't ship as a build artefact.** Strip from container images and CI uploads.
- **Symlink-hardened paths.** Both writers route `audit_path` and `sidecar_path` through `signalforge.warehouse._path_safety.canonicalise_path` at writer entry. A symlinked `.signalforge/grade.jsonl -> /etc/passwd` is rejected as `GradeAuditWriteError` (wrapping the underlying `ProfileNotFoundError`) before the `os.open` ever fires.

## Debugging

Logger name: `signalforge.grade.engine` (and sibling modules under
`signalforge.grade`).

```python
import logging
logging.getLogger("signalforge.grade").setLevel(logging.DEBUG)
```

Levels:

- **INFO** — One line per `grade_artifacts` invocation at the end of the run, lazy-format JSON per DEC-027 (`run_id`, `model_unique_id`, `pass_rate`, `mean_score`, `passed`, `aggregate_complete`, `duration_seconds`, `results`). Mirrors `safety-layer.md` DEC-022 / `llm-drafter.md` DEC-011 / `prune-engine.md` DEC-017 — never f-string-interpolate user-controlled strings into a logger call.
- **WARNING** — One line when `total_budget_seconds` trips, JSON-encoded `{run_id, model_unique_id, evaluated, remaining_pairs, total_budget_seconds}`. Plus the inherited `signalforge.llm` retry warnings (one per retry attempt at the LLM seam).
- **DEBUG** — Reserved for future per-criterion latency observability; v0.1 emits no DEBUG from the engine.

The grade layer never logs full evidence / reasoning content. The
audit JSONL is the single durable record of decision-level detail;
logger output is a hint that the decision happened, not what was in
it. The custom `__repr__` on `GradingResult` and `GradingReport`
defends accidental `_LOGGER.warning("result: %s", result)` calls from
dumping multi-paragraph reasoning into log sinks.

**Reading a fail-closed `GradeAuditWriteError`.** The cause is exposed
as `.cause` and on `__cause__`. Common causes:

- Parent directory not writable (no `+w` for the user, or `.signalforge/` is a symlink to a read-only mount).
- Disk full (`ENOSPC`).
- Symlink containment violation (the audit / sidecar path canonicalises outside `<project_dir>`). The cause is a `signalforge.warehouse.errors.ProfileNotFoundError`.
- Oversize record (raises `GradeAuditRecordTooLargeError` instead — for the JSONL writer, reduce the LLM's `reasoning` payload via `max_output_tokens`; for the sidecar, the 1 MiB cap is generous enough that hitting it suggests a runaway LLM response that should already have been rejected by the JSONL writer's per-call cap).

## Failure modes / typed-error cross-reference

| Class                              | When raised                                                                              | Where it surfaces                                  | How to fix                                                                                       |
| ---------------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `GradeError`                       | Base class; never raised directly.                                                       | `signalforge.grade.errors`                         | Catch it to handle every grade-layer failure uniformly.                                          |
| `GradeConfigError`                 | `signalforge.yml` `grade:` block failed parse / schema validation (`extra="forbid"`, out-of-range knob, malformed rubric override). | `load_grade_config`                                | Inspect the `grade:` block. Typos like `mdoel:` are caught here.                                 |
| `GradeRubricError`                 | The resolved rubric is empty or carries duplicate `id` values.                           | `validate_rubric` (called at `grade_artifacts` entry and inside `load_grade_config`'s rubric validator) | Provide at least one criterion; ensure every `id` is unique.                                     |
| `GradeLLMError`                    | One-level wrap of `signalforge.llm.LLMError`. Retry budget exhausted, auth failure, server error, malformed cache block. | `_grade_one` per pair (degraded by orchestrator); only escapes if the entire run can't recover. | Inspect `.cause` / `__cause__` for the underlying LLM-layer detail. Common: missing `ANTHROPIC_API_KEY`, rate-limit exhaustion. |
| `GradeBudgetExceededError`         | `total_budget_seconds` tripped before ANY criterion was graded (a "the run did nothing" failure). | `grade_artifacts` (rare — the normal budget path is per-pair degrade). | Raise `total_budget_seconds`, narrow the candidate set, or reduce the rubric's criterion count. |
| `GradePromptEnvelopeBreachError`   | An artefact payload contains the literal `</ARTIFACT>` close tag.                        | Whole-run pre-flight `_scan_envelope_breach`; per-call defence-in-depth in `render_dynamic_block`. | Inspect the offending artefact (`exc.artifact_id`); remove the literal tag from the column description / rationale. |
| `GradeOutputError`                 | LLM-judge response failed parse / anchor-contract validation. Carries `violation_type`.  | `parse_grade_response` per pair (degraded by orchestrator).                  | Pattern-match on `.violation_type` (`json_parse`, `criterion_id_mismatch`, `score_out_of_range`, …). Re-running typically resolves transient JSON failures; structural mismatches usually point at a prompt-template regression. |
| `GradeAuditWriteError`             | Fail-closed audit / sidecar write failure (`OSError`, `PermissionError`, encoding, `fsync`, symlink containment). DEC-006 / DEC-012. | `write_grade_event` / `write_grading_report` (wrapped at the orchestrator's audit-write seams). | Verify `<project_dir>/.signalforge/` is writable, has disk space, and is not a symlink escaping the project tree. Fix the I/O issue and re-run. |
| `GradeAuditRecordTooLargeError`    | Serialised JSONL line > 4000 bytes OR sidecar JSON > 1 MiB. Raised BEFORE any file open. | `write_grade_event` / `write_grading_report`.      | For JSONL: reduce `max_output_tokens` so the judge's reasoning stays short; for sidecar: investigate any single result with megabyte-range reasoning (likely a prompt regression).  |

## Regen instructions for fixtures

The grade-layer fixtures at `tests/fixtures/grade/grade_event_v1.jsonl`
and `grade_report_v1.json` are **hand-authored**, not produced by a
live LLM run. They exist solely as drift gates for the
`extra="forbid"` strict mirrors in
`tests/grade/test_drift_detector.py`. To regenerate after a model
field change:

1. Update the production model (`signalforge.grade.models` —
   `GradeEvent`, `GradingReport`, or `GradingResult`).
2. Update the matching strict mirror in
   `tests/grade/test_drift_detector.py`. Both must change in the same
   commit, or the strict-validates-fixture check fails.
3. Edit the fixture JSON / JSONL by hand to add / remove the field.
   Keep the values readable (no test fixture should be a wall of
   placeholder hashes).
4. Run `pytest tests/grade/test_drift_detector.py -v` — the strict
   model must validate the fixture after the change.

`tests/fixtures/grade/sample_candidate.json` is also hand-authored;
regen by editing in place. v0.2 may add a regenerate.sh script under
`tests/fixtures/regenerate.sh`-style convention if the rubric / model
shape stabilises enough that a deterministic generator pays for itself.

`tests/fixtures/grade/example_config.yml` is the source of truth for
the worked example in [Configuration](#configuration-signalforgeyml-grade-block)
above — the `test_load_grade_config_doc_example_round_trips` test
loads it through `load_grade_config` and asserts the defaults from
DEC-023..DEC-027 are populated, so the doc and the loader cannot
silently drift.

## CLI integration note

Tracked in [issue #9](https://github.com/wjduenow/SignalForge/issues/9).
The `signalforge generate` CLI will load the grade config via
`load_grade_config(...)` and invoke `grade_artifacts(...)` after the
prune step completes; the diff renderer (#8) consumes the returned
`GradingReport` to render per-criterion stars + per-artefact
`one_line_why` lines (Architectural Commitment #5 — explainable
diffs). When `fail_on_below_threshold: true` is set in `signalforge.yml`,
the CLI catches the resulting `GradeBelowThresholdError` and exits with
the `INPUT` exit-code tier (exit 2) — see
[Threshold-fail behaviour](#threshold-fail-behaviour) above and
[`docs/cli-ops.md`](cli-ops.md) once US-009 lands. The default
posture (report-only) is preserved: a below-threshold run with the
default `fail_on_below_threshold: false` returns the report and the
CLI exits 0 once the diff renders.

## References

- Design record: [`plans/super/7-quality-grader.md`](../plans/super/7-quality-grader.md).
- Prune-layer counterpart (the layer the grader mirrors most patterns
  from for fail-closed audit + budget semantics):
  [`docs/prune-ops.md`](prune-ops.md).
- Drafter counterpart (the layer the grader mirrors for the LLM SDK
  seam, prompt-injection envelope, and per-call response audit):
  [`docs/draft-ops.md`](draft-ops.md).
- Safety-layer counterpart (the layer that establishes the fail-closed
  JSONL convention and `extra="forbid"` config-shape rule):
  [`docs/safety-ops.md`](safety-ops.md).
- Manifest reader conventions
  (`frozen` / `extra="ignore"` / drift-detector pattern):
  [`.claude/rules/manifest-readers.md`](../.claude/rules/manifest-readers.md).

Cross-reference DECs: DEC-001 (public API surface), DEC-002 (per-pair
score shape + threshold-AND aggregate), DEC-004 (per-criterion
fan-out — one LLM call per `(artefact, criterion)` pair), DEC-005
(`fail_on_below_threshold` graduated in #9 / US-002 / DEC-021), DEC-006 (fail-closed JSONL
audit), DEC-007 (locked default rubric), DEC-009 (canonical
`artifact_id` dotted-path format), DEC-010 (`rubric_hash`
reproducibility), DEC-011 (`Rubric` as `TypeAlias`), DEC-012
(end-of-run sidecar JSON), DEC-013 (whole-run envelope-breach scan),
DEC-014 (cost transparency — this doc's [Cost guidance](#cost-guidance-dec-014)
section), DEC-015 (degraded path on retry-exhaustion / parser-failure
/ budget-exceeded), DEC-016 (default rubric criterion text + threshold
defaults), DEC-017 (`Criterion` shape), DEC-018 (criterion-outer /
artefact-inner iteration order; `one_line_why` cap), DEC-020
(`run_id`), DEC-022 (`project_dir` semantics), DEC-023..DEC-027
(locked config defaults), DEC-028 (nine-class error hierarchy).
