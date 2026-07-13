# #268 — `scope=sample` for ingested dbt-expectations tests via sqlglot AST relation-rewriting

## Meta

| Field | Value |
|---|---|
| Ticket | [#268](https://github.com/wjduenow/SignalForge/issues/268) |
| Phase | `detailing` |
| Branch | `feature/268-ingest-sample-scope` |
| Worktree | `../worktrees/SignalForge/268-ingest-sample-scope` |
| Base | `dev` |
| Sessions | 1 (2026-07-12) |
| Blocked on | #154 (landed — `73ca9d5` / PR #269) |

---

## 1. Discovery

### 1.1 The ticket

#154 shipped the manifest-ingested test adapter: dbt-compiled test nodes (`dbt-expectations`,
`dbt-utils`, in-house generic tests) are read from `manifest.json`'s `compiled_code` and pruned as
`CandidateTestCustomSQL(from_manifest=True)`. But #154 DEC-007 evaluates them at **`scope=full` only**
— the engine routes every `from_manifest` candidate to the source table and emits one INFO if
`--scope=sample` was requested.

The reason is a broken invariant. The #116 drafted-`custom_sql` sample path rewrites the model's own
relation to the sample CTE / `_SESSION._sf_sample_*` temp by **string substitution**, and that works
only because SignalForge produces *both* the injected token (`{{ this }}` →
`model.resolve_this().qualified_name`, unquoted dotted) *and* the search token from the same function.
dbt's `compiled_code` is **foreign-rendered**: the relation carries dbt's own dialect-specific quoting
(`` `proj`.`ds`.`tbl` `` — three backtick pairs on BigQuery; `"dev"."main"."orders"` on DuckDB/Snowflake),
appears an arbitrary number of times, inside CTEs and subqueries. Neither `own_qualified` (unquoted)
nor `own_table_quoted` (single pair) matches. The fail-closed guard holds — **no prod full-scan risk** —
but sampling is inert.

**#268 replaces the string substitution with sqlglot AST analysis** so ingested tests can be sampled.

### 1.2 Current-code map (Codebase Scout)

**`prune/compiler.py`** — no `import sqlglot`; it *consumes* four helpers from
`ingest/_compiled_sql` (`is_deterministic_sql`, `is_row_returning`, `is_prunable_count_scalar`,
`validate_ingested_sql`) and threads the live dialect (`dialect=dialect.name`).

- `_compile_custom_sql` (`compiler.py:621`) — the **ingested arm** is a guard at the top (`689-778`):
  determinism gate → comment-tolerant `validate_ingested_sql` → #267 count-scalar restructure
  (`740-777`, the pure-f-string wrap) → else `return test.sql` verbatim (`778`). `scope` /
  `sample_size` / `partition_filter` are **ignored** on this branch.
- The **drafted** `custom_sql` Direction-1 substitution lives at `820-895`: `own_qualified =
  model.resolve_this().qualified_name` (`823`), `own_table_quoted = _qualified_table_name(table_ref,
  dialect)` (`824`); the materialised arm fail-closes at `888` if `own_qualified not in resolved_sql`
  and otherwise `resolved_sql.replace(own_qualified, own_table_quoted)` (`895`).
- Sample CTE: `_render_sample_cte` (`190-240`) → `warehouse/_sample_sql.render_sample_select`
  (shared with the adapters; switches inline-vs-projection on `dialect.sample_hash_in_projection`).
- Multi-table classification: `_is_multi_table` (`600`) — a literal-stripped `\bjoin\b` regex.

**`prune/engine.py`** — routing is centralised:

- `_test_requires_source_table(test, sample_strategy)` (`510-599`); the #154 arm is
  `if isinstance(test, CandidateTestCustomSQL) and test.from_manifest: return True` (`590-591`).
- **Two call sites** (the #170 two-conditional rule): `all_bypass_to_source` (`1443-1448`, which
  *skips `materialise_sample` entirely* when every candidate bypasses) and the per-test
  `per_test_table_ref` override (`1595-1599`).
- The #154 `--scope=sample` INFO (`1361-1377`).
- `model` and `dialect` are both resolved at `1347-1348` — **before both routing sites.**

**`ingest/reader.py`** — `read_manifest_tests(manifest, model, …)` (`524-625`); candidate built at
`689-700` with `from_manifest=True`. It has the `Model` in hand (so `model.resolve_this()` is
available) but **not** the `Dialect` (that comes from `adapter.dialect()` inside `prune_tests`) — and
it is a **stage-0 reader: no SQL building, no logging**. It currently calls the sqlglot helpers with
the default `dialect="bigquery"` (a pre-existing gap).

**`Dialect`** (`warehouse/models.py:155-183`) — no sqlglot-dialect field, but `Dialect.name` already
*is* a valid sqlglot dialect string for all four constants, and the compiler already uses it as an
opaque sqlglot token (not a branch).

**Key tests that pin today's behaviour and must invert:**

- `tests/prune/test_compiler.py::test_compile_ingested_custom_sql_sample_scope_never_samples` (`:1023`)
- `tests/prune/test_compiler.py::test_compile_ingested_custom_sql_needs_no_model` (`:1107`)
- `tests/prune/test_engine.py::test_prune_tests_ingested_custom_sql_under_sample_evaluates_full_scope`
  (`:5537`) — asserts `compiled_sql == _INGESTED_SOURCE_BODY`, no `WITH sample`, no `_SESSION`, **and**
  the INFO line.
- The Direction-1 template to mirror:
  `test_prune_tests_custom_sql_single_table_references_temp_table_under_materialised` (`:3114`).

**Fixtures:** `tests/fixtures/dbt_project_expectations/target/manifest.json` (5 real compiled test
nodes — **DuckDB**-quoted). There is **no BigQuery-compiled `compiled_code` fixture**; the BigQuery
quoting is hand-modelled in tests (`` from `fake_project`.`dataset`.`orders` ``).

### 1.3 Empirical sqlglot prototype (sqlglot 30.2.1, run against the real fixtures)

- **Identification is clean.** `exp.Table.catalog/.db/.name` normalise every dialect's quoting to the
  same tuple. **But CTE references also parse as `exp.Table`** (`from grouped` →
  `Table(this=Identifier(grouped))`), structurally identical to a bare 1-part relation — they must be
  excluded via the CTE alias set (`{c.alias_or_name for c in tree.find_all(exp.CTE)}`). Subquery
  aliases parse as `exp.Subquery`, never `exp.Table` (safe).
- **Matching:** use `sqlglot.optimizer.normalize_identifiers.normalize_identifiers(node, dialect=…)` —
  it *is* the per-dialect fold rule (Snowflake upper, Databricks/DuckDB lower, BigQuery preserve,
  quoted → preserved). **Exact full-tuple match only**; suffix matching would make a bare `orders`
  false-positive against a different table in the session's default schema.
- **Rewriting — swap the `exp.Table` (a), not CTE-alias (b).** (b) is materially worse: `Select.with_()`
  has no `prepend` kwarg, and appending our sample CTE *after* dbt-expectations' `grouped_expression`
  produces SQL that **fails on execution** (`CatalogException: Table with name sample does not exist` —
  CTEs may only reference *earlier* CTEs). Prepending requires poking `args["with_"]` (a key that was
  *renamed* in sqlglot 30) — version-fragile.
- **Byte-preserving splice is feasible and it worked.** `exp.Table` nodes carry no source offsets, but
  `sqlglot.tokenize()` `Token.start`/`.end` do. So: **parse to DECIDE, token-splice to REWRITE.** All six
  real fixture bodies rewrote correctly with comments, indentation and dbt's blank lines byte-intact;
  a JOIN's *other* relation was left alone; aliases (`AS o`) survive for free. `ast_count ==
  splice_count` is a fail-loud cross-check. (A full AST re-render, by contrast, upper-cases keywords,
  converts `--` → `/* */`, and rewrites `is not null` → `NOT … IS NULL` — semantically faithful but we
  would be shipping sqlglot's bytes, not dbt's.)
- **Failure modes, all measured:** unparseable → `ParseError`; **zero matches** (test hits only a
  source) → 0 rewrites, *no error* — needs an explicit branch; JOIN to another relation → other relation
  untouched; `information_schema` / TVF / `UNNEST` → no match; **self-join → 2 matches, both must land on
  the same temp**; a CTE shadowing the model name → correctly 0 matches.
- **`oneshot` has no table to point at.** Strategy (a) requires a materialised temp table.

### 1.4 Rules constraints (Convention Checker) — the binding ones

| Rule | Constraint | Obligation |
|---|---|---|
| `llm-drafter.md` § sqlglot confinement | "If a **FOURTH** sqlglot importer lands, promote the convention to a real confinement scan." Importers today: `draft/parser.py`, `ingest/_compiled_sql.py`. | Any `import sqlglot` under `prune/` is the 4th → owes a **promoted AST confinement scan + planted-violation self-check**. |
| `ingest-layer.md` | **Stage-0: no SQL building.** #267 split classify-at-ingest (bool) / restructure-at-compiler *because of this rule*. | A relation **rewriter** may not live in `ingest/`. A relation **locator** (analysis only) can. |
| `business-rule-tests.md` Direction-1/2 | *"What does this test's compiled SQL query — row-level data, or metadata/aggregates?"* | **The sharpest trap:** a #267 **count-of-rows scalar** is an aggregate — `COUNT(*)` over a hash-mod'd sample is semantically meaningless. Narrowing `from_manifest` → `False` wholesale would silently start sampling them. |
| `business-rule-tests.md` | Multi-table stays full-scan ("sampling a join is semantically wrong"). | An ingested body with a JOIN must not be sampled. |
| `business-rule-tests.md` / `prune-engine.md` | Gate on `from_manifest`, never `type=="custom_sql"`. Pin the **engine-routing** test, not just the compiler snapshot. | Behavioural assertion: dispatched SQL references `_SESSION._sf_sample_*`, never the source (and the inverse for count-scalars). |
| `prune-engine.md` | **DropReason 5-value LOCK**; conservative-bias routing. | Every #268 failure (ParseError, zero-match, count mismatch) → existing `kept-without-evidence` via `_InvalidIdentifier`. No 6th reason, no audit-schema bump, no new error class. |
| `prune-engine.md` DEC-025 | Compiler is dialect-driven, **never `if dialect.name ==`**. `identifier_case` fold-then-quote must match the adapter's CREATE (#124). | The emitted relation goes through `_fold_identifier` / `_quote` / `_qualified_table_name` — not sqlglot's generator. |
| `prune-engine.md` #171 DEC-009 | `_test_requires_source_table` is the single source of truth; **two** engine sites; **mixed-candidate test is load-bearing**. | Narrow inside the helper only; ship a mixed-candidate engine test. |
| `warehouse-adapters.md` (#121/#124/#226) | *"sqlglot-parse + snapshot + fakes certify SHAPE, not that a live warehouse ACCEPTS the SQL."* #226's live run found 3 bugs the entire offline tier passed clean. | **Budget a gated live-cert story.** Treat the live run as the merge gate. |
| `prune-engine.md` #223 DEC-002 | A pure-sqlglot parse-guard with no execution fake runs **ungated**. | New rewritten-SQL fixtures get an ungated `sqlglot`-parse guard. |
| `cli-layer.md` | 5/6-surface parity **only if a flag/token changes**. | Prefer **no new flag** — `--scope=sample` should simply start working. |
| `python-build.md` / CLAUDE.md | `sqlglot>=30,<31` is already a hard runtime dep. Validation = `ruff` + `ruff format` + `pyright` + `pytest`. | No pyproject change. |
| Docs / CHANGELOG | Behaviour change with cost impact. | `CHANGELOG.md` `[Unreleased]` § Changed; rewrite `docs/prune-ops.md` (~240-256), `docs/ingest-ops.md`; update `prune-engine.md`, `business-rule-tests.md` (lines 218 + 224), `ingest-layer.md`, `llm-drafter.md`. |

### 1.5 The architectural crux

The engine picks `per_test_table_ref` and `compile_scope` **before** calling `_compile_test`. But
whether an ingested body is *samplable* can only be known by **parsing it** (row-returning? exactly one
distinct relation, and is it the model's own? parseable?). So the classification must be visible to the
engine's routing loop, not discovered inside the compiler.

Both routing sites sit *after* `model` and `dialect` are resolved (`engine.py:1347-1348`), so the
engine **can** call a dialect-correct classifier. That is the shape the plan takes.

---

## 2. Scoping answers (session 1)

| Q | Answer |
|---|---|
| sqlglot home | **Split: locate in ingest, splice in compiler.** A pure ANALYSIS helper in `ingest/_compiled_sql.py` parses and returns spans / verdicts; `prune/compiler.py` does a pure-string splice. Stage-0's no-SQL-building rule holds; **no 4th sqlglot importer**, so no promoted confinement scan is owed. Mirrors the #267 classify-at-ingest / restructure-at-compiler precedent exactly. |
| `oneshot` | **Materialised only.** `oneshot` ingested tests keep bypassing to source. sqlglot's `Select.with_()` cannot *prepend*, and appending our sample CTE after dbt-expectations' `grouped_expression` produced SQL that **failed on execution**. The `oneshot` half is a follow-up, deviating from the ticket's stated acceptance. |
| #267 count-scalars | **Stay routed to source.** An aggregate over a hash-mod'd sample is semantically wrong (Direction-2). #267's verdicts are unchanged. |
| Live cert | **Gated BigQuery live-cert story.** Treat the live run as the merge gate (#226 lesson). |

## 3. Architecture Review

### 3.1 Ratings

| Area | Rating | Headline |
|---|---|---|
| Security (splice integrity) | **blocker ×5** | The splice *text* is safe; the **integrity invariant** is not. |
| Correctness (routing) | **blocker ×1** | Materialisation-failure path is a functional regression for all-ingested batches. |
| Performance / cost | concern | CTAS is `SELECT *` — can bill more than the tests it replaces. |
| Observability | concern | The existing `--scope=sample` INFO becomes a lie. |
| Audit / data model | concern | Pre-existing 4000-byte cap bug; sampled-vs-bypassed is unobservable. |
| API design | pass | No new flag, no new error class, no new variant, DropReason stays 5-valued. |
| Testing strategy | concern | Needs an engine-routing pin + a `materialise_sample` **not**-called pin + a live cert. |

### 3.2 Blockers (all have accepted mitigations — see DECs)

**AR-B1 — a count cross-check is NOT an integrity proof.** The prototype's `ast_match_count ==
span_count` invariant is defeatable. `select proj.ds.tbl.c from \`proj.ds.tbl\`` yields exactly one
matching `exp.Table` *and* one dotted token run — but the run is the **column qualifier**, so a naive
splice rewrites the column path and leaves the `FROM` on **production** while the check passes.

**AR-B2 — CTE-alias shadowing defeats the exclusion set.** `WITH \`proj.ds.tbl\` AS (…) SELECT * FROM
\`proj.ds.tbl\`` parses the CTE alias as a *single dotted `Identifier`*, so the proposed
`{c.alias_or_name for c in find_all(exp.CTE)}` set misses it, while the reference normalises to a
`Table` tuple that **exactly matches the model relation**. The splice rewrites a CTE reference to the
temp table → plausibly zero rows → `always-passes` → **a real test is deleted.** Worst outcome the
system can produce.

**AR-B3 — the compiler must fail closed independently of the engine.** `_compile_custom_sql`'s
`from_manifest` arm **never reads `table_ref`**; it ends at `return test.sql`. Narrow the engine gate
and any compiler path that doesn't rewrite emits *production-referencing* SQL against a `table_ref`
the engine believes is the temp table → **silent full-scan of prod, recorded as an evidence-backed
verdict at `scope="sample"`**. The drafted arm already has this guard (`own_qualified not in
resolved_sql` → `_InvalidIdentifier`); the ingested arm has none.

**AR-B4 — multi-relation must be refused on the AST, not the `_JOIN_RE` regex.** The regex misses
comma-joins, correlated subqueries and `NOT EXISTS`. Sampling one leg of a join can produce a **false
pass → `always-passes` → the test is dropped.**

**AR-B5 — materialisation failure is a functional regression.** Today an all-ingested batch
short-circuits and `materialise_sample` is never called. Post-#268 it *is* called, and on a >100M-row
unpartitioned model BigQuery raises `SamplingRequiresPartitionFilterError` / `UnknownTableSizeError`
**before issuing the CTAS** → every candidate → `kept-without-evidence`. So
`prune-existing --from-manifest --scope=sample` goes from *N real verdicts against source* to **zero
pruning** on exactly the tables operators care most about.

### 3.3 Latent #154 bugs surfaced (pre-existing, but #268 lands on top of them)

- **`RecursionError` escapes the sqlglot guards.** `_compiled_sql`'s helpers catch only
  `sqlglot.errors.SqlglotError`. A ~2000-deep nested-paren body raises `RecursionError` out of
  `is_deterministic_sql` and **aborts the whole prune run** with no audit rows written.
- **No size cap on `compiled_code`.** The 5 MB `_INGEST_SCHEMA_SIZE_LIMIT_BYTES` guards `read_schema` /
  `read_sql_dir` file reads only. `read_manifest_tests` takes an already-parsed `Manifest`; a
  pathological body is parsed with no guard.
- **The 4000-byte audit cap is already blown by a real dbt-expectations body.** `PruneEvent` serialises
  the body **twice** (`test.sql` *and* `compiled_sql`), untruncated. A ~1.8 KB `compiled_code` exceeds
  `_PRUNE_AUDIT_RECORD_LIMIT_BYTES` → `PruneAuditRecordTooLargeError` → **exit 3, run aborted
  mid-batch** with earlier decisions already fsync'd.
- **The ingest reader runs its sqlglot gates at the default `dialect="bigquery"`** while the compiler
  runs them at `dialect.name` — the two can disagree.

### 3.4 Confirmed non-issues

- The spliced text is **not** attacker-influenced: `run_id` is `blake2b`-hex, `TableRef.__post_init__`
  runs `validate_identifier`, and `_qualified_table_name` folds+quotes per `Dialect`.
- The tokenizer will **not** hand you a span inside a comment or a string literal (verified).
- `Token.start`/`.end` are **character** offsets and `.end` is **inclusive**; multibyte literals earlier
  in the body do not shift them.
- Homoglyph / case-variant identifiers fail the exact-tuple match → fail closed.
- `model` and `dialect` are in scope at **both** engine routing sites.
- An all-count-scalar ingested batch still short-circuits → **no spurious CTAS** (must be pinned).
- The diff `why` cascade keys on `from_manifest`, not routing — unaffected.

## 4. Refinement Log — Decisions

**DEC-001 — sqlglot stays out of `prune/`; locate in ingest, splice in the compiler.**
`ingest/_compiled_sql.py` (the existing, 2nd sqlglot importer) grows pure **analysis** helpers that
parse and return verdicts + character spans. `prune/compiler.py` does a pure-string splice and imports
no sqlglot. Stage-0's no-SQL-building rule holds (`ingest-layer.md`); **no 4th sqlglot importer lands**,
so the `llm-drafter.md` confinement convention stays documented-not-gated and no AST scan is owed.
Mirrors the #267 classify-at-ingest / restructure-at-compiler split exactly.

**DEC-002 — materialised only; `oneshot` keeps bypassing to source.** Rewriting the relation requires a
temp table to point at, which `oneshot` does not have. The CTE alternative was prototyped and
**failed on execution**: sqlglot's `Select.with_()` has no `prepend` kwarg, and appending our sample CTE
after dbt-expectations' `grouped_expression` produces `CatalogException: Table with name sample does not
exist` (a CTE may only reference *earlier* CTEs). Prepending means poking `args["with_"]`, a key that was
*renamed* in sqlglot 30 — version-fragile. This deviates from the ticket's stated acceptance
("materialised + oneshot"); the `oneshot` half is filed as a follow-up.

**DEC-003 — #267 count-of-rows scalars stay routed to source.** An aggregate over a hash-mod'd sample is
semantically meaningless (`business-rule-tests.md` Direction-2). #267's verdicts are unchanged. The
samplability check therefore runs **after** `is_row_returning`.

**DEC-004 — the integrity gate is an AST post-condition, NOT a count.** `ast_match_count == span_count`
is defeatable (AR-B1): `select proj.ds.tbl.c from \`proj.ds.tbl\`` matches on count while the span points
at the **column qualifier**, leaving the `FROM` on production. Replace it with a proof on the *rewritten*
SQL: re-parse and require (a) it parses clean, (b) **zero** residual `exp.Table` normalising to the model
relation, (c) exactly *N* `exp.Table` equal to the temp ref. Any failure → `_InvalidIdentifier` →
`kept-without-evidence`. Post-splice `validate_ingested_sql` is **not** a substitute — it only checks
top-level `;` and paren balance and cannot detect a partial rewrite.

**DEC-005 — CTE/scope resolution via `sqlglot.optimizer.scope`; fail closed on an alias collision.** The
naive `{c.alias_or_name for c in find_all(exp.CTE)}` set misses a **dotted** CTE alias
(`WITH \`proj.ds.tbl\` AS (…)`), whose reference normalises to a `Table` tuple exactly matching the model
relation — the splice would rewrite a CTE reference, plausibly yield zero rows, and **delete a real test**
(AR-B2). Resolve scopes properly and only rewrite tables that are genuine physical relations. If any CTE
alias collides with the model relation, refuse to sample.

**DEC-006 — "single relation" is enforced on the AST, never the `_JOIN_RE` regex.** The regex misses
comma-joins, correlated subqueries and `NOT EXISTS` (AR-B4). The rule: after scope resolution, the set of
distinct physical relations in the body must be exactly `{model relation}`. Anything else → bypass to
source. Sampling one leg of a join risks a **false pass → `always-passes` → the test is dropped.**

**DEC-007 — the compiler fails closed independently of the engine.** `_compile_custom_sql`'s
`from_manifest` arm currently ignores `table_ref` and returns `test.sql` verbatim. New invariant: if
`from_manifest` **and** `table_ref` is not the source relation **and** no verified rewrite is in hand →
`_InvalidIdentifier`. Without this, a one-line engine change re-opens a **silent full-scan of production
recorded as an evidence-backed verdict at `scope="sample"`** (AR-B3). Mirrors the drafted arm's existing
`own_qualified not in resolved_sql` guard.

**DEC-008 — precompute the rewrite plan once; keep `_test_requires_source_table` pure.** Samplability
needs a parse, but the helper is a documented pure function with direct unit tests, and it is called
**twice per candidate** at routing. So: build `plan: dict[int, IngestedSamplePlan]` keyed by **index into
`pairs`** (not `id(test)` — two byte-identical candidates are legal) *before* the routing block, and give
the helper a pure `samplable: bool = False` kwarg. The plan carries the **verified rewritten SQL** and a
reject reason; it is threaded into `_compile_test` as a single `ingested_sql_override: str | None` kwarg
so routing and compile agreement is structural, not coincidental. The compiler still re-validates the
override (DEC-007), so the safety contract is not bypassed. This collapses what would have been **5 parses
per body** down to 1.

**DEC-009 — materialisation failure falls back to source at full scope.** On `WarehouseError` from
`materialise_sample`, if every candidate is either bypass-to-source or samplable-ingested (nothing
genuinely *needs* the sample), re-route to `source_table_ref` at `scope="full"` and continue — today's
behaviour. Emit the existing degraded WARNING with an added `"fallback": "source"` key. Only fall through
to the blanket `kept-without-evidence` when a drafted row-level test is in the batch. Without this,
`prune-existing --from-manifest --scope=sample` on a >100M-row unpartitioned model goes from N real
verdicts to **zero pruning** (AR-B5).

**DEC-010 — ≥2 samplable ingested candidates required to break the bypass short-circuit.** The CTAS is
`SELECT *` + `TO_JSON_STRING(t)` — it reads every column, while a narrow ingested test on a wide table is
column-pruned by BigQuery at source today. A single-samplable-test batch can never pay for the CTAS, so it
keeps bypassing. Documented in `docs/prune-ops.md`.

**DEC-011 — `bypassed_to_source: bool` on `PruneDecision` + `PruneEvent`; `_PRUNE_AUDIT_SCHEMA_VERSION`
3 → 4.** `PruneDecision.scope` is copied from `config.scope`, so a *bypassed* ingested test is already
recorded as `scope="sample"` today (and its `why` reads "on 0 sample rows"). Post-#268 a sampled and a
bypassed ingested test would be indistinguishable in the audit — against Architectural Commitment #5. The
new field also fixes the same latent lie for `row_count_between` / `unique_combination` /
`row_count_anomaly_by_period`, unfixed since #169. Strict drift mirror + `prune_event_v1.jsonl` fixture
move in lockstep. **`DropReason` stays 5-valued; no new error class; no new flag; no new variant.**

**DEC-012 — the three latent #154 bugs are fixed here, not deferred.** All three sit directly in #268's
blast radius:
1. `RecursionError` (and a bad `dialect=` `ValueError`) escapes `except sqlglot.errors.SqlglotError` in
   every `_compiled_sql` helper → **aborts the whole prune run with no audit rows written**. Catch both,
   return the conservative verdict.
2. **No size cap on `compiled_code`.** The 5 MB `_INGEST_SCHEMA_SIZE_LIMIT_BYTES` guards file reads only;
   `read_manifest_tests` takes an already-parsed `Manifest`. Cap at ingest and skip-record over-cap bodies
   using the existing **closed 3-value `SkipReason`**.
3. **The 4000-byte audit cap is already blown by a real dbt-expectations body** — `PruneEvent` serialises
   it **twice** (`test.sql` *and* `compiled_sql`), so a ~1.8 KB body raises
   `PruneAuditRecordTooLargeError` → **exit 3, run aborted mid-batch** with earlier decisions already
   fsync'd. Truncate `compiled_sql` in `_build_prune_event`; `compiled_sql_hash` keeps the forensic chain
   intact.

**DEC-013 — thread the active dialect through the ingest reader's gates.** `read_manifest_tests` calls
`is_row_returning(cc)` / `is_deterministic_sql(cc)` with the default `dialect="bigquery"` while the
compiler passes `dialect.name`. Two parses of the same body under different dialects can **disagree**,
which post-#268 means the engine and compiler can reach opposite samplability verdicts.

**DEC-014 — observability.** Replace the existing `--scope=sample` INFO (which becomes a lie) with one
aggregate INFO per call carrying `{model_unique_id, sample_strategy, ingested_count, sampled_count,
bypassed_to_source_count, bypass_reasons}` — where `bypass_reasons` is a plain `dict[str, int]` histogram
**built outside** the `json.dumps(...)` call (the grep gate walks the whole argument subtree and rejects
any `JoinedStr`). Per-test breadcrumbs are **DEBUG**, not INFO — a wide model with 40 ingested tests
would otherwise emit 40 INFO lines.

**DEC-015 — span semantics.** `Token.start`/`.end` are **character** offsets and `.end` is **inclusive**.
Slice `sql[start : end + 1]` on the **identical `str` object** that was tokenized (never a
comment-stripped or normalized copy — offsets from one string applied to another is how this becomes an
injection), and splice **back-to-front**. Assert the slice re-tokenizes to the expected identifier before
splicing.

**DEC-016 — the gated live BigQuery run is the merge gate**, not the sqlglot parse-guard. #226's live run
found three bugs the entire offline tier passed clean.

## 5. Detailed Breakdown

Ordering is bottom-up: ingest analysis → compiler → engine routing → audit → observability → tests →
live cert → docs.

---

### US-001 — Harden the ingest sqlglot gates (the #154 latent bugs)

**Traces to:** DEC-012 (1)(2), DEC-013.

Catch `RecursionError` and `ValueError` alongside `sqlglot.errors.SqlglotError` in every
`_compiled_sql` helper, returning the existing conservative verdict. Add a `compiled_code` size cap and
skip-record over-cap bodies. Thread the active dialect through `read_manifest_tests`' gate calls.

**Files:** `src/signalforge/ingest/_compiled_sql.py`, `src/signalforge/ingest/reader.py`,
`tests/ingest/test_compiled_sql.py`, `tests/ingest/test_manifest_tests.py`.

**TDD:**
- A ~2000-deep nested-paren body → `is_deterministic_sql` / `is_row_returning` /
  `is_prunable_count_scalar` each return their conservative verdict, **no `RecursionError` escapes**.
- An unknown `dialect="nope"` → conservative verdict, no `ValueError` escapes.
- An over-cap `compiled_code` → one `SkippedTest`, **reason drawn from the existing closed 3-value
  `SkipReason`** (no 4th value).
- `read_manifest_tests` passes the caller's dialect (not the `"bigquery"` default) into every gate.

**Done when:** the four helpers are total over hostile input; no manifest body reaches sqlglot unbounded;
`uv run pytest` green.

**Depends on:** none.

---

### US-002 — `plan_relation_rewrite` / `verify_relation_rewrite` in `ingest/_compiled_sql`

**Traces to:** DEC-001, DEC-004, DEC-005, DEC-006, DEC-015. **The hardest story — the AR called relation
identification "the hardest sqlglot component".**

Two pure-analysis helpers (no SQL emitted → stage-0 intact):

- `plan_relation_rewrite(sql, *, relation: tuple[str, ...], dialect: str) -> RewritePlan | None`
  — parse; resolve scopes via `sqlglot.optimizer.scope`; `normalize_identifiers` both sides; **exact
  full-tuple match** (never suffix — a bare `orders` would false-positive against a different table in the
  session's default schema); enforce **single physical relation** (DEC-006); refuse on a **CTE alias
  colliding** with the relation (DEC-005); tokenize and return the **character spans** of each matched
  identifier run. `None` = not samplable, with a machine-readable reason.
- `verify_relation_rewrite(rewritten_sql, *, source: tuple[str,...], temp: tuple[str,...], expected_n: int,
  dialect: str) -> bool` — the DEC-004 post-condition: parses clean, **zero** residual source-relation
  tables, exactly `expected_n` temp-relation tables.

**TDD** (each case a distinct body):
- BigQuery `` `p`.`d`.`t` ``, DuckDB/Snowflake `"a"."b"."c"`, Databricks backtick — all normalise and match.
- **The AR-B1 body:** `select proj.ds.tbl.c from \`proj.ds.tbl\`` — the column qualifier must **not** be
  spliced; the `FROM` must be.
- **The AR-B2 body:** `WITH \`proj.ds.tbl\` AS (…) SELECT * FROM \`proj.ds.tbl\`` — refused, not rewritten.
- CTE reference shadowing the model name (`WITH orders AS (…)`) → zero matches, refused.
- Self-join → 2 spans, **both** rewritten to the **same** temp; a partial rewrite fails `verify_*`.
- JOIN / comma-join / correlated subquery / `NOT EXISTS` against another relation → refused (DEC-006).
- Zero-match (test references only a source) → `None`, explicit reason.
- Multibyte literal *before* the relation → spans still slice correctly (DEC-015).
- Homoglyph / case-variant identifier → fails the exact match → refused.
- Comment and string-literal text that *looks* like the relation → no span, untouched.
- Unparseable body → `None`, no exception.

**Done when:** every adversarial body above is refused or correctly rewritten; `verify_*` rejects a
hand-corrupted partial rewrite. **No `import sqlglot` anywhere under `src/signalforge/prune/`.**

**Depends on:** US-001.

---

### US-003 — Compiler: rewrite the ingested arm + the fail-closed guard

**Traces to:** DEC-001, DEC-003, DEC-007, DEC-008, DEC-015.

`_compile_custom_sql`'s `from_manifest` arm gains: an `ingested_sql_override: str | None` kwarg threaded
through `_compile_test`; the pure back-to-front string splice; re-validation of the override
(`validate_ingested_sql` + `is_deterministic_sql`); and the **DEC-007 fail-closed guard** — if
`table_ref` is not the source relation and no verified rewrite is in hand → `_InvalidIdentifier`.
Count-scalars (DEC-003) are unaffected and still return the #267 composed wrap. **`prune/compiler.py`
still imports no sqlglot.**

**Files:** `src/signalforge/prune/compiler.py`, `tests/prune/test_compiler.py`.

**TDD:**
- Ingested + temp `table_ref` + a valid plan → compiled SQL references `_SESSION._sf_sample_*` and
  **never** the source. Comments/indentation from dbt survive byte-intact outside the spans.
- Ingested + temp `table_ref` + **no** override → `_InvalidIdentifier` (the DEC-007 guard). **This is the
  test that prevents a silent prod full-scan.**
- Ingested + source `table_ref` → body verbatim (today's behaviour, unchanged).
- Count-scalar + temp `table_ref` → never reaches the splice (routing keeps it at source), and the guard
  holds if it somehow does.
- `test_compile_ingested_custom_sql_sample_scope_never_samples` (`:1023`) and
  `test_compile_ingested_custom_sql_needs_no_model` (`:1107`) **invert** — rewrite them, don't delete them.
- `test_compile_drafted_custom_sql_still_samples_unchanged` stays green (the regression fence).

**Done when:** the compiler is total (never raises); the guard is pinned; the drafted path is byte-identical.

**Depends on:** US-002.

---

### US-004 — Engine routing: precompute the plan, narrow the bypass, fall back on failure

**Traces to:** DEC-008, DEC-009, DEC-010.

Build `plan: dict[int, IngestedSamplePlan]` keyed by index into `pairs`, **before** the routing block.
Give `_test_requires_source_table` a pure `samplable: bool = False` kwarg (its existing 2-arg unit tests
stay green). Apply the **≥2 samplable** gate (DEC-010). Add the **materialisation-failure fallback to
source** (DEC-009). Both routing sites (`all_bypass_to_source` and the per-test `per_test_table_ref`
override) read the same precomputed plan — the #170 two-conditional rule.

**Files:** `src/signalforge/prune/engine.py`, `tests/prune/test_engine.py`.

**TDD:**
- **Mixed batch** (samplable ingested + count-scalar ingested + drafted row-level) — each routes to the
  right `table_ref`. *Load-bearing per `prune-engine.md`: a single-variant test only exercises the
  short-circuit.*
- **All-count-scalar ingested batch → `materialise_sample` `assert_not_called()`.** The exact regression
  #268 could silently introduce.
- Single samplable ingested test → still bypasses (DEC-010); `materialise_sample` not called.
- `materialise_sample` raises `SamplingRequiresPartitionFilterError` on an all-ingested batch → every
  candidate gets a **real verdict against source**, not `kept-without-evidence` (DEC-009).
- Same failure with a drafted row-level test in the batch → blanket `kept-without-evidence` (unchanged).
- `_test_requires_source_table` stays pure — existing direct unit tests unchanged.

**Done when:** routing is correct across the mixed matrix and the regression in AR-B5 is closed.

**Depends on:** US-003.

---

### US-005 — Audit: `bypassed_to_source` + schema 3→4 + `compiled_sql` truncation

**Traces to:** DEC-011, DEC-012 (3). **Serialize this bead — it touches the shared audit registry
(`PruneEvent`, the strict drift mirror, the committed fixture).**

Add `bypassed_to_source: bool` to `PruneDecision` + `PruneEvent`; bump `_PRUNE_AUDIT_SCHEMA_VERSION`
3 → 4 (field stays `int`, not `Literal`, so v3 records still round-trip). Update `StrictPruneEvent` and
`tests/fixtures/prune/prune_event_v1.jsonl` in lockstep. Truncate `compiled_sql` in `_build_prune_event`
to a bounded prefix — `compiled_sql_hash` is stored separately, so the forensic chain survives.

**Files:** `src/signalforge/prune/models.py`, `src/signalforge/prune/audit.py`,
`src/signalforge/prune/engine.py`, `tests/prune/test_drift_detector.py`,
`tests/fixtures/prune/prune_event_v1.jsonl`, `tests/prune/test_audit.py`.

**TDD:**
- A **real ~2 KB dbt-expectations body** writes a `PruneEvent` **without** raising
  `PruneAuditRecordTooLargeError`. *(Pins the DEC-012(3) fix — this aborts the run today.)*
- A sampled ingested decision → `bypassed_to_source=False`; a bypassed one → `True`; a
  `row_count_between` → `True`.
- Drift mirror rejects an unknown field; the v3 fixture still round-trips.
- `__repr__` / `__repr_args__` still redact the SQL body.

**Done when:** a real dbt-expectations body no longer aborts the run; sampled vs bypassed is legible in
the audit.

**Depends on:** US-004.

---

### US-006 — Observability: the routing INFO + per-test DEBUG

**Traces to:** DEC-014.

Replace the `--scope=sample` INFO (now false for the samplable subset) with the aggregate INFO + reason
histogram; add the per-test DEBUG breadcrumb. Lazy-format `%s` + `json.dumps({...})`; the histogram dict
is built **outside** the `json.dumps` call (the grep gate recurses into the argument subtree and rejects
any `JoinedStr`).

**Files:** `src/signalforge/prune/engine.py`, `tests/prune/test_engine.py`.

**TDD:** the INFO fires once per call with correct `sampled_count` / `bypassed_to_source_count` /
`bypass_reasons`; the existing test asserting `"scope=sample requested"` is rewritten; DEBUG breadcrumbs
carry the reason; `tests/llm/test_logger_grep_gate.py` stays green.

**Done when:** the log no longer lies and the grep gate passes.

**Depends on:** US-004.

---

### US-007 — Behavioural pins + the ungated sqlglot parse-guard

**Traces to:** DEC-004, DEC-006, DEC-016; `business-rule-tests.md` § "pin the engine-routing test".

The Direction-1 behavioural assertion, mirroring
`test_prune_tests_custom_sql_single_table_references_temp_table_under_materialised` (`test_engine.py:3114`):
the **dispatched** SQL for a sampled ingested test references `_SESSION._sf_sample_<16hex>` and **never**
the source. Plus an **ungated** `sqlglot`-parse guard over the rewritten-SQL fixtures (per #223 DEC-002 —
sqlglot is a base dep and there is no Spark/BQ execution fake, so it runs in the default suite), with a
`>= N` fixture-count floor and a planted-violation self-check.

**Files:** `tests/prune/test_engine.py`, `tests/prune/test_ingested_rewrite_parse_guard.py` (new),
`tests/fixtures/prune/compiled_sql/ingested/` (new).

**Done when:** the routing is pinned behaviourally, not just by snapshot.

**Depends on:** US-005, US-006.

---

### US-008 — Gated BigQuery live cert (**the merge gate**)

**Traces to:** DEC-016.

There is **no BigQuery-compiled `compiled_code` fixture** — the committed one is DuckDB. Add an
`inject_manifest_test_node` helper (sibling of `inject_model_business_rules`) that injects a hand-crafted
BigQuery-quoted compiled test node into the Austin fixture manifest **at `tmp_path`**. Run prune at
`--scope=sample --sample-strategy materialised`.

Belt-and-suspenders gating: `@pytest.mark.bigquery` **and** a runtime `_skip_reason()`. Engineered
determinism per `testing-signal.md`: the Austin fixture is **source-as-model**, so the always-pass must
come from a **natural NOT NULL source column** (`trip_id` / `start_time`) — never an engineered literal.

**TDD:** an engineered always-passes ingested test is **dropped**; the dispatched SQL hit
`_SESSION._sf_sample_*`; an engineered-violation body is **kept**.

**Done when:** `SF_RUN_BQ=1 GOOGLE_CLOUD_PROJECT=… uv run pytest -m bigquery --no-cov` passes against a
real warehouse. **Do not merge on the parse-guard alone** (#226 lesson).

**Depends on:** US-007.

---

### US-009 — Docs + CHANGELOG

**Traces to:** all DECs. No new flag → **no CLI/SKILL.md parity obligation** (state this explicitly).

- `docs/prune-ops.md` — rewrite the full-scope-only paragraph (~240-256), the routing table, the new INFO
  contract, the DEC-010 CTAS cost model and the DEC-009 fallback.
- `docs/ingest-ops.md` — correct the full-scope claim in the `compiled_code` narrative.
- `CHANGELOG.md` `[Unreleased]` — § Changed (behaviour + cost) and § Fixed (the three #154 bugs).
- Watch the mkdocs trap: no literal `##` inside a fenced block — use a 4-space-indented block.

**Depends on:** US-008.

---

### US-010 — Quality Gate

Code reviewer ×4 across the full changeset, fixing every real bug each pass; CodeRabbit; then
`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`
green, plus `-m bigquery --no-cov`. Re-run `ruff format --check .` against the latest `dev` HEAD after
each merge (the #170 merge-drift lesson).

**Depends on:** US-009.

---

### US-011 — Patterns & Memory

Update `.claude/rules/`: `prune-engine.md` (§ ingested tests — retire the DEC-007 full-scope bullet; the
routing table; the new `bypassed_to_source` field; audit v3→4), `business-rule-tests.md` (lines 218 + 224
— the "#268 deferred" and "no sqlglot in the compiler" claims; add the ingested case as the 5th
Direction-1/2 precedent), `ingest-layer.md` (the new analysis helpers; the size cap),
`llm-drafter.md` (§ sqlglot confinement — restate **why it stayed a convention**: the split kept the
compiler a consumer). Plus the durable lesson: **a count is not an integrity proof — when you rewrite
foreign SQL, prove the post-condition on the rewritten AST.**

**Depends on:** US-010.

## 6. Beads Manifest
