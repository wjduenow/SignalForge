# Prune layer — operations guide

Operational reference for users of `signalforge.prune`. Companion to
[`docs/safety-ops.md`](safety-ops.md),
[`docs/draft-ops.md`](draft-ops.md),
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md), and
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md), and to the
design record in [`plans/super/6-prune-engine.md`](../plans/super/6-prune-engine.md).

The prune layer sits between the LLM-drafting layer (#5) and the diff
renderer (#8). Every candidate test produced by the drafter goes through
one entry point — `signalforge.prune.prune_tests` — which compiles the
test to failing-rows SQL, runs it against the warehouse, classifies the
verdict, and writes a fail-closed JSONL audit record per decision.

This is the load-bearing differentiator (Architectural Commitment #1 in
[`CLAUDE.md`](../CLAUDE.md)) — competitors generate; SignalForge
generates *and* grades.

## Default posture

Sample-scope is the default. Architectural Commitment #1 (signal over
volume) penalises always-pass tests because they consume reviewer
attention without catching anything; sampling 100k rows is enough signal
to detect always-pass while keeping query bytes under control.

The layer is fail-closed on the audit-write boundary: any I/O error from
`PruneEvent` persistence aborts the run as `PruneAuditWriteError`
(DEC-016, mirrors safety's DEC-011 and draft's DEC-006). And the layer
is conservative on the verdict boundary: tests we cannot evaluate
(warehouse error, total-budget exhausted, ambiguous evidence) are
**kept**, not dropped — `kept-without-evidence` lands the test in front
of a human reviewer rather than silently losing potential signal.

User-facing tagline: **always-pass tests are dropped; everything we
cannot confidently drop is kept.**

## Public API

Import from `signalforge.prune`. The 14 names exported by `__all__`:

### Orchestrator

- **`prune_tests(model, adapter, candidates, manifest, *, config=None, project_dir=None, audit_path=None) -> PruneResult`** — End-to-end orchestrator. Compiles every `CandidateTest`, runs each through the warehouse adapter, writes one JSONL audit record per decision, returns the aggregate `PruneResult`. Mirrors `signalforge.draft.draft_schema` so the CLI / wrapper layers see one consistent end-to-end shape across pipeline stages. `project_dir` defaults to `Path.cwd()`. `audit_path` defaults to `<project_dir>/.signalforge/prune.jsonl`.

### Result shapes

- **`PruneResult`** — Aggregate verdict for one model. Frozen Pydantic model with fields `prune_schema_version: Literal[1]`, `model_unique_id: str`, `decisions: tuple[PruneDecision, ...]`, `elapsed_ms: int`, `signalforge_version: str`. Computed properties: `kept_decisions`, `dropped_decisions`, `kept_count`, `dropped_count`, `total_tests` — all derived from `decisions` (DEC-003) so a `PruneResult` reconstructed from a JSONL log carries identical views to a freshly produced one.

- **`PruneDecision`** — One verdict per candidate test. Carries `test_anchor: str` (`"column.<name>"` or `"model"`), `test: CandidateTest` (the typed discriminated union from the drafter — six variants as of issue #169, including `custom_sql` (#116) and `row_count_between` (#169); NOT a loose dict — DEC-004; the grader and diff renderer reuse the drafter's per-variant display logic), `decision: Literal["kept", "dropped"]`, `reason: DropReason`, `failures: int`, `sampled_rows: int | None`, `scope: Scope`, `elapsed_ms: int`, `compiled_sql_hash: str` (16 hex chars; blake2b-8 per DEC-005), `compiled_sql: str`, `why: str`, `sample_failures: tuple[dict[str, Any], ...] | None`.

### Configuration

- **`PruneConfig`** — User-facing knobs. Frozen Pydantic model with `extra="forbid"` (config-shaped per DEC-015 — typos fail loud). Field reference: see [Configuration](#configuration-signalforgeyml-prune-block) below.

- **`load_prune_config(project_dir, path=None) -> PruneConfig`** — Loads the `prune:` block from `signalforge.yml`. Resolves to `<project_dir>/signalforge.yml` when `path` is `None`. Returns defaults when the file is missing, empty, or the `prune:` key is absent. Raises `PruneConfigError` on parse / schema failures. Mirrors `load_safety_config` / `load_draft_config` so the CLI sees one calling convention across stages.

### Discriminator literals

- **`DropReason`** — `Literal["always-passes", "requires-future-data", "failed-on-known-clean-data", "kept", "kept-without-evidence"]`. Closed set so the diff renderer (#8) can branch on the literal value rather than sniffing prose.

- **`Scope`** — `Literal["sample", "full"]`. When `scope == "full"`, `PruneDecision.sampled_rows` is `None` (every row inspected).

### Audit

- **`PruneEvent`** — One JSONL audit record per `PruneDecision`. Constructed ONLY by `signalforge.prune.audit._build_prune_event` (DEC-018; AST-gated by `tests/test_audit_completeness.py`). `extra="ignore"` for forward-compat read-back. See [Audit JSONL schema](#audit-jsonl-schema) for the field set.

### Errors

`from signalforge.prune import errors`. Every exception subclasses
`PruneError` and carries a class-level `default_remediation` rendered on
a `↳ Remediation:` line by `__str__`.

- **`PruneError`** — Base class. Never raised directly.
- **`PruneConfigError`** — `signalforge.yml` `prune:` block failed parse or schema validation.
- **`PruneTrustedModelNotFoundError`** — `prune.trusted_models` references a `unique_id` not in the manifest. Subclass of `PruneConfigError`. Raised at orchestrator entry, BEFORE any warehouse call (DEC-008).
- **`PruneTimeoutError`** — Internal control-flow signal for budget-exhausted dispatch. Callers of `prune_tests` do NOT see this — the orchestrator routes the in-flight test plus every remaining un-started test to `kept-without-evidence` (DEC-011).
- **`PruneAuditWriteError`** — Fail-closed audit-write failure (`OSError` / `PermissionError` / encoding / `fsync`). Aborts the run; original cause exposed via `.cause` and `__cause__` (DEC-016).
- **`PruneAuditRecordTooLargeError`** — Serialised JSONL line exceeded the POSIX-atomic-append cap (4000 bytes). Raised BEFORE any file is opened so an oversize record leaves no on-disk artefact.

DEC-006 deliberately omits a `PruneCompilerError` class. Compilation
always succeeds; failures like `relationships(to: unknown)` emit a
structured `requires-future-data` drop reason rather than an exception.

## Configuration: `signalforge.yml` `prune:` block

Top-level namespace is `prune:` (DEC-020; sibling keys `safety:`,
`llm:`, future `grade:` are reserved for other stages and silently
ignored by the prune loader).

```yaml
safety:
  # ... (loaded by signalforge.safety)
llm:
  # ... (loaded by signalforge.draft)
prune:
  enabled: true            # set false to skip prune entirely (no warehouse contact)
  scope: sample            # "sample" | "full"
  sample_strategy: materialised  # "materialised" (default, v0.2) | "oneshot" (v0.1 fallback)
  sample_size: 100000      # rows
  test_timeout_seconds: 30
  total_budget_seconds: 600
  capture_failure_rows: 3
  min_kept_rate_warn: 0.0  # WARN when kept/total <= this; 0.0 = warn on "all dropped"
  trusted_models:
    - model.shop.dim_customers
  partition_filter:
    column: event_dt
    op: ">="
    value: "2026-01-01"
```

Field-by-field:

- **`enabled`** — `bool`. Default `true`. When `false`, `prune_tests` short-circuits: it does NOT issue any warehouse calls, does NOT validate `trusted_models`, and routes every candidate to `kept-without-evidence` with `why="prune disabled in signalforge.yml"`. The audit JSONL still records one `PruneEvent` per candidate (fail-closed audit preserved). The CLI emits an INFO line at prune-stage entry. **Trade-off:** disabling prune lets always-pass tests reach the diff — directly counter to Architectural Commitment #1 (signal over volume). Use as a temporary escape hatch when warehouse contact is unavailable (offline, credentials issue, cost ceiling) and you still need a draft run.
- **`scope`** — `"sample"` | `"full"`. Default `"sample"`. Whether candidate tests run against a deterministic warehouse sample or a full table scan. Switch to `"full"` only when the model is small enough that `sample_size` would scan most of it anyway.
- **`sample_strategy`** — `"materialised"` | `"oneshot"`. Default `"materialised"` (v0.2 — see issue #22). When set to `"materialised"`, `prune_tests` calls `adapter.materialise_sample(...)` ONCE before the per-test loop — the adapter creates a `_SESSION._sf_sample_<run_id>` temp table and every test's compiled SQL reads from it (per-test bytes drop from ~9.92 GB to <100 MB on the AR-B1 reference workload). When set to `"oneshot"`, the v0.1 path runs unchanged — every test issues its own deterministic-sample query against the source table. Adapters that don't override `materialise_sample` (any non-BigQuery adapter in v0.2) raise `MaterialisationNotSupportedError`; the orchestrator then routes every candidate to `kept-without-evidence` per the conservative-bias rule (see [Drop-reason taxonomy](#drop-reason-taxonomy)). Operators on non-BQ adapters opt out via `sample_strategy: oneshot`.
- **`sample_size`** — Integer row count for sample scope. Default `100_000`. Passed to `WarehouseAdapter.sample_rows`. Increase when the always-pass false-positive rate on small samples hides real signal; decrease to cap query bytes on very wide tables (column-pruning does NOT apply through `FARM_FINGERPRINT(TO_JSON_STRING(t))` — see [Cost model](#cost-model-us-003-verification)).
- **`test_timeout_seconds`** — Per-test wall-clock budget. Default `30`. **Reserved for v0.2** — the adapter's `_default_job_config(timeout_ms=...)` plumbing exists (US-002 of issue #3) but `WarehouseAdapter.run_test_sql` does not yet accept a per-call timeout kwarg, so v0.1 does not enforce this knob. Per-test wall-clock control in v0.1 comes implicitly from `total_budget_seconds` plus the `WarehouseError` catch path: a test that exceeds the warehouse's own budget surfaces as a typed error → `kept-without-evidence`. See [v0.2 deferrals](#v02-deferrals).
- **`total_budget_seconds`** — Whole-run wall-clock budget. Default `600`. Once exceeded, every remaining test drains to `kept-without-evidence` with `why="Total prune budget (Ns) exceeded before evaluation."` (DEC-011). Conservative bias — no test is silently dropped because the run ran long.
- **`capture_failure_rows`** — Number of failing rows recorded on the `PruneDecision.sample_failures` field per failed test. Default `3`. Set to `0` to omit row-level evidence entirely (the audit record stays compact for very wide tables).
- **`trusted_models`** — List of manifest `unique_id`s whose data is treated as known-clean. A failure on a trusted model surfaces as `failed-on-known-clean-data` (drop, presumed buggy test) rather than `kept`. Opt-in only (Q1=B). Validated against the manifest at `prune_tests` entry — typos raise `PruneTrustedModelNotFoundError` BEFORE any warehouse call (DEC-008).
- **`min_kept_rate_warn`** — Float in `[0.0, 1.0]`. Default `0.0`. Soft signal threshold for "did the prune work as intended?" (issue #51). When `kept_count / total_tests` is at or below this value AND at least one candidate was evaluated, the orchestrator emits one `WARNING`-level log line summarising the run shape (model id, total/kept/dropped counts, kept rate, threshold). Default `0.0` fires only when every candidate was dropped — the "did we lose the whole LLM draft?" signal. Set to `0.10` to catch "fewer than 10% kept" on typical staging models, or to `1.0` to always emit the summary line. The WARNING is informational — the run still returns a `PruneResult` and exits cleanly. Empty candidate sets skip the check (no division-by-zero; the drafter producing nothing is its own degenerate signal). See [Expected drop rates](#expected-drop-rates) for the empirical context this threshold is meant to calibrate against.
- **`partition_filter`** — Optional `PartitionFilter` ADT (`{column, op, value}`) scoping every sample query. Required by the warehouse adapter for tables with `num_rows >= 100M`; otherwise optional. Pydantic recursively validates the YAML mapping into the typed shape.

Unknown keys under `prune:` raise `PruneConfigError` (Pydantic
`extra="forbid"`, DEC-015). Typos like `scop:` or
`total_budget_secnds:` fail loud at load time rather than silently
no-op'ing.

## Drop-reason taxonomy

Every kept and dropped test ships with a structured `PruneDecision`
carrying a one-line `why`. The reasons are a closed `DropReason` literal
so the diff renderer (#8) can branch on the value.

| Reason | Decision | Why |
|--------|----------|-----|
| `always-passes` | dropped | Zero failing rows on the sampled or full set; no signal worth shipping. The load-bearing case for Architectural Commitment #1. |
| `requires-future-data` | dropped | A `relationships` test references a `to:` parent model not in the loaded manifest, OR a `custom_sql` test's `{{ ref() }}` / `{{ source() }}` target is absent from the manifest (issue #116). No warehouse call issued — the compiler returns a `_RequiresFutureData` sentinel and the orchestrator routes it directly to this reason (DEC-026). |
| `failed-on-known-clean-data` | dropped | Test failed AND `model.unique_id` is in `prune.trusted_models`; the test is presumed buggy. Symmetric noise-direction split with `always-passes` — both directions of noise need pruning per `CLAUDE.md`. |
| `kept` | kept | Test failed on an untrusted model with non-zero failures. Reviewer should evaluate. |
| `kept-without-evidence` | kept | Could not evaluate — warehouse error (typed `WarehouseError` subclass), total budget exceeded (DEC-011), or a `custom_sql` test whose SQL carries unsupported Jinja / an ambiguous `ref()` / a SQL-safety rejection (issue #116; see [`custom_sql` evaluation](#custom_sql-evaluation)). Ship conservatively; reviewer decides. |

Conservative bias: when in doubt, keep. Architectural Commitment #1
penalises always-pass tests (no signal, consumes reviewer attention) but
does not penalise kept tests with ambiguous evidence — those land in
front of a human reviewer who can make the final call.

## `custom_sql` evaluation

The drafter's fifth test variant — `custom_sql`, the free-form singular
SQL business-rule test (issue #116; see
[`docs/draft-ops.md`](draft-ops.md#custom-business-rule-tests-custom_sql))
— is pruned through the same orchestrator and routes to the same five
`DropReason` literals as the four built-ins. There is **no new drop
reason**; what differs is how the test compiles and gets sampled.

**Jinja resolution first.** `custom_sql.sql` may reference `{{ this }}`,
`{{ ref('<model>') }}`, and `{{ source('<src>', '<table>') }}`. The
compiler resolves these via the bounded resolver
(`signalforge.manifest.template.resolve_template_refs`) before any
warehouse call. The resolution outcome decides the routing:

- **Resolved cleanly** → the test is sampled / full-scanned and routes
  to `always-passes` / `kept` / `failed-on-known-clean-data` exactly
  like a built-in.
- **`{{ ref() }}` / `{{ source() }}` targets a model/source absent from
  the manifest** → `requires-future-data` (mirrors the `relationships`
  missing-target precedent — the referenced model simply isn't built
  yet; revisit when the dependency lands). No warehouse call.
- **Control-flow Jinja (`{% if %}`, `{% for %}`, `var()`, `env_var()`,
  macros), an ambiguous `ref()` (matches multiple packages), or a
  SQL-safety pre-flight rejection on the resolved SQL** →
  `kept-without-evidence`. We cannot evaluate it, so we ship it for the
  reviewer rather than silently dropping it. No warehouse call.

**Single-table vs. multi-table sampling.** Once the SQL resolves, the
compiler decides how to bound the scan with a cheap heuristic — does a
word-boundary `JOIN` keyword survive string-literal stripping?

- **Single-table** (no `JOIN`) — the resolved SQL references only the
  model's own table. In `scope="sample"` the model's table is
  substituted with the deterministic-sample CTE alias (identical to the
  built-ins, so per-test bytes stay bounded). In `scope="full"` a
  partition filter is applied when one is configured.
- **Multi-table** (a `JOIN` survives literal-stripping) — runs
  **full-scan (unsampled)**, because sampling only one side of a join is
  semantically wrong: an orphan-detection join against a *sampled* child
  would report false orphans for parents that are simply absent from the
  sample. A partition filter is still applied to the model's own table
  when one is available.

**The bytes cap is the only guardrail on a multi-table full-scan.** A
multi-table `custom_sql` test reads every row of the joined tables —
there is no sample CTE to bound it. The adapter's
`maximum_bytes_billed` cap (default 100 MB, DEC-005; raise via the
profile-level `maximum_bytes_billed` field — see
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md)) is what
stops a runaway scan. **Tuning note:** if a multi-table business rule
spans large fact tables, either raise the cap deliberately (and accept
the per-test cost) or scope the rule with a `partition_filter` so the
model's own side is bounded. When the resolved query's pre-execution
byte estimate exceeds the cap, the warehouse rejects the query before
execution; the typed `WarehouseError` is caught and the test routes to
`kept-without-evidence` (`why` carries the warehouse error class) — the
test ships, unevaluated, for the reviewer.

In the [expected-drop-rate](#expected-drop-rates) framing below,
`custom_sql` tests behave like the built-ins: a business rule that the
warehouse data never violates is `always-passes` (dropped, no signal); a
rule the data *does* violate is `kept` (real signal — exactly the rows a
reviewer wants to see). The one categorical difference is the higher
`kept-without-evidence` / `requires-future-data` fraction: free-form SQL
has more ways to be unevaluable (unsupported Jinja, unbuilt refs) than a
generic schema test. That is the conservative-bias contract working as
designed — an unevaluable business rule is shipped, never silently lost.

### `custom_sql`: manifest-ingested and drafted

`custom_sql` candidates reach the prune engine from **two** sources, and a
boolean on the candidate — `from_manifest` — distinguishes them:

- **Drafted** (`from_manifest=False`) — the LLM's business-rule SQL, or a
  hand-authored `tests/*.sql` singular test. Its `{{ this }}` resolves to the
  model's own relation, so it obeys the single-table-vs-multi-table sampling
  above byte-for-byte.
- **Manifest-ingested** (`from_manifest=True`) — a dbt-compiled generic test
  node's `compiled_code`, brought in by
  [`read_manifest_tests`](ingest-ops.md#recognition-of-dbt-compiled-manifest-tests)
  via `signalforge prune-existing --from-manifest` (issue #154). The
  already-Jinja-resolved body flows through the **same** `custom_sql` prune
  pipeline — `resolve → safety-check → wrap → conservative-bias routing` — and
  routes to the same five `DropReason` literals. No new drop reason; no 7th
  test variant.

The manifest-ingested path has its own routing, validation and cost story —
the subsections below cover it.

#### Sampling an ingested body: relation-rewriting, and the gates it must clear (#268)

dbt renders the model's relation with its *own* quoting (e.g.
`` `proj`.`ds`.`tbl` `` on BigQuery — three backtick pairs), which matches
neither substitution token the `{{ this }}` sample-CTE rewrite looks for. A
string substitution cannot find a relation it did not itself render, so #154
routed **every** ingested body to the source table at `scope="full"`,
whatever scope you asked for.

Issue **#268** lifts that for the common case: the engine locates the model's
own relation in the compiled body via **sqlglot AST analysis** and splices
each reference to the materialised `_SESSION._sf_sample_*` temp table with a
byte-preserving token splice (dbt's comments, indentation and blank lines
survive intact outside the spliced spans). A body must clear **every** gate
below to be sampled — any failure routes it back to the source table at full
scope, i.e. exactly the #154 behaviour:

| Gate | Why |
|---|---|
| `prune.scope: sample` **and** `prune.sample_strategy: materialised` | The rewrite needs a temp table to point *at*. **`oneshot` still routes ingested tests to source** — the CTE-based alternative it would need was prototyped and failed on execution (a CTE may only reference *earlier* CTEs, and sqlglot cannot prepend one). Deferred. |
| `from_manifest` | A *drafted* `custom_sql` keeps the `{{ this }}` substitution path, byte-unchanged. |
| Row-returning | A **#267 count-of-rows scalar is an aggregate** — `COUNT(*)` over a hash-mod'd sample returns the *sample size*, not the real count. Count-scalars keep running against the source; #267's verdicts are unchanged. |
| Exactly **one** physical relation, and it is the model's own | Enforced on the AST, never a `JOIN` regex (which misses comma-joins, correlated subqueries and `NOT EXISTS`). Sampling one leg of a join can produce a **false pass → `always-passes` → a real test deleted**. |
| No CTE alias collides with the relation | A CTE aliased with the model's (possibly dotted) name would otherwise have its *reference* rewritten to the temp table — plausibly zero rows, same false-pass outcome. |
| The spliced SQL passes an **AST post-condition** | The rewrite is re-parsed and must (a) parse clean, (b) contain **zero** residual references to the source relation, (c) contain exactly *N* references to the temp table. A count of matched tokens is *not* an integrity proof — the proof is on the rewritten AST. |
| **≥ 2** samplable ingested candidates in the batch | The cost model — see below. |

A body that fails a gate is not an error: it keeps its pre-#268 routing (source
table, full scope, `maximum_bytes_billed` as the cost guardrail — you opted in
via `--from-manifest`). The `DropReason` literal set is **still 5-valued**; no
new error class, no new flag, no new test variant. Only a body that genuinely
cannot be *evaluated* routes to `kept-without-evidence`, as before.

**The compiler fails closed independently of the engine.** If an ingested body
is ever handed a `table_ref` that is not the model's own source relation *and*
no verified rewrite came with it, the compiler refuses it
(`kept-without-evidence`) rather than dispatch dbt's production-quoted SQL
against a run the engine has booked as `scope="sample"`. That would be a silent
full scan of production recorded as evidence — and an `always-passes` verdict
from it would drop a real test.

#### Cost model — why the ≥ 2 gate exists

The materialisation CTAS is a `SELECT *` (plus a whole-row hash), so it reads
**every column** of the model. A narrow ingested test running against the source
is column-pruned by the warehouse. The break-even is roughly
`N × test_column_bytes > table_bytes` — so a **single** samplable ingested test
can never pay for the CTAS, and the engine keeps it bypassing to source.

**The ≥ 2 gate is a coarse heuristic, not a break-even guarantee.** It rules out
the always-losing single-test case, but it does **not** prove the CTAS pays for
itself at exactly two: two narrow tests on a very wide table can still be cheaper
run column-pruned at source than one whole-table `SELECT *` CTAS + two sampled
scans. A precise decision would compare the estimated CTAS bytes against the
summed per-test source bytes, which the engine does not do today (it has no
per-candidate byte estimate at routing time). If your ingested tests are narrow
and your models are very wide, prefer `--scope full` for the ingested pass, or
raise the effective threshold by pruning fewer models per run. A cost-aware gate
is tracked as a follow-up.

Two consequences worth planning for:

- A `--from-manifest` run at `--scope=sample --sample-strategy materialised`
  with ≥ 2 samplable bodies now **materialises a temp table where it previously
  did none** — a real (bounded, one-off) cost that pre-#268 runs did not pay.
  Wide tables with few ingested tests are the case to watch; `--scope=full` (or
  `--sample-strategy oneshot`) restores the old behaviour exactly.
- If `materialise_sample` fails (e.g. `SamplingRequiresPartitionFilterError` /
  `UnknownTableSizeError` on a >100M-row unpartitioned table) and **nothing in
  the batch genuinely needs the sample** — every candidate is either a
  bypass-to-source variant or a samplable ingested body, which has a perfectly
  good full-scope form — the engine **falls back to the source at full scope and
  keeps going**, emitting one WARNING carrying `"fallback": "source"`. You get N
  real verdicts, not N `kept-without-evidence`. Only a *drafted* row-level test
  in the batch (which has no source fallback) still routes the whole batch to
  `kept-without-evidence`.

#### Observability — one INFO, plus DEBUG breadcrumbs

The #154 INFO (`scope=sample requested; evaluating full-scope against source`)
is **gone** — it became a lie the moment some ingested bodies started being
sampled. On any `scope="sample"` run carrying at least one ingested candidate
the engine now emits **one aggregate INFO** per `prune_tests` call:

    ingested custom_sql routing: {"model_unique_id": "...", "sample_strategy":
    "materialised", "ingested_count": 7, "sampled_count": 5,
    "bypassed_to_source_count": 2, "bypass_reasons": {"aggregate-scalar": 1,
    "multi-relation": 1}}

`bypass_reasons` is a `{reason: count}` histogram over a closed reason set
(`unparseable`, `zero-match`, `multi-relation`, `cte-alias-collision`,
`span-mismatch`, `aggregate-scalar`, `below-min-samplable`,
`materialisation-failed`, `verify-failed`, `strategy-not-materialised`), sorted
so two runs over the same batch log byte-identical JSON. Each bypassed candidate
also gets a **DEBUG** breadcrumb naming its `test_anchor` + reason — DEBUG, not
INFO, because a wide model with 40 ingested tests would otherwise emit 40 INFO
lines. A `scope="full"` run stays log-silent, as before.

Whether an individual test was sampled or bypassed is durably recorded on its
audit record: see [`bypassed_to_source`](#audit-jsonl-schema).

#### Comment-tolerant validation

dbt's `compiled_code` routinely carries `--`
line comments and `/* */` block comments. The `#116` `validate_test_sql` used
for drafted `custom_sql` rejects both wholesale — which would mass-degrade
real ingested bodies to `kept-without-evidence`. The ingested path instead
uses the comment-tolerant `validate_ingested_sql`: it strips comments first
(string-literal-aware, so a `--` inside a quoted string is preserved), then
runs the same top-level-`;` and unbalanced-parentheses injection scan. A
genuine injection signal still fails loud → `kept-without-evidence`; a
comment-bearing but otherwise-clean body passes. The determinism check that
already ran at ingest is kept as a belt-and-braces `kept-without-evidence`
fallback in the compiler (the total-compilation choke point).

#### Count-of-rows scalar restructure (#267)

A manifest-ingested body that is
a bare count-of-rows scalar — `SELECT count(*) …`, `count(col)`, or
`count(DISTINCT …)` — is **not** row-returning, so wrapping it directly in the
adapter's `SELECT COUNT(*) AS failures FROM (<sql>)` envelope would report
`failures=1` **always** (a silent wrong `kept`). Rather than skip-record it (as
#154 did), the compiler restructures the scalar into a failing-rows form:

```sql
SELECT sf_agg_value FROM (SELECT (<body>) AS sf_agg_value) AS sf_agg WHERE sf_agg_value <> 0
```

so the outer envelope now reflects the true verdict — **0 rows ⇒
`always-passes` (dropped), ≥ 1 row ⇒ `kept`** — matching `row_count_between`'s
failing-rows contract. This **recovers the likely-intended failing-rows
semantics** under dbt's "returned rows = failures" contract (`count == 0` ⇒
pass); it deliberately reinterprets the body and does not mirror dbt's own
verdict, which scores a raw `count(*)` body as always-failing — see
[`docs/ingest-ops.md` § count-of-rows](ingest-ops.md#count-of-rows-scalar-bodies-are-pruned-267)
for the full soundness note and the non-empty-smoke-test caveat.
The restructure is a pure-string wrap (no sqlglot in the compiler); the
composed SQL is re-run through `validate_ingested_sql`. A count-of-rows scalar
is an **aggregate**, so `_test_requires_source_table` keeps routing it to the
**source table** under every sample strategy — it is explicitly excluded from
#268's relation-rewriting (a `COUNT(*)` over a hash-mod'd sample returns the
sample size, not the real count). **Belt-and-braces:** a scalar body that
reaches the compiler but isn't a
restructurable count (one that slipped the ingest gate) returns
`_InvalidIdentifier` → `kept-without-evidence` rather than the always-`1` wrap;
the 5-value `DropReason` is unchanged.

#### KEY FINDING — dbt-expectations bodies are row-returning, so they prune

`dbt-expectations` compiles *every* macro — including
`expect_table_row_count_to_be_between` — into a row-returning
`validation_errors` shell whose `count(*)` sits inside a nested CTE, so the
`COUNT(*)`-wrap the prune engine applies is semantically correct
(`failures=1` ⇒ out of bounds). The aggregate-SKIP disposition — which the
ingest bridge applies to a **non-count** scalar/aggregate body (`AVG` / `SUM` /
`MIN` / `MAX`, multi-aggregate, or arithmetic-on-count) to avoid the
always-`failures=1` wrong-verdict trap — never fires on a `dbt-expectations`
macro, and since #267 no longer fires on a bare count-of-rows body either (that
is restructured and pruned, above). See
[`docs/ingest-ops.md` § KEY FINDING](ingest-ops.md#key-finding-dbt-expectations-bodies-are-almost-all-prunable)
for the full walk-through.

## Row-count cost model

The sixth test variant, `row_count_between` (issue #169; see
[`docs/draft-ops.md`](draft-ops.md#row-count-tests-row_count_between)),
is pruned through the same orchestrator and routes to the same five
`DropReason` literals as the five other variants. There is **no new
drop reason**; what differs is the SQL shape and the cost profile.

**Compiled SQL.** The compiler emits a failing-rows CTE wrapping a
single `COUNT(*)` (DEC-014):

```sql
SELECT n
FROM (SELECT COUNT(*) AS n FROM <table> [WHERE <where>]) AS rc
WHERE n < <minimum> OR n > <maximum>
```

The bound-violation predicate adapts to which bounds are set
(`n < <min>`, `n > <max>`, or the conjunction). The adapter wraps the
output in the standard `SELECT COUNT(*) AS failures FROM (<sql>) AS t`
contract — zero rows from the inner SELECT (the bound holds) means
`failures=0` → engine routes `always-passes` and the test drops; one
row (the bound was violated) means `failures=1` → engine routes `kept`
(or `failed-on-known-clean-data` on a trusted model). The
CTE-then-WHERE shape is load-bearing: a bare `SELECT COUNT(*) FROM <table>`
wrapped by the adapter would always emit `failures=1` regardless of the
bounds, because the inner `COUNT(*)` always returns exactly one row.
The CTE pushes the bound check into the inner SELECT so the outer
`failures` count reflects the real verdict (US-007a corrected this
shape after #169 first landed).

**Sample-mode behaviour — engine routes past the materialised sample.**
The compiled SQL is identical regardless of `prune.scope`. A sampled
`COUNT(*)` is semantically wrong — a sample-bucket-mod'd subset can't
be compared against the full-table bounds, and a materialised sample
counted directly would return the **sample size** (typically 100K
rows), not the model's true row count.

`prune_tests` therefore overrides `table_ref` to the **source table**
for every `row_count_between` candidate, regardless of
`sample_strategy` (`materialised` or `oneshot`) and regardless of
whether the rest of the run uses the sample. The
[materialised-sample-substitution contract](#post-q4c-temp-table-materialised-sample-v02-issue-22)
from issue #116 still applies to the other five test types (which
read row-level data the sample faithfully represents); only
`row_count_between` is the exception. When every candidate in a run
is `row_count_between`, the engine also skips the
`materialise_sample` / `get_row_count` pre-work entirely — there's no
sample to set up, so adapter errors on that path can no longer route
the bypassing tests to `kept-without-evidence`.

**Cost guidance.** A `row_count_between` query is a single aggregate
`COUNT(*)` on the source table — cheap even on petabyte tables on both
BigQuery and Snowflake (a few seconds, scan billed on the bytes the
analyzer touches; not a metadata-only operation but bounded by the
size of the columns the aggregate references). A `where`-filtered
`COUNT(*)` is **partition-aligned at best, full-scan at worst** — if
the filter aligns with the partition column the scan reads only the
matched partitions; if it doesn't, the warehouse reads the whole table
to evaluate the predicate.
The adapter's `maximum_bytes_billed` cap (default 100 MB; raise via the
profile-level `maximum_bytes_billed` field if needed) plus
`prune.total_budget_seconds` are the safety nets — a `row_count_between`
query that exceeds the cap is rejected by the warehouse before
execution and the test routes to `kept-without-evidence` per the
conservative-bias contract (the `why` field carries the warehouse error
class name).

**Empty-table → `kept` (DEC-010).** An empty warehouse table evaluated
against `minimum=100` produces `n=0`, which violates the bound, which
emits one failing row, which routes to `kept`. **This is the intended
behaviour, not a degenerate edge case**: catching "the table is empty
when it shouldn't be" is exactly what the test exists to do — a
broken upstream pipeline is real signal, and the bounded-cardinality
assertion is the canonical way to surface it. There is no special-case
in the engine; the routing follows the standard decision matrix. If
you're reading a `kept` decision against an empty table and wondering
whether the test "fired correctly," the answer is yes — the bound was
violated and the diff is telling you the upstream is broken. An
operator who wants "empty table is fine" semantics for a particular
model should either not declare `row_count_between` on that model or
add it to `exclude_tests` in `signalforge.yml`.

In the [expected-drop-rate](#expected-drop-rates) framing below,
`row_count_between` tests behave like the built-ins: a model whose
warehouse rows fall comfortably within the bounds is `always-passes`
(dropped, no signal); a model whose row count violates the bound is
`kept` (real signal — exactly the case a reviewer wants to see). The
one categorical difference is that a single `row_count_between`
candidate exercises the **whole table** (or the whole `where`-filtered
slice), not a per-column sample — so its cost is a `COUNT(*)` scan
rather than the per-column sample CTE. Plan budget accordingly on
projects ingesting many existing `expect_table_row_count_to_be_between`
declarations via `prune-existing` — the per-test cost is small but
N-many `COUNT(*)`s adds up.

### `unique_combination` — same engine routing, GROUP BY shape (issue #170)

The seventh test variant, `unique_combination` (see
[`docs/draft-ops.md`](draft-ops.md#composite-uniqueness-unique_combination)
and [`docs/drafter-catalogue.md`](drafter-catalogue.md)), is pruned
through the same orchestrator and routes to the same five `DropReason`
literals. The compiler emits a multi-column GROUP BY identical in shape
to the single-column `unique` test:

```sql
SELECT <col1>, <col2>[, ...] FROM <table> [WHERE <where>]
GROUP BY <col1>, <col2>[, ...]
HAVING COUNT(*) > 1
```

The shuffle cost is the same as single-column `unique` — a GROUP BY
over the same row count produces the same intermediate row count
regardless of key cardinality (bounded by `maximum_bytes_billed`).

**Sample-mode behaviour — engine routes past the materialised sample.**
A sampled GROUP BY is semantically approximate: uniqueness violations
in the full table may not surface in the sample (false-negative). The
prune engine therefore routes `unique_combination` past the
materialised-sample substitution to the **source table**, mirroring
the `row_count_between` metadata-bypass pattern (DEC-006 of #170).
This applies under both `sample_strategy="materialised"` and
`sample_strategy="oneshot"`. Plan cost accordingly — a
`unique_combination` candidate always full-scans the source (bounded
by `maximum_bytes_billed`), never the sample.

### `row_count_anomaly_by_period`

The eighth test variant, `row_count_anomaly_by_period` (issue #171; see
[`docs/drafter-catalogue.md` § `row_count_anomaly_by_period`](drafter-catalogue.md#row_count_anomaly_by_period)),
is pruned through the same orchestrator and routes to the same five
`DropReason` literals as the seven other variants. There is **no new
drop reason**; what differs is the two-query evaluation shape, the
time-bound reproducibility carve-out, and the partition-filter cost
mechanics on date-partitioned source tables.

**Two-query split — stats query first, then violation query (DEC-008).**
Unlike the single-statement variants above, `row_count_anomaly_by_period`
compiles to TWO queries that the engine runs sequentially:

1. **Stats query (Query 1)** — returns one row of method-specific
   statistics from the model's `lookback_periods` of history: `(median,
   MAD, n)` for `method=mad`, `(μ, σ, n)` for `method=zscore`, `(p_lo,
   p_hi, n)` for `method=percentile`, `(min, max, n)` for
   `method=min_max`. Under `seasonality="dow"` the result is one row
   per day-of-week bucket. Populates `AnomalyTestStats` on the
   `PruneDecision`.
2. **Violation query (Query 2)** — the actual band-violation check for
   the most-recent period (the `as_of` bucket). Returns failing rows
   when the bucket's `COUNT(*)` falls outside the band; the adapter
   wraps in the standard `SELECT COUNT(*) AS failures FROM (<sql>) AS t`
   contract.

If Query 1 reports `n_periods < min_samples_per_bucket` (cold-start),
the engine **skips Query 2 entirely** and routes the candidate to
`kept-without-evidence` with structured `why="insufficient history:
<n>/<min> periods"`. The DropReason literal stays at five values —
cold-start re-uses the existing `kept-without-evidence` slot per the
conservative-bias contract (see [Drop-reason taxonomy](#drop-reason-taxonomy)).

**Time-bound reproducibility carve-out — `--as-of YYYY-MM-DD` (DEC-001).**
Every other SignalForge primitive satisfies Architectural Commitment #5
("same input → same prune decision"). `row_count_anomaly_by_period`
cannot: a per-period anomaly check evaluated on Monday and again on
Tuesday may produce different decisions because the underlying band
shifts as history accrues and the "most-recent period" moves forward.
Reproducibility is restored at the `(model, as_of)` granularity via
the `--as-of` CLI flag (`signalforge generate` and
`signalforge prune-existing`).

When omitted, the engine resolves to `date.today()` at prune time and
emits one INFO log line naming the resolved value (lazy-format JSON);
the resolved date lands on every `PruneEvent.as_of` audit field for
after-the-fact reproducibility (re-run with `--as-of <recorded value>`
to reproduce the prior decision). In a multi-model `--select` batch
the same `--as-of` applies to every model — resolved once at the
orchestrator. See [`docs/cli-ops.md` § `--as-of`](cli-ops.md) for the
flag reference.

**Sample-mode behaviour — always routes to source, regardless of
strategy (DEC-002, DEC-009).** A hash-mod sample over a date-partitioned
table does not preserve per-period counts (a 1/N sample shrinks the
"yesterday" bucket the same way as every other bucket, so per-period
anomaly detection on a sample reports the sample's own anomaly
profile — useless). The engine's `_test_requires_source_table` helper
returns `True` for `row_count_anomaly_by_period` under **any**
`sample_strategy` (`materialised` OR `oneshot`), tighter than
`row_count_between` / `unique_combination` (which bypass under
`materialised` only in pre-#171 builds; #171 graduated both to also
bypass under `oneshot` for the same semantic-correctness reason). One
INFO log line names the per-test source override.

**Partition-filter cost mechanics (DEC-012) — load-bearing for cost.**
The compiled SQL **must** include a partition-pruning WHERE clause:

    <date_column> >= <as_of> - INTERVAL <lookback_periods> <period>
    AND <date_column> < <as_of> + INTERVAL 1 <period>

(or the dialect-equivalent form). Without it, the warehouse scans
every partition; with it, BigQuery and Snowflake prune to the lookback
window only.

**Worked example.** A 1-billion-row event table partitioned daily
(`PARTITION BY DATE(event_ts)`) with ~11M rows / day. A 90-day-lookback
`row_count_anomaly_by_period` test:

- **Unfiltered** — scans all 1B rows. At ~10 bytes / row for the
  partitioned-date column alone, that's **~9 GB scanned per test**.
  Over 50 candidate anomaly tests across a project, ~450 GB of
  warehouse cost per `signalforge generate` run.
- **Partition-filter pruned** — scans 90 partitions of ~11M rows each
  (~990M rows narrowed). With BigQuery's partition pruning the
  metadata-only date scan reads ~300 MB. **~30× reduction**; ~15 GB
  across 50 candidates instead of 450 GB.

The `--as-of` value drives the partition-filter literal; choosing
`--as-of 2026-01-15` with `lookback_periods=90 period=day` prunes to
the `[2025-10-17, 2026-01-16)` partition range, regardless of which
date you run the command.

There is **no new opt-in flag** for the partition filter — the
compiler always emits it (DEC-012). The existing
`maximum_bytes_billed` cap remains the safety net: a query that
exceeds it surfaces as `BytesBilledExceededError` → routes to
`kept-without-evidence` per the conservative-bias contract.

**DOW degrade WARNING (US-011).** Under `seasonality="dow"` + thin
per-DOW samples (any DOW bucket below `min_samples_per_bucket`), the
engine **degrades to non-seasonal**: recomputes the stats query
without DOW partitioning and proceeds with the test. The degrade
emits one operator-actionable WARNING log line per test naming the
model, test column, the affected DOW bucket(s), and the per-bucket
counts. The candidate's decision then proceeds normally (typically
`kept` or `dropped`); the WARNING is informational so operators see
when `seasonality="dow"` is asking more of their history than they
have data for. Tune by lowering `min_samples_per_bucket`, widening
`lookback_periods`, or switching to `seasonality="none"` in
`signalforge.yml`.

**Non-BigQuery adapter degrade — `StatsQueryNotSupportedError`.** v0.7
ships the BigQuery override of `WarehouseAdapter.run_stats_query` (the
new vendor-neutral seam for the two-query split). Non-BigQuery
adapters (Snowflake, Postgres) inherit the ABC default, which raises
`StatsQueryNotSupportedError` — the prune engine catches this as any
other `WarehouseError` and routes the anomaly test to
`kept-without-evidence`. Operators on non-BigQuery warehouses see all
`row_count_anomaly_by_period` candidates land in `kept-uncertain` with
the typed error name in the `why` field until each adapter grows its
own `run_stats_query` override. Mirrors the
`MaterialisationNotSupportedError` / `EstimateNotSupportedError` /
`RowCountNotSupportedError` graceful-degrade pattern (see
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md)).

**Cost-and-budget guidance.** A `row_count_anomaly_by_period` candidate
issues 1 (cold-start) or 2 (warm) warehouse queries per test. With the
partition filter active the per-test cost is small (single-digit
seconds, ~300 MB on a billion-row daily-partitioned table); without
the partition column populated the cost grows ~30× and the
`maximum_bytes_billed` cap becomes the actual ceiling. Plan
`prune.total_budget_seconds` accordingly when the project carries N
anomaly candidates: budget ~2-5s per warm candidate plus the
materialisation step's own time when other variants share the run.

## Expected drop rates

**A high drop rate is the working state, not the failure state.** The
LLM is intentionally drafting broadly — it proposes `not_null` on every
column, `unique` on every column that looks like a primary key, etc.
The prune layer trims the candidates that always pass on warehouse
samples (no signal) and the ones that fail on known-clean data (likely
buggy test). What survives is what a human reviewer should actually
look at. A run that drops ~60-80% of the drafted tests on a typical
staging model is doing exactly what it's supposed to.

**Reference numbers — Austin bikeshare staging fixture.** Run captured
2026-05-09 against
`bigquery-public-data.austin_bikeshare.bikeshare_trips` (~2.27M rows,
7 columns), `safety.mode: aggregate-only`,
`prune.sample_strategy: materialised`, `claude-sonnet-4-6` drafter:

- **Total candidate tests drafted:** 8
- **Dropped (`always-passes`):** 5 (62.5%)
- **Kept (`reason="kept"`, non-zero failing rows):** 3 (37.5%)

Per-test-type breakdown for this run:

| Test type | Drafted | Dropped | Kept | Notes |
|-----------|---------|---------|------|-------|
| `not_null` | 7 | 4 (57%) | 3 (43%) | Dropped on natural NOT NULL columns (`trip_id`, `bike_id`, `start_time`, `duration_minutes`); kept on `subscriber_type` (249 nulls — walk-up users), `start_station_id` (199 nulls), `end_station_id` (1408 nulls — stations decommissioned mid-trip) |
| `unique` | 1 | 1 (100%) | 0 | `trip_id` is the natural primary key — `unique` always passes |

The three kept tests are exactly the signal the differentiator
promises: real nullability that a reviewer should think about before
shipping a `not_null` test. The five dropped tests would have been
review-noise — they pass deterministically against the source data.

**Calibration heuristics** (based on internal testing across staging
models):

- **Wide fact tables with mostly-NOT-NULL surrogate keys** drop more
  aggressively — most `not_null` candidates trivially pass; the kept
  set concentrates on FK columns and optional dimensions.
- **Narrow dimension tables** tend to have a higher kept rate — fewer
  drafted tests overall, and the columns under test are usually
  declared-nullable business attributes.
- **`unique` candidates on declared primary keys** are almost always
  dropped as always-passes (the warehouse data matches the model's
  contract). When a `unique` candidate is *kept*, that's high-signal:
  the surrogate-key contract is broken in production data.
- **`relationships` candidates** are usually kept (referential
  integrity is rarely perfect at warehouse scale); when they drop as
  `requires-future-data`, the target model isn't in the manifest yet
  (likely an unimplemented downstream).

A run that drops everything is the failure mode to watch for — it
suggests the LLM proposed nothing the warehouse data contradicted, or
that the model under draft has so little data that every test is
trivially passing on the sample. Set
[`prune.min_kept_rate_warn`](#configuration-signalforgeyml-prune-block)
to surface this case at run time.

## Cost model (US-003 verification)

The deterministic-sample predicate

```sql
WHERE MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), bucket) < 1
```

serialises the entire row into the predicate. BigQuery cannot
column-prune through a function argument, so sample-mode reads **all
columns** of the table, not just the column under test. The Phase-1
estimate that pruning 30 candidate tests against a 100k-row sample would
cost approximately 24 MB / approximately one tenth of one US cent
assumed only the column under test would be read; the worst case is
50–500x that figure.

**Verified figure (US-003): 9,924,771,840 bytes (≈9.92 GB), run 2026-05-01
against `bigquery-public-data.iowa_liquor_sales.sales` (~30M rows, ~24
columns), 100k-row deterministic sample.** AR-B1 confirmed: the
`TO_JSON_STRING(t)` predicate triggers a full-row scan, and the actual
cost is **~99× the Phase-1 estimate** (24 MB) and ~2× the probe's 5 GB
sanity ceiling. The figure is BigQuery's pre-execution analyzer estimate;
the adapter's 100 MB `maximum_bytes_billed` cap (DEC-005) blocked the
query before execution, so this is the cost the user would pay if the
cap were lifted, not a measured `total_bytes_billed` off a completed
job. The pre-execution estimate matches what BQ would bill on a real
prune run (the analyzer reads the same statistics the billing pipeline
uses).

To reproduce:

```bash
gcloud auth application-default login
SF_RUN_BQ=1 pytest -m bigquery tests/warehouse/test_sample_cost_probe.py -s --no-cov
```

The probe currently fails (rather than `xfail`s) on the
`bytesBilledLimitExceeded` path because the assertion ceiling and the
adapter cap are decoupled. Refining the probe to detect the BigQuery
reason code `bytesBilledLimitExceeded` (which appears in the error
message regardless of HTTP status) and `xfail` cleanly is tracked as a
follow-up. Note the SDK exception class is unstable on this path: the
adapter's `map_bq_exception` (`adapters/_client.py`) catches
`google.api_core.exceptions.BadRequest` (HTTP 400), but the live run
on 2026-05-01 with `google-cloud-bigquery==3.41.0` raised
`google.api_core.exceptions.InternalServerError` (HTTP 500). The reason
code is the durable identifier; match on substring rather than the
exception class. The 9.92 GB figure is captured directly from the
error message: `Query exceeded limit for bytes billed: 100000000.
9924771840 or higher required.`

**Q4=A is NOT adequate for v0.1 sample-mode on wide tables.** Issue #22
tracks Q4=C escalation (temp-table-materialised sample) for v0.2. In the
meantime, sample-mode prune runs on tables wider than ~10 columns will
either trip the adapter's 100 MB cap and fail, or — if a maintainer
raises the cap via the profile-level `maximum_bytes_billed` field
(`load_profile`, see `docs/warehouse-adapter-ops.md`) — bill at roughly
`(rows × bytes_per_row)` for **every test** in the candidate set.
Schema-only mode remains the v0.1 default precisely because the cost
model for sample-mode is not where we want it.

Probe thresholds (constants in the test, kept for the post-Q4=C run):

- `_BYTES_WARN_AT = 500_000_000` (500 MB) — soft WARNING fires above this.
- `_BYTES_CEILING = 15_000_000_000` (15 GB) — assertion fires above this; the test fails. Raised from 5 GB during the 2026-05-08 maintainer probe-run after AR-B1's 9.98 GB measurement was confirmed to genuinely exceed the original 5 GB ceiling (probe self-inconsistency).

### Post-Q4=C: temp-table-materialised sample (v0.2, issue #22)

Issue #22 lands `sample_strategy: materialised` as the v0.2 default.
The materialise-once pattern amortises the full-row scan across every
candidate test by pre-computing the deterministic sample into a
`_SESSION._sf_sample_<run_id>` temp table; per-test queries read from
the materialised sample (post-LIMIT, narrow) rather than re-running
`MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), <bucket>) < 1` against
the source table for every test.

Maintainer-run figures (recorded 2026-05-08 against
`bigquery-public-data.iowa_liquor_sales.sales`, ~30M rows, 100k-row
deterministic sample, billed to `duenow-nest`):

- **Materialisation query bytes_billed:** ~9.98 GB (one-time CTAS;
  scans every column once with the deterministic predicate
  `MOD(ABS(FARM_FINGERPRINT(TO_JSON_STRING(t))), <bucket>) < 1`).
  Effectively the same scan as the v0.1 oneshot path, but paid once
  per `prune_tests` invocation rather than once per candidate test.
- **Per-test bytes_billed (representative `IS NULL` test):** **10,485,760
  bytes** (~10 MB). Two orders of magnitude under the 100 MB
  acceptance gate.
- **Total run bytes_billed (1 materialise + 30 per-test):** ~9.98 GB +
  30 × ~10 MB ≈ **10.3 GB**.
- **Cost ratio vs. v0.1 oneshot baseline** (~9.98 GB × 30 = ~299 GB):
  **≈29× cheaper** end-to-end on a 30-test run; ratio scales linearly
  with N as the materialisation cost amortises across more tests.

The two regression-guard tests (`@pytest.mark.bigquery`, DEC-007 of
issue #22):

- `test_sample_rows_cost_baseline_oneshot` — pins the AR-B1 9.92 GB
  baseline for `sample_strategy=oneshot` so a regression in the
  oneshot path stays visible after the materialised default takes
  over.
- `test_sample_rows_cost_materialised` — asserts per-test
  bytes_billed drops below 100 MB under
  `sample_strategy=materialised`.

The 9.92 GB AR-B1 figure remains the v0.1 oneshot reference and the
oneshot fallback's cost story for non-BQ adapters in v0.2.

## Audit JSONL schema

> **Consumer guide.** For cross-stage joins, `jq` / pandas worked examples,
> the forward-compat policy, and the redaction surface, see
> [`docs/audits.md`](audits.md). This section is the prune-layer
> production contract.

Every `PruneDecision` produces exactly one JSONL record at
`audit_path` (default `<project>/.signalforge/prune.jsonl`). One record
per line; atomic concurrent appends via `O_APPEND | O_CREAT | 0o600` and
a single `os.write` (DEC-016). The third instance of the convention
across the codebase — mirrors `signalforge.safety.audit` (DEC-011 of
safety) and `signalforge.draft.audit` (DEC-006/008/013 of llm-drafter).

`PruneEvent` fields:

| Field                  | Type                                | Meaning                                                                                          |
| ---------------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------ |
| `audit_schema_version` | integer                             | Audit shape version. Currently **`4`** (1→2 by #55 when `config_hash` migrated to `blake2b-8`; 2→3 by #171 when `as_of` + `stats` landed; 3→4 by #268 when `bypassed_to_source` landed). The field is a plain `int`, not a `Literal`, so older records still round-trip — audit replay across versions is a real requirement. Bump only on shape change; `extra="ignore"` handles additions. |
| `signalforge_version`  | PEP-440 version string              | Package version that produced the record.                                                        |
| `record_id`            | 32-hex-char string                  | Fresh `uuid4().hex` per record; gives reviewers a stable handle for a single decision.           |
| `timestamp`            | ISO-8601 UTC, microsecond, `Z`      | When the decision was finalised.                                                                 |
| `config_hash`          | 16 hex chars                        | `blake2b(canonical_config_json, digest_size=8)`. Migrated from `SHA-256[:16]` by issue #55 so the audit corpus reads one hash recipe across every writer. Mirrors safety's `policy_hash` (DEC-005). |
| `model_unique_id`      | string                              | dbt `unique_id` of the pruned model.                                                             |
| `test`                 | discriminated-union object          | The original `CandidateTest` from the drafter (typed; not a loose dict — DEC-004). A `custom_sql` test's `sql` body is truncated on the audit copy when over-cap — see the note below the table. |
| `test_anchor`          | string                              | `"column.<name>"` for column-scoped tests; literal `"model"` for model-level tests.              |
| `decision`             | `"kept"` \| `"dropped"`             | Top-level verdict.                                                                               |
| `reason`               | `DropReason` literal                | One of the five reasons in [Drop-reason taxonomy](#drop-reason-taxonomy).                        |
| `failures`             | integer                             | Failing-row count from the warehouse. `0` for `always-passes` and `requires-future-data`.        |
| `sampled_rows`         | integer or `null`                   | Sample size the test ran against. `null` for full-scope or no-warehouse-call decisions — **including a test that bypassed the sample under `scope="sample"`**; read `bypassed_to_source` to tell the two apart. |
| `scope`                | `"sample"` \| `"full"`              | Mirrors `PruneConfig.scope` — the scope the operator **requested**, not necessarily the one this test ran at (see `bypassed_to_source`). |
| `elapsed_ms`           | integer                             | Per-test wall-clock cost. `0` for budget-exhausted (test never ran).                             |
| `compiled_sql_hash`    | 16 hex chars                        | `blake2b(sql.encode(), digest_size=8).hexdigest()` over the **full** compiled SQL — computed before any audit truncation, so it stays the forensic anchor. Stable empty-string hash for no-SQL outcomes. |
| `compiled_sql`         | string                              | The exact SELECT issued to the warehouse, truncated when over-cap (see below). Empty for `requires-future-data` and budget-exhausted. |
| `why`                  | string                              | One-line human-readable rationale. Architectural Commitment #5.                                  |
| `sample_failures`      | array of object or `null`           | Up to `capture_failure_rows` failing rows. `null` when capture is disabled or no failures.       |
| `bypassed_to_source`   | boolean                             | **New in schema v4 (#268).** `true` when the test was routed *past* the sample to the source production table: a metadata-aggregate variant under a sample scope (`row_count_between` / `unique_combination` / `row_count_anomaly_by_period`), or a manifest-ingested `custom_sql` whose body could not be safely rewritten onto the sample relation. `false` under `scope="full"` (there is no sample to bypass), for tests that genuinely ran against the sample, and for decisions taken with **no** routing at all (prune disabled, budget exhausted, or the blanket materialisation-failure degrade where a drafted row-level test forced every candidate to `kept-without-evidence` without compiling). Note the DEC-009 materialisation-failure **fallback** path is `true`, not `false`: when nothing in the batch needed the sample, the candidates re-route to and run against the source, so they *were* routed past the (attempted) sample. Since `scope` is copied verbatim from the config, this is the only field that tells a reviewer whether the verdict came from the sample or from a full scan of the source. |
| `as_of`                | ISO date or `null`                  | Evaluation date for time-bound decisions (`row_count_anomaly_by_period`; #171). `null` otherwise. |
| `stats`                | object or `null`                    | Method-tagged `AnomalyTestStats` from the anomaly stats query (#171). `null` otherwise.          |

**SQL truncation on the audit record (#268).** A `PruneEvent` serialises a
`custom_sql` body **twice** — once as `test.sql` and once as `compiled_sql`. A
real dbt-expectations `compiled_code` is routinely 1–2 KB, so an untruncated
record blew the 4000-byte per-line cap and raised
`PruneAuditRecordTooLargeError`, aborting the run mid-batch. The cap itself is
load-bearing (`PIPE_BUF` atomic concurrent appends) and is not raised; instead
**both** SQL surfaces are bounded to a 1200 JSON-escaped-byte prefix with a
**visible** truncation marker (`-- [signalforge: SQL truncated for the audit
record …]`). The metric is JSON-escaped bytes, not raw characters: the writer
serialises with `ensure_ascii=True`, so a multibyte character can escape to up
to 12 bytes on the line — a raw-character budget would under-count and a crafted
multibyte body could still overflow the cap.
`compiled_sql_hash` is computed over the full SQL, so the forensic chain
survives, and the in-memory `PruneDecision` handed to the diff / grade stages is
**not** truncated. Under-cap bodies (every drafted built-in) are byte-identical
to pre-#268.

**Fail-closed semantics.** `OSError` / `PermissionError` / encoding
failures from `os.write` / `os.fsync` propagate raw; the orchestrator
wraps them as `PruneAuditWriteError` and aborts the run.
`PruneAuditRecordTooLargeError` (size cap, raised before any file open)
also aborts the run. Don't wrap audit-write calls in defensive
try/except — propagation IS the defence (mirrors
`safety-layer.md` DEC-011).

**Schema-drift gate.** `tests/fixtures/prune/prune_event_v1.jsonl` is
the canonical schema fixture; `tests/prune/test_drift_detector.py`
pairs the production model (`extra="ignore"`) with a one-off
`extra="forbid"` strict model and validates against the fixture.
Adding a field to `PruneEvent` without updating the strict model OR
the fixture breaks the test loudly. Don't bypass.

### Audit reading guide: spotting materialised vs. oneshot runs (issue #22)

The `compiled_sql` field on every `PruneEvent` is the durable signal
that distinguishes a materialised-strategy run from a oneshot run:

- **Materialised (v0.2 default):** every test's `compiled_sql`
  references the temp table — look for `FROM \`_SESSION._sf_sample_<run_id>\``
  (two-part `_SESSION._sf_sample_<run_id>`; no `<project>.` prefix
  because the adapter returns `TableRef(project=None, ...)` —
  BigQuery rejects the three-part `<project>._SESSION.<name>` form
  even inside the owning session). The `_SESSION` dataset and the
  `_sf_sample_<16-hex>` table name are load-bearing — `_SESSION` is
  BigQuery's session-scoped namespace, and the 16-hex `run_id`
  derives deterministically from
  `blake2b(table.qualified_name + signalforge_version + str(n) + canonical_json(partition_filter), digest_size=8).hexdigest()`
  (inputs joined with NUL separator). Same input → same `run_id` →
  same `compiled_sql_hash` (DEC-001 of #22).
- **Oneshot (v0.1 fallback / non-BQ adapters):** `compiled_sql`
  references the source table directly — no `_SESSION` prefix.

Joining a `prune.jsonl` line to the materialisation query in
`INFORMATION_SCHEMA.JOBS_BY_PROJECT` is the operator's path for
post-mortem cost attribution; see
[`docs/warehouse-adapter-ops.md` § Session cleanup & manual recovery](warehouse-adapter-ops.md#session-cleanup--manual-recovery)
for the query template.

Since #268, `compiled_sql` alone is no longer sufficient to tell whether a given
test in a materialised run actually *read* the sample: a metadata-aggregate
variant or a non-rewritable manifest-ingested body is dispatched against the
**source** even inside a materialised run. `bypassed_to_source` (schema v4) is
the field that says so directly — filter on it before drawing cost or coverage
conclusions from a mixed run.

A `kept-without-evidence` decision whose `why` field starts with
`"sample materialisation failed: "` is the conservative-bias signal
that materialisation raised at orchestrator entry — every candidate
in the run shares the same `why` shape, and the operator should
inspect the orchestrator-level WARNING (see
[`docs/cli-ops.md` § Stderr shapes](cli-ops.md#stderr-shapes-warning))
for the materialisation error class and message. **Since #268 that blanket
degrade only fires when a *drafted* row-level test is in the batch:** if every
candidate could have run against the source anyway, the engine falls back to
full scope against the source and returns real verdicts, logging one WARNING
carrying `"fallback": "source"` instead.

## Audit log sensitivity

`prune.jsonl` contains the model's compiled SQL and (when
`capture_failure_rows > 0`) up to N rows of failing data per test.
Treat the file at-rest the same way you treat the safety audit:

- **Gitignore `.signalforge/`** (already configured in this repo's `.gitignore`).
- **Restrict at-rest permissions.** The writer creates the file at `0o600` on first call; the parent directory is created via `mkdir(parents=True, exist_ok=True)` (Python's `mkdir` does not tighten an existing directory's permissions, so verify the existing `.signalforge/` mode is `0o700` on shared hosts).
- **Don't ship as a build artifact.** Strip from container images and CI uploads.
- **Set `capture_failure_rows: 0`** for PII-laden models if the safety layer's redaction policy isn't enough — the prune layer captures real warehouse rows for failures, which can include PII not flagged for the LLM redactor.

## Running real-warehouse tests

The prune layer's integration tests share the warehouse adapter's
gating discipline. Default CI excludes them via the `-m 'not bigquery'`
filter; opt-in requires both the marker and an `SF_RUN_BQ=1` env var.

```bash
gcloud auth application-default login
SF_RUN_BQ=1 pytest -m bigquery --no-cov
```

The prune-layer integration test (`tests/prune/test_integration_bigquery.py`)
requires ambient gcloud auth (matches the warehouse adapter's
`tests/warehouse/test_bigquery_integration.py`).

The diagnostic cost probe (US-003) at
`tests/warehouse/test_sample_cost_probe.py` runs under the same gate.
It is a documentation-grade probe — a soft WARNING fires at 500 MB; the
test fails only above the 5 GB sanity ceiling. See
[Cost model](#cost-model-us-003-verification).

## Debugging

Logger name: `signalforge.prune.engine` (and sibling modules under
`signalforge.prune`).

```python
import logging
logging.getLogger("signalforge.prune").setLevel(logging.DEBUG)
```

Levels:

- **WARNING** — One line per `kept-without-evidence` decision routed by a typed `WarehouseError`. Lazy-format JSON per DEC-017 (`signalforge_version`, `model_unique_id`, `test_anchor`, `error_class`). Never f-string-interpolate user-controlled strings into a logger call — a column name or model id containing ANSI escapes (`\x1b[31m...`) would inject into log viewers.
- **INFO** / **DEBUG** — reserved for future budget-loop / batching observability; v0.1 emits no INFO/DEBUG from the engine.

The prune layer never logs full row data. The audit JSONL is the single
durable record of decision-level detail; logger output is a hint that
the decision happened, not what was in it.

**Reading a fail-closed `PruneAuditWriteError`.** The cause is exposed
as `.cause` and on `__cause__`. Common causes:

- Parent directory not writable (no `+w` for the user, or `.signalforge/` is a symlink to a read-only mount).
- Disk full (`ENOSPC`).
- Oversize record (raises `PruneAuditRecordTooLargeError` instead — reduce `capture_failure_rows` or trim `compiled_sql` size by simplifying the candidate test; the cap is 4000 bytes for POSIX-atomic concurrent appends).

## Snowflake compiler dialect (v0.2, issue #121)

The prune compiler emits valid Snowflake SQL purely from the `SNOWFLAKE_DIALECT`
value object — no branching on warehouse name, no warehouse SDK import under
`signalforge/prune/`. Everything warehouse-specific is read from `Dialect`
fields: the identifier quote char, per-component vs whole-path qualified-name
quoting, the deterministic-sample row-hash expression, the date/timestamp
literal cast form, the sample-CTE alias, and `identifier_case`.

**Identifier case-folding.** Snowflake folds *unquoted* identifiers to
UPPERCASE and matches *quoted* identifiers verbatim, so a conventional
`CREATE TABLE … (customer_id …)` stores the column as `CUSTOMER_ID`. The
compiler therefore folds every identifier (columns and each qualified-name
component) to UPPER **before** quoting (`"CUSTOMER_ID"`, `"DB"."SCHEMA"."T"`),
which resolves correctly against conventionally-created Snowflake tables while
keeping the always-quote injection-safe posture. **Residual:** a table
genuinely created with quoted-lowercase DDL (`CREATE TABLE … ("customer_id" …)`)
would not match `"CUSTOMER_ID"` — accepted as the rare case; the conventional
majority is the right default. BigQuery's `identifier_case="preserve"` is a
no-op, so BigQuery output is byte-identical to v0.1.

**Sampling reproducibility caveat.** BigQuery's `FARM_FINGERPRINT` is stable
across time, so the deterministic sample is reproducible indefinitely.
Snowflake's `HASH()` is deterministic only *within a Snowflake release* —
Snowflake documents that it may change across versions. This satisfies
SignalForge's reproducibility commitment (same input → same prune decision
*within a run*) but is a weaker cross-time guarantee than BigQuery's. Prune
decisions made in one run are internally consistent regardless.

**`QUALIFY` not used.** Snowflake supports `QUALIFY`, but the `unique` test
keeps the dialect-portable `GROUP BY … HAVING COUNT(*) > 1` (works on both
warehouses); `supports_qualify` stays forward-compat metadata.

**Validation.** Snowflake SQL is pinned by byte-exact snapshot fixtures under
`tests/fixtures/prune/compiled_sql/snowflake/`. A maintainer-only gated suite
(`uv run pytest -m snowflake --no-cov`) executes the four built-ins through
`fakesnow` (asserting failing-row *shape* by rule semantics, never `HASH()`
values) and parses every fixture through `sqlglot`'s Snowflake dialect.
Real-Snowflake `HASH(*)` semantics, true case-folding, and sampling behaviour
are validated by the live harness in issue #124.

## v0.2 deferrals

The prune layer is intentionally narrow in v0.1. The following
concerns are explicitly deferred:

- **Per-decision `bytes_billed` recording (DEC-027).** The adapter does not surface job stats in v0.1; the diagnostic probe (US-003) reads them via `INFORMATION_SCHEMA.JOBS_BY_USER` out-of-band rather than through the adapter API. v0.2 extends the adapter's seam to return job stats so the `PruneDecision` can carry the figure natively.
- **Per-test `timeout_ms` threading.** `PruneConfig.test_timeout_seconds` is documented but not yet threaded through `WarehouseAdapter.run_test_sql` per call. The plumbing exists in `make_query_job_config` (DEC-013, AR-B2 of issue #6); surfacing it through the public adapter signature is a v0.2 task.
- **Test batching — Q4=B / Q4=C optimisations.** The Phase-1 plan catalogues two cost optimisations (per-column `COUNTIF` batching; temp-table-materialised sample). v0.1 does not adopt either. US-003 produces the data needed to evaluate the temp-table option in v0.2.
- **Multi-warehouse adapters.** Postgres, Databricks, Redshift adapters slot in behind `WarehouseAdapter` without prune changes once their adapters land. The prune compiler is fully dialect-driven (DEC-025), reading all warehouse-specific SQL from the `Dialect` value object, never branching on dialect `name`. **Snowflake compiler support landed in issue #121** (see § "Snowflake compiler dialect" above); its live warehouse harness is #124. **Databricks compiler support landed in issue #223:** `DATABRICKS_DIALECT` emits Spark/Databricks SQL (backtick quoting, `xxhash64` sign-bit-masked sampling hash, Spark date-arithmetic fragments), pinned by byte-exact snapshot fixtures under `tests/fixtures/prune/compiled_sql/databricks/` and certified for syntactic validity by an **ungated** `sqlglot` `databricks`-dialect parse-guard over every fixture (`tests/prune/test_compiler_databricks.py`, runs in the default suite — no marker — because `sqlglot` is a base dep and Databricks has no offline execution fake); real-Spark execution semantics are deferred to the live harness in #226. A new vendor populates a `Dialect` and the compiler emits correct SQL with no compiler change.
- **Confidence intervals on `always-passes`.** Surfacing "less than or equal to 3/N upper-bound failure rate at 95 percent confidence" (rule of three) on the decision record so reviewers can calibrate the always-pass verdict. Also covers great-expectations-style `mostly:` thresholds.
- **Historical always-pass evidence.** Running candidate tests against multiple `run_results.json` snapshots to assert "never failed in last N runs." The Phase-1 plan considers this for the `failed-on-known-clean-data` evidence channel and defers to v0.2.
- **dbt-utils test types.** `dbt_utils.unique_combination_of_columns`, `dbt_utils.accepted_range`, `dbt_utils.expression_is_true`, etc. The drafter's `CandidateTest` union has six variants — the four generic schema tests plus the `custom_sql` business-rule escape hatch (issue #116) plus `row_count_between` (#169); the prune compiler compiles all six. **One `dbt_expectations` macro graduated in #169:** `dbt_expectations.expect_table_row_count_to_be_between` is recognised by `prune-existing` and promoted to the structured `row_count_between` variant (see `docs/ingest-ops.md` § "Recognition of `expect_table_row_count_to_be_between`"). Other namespaced dbt-utils / dbt-expectations macros remain v0.2+ territory (a `custom_sql` test can express many of them by hand in the meantime).
- **`where:` test modifier and `severity: warn` / `mostly:`.** dbt-core supports a `where:` predicate on every test plus `severity` and `mostly` knobs; v0.1 prune does not consume any of these.
- **`prune_decision_id`-keyed checkpoint / resumption.** Long-running prune runs that resume from disk after a crash. v0.2.
- **LLM-generated rationale on `kept` decisions.** The grader (#7) produces rubric-scored rationale; prune writes only the structured drop reason plus failure count plus scope.

## CLI integration note

Tracked in [issue #9](https://github.com/wjduenow/SignalForge/issues/9).
The `signalforge generate` CLI will load the prune config via
`load_prune_config(...)` and invoke `prune_tests(...)` after the LLM
draft completes; the diff renderer (#8) consumes the returned
`PruneResult` to emit kept/dropped artifacts with their per-decision
`why` lines (Architectural Commitment #5 — explainable diffs).
