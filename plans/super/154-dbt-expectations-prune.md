# Super Plan — #154: dbt-expectations prune+grade adapter via manifest `compiled_code`

## Meta

- **Ticket:** #154 — Scope: dbt-expectations prune+grade adapter via manifest compiled_code
- **Branch:** `feature/154-dbt-expectations-prune`
- **Worktree:** in-place (main checkout, `feature/154-dbt-expectations-prune`)
- **Phase:** devolved (draft PR #266, base `dev`; epic `bd_1-scaffolding-ajm`)
- **Sessions:** 1 (2026-07-03)
- **Follow-up issues (deferred):** #267 (aggregate/scalar macros — source-table routing),
  #268 (`scope=sample` via sqlglot AST relation-rewriting)
- **Structural precedent:** #116 (custom_sql, the 5th test type) — single plan, devolved to beads. NOT the Airflow epic #228.

## Summary

Close the **un-graded half** of the prune gate for teams that already author
`dbt-expectations` (and other generic/namespaced) tests. Today the ingest layer
recognises these tests and routes them to `IngestResult.skipped` with
`SkipReason="custom-or-generic-test"` — they pass through untouched, so
"signal over volume" (Architectural Commitment #1) covers only the four built-ins
+ `custom_sql`.

The cheap path leans on dbt's own work: after `dbt compile`, every test node in
`manifest.json` carries `compiled_code` (already Jinja-resolved). We read that
string off `resource_type == "test"` nodes and route it through the **existing
`custom_sql` prune pipeline** (`resolve → safety-check → wrap → conservative-bias
routing`), so it gets the same kept / kept-uncertain / dropped / flagged treatment
as everything else. The integration home is the read-only, no-LLM `prune-existing`
CLI.

### Locked at kickoff (framing inputs — challenge in refinement)

- **Scope:** prune-only-via-`custom_sql`. **No drafting** of dbt-expectations tests
  (macro semantics in the prompt / macro→`custom_sql` translation is out of scope;
  the drafting story stays `meta.signalforge.business_rules → custom_sql`).
- **Variant strategy (recommended, near-locked):** REUSE `CandidateTestCustomSQL`
  (`type="custom_sql"`, `column=None` ⇒ model-level) to carry the compiled SQL —
  NOT a 7th `CandidateTest` variant. Collapses the 6-dispatch-site burden to ONE
  new recognition arm; no `_PROMPT_VERSION` rotation, no grade/rubric change, no
  `audit_schema_version` bump. (Convention Checker §9, §27–28.)

## Discovery

### Code seams to extend (Codebase Scout findings)

**Manifest layer — the one genuinely net-new foundation.**
- `manifest/loader.py:298–303` (DEC-017) filters `nodes` to `resource_type == "model"`
  ONLY. Test/seed/snapshot nodes are dropped **before** `Manifest` construction.
  `Manifest.nodes` is `dict[str, Model]` (`models.py:211`); `Model.unique_id`
  hard-rejects anything not starting with `"model."` (`models.py:165–170`).
- **No `Test`/`GenericTest` Pydantic model exists; nothing reads a test node's
  `compiled_code`.** Reading test nodes is a genuinely new ingestion surface —
  a new frozen read-back model + a new `resource_type == "test"` filter arm in
  `_load` (`loader.py:294–333`), parallel to `Manifest.nodes`/`Manifest.sources`.
- **`compiled_code` availability mirrors the catalog.json/`data_type` gap exactly**
  (`manifest-readers.md` §catalog, #159): `dbt parse` does NOT populate it; only
  `dbt compile` / `dbt run` / `dbt docs generate` does. Confirmed empirically —
  `compiled_code` is `null` on every committed fixture (all `dbt parse` output).
  `ModelMissingSqlError` (`manifest/errors.py:82`, DEC-004) is the existing
  precedent for "this field is only present after a real build — don't silently
  fall back."

**Ingest layer — the new recognition arm.**
- `ingest/parser.py:243–249` hard-skips dotted/namespaced tests
  (`dbt_expectations.*`, `"." in name` or dict body) → `SkipReason="custom-or-generic-test"`.
- Ingest reads `schema.yml` (`read_schema`) + `tests/*.sql` (`read_test_files`) from
  disk ONLY. **No code path consumes manifest test nodes** — a new ingest bridge
  (manifest test node → `CandidateTestCustomSQL`) is needed.
- `SkipReason` is a **closed 3-value Literal** (`ingest/models.py:36–40`): reuse an
  existing reason for no-`compiled_code`/unparseable cases, never grow the enum.

**Prune layer — reuse `custom_sql`; add a determinism filter.**
- `_compile_custom_sql` (`compiler.py:615`) is the reuse target: `resolve → validate_test_sql
  → wrap`. **manifest `compiled_code` is already Jinja-resolved**, so the resolve
  step is a no-op — but `validate_test_sql` (safety-check) MUST still run before wrap.
- **No non-deterministic-SQL detection exists today** — `custom_sql` trusts input
  (only `validate_test_sql` for stray `;`/`--` + a JOIN heuristic). A determinism
  filter (TABLESAMPLE / time-dependent) is **net-new**; route its negative outcome
  through the existing `_InvalidIdentifier` sentinel → `engine.py:1595` →
  `kept-without-evidence` (no `DropReason` expansion — the 5-value lock holds,
  `models.py:55–61`).
- Materialised-sample substitution: a row-returning compiled body is **Direction 1**
  (builds its own `FROM` → compiler must rewrite the source qualified name → the
  temp `_SESSION._sf_sample_*`, else it full-scans production). A metadata/aggregate
  body (e.g. `expect_table_row_count_*` → `COUNT(*)`) is **Direction 2** (sampling
  is semantically wrong). All flow as `custom_sql`, so the engine cannot
  discriminate by type — **this is a refinement fork** (§Q3/Q4 below).

**Grade layer — auto-pickup, but the artifact-text fork is real.**
- Grade resolves artifact text via **string-discriminator** (`t.type == test_type`),
  so `custom_sql` is already in the catalogue — no grade-side arm change. BUT grade
  scores each test's `rationale` (`grade/engine.py:234/238`), and an ingested
  compiled test has **no drafter rationale**. What (if anything) is graded is a fork
  (§Q1 below). The `artifact_id` `args_hash` collision suffix (hashed over `sql`) is
  load-bearing since every ingested test shares `type="custom_sql"` on the same model.

**Diff layer — macro name must ride into `why`.**
- `custom_sql` today routes to `proposed_test_files` (standalone `.sql`, the 6th
  fail-closed writer). Ingested-from-manifest tests are **read-only** (we didn't
  author them) → they belong on the kept/dropped/flagged table, NOT `proposed_test_files`.
- The macro name (`test_metadata.name` / `unique_id`) MUST travel manifest → candidate
  → diff `why` so the operator can locate and remove the right test in their project.

**CLI — `prune-existing` is the home.**
- `cli/prune_existing.py:489` `cmd_prune_existing`: ingest → prune → diff, no LLM,
  read-only (no `--write`). `prune_tests` already receives `manifest` (`:657`) but
  nothing extracts test-node compiled SQL from it.

### Rule constraints to satisfy (Convention Checker findings — condensed)

*Full 52-item checklist retained in the refinement log; the load-bearing ones:*

- **[manifest-readers.md]** New test-node model = Pydantic v2 `frozen=True,
  extra="ignore"` + a MANDATORY `Strict*` `extra="forbid"` drift detector against a
  committed fixture. Symlink-hardened path via existing `canonicalise_path`. No
  logging (stage-0). No `_PROMPT_VERSION` rotation.
- **[ingest-layer.md]** `SkipReason` stays 3-value (reuse, never grow). No new
  `CandidateTest` subclass. Reader carries raw SQL string; identifier/SQL validation
  deferred to prune (warehouse-agnostic). Anchor-contract: model-level `custom_sql`
  (`column=None`) is anchor-exempt; unmodellable tests skip-and-record, never fail-loud.
  No new `errors.py` (scan-7 asserts exactly 14).
- **[prune-engine.md]** `DropReason` 5-value LOCK (never a 6th). Conservative-bias
  routing template verbatim. `validate_test_sql` on compiled SQL before wrap. No new
  `PruneEvent` field ⇒ no `_PRUNE_AUDIT_SCHEMA_VERSION` bump. Compiler stays
  dialect-driven (no `google.cloud` import). **Materialised-sample Direction-1
  substitution MUST be pinned with a behavioural assertion (dispatched SQL references
  `_SESSION._sf_sample_*`, never the source), not just a compiler snapshot.**
- **[grade-layer.md]** No rubric change, no grade `_PROMPT_VERSION` rotation.
  `<ARTIFACT>` envelope breach guard still applies to whatever text is graded.
  Whatever the no-rationale decision, route through graceful-degrade
  (`score=None`, `aggregate_complete=False`), never silent drop; a default-budget
  partial fails loud via `require_complete` (#202).
- **[diff-renderer.md]** `tier` stays 4-value; `kept-without-evidence` → `kept-uncertain`;
  no `audit_schema_version` bump. Macro name into `why`. Ingested tests on the
  kept/dropped table, NOT `proposed_test_files`. Em-dash for N/A score; no emoji.
- **[cli-layer.md]** 4-tier exit codes; no new tier. `prune-existing` #105
  conventions: no bespoke `Cli*` wrappers if lib errors already mapped; audit each
  inherited flag; read-only default. IF a new flag lands → 5-surface parity + SKILL.md
  (6th surface). A non-flag capability does NOT trip the mechanical SKILL parity gate
  but still obligates a prose SKILL.md update if what it teaches changes.
- **[testing-signal.md]** No `assert True`. Drift detector + committed fixture for the
  new read-back model. Fixture MUST come from `dbt compile` (not `dbt parse`) so test
  nodes carry `compiled_code`; strip `generated_at`/`invocation_id`; commit a regen
  script. Engineered-determinism: tautological compiled test → `always-passes`;
  engineered-failing → `kept`.
- **[docs]** ingest-ops / prune-ops / diff-ops / cli-ops updates. Watch the #170
  mkdocs gotcha (literal `##` inside a fence breaks anchors — use 4-space indent).

### Proposed scope (pre-refinement)

A single cohesive plan devolving to ~7–9 beads:

1. **Manifest test-node reader** — new frozen `GenericTest` read-back model
   (`unique_id`, `compiled_code`, `test_metadata{name,namespace,kwargs}`,
   `column_name`, `attached_node`/`depends_on.nodes`, `file_key_name`) + a
   `resource_type == "test"` filter arm; silent-degrade on absent/null `compiled_code`
   (catalog.json posture); drift detector + `dbt compile`d fixture + regen script.
2. **Ingest bridge** — manifest test node → `CandidateTestCustomSQL` (model-level);
   macro identity threaded onto the candidate; unrecognised/no-compiled-code → existing
   `SkipReason` with a remediation detail; determinism/aggregate filter.
3. **Prune determinism filter** — detect non-deterministic (TABLESAMPLE / time-dependent)
   compiled bodies → `_InvalidIdentifier` → `kept-without-evidence`.
4. **Diff macro-name `why`** — thread the macro identifier into the diff entry; ensure
   ingested tests land on the table, not `proposed_test_files`.
5. **Grade-artifact resolution** (per §Q1 outcome).
6. **`prune-existing` CLI wiring** — manifest-test ingest arm (+ flag per §Q5).
7. **Docs + worked example** — a real dbt-expectations `schema.yml` compiled to a manifest.
8. **Quality Gate** (code-review ×4 + CodeRabbit).
9. **Patterns & Memory** — update `.claude/rules/` (ingest / prune / manifest-readers)
   + memory.

## Scoping answers (locked)

1. **Grade scope → LLM-grade with a synthesized rationale.** Ingested compiled tests
   get a synthesized artifact text (macro name + args, e.g.
   `expect_column_values_to_be_between(column=x, min=0, max=100)`) and ARE scored by
   the LLM judge — the literal "prune+**grade**" per the issue title, not
   prune+diff-taxonomy-only. **Consequence / open architecture concern:** the home
   `prune-existing` is currently no-LLM / no-grade (#105). This pass must add a grade
   capability to that path (an opt-in grade stage) OR route the graded manifest-test
   flow through a graded entry point. Resolve in refinement (see AR concern).
   The synthesized rationale also feeds the diff `why` cascade (rationale → evidence →
   fallback), so it is load-bearing regardless of where grade runs.
2. **Macro coverage → macro-agnostic + determinism/aggregate filter.** Any test node
   with `compiled_code` that is deterministic AND row-returning becomes a candidate;
   covers dbt-utils / in-house macros with no per-macro code. A determinism+shape
   filter routes unsafe bodies to `kept-without-evidence` / skip. Satisfies the
   "macro-agnostic enough that dbt-utils/in-house work too" AC.
3. **Aggregate macros → row-returning only in pass 1; skip-record aggregates.**
   Aggregate-shaped compiled bodies (`COUNT(*)`, single numeric — `expect_table_row_count_*`,
   `expect_column_mean_*`) are detected and skip-recorded with a remediation detail.
   Sampling stays Direction-1-safe for every ingested candidate. Aggregate support
   (source-table routing, the row_count_between Direction-2 precedent) is an explicit
   follow-up, NOT this pass.
4. **Enablement → opt-in flag on `prune-existing` (e.g. `--from-manifest`).**
   Existing prune-existing behavior stays byte-unchanged; operators opt in explicitly.
   Triggers 5-surface parity (help / docstring / cli-ops / test / DEC) + SKILL.md
   (6th surface). Exact flag name TBD in detailing.

## Architecture Review

Four focused reviews (SQL-handling/determinism, manifest read-back backward-compat,
grade-on-prune-existing wiring, fixture feasibility). **Overarching signal: #154 is a
sqlglot-AST feature, not a string-substitution one.** sqlglot is already a hard runtime
dep (`pyproject.toml:24`), confined to `draft/parser` today (#159). Four SQL sub-areas
(determinism detection, aggregate-shape gate, comment-tolerant validation, and — if
pursued — relation rewriting) independently need AST parsing; the `#116`
string-substitution / substring-validation machinery is **safe but wrong-tool** for
dbt's foreign-rendered SQL. #154 extends the sqlglot confinement to `ingest` + the
`prune` compiler.

| # | Area | Rating | Finding |
|---|------|--------|---------|
| 1 | Manifest read surface (`Manifest.tests` sibling filter) | **pass** | Test nodes already flow through `loader.py:299` and are discarded; a sibling `resource_type=="test"` filter → `Manifest.tests: dict[str,GenericTest]=Field(default_factory=dict)` is idiomatic, survives the frozen `model_copy` catalog overlay, breaks no `Manifest(...)` call site. |
| 2 | Test→model association | **concern** | No single authoritative field across v9–v12: `attached_node` is v10+ (absent in v9, which the repo supports); loader **discards** the detected version (`loader.py:292`). Must **feature-detect** via a precedence ladder (`attached_node` → `file_key_name` + `depends_on.nodes` disambiguated by `column_name`/`test_metadata`), with per-version fixture coverage. |
| 3 | Manifest drift detector + compiled fixture | **blocker → resolvable** | `StrictGenericTest(extra="forbid")` mirror + committed fixture is mandatory, but **no committed fixture has a test node or populated `compiled_code`** (all are `dbt parse` output). Resolved by the fixture work (row 13). |
| 4 | Grade home | **pass (Option A)** | `generate` drafts, never ingests — structurally cannot reach ingested tests. Grade must live on `prune-existing` as an opt-in `--grade` stage (between prune `:657` and diff `:686`, feeding `grading_report=`). Credential gate is implicit (DEC-006/#135 — missing key → `LLMAuthError` tier 3 at call time); off-by-default preserves the zero-credential property for existing runs. |
| 5 | `--grade` ↔ `--from-manifest` coupling | **concern** | `--grade` is only meaningful coupled to `--from-manifest` — schema.yml / singular tests carry `rationale=None`, so grading them is noise. Recommend: `--grade` **requires** `--from-manifest` (argparse error otherwise). |
| 6 | `artifact_id` / `args_hash` collision | **pass** | Every ingested test on a model shares `type=custom_sql`; `compute_args_hashes` already emits an 8-hex `args_hash` suffix over `{type,column,sql}` (+`:n` ordinal on exact-dup) → no `(run_id,artifact_id,criterion_id)` JSONL collision. No change. |
| 7 | Rationale as grade text | **pass** | Synthesize the rationale at ingest **construction** (frozen model — build with it, never mutate); grade picks it up at `engine.py:234/238` with **zero grade-side edit**. |
| 8 | Direction-1 sample substitution | **blocker → deferral** | dbt renders the relation with its own quoting (`` `proj`.`ds`.`tbl` `` — three backtick pairs on BQ) matching neither `own_qualified` (unquoted) nor `own_table_quoted` (single pair). Existing fail-closed guard (`compiler.py:752-758`) holds — **not** a prod full-scan risk — but every ingested test silently degrades to `kept-without-evidence` under `scope=sample`. Resolution: **pass 1 evaluates ingested tests at `scope=full`** (INFO if `--scope=sample` requested); sqlglot AST relation-rewriting for sampling is an explicit follow-up. |
| 9 | Determinism filter | **concern (net-new)** | No detection today. Must catch `TABLESAMPLE`, `RAND/RANDOM`, `CURRENT_TIMESTAMP/CURRENT_DATE/NOW()/GETDATE()`, `ORDER BY RAND()…LIMIT`, `UUID/GENERATE_UUID`. Substring/regex is unsafe (false-positives on `random_id` column names, literal contents) → **sqlglot AST function-node inspection**. Placement: **ingest skip-record (primary) + compiler `_InvalidIdentifier` fallback (belt-and-braces)**. |
| 10 | Aggregate-shape gate | **concern (must-build correctness)** | NOT just cost. Wrapping a scalar/aggregate body in `SELECT COUNT(*) AS failures FROM (<sql>) AS t` yields `failures=1` **always** → silent wrong `kept` verdicts (the exact `row_count_between` bug, `compiler.py:838-846`). Detect via sqlglot: all top-level projections aggregate + no `GROUP BY` → scalar → skip-record. Mandated by scoping answer #3. |
| 11 | `validate_test_sql` over-rejection | **concern (borderline blocker)** | dbt-compiled SQL routinely carries `--` and `/* */` comments; `validate_test_sql` (`_sql_safety.py:257-260`) rejects both → **mass false-reject** → most real bodies degrade to `kept-without-evidence` → ingestion "nearly useless." Needs a **comment-tolerant validation path** (strip comments before the safety scan, or sqlglot single-statement validation) for ingested SQL. |
| 12 | `</ARTIFACT>` envelope breach | **concern (low-prob, whole-run blast radius)** | A macro arg (regex / value list) containing the literal `</ARTIFACT>` fails the **entire** grade run closed (`grade/engine.py:287`). Mitigate cheaply at synthesis: strip/escape `</ARTIFACT>` (and the `<\n/ARTIFACT>` whitespace-split variant) from the macro-args string before it becomes the rationale. |
| 13 | Fixture generation | **pass** | Real `dbt compile` on **dbt-duckdb + dbt-expectations** is credential-free + Ralph-runnable (the hand-craft escape hatch is gated on *lack of credentials*; duckdb is embedded). Extend `regenerate.sh` (`packages.yml` + `dbt-expectations`, `dbt deps && dbt compile`, broaden the `jq` scrub). **Per-node hand-patch** fallback only if a single macro won't compile on duckdb. |
| 14 | Engineered determinism | **pass** | `test_e2e_row_count_between.py` is the near-exact template — engineers dbt-expectations determinism **through macro args** (dbt compiles them deterministically): impossible bounds → kept; vacuous bounds → always-passes. Unit leg: `FakeBigQueryClient.expect_query(returns=[{"failures":N}])`. |
| 15 | Unit surface w/o warehouse | **pass (strong)** | `FakeBigQueryClient` already drives the `custom_sql` prune path with **no extension** (#154 reuses that pipeline). The three new pure functions (ingest bridge, determinism filter, aggregate detector) are string-in/verdict-out → fully unit-testable. |

**Blockers, both resolvable (no design dead-ends):** (3) the compiled fixture — resolved by
extending the regen script; (8) `scope=sample` substitution — resolved by deferring
sampling for ingested tests (full-scope pass 1). No architecture dead-ends; the feature
is buildable.

## Refinement Log

### Decisions

- **DEC-001 — Reuse `CandidateTestCustomSQL`, no 7th variant.** Manifest-compiled test
  SQL flows as the existing `custom_sql` type (`column=None` ⇒ model-level). Collapses
  the 6-dispatch-site burden to ONE new recognition arm; no `_PROMPT_VERSION` rotation,
  no grade/rubric change, no `audit_schema_version` bump. *(Rationale: Convention
  Checker §9/§27–28; the input differs (dbt-rendered vs LLM-drafted) but the pipeline
  shape is identical.)*

- **DEC-002 — LLM-grade with a synthesized rationale.** Ingested tests get artifact
  text synthesized from macro name + arg summary and ARE judged. Grade lives on
  `prune-existing` as an opt-in `--grade` stage (Option A — `generate` structurally
  can't reach ingested tests). Off by default; `--grade` **requires `--from-manifest`**
  (argparse error otherwise). Credential gate is implicit (DEC-006/#135 — missing key →
  `LLMAuthError` tier 3); existing prune-existing runs stay byte-unchanged / zero-cost.
  *(Scoping answer #1 + AR rows 4/5; user-confirmed coupling.)*

- **DEC-003 — Macro-agnostic + determinism/aggregate filter, not an allowlist.** Any
  test node with `compiled_code` that is deterministic AND row-returning becomes a
  candidate. Covers dbt-utils / in-house macros with no per-macro code. *(Scoping
  answer #2; satisfies the macro-agnostic AC.)*

- **DEC-004 — Row-returning only in pass 1; aggregate bodies skip-recorded.** Aggregate/
  scalar-returning compiled bodies (`COUNT(*)`, single numeric) are detected and
  skip-recorded. **This is a correctness gate, not an optimization** — wrapping a scalar
  in `SELECT COUNT(*) AS failures FROM (<sql>) AS t` yields `failures=1` always → silent
  wrong `kept` verdicts (the `row_count_between` bug). Aggregate support (source-table
  routing, the Direction-2 precedent) is an explicit follow-up — **filed as #267**.
  *(Scoping answer #3 + AR row 10.)*

- **DEC-005 — Opt-in `--from-manifest` flag on `prune-existing`.** Existing behavior
  byte-unchanged; operators opt in explicitly. 5-surface parity + SKILL.md (6th).
  Exact flag name locked in detailing as `--from-manifest`. *(Scoping answer #4.)*

- **DEC-006 — sqlglot-AST is the SQL-handling foundation.** Determinism detection,
  aggregate-shape gate, and comment-tolerant validation all use sqlglot AST (regex is
  unsafe — false-positives on column names / literal contents). Extends the sqlglot
  confinement (`#159`, `pyproject.toml:24`) from `draft/parser` to `ingest` + the `prune`
  compiler. The `#116` string-substitution / substring-validation machinery is safe but
  wrong-tool for dbt's foreign-rendered SQL and is NOT reused unchanged. *(AR
  overarching signal; the convergent need across AR rows 8–11.)*

- **DEC-007 — `scope=sample` deferred for ingested tests; pass 1 evaluates at
  `scope=full`.** dbt's quoted relation matches neither substitution token, so sampling
  silently degrades ingested tests to `kept-without-evidence`. Pass 1 evaluates ingested
  candidates at full scope regardless of `--scope`, emitting one INFO if `--scope=sample`
  was requested. `maximum_bytes_billed` still caps cost; the operator opted in via
  `--from-manifest`. sqlglot AST relation-rewriting for true sampling is a follow-up —
  **filed as #268**. *(User-confirmed; AR row 8.)*

- **DEC-008 — Manifest test-node read surface: new `GenericTest` + `Manifest.tests`.**
  A frozen `GenericTest` read-back model (`extra="ignore"`, `populate_by_name=True`)
  carrying `unique_id`, `compiled_code`, `test_metadata{name,namespace,kwargs}`,
  `column_name`, `depends_on`/`attached_node`, `file_key_name`. Surfaced via a sibling
  `resource_type=="test"` filter in `_load`, parallel to `filtered_sources`, into
  `Manifest.tests: dict[str,GenericTest] = Field(default_factory=dict)`. Survives the
  frozen `model_copy` catalog overlay; breaks no `Manifest(...)` call site; `nodes`
  stays model-only (invariant intact). Mandatory `StrictGenericTest(extra="forbid")`
  drift detector + committed fixture. *(AR rows 1/3.)*

- **DEC-009 — Test→model association by feature-detect precedence ladder.** No single
  authoritative field across v9–v12 and the loader discards the detected version.
  Ladder: `attached_node` (v10+) → else `file_key_name` + `depends_on.nodes`
  disambiguated by `column_name` / `test_metadata.kwargs.model`. Per-version fixture
  coverage. *(AR row 2.)*

- **DEC-010 — `compiled_code` absence: stage-0 silent read, downstream skip-record +
  summary remediation.** The manifest reader stays stage-0 silent (tolerates null
  `compiled_code` like `Column.data_type=None`). The AC's "not a silent skip" is
  satisfied at the ingest layer: a test node with absent/null `compiled_code` →
  `SkippedTest` with a remediation detail naming `dbt compile`; when `--from-manifest`
  finds test nodes but ZERO carry `compiled_code`, emit one prominent summary remediation
  (the "run `dbt compile` / `dbt build` and commit `target/manifest.json`" answer,
  mirroring the catalog.json `data_type` guidance). Soft, never a hard abort. *(AR row 4;
  AC "not a silent skip".)*

- **DEC-011 — Synthesized rationale built at ingest construction, envelope-safe.** The
  rationale (macro name + arg summary) is set when the frozen `CandidateTestCustomSQL`
  is constructed (never mutated). Strip/escape `</ARTIFACT>` (and the `<\n/ARTIFACT>`
  whitespace-split variant) from the macro-args string before it becomes the rationale,
  so a hostile macro arg can't fail-close the whole grade run. Feeds grade (zero
  grade-side edit) AND the diff `why` cascade. *(AR rows 7/12.)*

- **DEC-012 — Determinism filter: ingest skip-record (primary) + compiler
  `_InvalidIdentifier` fallback.** sqlglot AST function-node inspection catches
  `TABLESAMPLE`, `RAND/RANDOM`, `CURRENT_TIMESTAMP/CURRENT_DATE/NOW()/GETDATE()`,
  `ORDER BY RAND()…LIMIT`, `UUID/GENERATE_UUID`. Ingest skip-records cleanly; the
  compiler keeps a belt-and-braces `_InvalidIdentifier` → `kept-without-evidence`
  fallback (the total-compilation choke point). **No `DropReason` expansion** (5-value
  lock). *(AR row 9.)*

- **DEC-013 — Comment-tolerant validation for ingested SQL.** dbt-compiled SQL carries
  `--` / `/* */` comments that `validate_test_sql` rejects wholesale. Strip comments
  before the safety scan (or sqlglot single-statement validation) on the ingested path;
  do NOT reuse the `#116` validator unchanged. *(AR row 11.)*

- **DEC-014 — `SkipReason` reuse, closed 3-value literal preserved.** No-`compiled_code`
  / non-deterministic / aggregate-shaped / unparseable → existing reasons
  (`custom-or-generic-test` for namespaced; `malformed-supported-test` for
  structurally-broken). Never grow the enum. *(ingest-layer.md; Convention Checker §8.)*

- **DEC-015 — Macro identity into the diff `why`; ingested tests on the table, not
  `proposed_test_files`.** The macro name (`test_metadata.name` / `unique_id`) threads
  manifest → candidate → diff entry `why` so the operator can locate/remove the right
  test. Ingested tests are read-only (we didn't author them) → kept/dropped/flagged
  table via `render_diff(existing_schema=...)`, NEVER the `proposed_test_files` writer.
  *(AR / diff-renderer.md §34–35.)*

- **DEC-016 — No new `errors.py`, no audit-schema bumps, no grade prompt rotation.**
  Reuse `ingest`/`prune`/`manifest` error hierarchies (scan-7 stays at 14). `PruneEvent`
  / `DiffReport` gain no fields ⇒ no `_PRUNE_AUDIT_SCHEMA_VERSION` / `audit_schema_version`
  bump. Grade `_PROMPT_VERSION` + rubric untouched. *(Convention Checker §15/§24/§28/§32.)*

- **DEC-017 — Fixture via real `dbt compile` on dbt-duckdb + dbt-expectations.**
  Credential-free + Ralph-runnable (hand-craft escape hatch is gated on *lack of
  credentials*; duckdb is embedded). Extend `regenerate.sh`: add `packages.yml`
  (`dbt-expectations`), `dbt deps && dbt compile`, broaden the `jq` scrub. Cover the five
  engine outcomes with concrete macros: `expect_column_values_to_be_between` (row-return
  kept / vacuous always-passes-drop), `expect_table_row_count_to_be_between` (aggregate
  skip), `expect_row_values_to_have_recent_data` (non-deterministic skip). Per-node
  hand-patch fallback ONLY if a single macro won't compile on duckdb. *(AR rows 13/14.)*

- **DEC-018 — `--grade` progress + wiring.** `--grade` renumbers prune-existing progress
  to `[N/4]` (the `generate` `--no-grade`/`[N/4]` precedent); off by default; requires
  `--from-manifest`. Wires `load_grade_config` + `grade_artifacts` between prune and diff,
  feeding `grading_report=`; writes the `grade.json` / `grade.jsonl` sidecars. *(AR
  rows 4/5.)*

## Detailed Breakdown

Stories in architecture order (manifest → ingest → prune → grade → cli → docs). Each is
Ralph-sized (one context window). Validation command:
`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

### US-001 — Manifest `GenericTest` read-back model + `Manifest.tests` sibling filter

- **Traces to:** DEC-008, DEC-009.
- **Description:** Add a frozen `GenericTest` Pydantic model and surface
  `resource_type=="test"` nodes into `Manifest.tests` via a sibling filter in `_load`,
  parallel to `filtered_sources`. Feature-detect the tested-model association.
- **Files:** `src/signalforge/manifest/models.py` (new `GenericTest`, `Manifest.tests`
  field + docstring note that `resource_type:"test"` entries are now consumed);
  `src/signalforge/manifest/loader.py` (`_load` sibling filter ~294–333; association
  ladder helper); `tests/manifest/test_models.py` (`StrictGenericTest` drift detector);
  `tests/fixtures/manifest/` (test-node fragment fixture — depends on US-002's compiled
  fixture, or a hand-authored fragment for the pure model test).
- **AC:** `GenericTest` is `frozen=True, extra="ignore", populate_by_name=True`; carries
  `unique_id`/`compiled_code`/`test_metadata`/`column_name`/`depends_on`/`attached_node`/
  `file_key_name`; `Manifest.tests` defaults empty (no `Manifest(...)` call site breaks);
  `nodes` stays model-only; `StrictGenericTest(extra="forbid")` validates a committed
  fixture and rejects a poisoned extra-key copy; association ladder unit-tested for
  `attached_node`-present (v10+) AND `attached_node`-absent (v9) shapes; validation passes.
- **Done when:** the drift detector + association tests are green and the catalog-overlay
  `model_copy` rebuild is confirmed to pass `Manifest.tests` through untouched.
- **Depends on:** none (fixture fragment can be hand-authored for the pure model test;
  the real compiled fixture lands in US-006).
- **TDD:** strict-mirror rejects extra key; association ladder for both version shapes;
  null `compiled_code` tolerated silently; `Manifest.tests` empty-default construction.

### US-002 — sqlglot-AST analysis helpers (determinism, aggregate-shape, comment-tolerant validation)

- **Traces to:** DEC-006, DEC-004, DEC-012, DEC-013.
- **Description:** Three pure functions over a compiled-SQL string, sqlglot-AST-based:
  `is_deterministic_sql` (no non-deterministic funcs / TABLESAMPLE), `is_row_returning`
  (not a scalar/aggregate-only projection), and a comment-tolerant `validate_ingested_sql`
  (strip `--`/`/* */` then the existing safety checks, or sqlglot single-statement
  validation). Confine sqlglot to the module.
- **Files:** new `src/signalforge/ingest/_compiled_sql.py` (or `prune/_compiled_sql.py` —
  decide placement so both ingest + compiler can import without a cross-stage smell);
  `tests/ingest/test_compiled_sql.py`.
- **AC:** determinism detector flags `TABLESAMPLE`/`RAND`/`CURRENT_TIMESTAMP`/`NOW`/
  `GETDATE`/`UUID`/`ORDER BY RAND()…LIMIT` and does NOT false-positive on a `random_id`
  column or those tokens inside a string literal; aggregate detector flags
  all-aggregate-projection-no-GROUP-BY and passes a genuine failing-rows `SELECT … WHERE`;
  comment-tolerant validator accepts dbt SQL with `--`/`/* */` comments and still rejects
  a real injection (`;`, unbalanced parens on the stripped body); import-guard/confinement
  respected; validation passes.
- **Done when:** all three helpers are pure, unit-tested with string fixtures (no
  warehouse), and a planted-violation-style negative test exists for each.
- **Depends on:** none.
- **TDD:** the false-positive cases (column named `random_id`; `</ARTIFACT>`-free literal
  containing `RAND`); the aggregate vs failing-rows discrimination; the comment cases.

### US-003 — Ingest bridge: manifest test node → `CandidateTestCustomSQL`

- **Traces to:** DEC-001, DEC-003, DEC-010, DEC-011, DEC-012, DEC-014, DEC-015.
- **Description:** New ingest entry (e.g. `read_manifest_tests(manifest, model, *,
  project_dir=None) -> IngestResult`) that walks `Manifest.tests` for the target model,
  and for each row-returning + deterministic + `compiled_code`-present test builds a
  model-level `CandidateTestCustomSQL(sql=compiled_code, rationale=<synthesized>,
  column=None)` carrying the macro identity; routes the rest to `SkippedTest` with the
  right reason + remediation. Synthesize the rationale envelope-safely (strip
  `</ARTIFACT>`). Emit the summary remediation when all test nodes lack `compiled_code`.
- **Files:** `src/signalforge/ingest/reader.py` (or `parser.py`) new entry + macro-identity
  threading; `src/signalforge/ingest/models.py` (macro identity onto the candidate/skip
  as needed — reuse existing shapes, no new `SkipReason`); `tests/ingest/test_manifest_tests.py`.
- **AC:** a row-returning deterministic dbt-expectations test → one `CandidateTestCustomSQL`
  with synthesized rationale naming the macro + args and NO `</ARTIFACT>`; aggregate /
  non-deterministic / no-`compiled_code` → `SkippedTest` (existing reason) with remediation;
  all-missing-`compiled_code` → one summary remediation; macro identity present on the
  candidate for the diff `why`; `SkipReason` stays 3-value; stage-0 (no logging); validation
  passes.
- **Done when:** the bridge is unit-tested across all five dispositions with string/fixture
  inputs (no warehouse).
- **Depends on:** US-001, US-002.
- **TDD:** each of the five dispositions; the `</ARTIFACT>` synthesis strip; the
  all-missing summary path; the closed-`SkipReason` assertion.

### US-004 — Prune wiring: full-scope routing + comment-tolerant compile for ingested tests

- **Traces to:** DEC-007, DEC-012, DEC-013.
- **Description:** Route ingested `custom_sql` candidates to `scope=full` evaluation
  (INFO if `--scope=sample`), apply the comment-tolerant validation on the compiled body,
  and keep the compiler `_InvalidIdentifier` determinism fallback. Confirm the source-table
  routing (no sample substitution attempted).
- **Files:** `src/signalforge/prune/engine.py` (full-scope routing for ingested candidates;
  INFO on requested-but-unsupported sample); `src/signalforge/prune/compiler.py`
  (`_compile_custom_sql` uses the comment-tolerant validator + determinism fallback for the
  ingested path); `tests/prune/test_engine.py`, `tests/prune/test_compiler.py`.
- **AC:** an ingested candidate under `--scope=sample` is evaluated at full scope with one
  INFO and NEVER references `_SESSION._sf_sample_*`; a non-deterministic body →
  `kept-without-evidence`; a comment-bearing body is NOT false-rejected; `DropReason` stays
  5-value; conservative-bias routing preserved; validation passes.
- **Done when:** behavioural assertions on the dispatched SQL (full-scope, source table) +
  the determinism-fallback + comment-tolerance are green with `FakeBigQueryClient`.
- **Depends on:** US-002, US-003.
- **TDD:** engineered tautology → `always-passes`; engineered-failing → `kept`;
  non-deterministic → `kept-without-evidence`; sample-requested → full-scope INFO.

### US-005 — Diff: macro name into `why`; ingested tests on the table (not proposed files)

- **Traces to:** DEC-015.
- **Description:** Thread the macro identifier into the diff entry `why` for ingested
  `custom_sql` tests, and ensure the ingested-origin path lands on the kept/dropped/flagged
  table (via `render_diff(existing_schema=...)`), never `proposed_test_files`.
- **Files:** `src/signalforge/diff/_emitter.py` / `engine.py` (macro-name `why`; skip the
  proposed-file writer for ingested-origin custom_sql); `tests/diff/test_engine.py`.
- **AC:** a dropped ingested test's `why` names the macro; ingested tests appear on the
  table and never in `proposed_test_files`; `tier` stays 4-value; no `audit_schema_version`
  bump; ANSI-strip + `max_why_chars` obeyed; validation passes.
- **Done when:** diff snapshot/behaviour tests pin the macro-`why` + table-placement.
- **Depends on:** US-003.
- **TDD:** macro name in `why` (kept-uncertain bypasses cascade → `decision.why`); no
  proposed-file emission for ingested origin.

### US-006 — Compiled fixture + regen script (dbt compile on duckdb + dbt-expectations)

- **Traces to:** DEC-017, DEC-008.
- **Description:** Add a fixture dbt project (or extend an existing one) with
  `dbt-expectations` tests, extend `regenerate.sh` to `dbt deps && dbt compile`, commit the
  compiled `manifest.json` (test nodes with populated `compiled_code`) + a loads test.
  Per-node hand-patch fallback if a macro won't compile on duckdb.
- **Files:** `tests/fixtures/<project>/` (packages.yml, model/seed, schema.yml with the
  ~4 macros), `tests/fixtures/regenerate.sh` (deps + compile + broadened `jq` scrub),
  committed `target/manifest.json`, `tests/manifest/test_<project>_loads.py`.
- **AC:** committed manifest carries `resource_type:"test"` nodes with non-null
  `compiled_code` for the five-outcome macro set; a loads test validates
  `signalforge.manifest.load(fixture_dir)` surfaces `Manifest.tests`; regen script is
  documented + reproducible; validation passes.
- **Done when:** the fixture drives US-001's drift detector + US-003's bridge tests with
  real compiled SQL.
- **Depends on:** none (unblocks the real-SQL assertions in US-001/US-003 but they can
  start against hand-authored fragments).
- **TDD:** loads test; the fixture contains the exact macros for each engine outcome.

### US-007 — CLI: `--from-manifest` + `--grade` on `prune-existing` (5-surface parity + SKILL)

- **Traces to:** DEC-005, DEC-002, DEC-018.
- **Description:** Add `--from-manifest` (opt-in manifest-test ingest arm; default off,
  byte-unchanged otherwise) and `--grade` (opt-in LLM grade; requires `--from-manifest`;
  renumbers progress to `[N/4]`; wires `load_grade_config` + `grade_artifacts` +
  sidecars). Reuse `_resolve_model_by_key`; no bespoke `Cli*` wrappers (lib errors already
  mapped).
- **Files:** `src/signalforge/cli/prune_existing.py` (both flags + wiring); `docs/cli-ops.md`;
  `src/signalforge/skills/signalforge/SKILL.md` (prose + flag tokens for the parity gate);
  `tests/cli/test_prune_existing.py`; `tests/cli/test_5_surface_parity_*.py` if warranted.
- **AC:** `--grade` without `--from-manifest` → argparse error (exit 2); `--from-manifest`
  off → byte-identical existing behavior; `--from-manifest` on → ingested tests pruned +
  on the diff; `--grade` on → graded (missing key → `LLMAuthError` tier 3); progress
  `[N/4]` under `--grade`; skill-parity gate green; 5-surface parity present; validation
  passes.
- **Done when:** CLI tests cover both flags, the coupling error, and the byte-unchanged
  default; SKILL.md carries the new flag tokens.
- **Depends on:** US-003, US-004, US-005.
- **TDD:** coupling argparse error; default byte-unchanged; graded vs ungated output;
  `[N/4]` renumber.

### US-008 — Docs + worked example

- **Traces to:** all DECs.
- **Description:** Update `docs/ingest-ops.md`, `docs/prune-ops.md`, `docs/diff-ops.md`,
  `docs/cli-ops.md` (+ `docs/grade-ops.md` for the `--grade` behavior) with the
  manifest-compiled-SQL path, the determinism/aggregate/full-scope decisions, the
  macro-`why`, and a worked example consuming a real dbt-expectations `schema.yml`. Watch
  the #170 mkdocs gotcha (no literal `##` inside a fence — use 4-space indent).
- **Files:** the five ops docs; a worked-example snippet.
- **AC:** each ops doc covers its slice; the worked example runs end-to-end conceptually;
  `mkdocs build` (non-strict) emits no new anchor breakage; validation passes.
- **Done when:** docs reviewed for the Direction-1/full-scope caveat + macro-agnostic
  coverage claim.
- **Depends on:** US-007.

### US-009 — Quality Gate (code-review ×4 + CodeRabbit)

- **Description:** Run the code reviewer 4× across the full changeset, fixing every real
  bug each pass; run CodeRabbit if available; project validation passes after all fixes.
- **Depends on:** US-001…US-008.

### US-010 — Patterns & Memory (priority 99)

- **Description:** Update `.claude/rules/` — `ingest-layer.md` (the manifest-compiled-SQL
  recognition path + `SkipReason` reuse), `prune-engine.md` (determinism filter +
  full-scope ingested routing + sqlglot analysis extension), `manifest-readers.md`
  (`GenericTest` / `Manifest.tests` sibling filter + feature-detect association),
  `business-rule-tests.md` (custom_sql now has a manifest-ingested source) — plus a
  memory note on the sqlglot-AST-for-foreign-SQL lesson and the compiled-fixture regen.
- **Depends on:** US-009.

### Rules-compliance gate (validated against `.claude/rules/`)

- **ingest-layer.md** — `SkipReason` stays 3-value (DEC-014); stage-0 no-logging (US-003);
  `str`/`Path` contract untouched (new entry takes a `Manifest`); no new `errors.py` (DEC-016).
- **business-rule-tests.md** — reuse `custom_sql` (DEC-001); Direction-1 substitution
  avoided by full-scope (DEC-007); materialised-sample gotcha sidestepped, pinned by a
  behavioural full-scope/source-table assertion (US-004).
- **prune-engine.md** — `DropReason` 5-value lock (DEC-012); conservative-bias routing
  (US-004); `validate_test_sql` on compiled SQL, comment-tolerant (DEC-013); no
  `PruneEvent` field / no audit-schema bump (DEC-016); compiler stays dialect-driven
  (sqlglot analysis is dialect-neutral parsing, not `dialect.name` branching).
- **grade-layer.md** — no rubric/`_PROMPT_VERSION` change (DEC-016); `<ARTIFACT>` breach
  guard respected via synthesis strip (DEC-011); graceful-degrade preserved.
- **diff-renderer.md** — 4-value `tier`, no audit bump (DEC-016); macro-name `why`, table
  not `proposed_test_files` (DEC-015).
- **cli-layer.md** — 4-tier exit codes; `prune-existing` #105 conventions; 5-surface parity
  + SKILL.md for the two new flags (US-007).
- **manifest-readers.md** — frozen `extra="ignore"` + drift detector + fixture (US-001/006);
  symlink-hardened path (existing gate); silent-degrade on null `compiled_code` (DEC-010).
- **testing-signal.md** — no assert-True; drift detector; engineered-determinism via macro
  args; fixture via `dbt compile` (DEC-017); planted-violation self-checks on any new scan.

## Beads Manifest

- **Epic:** `bd_1-scaffolding-ajm` — #154: dbt-expectations prune+grade adapter via manifest compiled_code
- **Worktree:** in-place (`feature/154-dbt-expectations-prune`)
- **Tasks (10):**

| Bead | Story | Depends on |
|---|---|---|
| `bd_1-scaffolding-ajm.1` | US-001 Manifest `GenericTest` + `Manifest.tests` | — (ready) |
| `bd_1-scaffolding-ajm.2` | US-002 sqlglot-AST helpers | — (ready) |
| `bd_1-scaffolding-ajm.3` | US-003 Ingest bridge | .1, .2 |
| `bd_1-scaffolding-ajm.4` | US-004 Prune wiring (full-scope) | .2, .3 |
| `bd_1-scaffolding-ajm.5` | US-005 Diff macro-`why` | .3 |
| `bd_1-scaffolding-ajm.6` | US-006 Compiled fixture + regen | — (ready) |
| `bd_1-scaffolding-ajm.7` | US-007 CLI `--from-manifest` + `--grade` | .3, .4, .5 |
| `bd_1-scaffolding-ajm.8` | US-008 Docs + worked example | .7 |
| `bd_1-scaffolding-ajm.9` | US-009 Quality Gate | .1–.8 |
| `bd_1-scaffolding-ajm.10` | US-010 Patterns & Memory | .9 |

- **Ready at devolve:** `.1`, `.2`, `.6` (no blockers).
- **Follow-up (deferred, separate issues):** #267 (aggregate macros), #268 (`scope=sample` AST rewrite).

