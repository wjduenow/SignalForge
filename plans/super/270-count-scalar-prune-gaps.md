# #270 — Follow-ups from #267 QG: comment-bearing / CTE-reprojected / cross-dialect count-scalar prune gaps

## Meta

| Field | Value |
|---|---|
| Ticket | [#270](https://github.com/wjduenow/SignalForge/issues/270) |
| Phase | `detailing` |
| Branch | `feature/270-count-scalar-prune-gaps` |
| Worktree | `../worktrees/SignalForge/270-count-scalar-prune-gaps` |
| Base | `dev` (at `f0bb1a6`, i.e. **after** #154 / #267 / #268) |
| Sessions | 1 (2026-07-14) |
| Blocked on | nothing — #154, #267, #268 all landed |

---

## 1. Discovery

### 1.1 The ticket

Four gaps surfaced by #267's Quality Gate, none of which blocked #267. They are all instances
of one root failure mode first named in #154 DEC-004: **a one-row (scalar) body wrapped in the
adapter's `SELECT COUNT(*) AS failures FROM (<sql>) AS t` envelope yields `failures = 1`
unconditionally**, so the prune engine books an evidence-backed `kept` for a test it never
actually evaluated.

| # | Gap | Symptom |
|---|---|---|
| G1 | Cross-dialect classify/compile divergence | Ingest classifies under `"bigquery"`, the compiler under `dialect.name`. A body that fails to parse under the LIVE dialect gets the permissive `is_row_returning → True` verdict → verbatim → always-1 → mislabeled `kept`. |
| G2 | CTE / derived-table re-projection of a count | `WITH c AS (SELECT COUNT(*) n FROM t) SELECT n FROM c` is cardinality-1 but reads as row-returning (the outer projection is a plain column) → verbatim → always-1. |
| G3 | Comment-intolerant adapter validation | `run_test_sql` calls the comment-**intolerant** `_sql_safety.validate_test_sql`, undoing the comment-**tolerant** `validate_ingested_sql` won at ingest+compile. Comment-bearing dbt `compiled_code` — the common case — never prunes at all. |
| G4 | `--tests-dir` vs `--from-manifest` asymmetry | A `tests/*.sql` singular test is `from_manifest=False`, so the #267 count-scalar restructure is skipped → always-1. |

### 1.2 The ground moved: #268 landed after this ticket was filed

`dev` is at `f0bb1a6`. Two things changed under #270's feet:

- **#268 DEC-013 added a `dialect=` kwarg to `read_manifest_tests`** — but the *only* production
  caller (`cli/prune_existing.py:605`) still omits it. The deferral is written into that
  function's docstring (`prune_existing.py:588-604`), which argues the mismatch "can cost signal,
  never correctness" because the compile path is fail-closed. **G1 contests exactly that claim.**
- **#268 DEC-012(1) hardened the parse surface** — `_PARSE_FAILURES = (SqlglotError, RecursionError,
  ValueError)` (`_compiled_sql.py:96`). This makes the permissive-on-parse-failure verdict *total*
  rather than accidental, which is precisely the mechanism G1 rides.

`_PRUNE_AUDIT_SCHEMA_VERSION` is now at **4** (bumped by #268 DEC-011).

### 1.3 Reframe — G2 and G4 are NOT wrong verdicts; they are *inconsistencies*

The ticket calls G2 and G4 "latent wrong verdicts." They are not. Under dbt's contract
(**rows returned = failures**), a body that always returns one row is a test dbt itself would
fail on every run. So the always-1 `kept` we produce today is *faithful*.

What is actually wrong is **inconsistency with #267**, which deliberately **reinterprets** a
top-level count scalar as `0 = pass` (#267 DEC-004; `business-rule-tests.md`: "`0=pass` is a
REINTERPRETATION, not faithful to dbt"). So the *same* count gets opposite readings depending on
whether it sits at the top level (reinterpreted) or behind a CTE / in a `tests/*.sql` file
(faithful). That reframing changes what each fix costs, and it is why G2 and G4 resolve toward
*refuse / document* rather than *extend the reinterpretation* (see DEC-004, DEC-005).

### 1.4 A fifth bug the ticket does not name

The #267 count-scalar restructure (`compiler.py:837-841`) splices the body inline into a
**single-line** f-string:

```python
composed = (
    "SELECT sf_agg_value FROM "
    f"(SELECT ({test.sql}) AS sf_agg_value) AS sf_agg "
    "WHERE sf_agg_value <> 0"
)
```

A body ending in a `--` line comment comments out `) AS sf_agg_value) AS sf_agg WHERE
sf_agg_value <> 0`. `validate_ingested_sql(composed)` cannot catch it — it *strips* comments
before scanning, so it validates a string we do not execute. This is masked today **only**
because the adapter rejects `--` outright. **Fixing G3 unmasks it.** G3 and this splice fix must
land together.

### 1.5 Where the code lives

| Concern | Location |
|---|---|
| The 4 classification gates | `ingest/_compiled_sql.py`: `is_deterministic_sql:149`, `is_row_returning:220`, `is_prunable_count_scalar:264`, `validate_ingested_sql:430` |
| Comment stripper (string-literal-aware, private) | `ingest/_compiled_sql.py::_strip_sql_comments:325` |
| Ingest gate chain | `ingest/reader.py::_classify_manifest_test:676` (calls the gates at `:735,:744,:752`) |
| The one production caller (omits `dialect=`) | `cli/prune_existing.py:605` |
| The `from_manifest` compile arm | `prune/compiler.py:745-900` (verbatim return at `:900`; count restructure at `:837`) |
| Comment-intolerant execution gate | `warehouse/_sql_safety.py::validate_test_sql:240` (rejects `--` at `:258`), called first in `run_test_sql` on `bigquery.py:933`, `snowflake.py:958`, `databricks.py:1232` |
| `--tests-dir` path (sets `from_manifest=False`) | `ingest/reader.py::read_test_files:335` |

### 1.6 Standing locks inherited (from `.claude/rules/`)

1. **`DropReason` stays 5-valued**; anything un-evaluable routes to `kept-without-evidence`.
2. **`SkipReason` stays a closed 3-value `Literal`**; reuse `malformed-supported-test`.
3. **sqlglot importers = 2** (`draft/parser.py`, `ingest/_compiled_sql.py`). The reusable rule:
   **locate-in-ingest, splice-in-compiler.** No `import sqlglot` under `prune/`. A 4th importer
   would owe a real AST confinement scan.
4. **Stage-0 ingest**: no logging, no warehouse import, no SQL building. Classification returns
   verdicts; SQL emission is the compiler's job.
5. **Gate on `from_manifest`, never on `type == "custom_sql"`** wherever ingested/drafted behaviour
   diverges (`business-rule-tests.md`).
6. **Regex is unsafe on foreign SQL — use the AST.**
7. **Shape ≠ acceptance.** Snapshots and a sqlglot parse-guard certify shape; only the **gated live
   run** certifies that the warehouse accepts the SQL (#121 / #226 / #268 DEC-016).

---

## 2. Scoping answers (session 1)

| Q | Answer |
|---|---|
| **G3 fix location** | **Strip comments in the compiler.** The `from_manifest` arm emits a comment-stripped body; the warehouse layer and all four adapters stay untouched, so `validate_test_sql` keeps its strict contract for every other caller. Side-benefit: it closes the validate-vs-execute byte mismatch and disarms the §1.4 splice bug. |
| **G2 disposition** | **Skip-record.** Teach the ingest classifier to detect *provable* one-row cardinality through CTE / derived-table re-projection, and skip-record it (`malformed-supported-test`). Do NOT extend the `0 = pass` reinterpretation — that would need a scalar-subquery-containing-`WITH` wrap (fresh cross-dialect risk) and could *drop* a test dbt fails every run. |
| **G4 disposition** | **Document as deliberate.** Hand-authored `tests/*.sql` keeps dbt's semantics: a `select count(*)` singular test returns one row, dbt fails it every run, and our always-`kept` is faithful. Reinterpreting an operator's own file could drop a test dbt fails. Also, `from_manifest` is the *mandated* discriminator and a tests-dir test is legitimately not `from_manifest` — reaching for `type == "custom_sql"` as a gate is explicitly forbidden. |
| **G1 fix** | **Both levers.** (a) The compiler REFUSES an ingested body that does not parse under the live dialect → `_InvalidIdentifier` → `kept-without-evidence` (the correctness fix; works regardless of what ingest decided). (b) Wire `adapter.dialect().name` into `read_manifest_tests` at the CLI so ingest and compiler agree (the signal fix — `dialect()` is pure, so building the adapter early costs nothing). |

---

## 3. Architecture Review

Three parallel reviews: (A) correctness of the G2 CTE-cardinality classifier — the false-positive
crux; (B) security/correctness of the G3 comment-strip + the §1.4 splice bug; (C) the G1 dialect
plumbing + the test/certification strategy.

| Area | Rating | Finding |
|---|---|---|
| **G2 classifier — false positives** | **pass** | The one real danger is the classifier firing on dbt-expectations' `validation_errors` shell (`WITH grouped_expression AS (SELECT COUNT(*) …), validation_errors AS (SELECT * … WHERE NOT(…)) SELECT * FROM validation_errors`) — which is 0-or-1 rows and *legitimately* row-returning. Misfiring there skip-records the **entire macro family**. Resolved by DEC-002: the predicate bails to row-returning on ANY pass-through filter (`WHERE`/`HAVING`/`LIMIT`/`QUALIFY`/`DISTINCT`), `JOIN`, set-op, or pivot; the shell's `validation_errors` `WHERE` is exactly what makes it bail. A false one-row verdict never *drops* a test (it skip-records), so the conservative default is safe. |
| **G2 classifier — mechanism** | **pass** | Reuses #268's already-imported `sqlglot.optimizer.scope` (`traverse_scope`/`Scope`) — `scope.sources[name]` is a child `Scope` for a CTE/derived-table ref and the `exp.Table` node itself for a physical relation. No hand-rolled CTE-name set (the `_physical_tables` precedent). Gotcha pinned: sqlglot 30.2.1 uses the arg key `"from_"`, not `"from"` — hard-coding `"from"` silently disables the rule. |
| **G3 comment-strip — validate≡execute** | **pass** | Today `validate_ingested_sql` scans a comment-stripped copy while the compiler executes `test.sql` verbatim — a validate-vs-execute byte gap (inert today only because the adapter then rejects the comments). Stripping once and returning the same bytes closes it. |
| **G3 — §1.4 single-line splice bug** | **pass (must fix together)** | Confirmed: a body ending in `-- tail` comments out `) AS sf_agg_value) AS sf_agg WHERE sf_agg_value <> 0`. Masked today only by the adapter's `--` reject; **fixing G3 unmasks it.** Stripping-before-compose removes the hazard; add a `\n` before the closing suffix as belt-and-braces. |
| **G3 — #268 relation-rewrite interaction** | **concern → resolved (DEC-001)** | #268 splices by character spans computed on the exact `test.sql`; applying those spans to a stripped copy is the DEC-015 injection. **Resolution:** strip only *complete* strings at the compiler (verbatim `test.sql`, the count-compose input, the already-spliced+verified `ingested_sql_override`) — the compiler performs no span operations, so desync is structurally impossible, and the engine's plan→splice→verify stays on the unstripped body, leaving #268's fixtures and parse-guard **untouched**. |
| **G3 — semantics / audit** | **pass** | Stripping is verdict-neutral on BQ/SF/Databricks (prune reads only the failing-row count; Spark/Oracle `/*+ hint */` changes the plan, not the result). Recording the stripped bytes as `compiled_sql` is correct — it is what ran; the raw `compiled_code` survives verbatim in the operator's manifest. |
| **G1 — correctness lever** | **pass** | The compiler-side refusal is the load-bearing correctness fix: a body that fails to parse under the live dialect must route to `_InvalidIdentifier` → `kept-without-evidence` *before* the verbatim always-1 wrap. Wiring the dialect at the CLI alone does NOT close it — the permissive gates admit an unparseable body regardless of dialect. |
| **G1 — CLI wiring feasibility** | **pass** | `dialect()` on an un-entered adapter is already proven safe: `prune.engine:1677` reads `adapter.dialect()` before its `with adapter:` at `:1698`. No adapter does I/O in `__init__`/`dialect()`; `from_profile` is pure. Move `load_profile`+`_make_warehouse_adapter` above the ingest step, thread `adapter.dialect().name`, pass the **same un-entered instance** to `prune_tests`. Blast radius = one call site (`generate` never ingests manifest tests). |
| **Standing locks** | **pass** | No new `DropReason` (5-value lock), no new `SkipReason` (3-value lock, reuse `malformed-supported-test`), no `import sqlglot` under `prune/` (both new helpers live in `ingest/_compiled_sql.py`; the compiler imports the pure functions), no audit-schema bump, no new `CandidateTest` variant, no new error class, no new CLI flag. |
| **Certification** | **pass (blocker on merge)** | Snapshots + the ungated sqlglot parse-guard certify SHAPE only; a comment-bearing body *executing* and a count-scalar restructure *executing* are certified only by the gated live BigQuery e2e (`@pytest.mark.bigquery`) — the merge gate (#121/#226/#268 DEC-016 lesson). |

**No blockers.** The one concern (G3 × #268 span-splice) is resolved by the strip-complete-strings-at-the-compiler design (DEC-001).

---

## 4. Refinement Log — Decisions

**DEC-001 — G3: strip comments at the compiler, on complete strings only; never upstream of a span operation.**
The `from_manifest` arm strips comments from the body it is about to emit — the verbatim source-bound
`test.sql`, the count-scalar compose input, and the already-spliced+verified `ingested_sql_override`.
All three are *complete* SQL strings; the compiler performs no sqlglot tokenization or span splicing, so
the #268 DEC-015 hazard ("offsets from one string applied to another") is structurally absent. The
engine's `_plan_ingested_samples`/`_finalise_ingested_plans` (plan→splice→verify) continue to operate on
the unstripped `test.sql`, so #268's committed `.out.sql` fixtures and the `test_ingested_rewrite_parse_guard`
drift check are **byte-unchanged** — an `.out.sql` byte change is a red flag, not an expected refresh.
Reject the alternative "strip upstream in the engine": it works but churns #268 fixtures for no gain.
`_strip_sql_comments` is promoted to a public `strip_sql_comments` in `ingest/_compiled_sql.__all__`
(a byte-*reducing* sanitizer, not SQL emission — same category as `_strip_string_literals`; adds no
sqlglot importer). The count-scalar compose gains a `\n` before the closing suffix (DEC-001a). The
warehouse layer and all four adapters stay **untouched** — `validate_test_sql` keeps its strict contract
for every other caller; the stripped body simply passes it now.

**DEC-002 — G2: prove exactly-one-row through CTE/derived-table re-projection; bail to row-returning on any filter/multiply.**
Add an *additive* branch to `is_row_returning` (after the existing root all-collapse check): build the
outermost scope (`build_scope(tree)` — on the full tree, not the `Subquery`-unwrapped root) and recurse
through single-source pass-through scopes to a base scalar-aggregate scope. Return "one row" only when
every hop is a plain `SELECT`, no `GROUP BY`, no `JOIN`/comma-join, a single `FROM` source that is a
`Table`/`Subquery` (no UNNEST/TVF/VALUES/PIVOT/UNPIVOT/TABLESAMPLE), and the source resolves via
`scope.sources[name]` to either a child `Scope` (recurse, depth-capped at 32) or, at the base, a physical
`Table` whose every projection collapses. **A pass-through hop carrying ANY of `WHERE`/`HAVING`/`QUALIFY`/
`LIMIT`/`OFFSET`/`DISTINCT` bails to row-returning** — this is precisely what spares dbt-expectations'
`validation_errors` shell and the `max_recency` macro (both filter the re-projected aggregate to 0-or-1
rows). `WHERE` is allowed *only* on the base aggregate node (a `COUNT` over zero matching rows is still one
row). Matches route to the existing `reason="malformed-supported-test"` skip (3-value `SkipReason`
unchanged). Conservative by construction: any parse/scope failure or unproven shape → row-returning, and a
false one-row verdict only skip-records, never drops. Fallback if the predicate proves fragile against real
bodies in implementation: fall back to the user's option C (document-only) rather than ship a fragile gate.

**DEC-003 — G1: compiler refuses an unparseable-under-live-dialect body (correctness); CLI threads the live dialect (signal).**
New pure helper `parses_under_dialect(sql, *, dialect) -> bool` in `ingest/_compiled_sql.py`, catching the
same `_PARSE_FAILURES` triple (`SqlglotError`, `RecursionError`, `ValueError`) and returning `False` on
failure or `tree is None`. The compiler's `from_manifest` arm calls it FIRST (before the determinism gate);
a `False` → `_InvalidIdentifier` → `kept-without-evidence`, so a body that BigQuery-parses but the live
dialect rejects never reaches the always-1 wrap. This is the correctness fix and holds regardless of what
dialect ingest classified under. Separately (signal, not correctness), `cmd_prune_existing` constructs the
un-entered adapter *before* the ingest step and threads `adapter.dialect().name` into
`read_manifest_tests(..., dialect=...)`, passing the same un-entered instance to `prune_tests`. The
`read_manifest_tests` default stays `"bigquery"` for other/future callers.

**DEC-004 — G4: `--tests-dir` count-scalar asymmetry is documented as deliberate, not "fixed".**
A hand-authored `tests/*.sql` singular test is `from_manifest=False` and keeps dbt's semantics: a
`SELECT count(*)` singular test returns one row, dbt fails it every run, and our always-`kept` is faithful.
Reinterpreting an operator's own file (the `0 = pass` restructure) could *drop* a test dbt fails — worse
than the status quo. And `from_manifest` is the *mandated* discriminator for ingested-vs-authored
(`business-rule-tests.md`: "gate on `from_manifest`, never `type=="custom_sql"`"); a tests-dir test is
legitimately not `from_manifest`. So the asymmetry is intentional: record it in `ingest-ops`/`prune-ops`
and close the item. No code.

**DEC-005 — behaviour changes are CHANGELOG-obligated; certified live, not by snapshot.**
G1/G2/G3 flip operator-visible verdicts (comment-bearing manifest tests go from never-pruned to pruned;
some always-`kept` become `kept-without-evidence` or skip-recorded). Per the cross-variant behaviour-change
rule, a `CHANGELOG.md [Unreleased] § Changed` entry with verbatim language is obligated, plus per-path
tests. The gated live BigQuery e2e is the merge gate: it must be extended to execute (a) a comment-bearing
compiled body and (b) a count-scalar restructure — snapshot/parse-guard equality certifies shape, not that
the warehouse accepts the SQL.

**DEC-006 — no schema/enum/flag/importer growth.** Reaffirmed as a hard constraint across all stories:
`DropReason` stays 5-valued, `SkipReason` 3-valued, `_PRUNE_AUDIT_SCHEMA_VERSION` stays 4, sqlglot
importers stay 2 (both new helpers land in the existing `ingest/_compiled_sql.py`), no new error class, no
new CLI flag, no new `CandidateTest` variant.

---

## 5. Detailed Breakdown

Bottom-up: ingest helpers → CTE classifier → compiler wiring → CLI dialect wiring → live cert → docs.
Validation command for every story: `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`.

> **Serialization note (orchestrator):** US-001 and US-002 both edit `ingest/_compiled_sql.py` — run them
> **sequentially**, never as two concurrent workers (per the shared-file collision rule).

### US-001 — `strip_sql_comments` + `parses_under_dialect` public helpers (ingest)
**Traces to:** DEC-001, DEC-003, DEC-006.
**Description:** In `src/signalforge/ingest/_compiled_sql.py`, promote `_strip_sql_comments` → public
`strip_sql_comments` and add `parses_under_dialect(sql, *, dialect="bigquery") -> bool`. Add both to
`__all__` (and re-export `parses_under_dialect` from `ingest/__init__` if the layer consumes it).
`parses_under_dialect` parses via `sqlglot.parse_one(sql, dialect=dialect)`, catches the shared
`_PARSE_FAILURES` triple, returns `False` on any failure or `tree is None`, `True` on a clean parse.
**AC:** both helpers exported; `parses_under_dialect` returns `True` on clean parse, `False` on
`SqlglotError` / `RecursionError` (reuse the `_deeply_nested_body()` fixture) / unknown-dialect
`ValueError` / `None`; `strip_sql_comments` behaviour byte-identical to the old private fn; no new sqlglot
importer (grep confirms count stays 2); validation command green.
**Files:** `src/signalforge/ingest/_compiled_sql.py`, `src/signalforge/ingest/__init__.py`,
`tests/ingest/test_compiled_sql.py`.
**TDD:** clean-parse→True; each of the three parse-failure classes→False; None→False; a planted-premise
self-check mirroring the existing unknown-dialect test.
**Depends on:** none.

### US-002 — CTE/derived-table one-row classifier in `is_row_returning` (ingest)
**Traces to:** DEC-002, DEC-006.
**Description:** Add the additive `scope_produces_exactly_one_row` recursion (depth-capped 32) and wire it
into `is_row_returning` AFTER the existing root all-collapse check, using `build_scope` (add to the
existing `from sqlglot.optimizer.scope import …` line) on the full `tree`. Resolve the FROM via
`sel.args.get("from") or sel.args.get("from_")`. Bail to row-returning on any pass-through
`WHERE`/`HAVING`/`QUALIFY`/`LIMIT`/`OFFSET`/`DISTINCT`, any `JOIN`/comma-join, set-op body, multi-source
FROM, non-`Table`/`Subquery` source, or `PIVOT`/`UNPIVOT`/`TABLESAMPLE`.
**AC:** the full must-fire / must-NOT-fire matrix from the review passes (esp. all four `validation_errors`
shells and `max_recency` stay row-returning); existing gate behaviour and the `is_prunable_count_scalar`
planted-negatives are untouched; the ingest bridge skip-records a fired body as `malformed-supported-test`;
`SkipReason` unchanged; validation command green.
**Files:** `src/signalforge/ingest/_compiled_sql.py`, `src/signalforge/ingest/parser.py` (skip routing if
needed), `tests/ingest/test_compiled_sql.py`, `tests/ingest/test_manifest_tests.py`.
**TDD:** the ~10 must-fire and ~15 must-NOT-fire SQL strings from the review, plus the `"from_"`-key
regression and a depth-cap case.
**Depends on:** US-001 (same file — serialize).

### US-003 — compiler: comment-strip + dialect-refusal + splice-newline (prune)
**Traces to:** DEC-001, DEC-001a, DEC-003, DEC-006.
**Description:** In `prune/compiler.py::_compile_custom_sql`'s `from_manifest` arm: (1) `body =
strip_sql_comments(test.sql)` once at the top; use `body` for the determinism gate, `validate_ingested_sql`,
count-compose, and the verbatim source return. (2) Call `parses_under_dialect(body, dialect=dialect.name)`
FIRST; `False` → `_InvalidIdentifier`. (3) Strip the `ingested_sql_override` before its existing
re-validation + return. (4) Reshape the count-scalar compose to put `\n` before `) AS sf_agg_value) AS
sf_agg WHERE sf_agg_value <> 0`. Import `strip_sql_comments`/`parses_under_dialect` from
`ingest._compiled_sql`. Warehouse layer and adapters untouched.
**AC:** a comment-bearing body compiles to a comment-free string that passes `validate_test_sql`; an
unparseable-under-live-dialect body → `_InvalidIdentifier` → kept-without-evidence (pinned per-dialect in
databricks + fakesnow suites); the §1.4 splice case (body ending in `-- x`) compiles correctly; **the #268
`tests/fixtures/prune/compiled_sql/ingested/*.out.sql` bytes are UNCHANGED** and
`test_ingested_rewrite_parse_guard` still passes; `compiled_sql` audit records the stripped bytes; no
sqlglot import under `prune/` (import-guard green); validation command green.
**Files:** `src/signalforge/prune/compiler.py`, `tests/prune/test_compiler.py`,
`tests/prune/test_compiler_databricks.py`, `tests/prune/test_compiler_fakesnow.py`,
`tests/prune/test_engine.py`.
**TDD:** comment-bearing body → comment-free output that `validate_test_sql` accepts; `-- tail` splice
case; unparseable-under-dialect → sentinel (bigquery-parses/other-fails); override strip; count-compose
newline shape.
**Depends on:** US-001, US-002.

### US-004 — wire the live dialect into ingest at the CLI (prune-existing)
**Traces to:** DEC-003.
**Description:** In `cmd_prune_existing`, construct the un-entered adapter (`load_profile` +
`_make_warehouse_adapter`) *before* the manifest-ingest step, thread `adapter.dialect().name` into
`_ingest_manifest_tests` → `read_manifest_tests(..., dialect=...)`, and pass the same un-entered instance
to `prune_tests`. Update the `_merge_manifest_tests` docstring (the one that currently documents the
deferral). Keep `read_manifest_tests`'s own default at `"bigquery"`.
**AC:** a spy confirms the live `adapter.dialect().name` reaches `read_manifest_tests`; the adapter is not
double-entered (no I/O at construction); the 15+ `_make_warehouse_adapter`-patched CLI tests stay green;
profile-error ordering change is benign (no test asserts it); validation command green.
**Files:** `src/signalforge/cli/prune_existing.py`, `tests/cli/test_prune_existing.py`.
**TDD:** dialect-threading spy; no-double-enter assertion; an offline cross-dialect-divergence pin (a body
that bigquery-parses but the wired dialect rejects → the compiler refusal fires end-to-end).
**Depends on:** US-001 (helper), US-003 (refusal path) — can start after US-003 lands.

### US-005 — extend the gated live BigQuery e2e (merge gate)
**Traces to:** DEC-005.
**Description:** In `tests/cli/test_e2e_bigquery_ingested_sample.py` (`@pytest.mark.bigquery`, run
`--no-cov`), inject two new bodies against the Austin source-as-model: (a) a real comment-bearing
(`--` + `/* */`) dbt-style body that must EXECUTE (proving the warehouse accepts the stripped SQL and the
verdict is real); (b) a bare `SELECT count(*) … WHERE <engineered>` body that must run at source /
`bypassed_to_source=True`, emit the `sf_agg_value` restructure, and yield `always-passes` (drop) for a
zero-count and `kept` (≥1 failure) for a non-zero-count — engineered-determinism per `testing-signal.md`.
**AC:** both new bodies execute against real BigQuery with the asserted verdicts; belt-and-suspenders
gating (marker + `_skip_reason()` + `tmp_path`) intact; maintainer-run only.
**Files:** `tests/cli/test_e2e_bigquery_ingested_sample.py`.
**Depends on:** US-002, US-003, US-004.

### US-006 — docs + CHANGELOG (5-surface graduation)
**Traces to:** DEC-004, DEC-005.
**Description:** `docs/ingest-ops.md` + `docs/prune-ops.md`: (a) G4 documented as deliberate — a tests-dir
`from_manifest=False` count scalar keeps dbt semantics (always-kept, faithful); (b) comment-bearing
manifest bodies now prune end-to-end (drop-rate reframe — "a high drop rate is the working state"); (c) the
G2 skip-record behaviour for CTE-reprojected counts; (d) the G1 refusal for unparseable-under-dialect
bodies. `CHANGELOG.md [Unreleased] § Changed` with verbatim behaviour-change language. Verify no CLI
flag/help changed (so the SKILL parity gate + `docs/cli-ops.md` are untouched).
**AC:** all four doc points land; CHANGELOG entry present; `mkdocs build` clean; validation command green.
**Files:** `docs/ingest-ops.md`, `docs/prune-ops.md`, `CHANGELOG.md`.
**Depends on:** US-002, US-003, US-004.

### US-007 — Quality Gate
Run the code reviewer 4× across the full changeset, fixing every real bug each pass; run CodeRabbit if
available; validation command green after all fixes. Special attention: (1) the #268 `.out.sql` bytes are
unchanged; (2) the G2 predicate does not fire on any real dbt-expectations body; (3) validate≡execute bytes
on every G3 path; (4) no sqlglot importer added; (5) the count-compose newline actually prevents the splice
bug. **Depends on:** US-001…US-006.

### US-008 — Patterns & Memory (priority 99)
Update `.claude/rules/` (`business-rule-tests.md` "#270 owns these four gaps" note → mark closed;
`ingest-layer.md` for the two new helpers + the CTE-cardinality classifier; `prune-engine.md` for the
compiler comment-strip + dialect-refusal; `warehouse-adapters.md` note that the adapter `validate_test_sql`
contract is now satisfied by a compiler-side strip rather than relaxed). Add/refresh the memory pointer for
the "strip complete strings downstream of the span machinery" reconciliation and the
`validation_errors`-has-a-WHERE discriminator. **Depends on:** US-007.

---

## 6. Beads Manifest

_(pending devolve)_
