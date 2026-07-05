# Ingest layer — operations guide

Operational reference for users of `signalforge.ingest`. Companion to
[`docs/prune-ops.md`](prune-ops.md),
[`docs/draft-ops.md`](draft-ops.md), and
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md), and to the design
record in
[`plans/super/104-ingest-external-tests.md`](../plans/super/104-ingest-external-tests.md).

## What it does

The drafter (`signalforge.draft`) is one way to produce candidate tests:
an LLM writes them. The ingest layer is the *other* way — point
SignalForge at an existing dbt `schema.yml` (hand-written, or output from
dbt-codegen, dbt Copilot, DinoAI, datapilot, …) and let the warehouse tell
you which of *those* tests actually add signal.

This extends Architectural Commitment #1 ("signal over volume") beyond
SignalForge's own LLM drafts: you can prune **any** generator's tests, not
just ours. `signalforge.ingest.read_schema` parses standard dbt test
syntax into the same typed `CandidateSchema` the drafter emits, so
`signalforge.prune.prune_tests` consumes it unchanged.

The reader understands the four test types SignalForge can compile to
warehouse SQL:

| dbt test | What prune checks |
|---|---|
| `not_null` | column has no NULLs in the sample |
| `unique` | column has no duplicate non-NULL values |
| `accepted_values` | column has no values outside the declared set |
| `relationships` | every child key resolves to a parent key |

Plus a fifth, sourced from a different file: an operator's hand-authored
**singular** tests under `tests/*.sql` are read into `custom_sql`
business-rule candidates (issue #116; see
[Singular `tests/*.sql` tests](#singular-testssql-tests)).

Plus a sixth, the **one recognised dbt-expectations macro**: hand-authored
`dbt_expectations.expect_table_row_count_to_be_between` declarations are
promoted to the `row_count_between` structured variant (issue #169 AC-5;
see [Recognition of `expect_table_row_count_to_be_between`](#recognition-of-expect_table_row_count_to_be_between)).

Every *generic* test the schema.yml carries that is not one of the four —
other `dbt_utils.*`, other `dbt_expectations.*`, custom generics, anything
namespaced — is **skipped and recorded**, never silently dropped (see
[Supported vs skipped](#supported-vs-skipped-tests)).

!!! tip "There's a CLI for this"
    The ingest layer is a **library** entry point — call `read_schema` and
    hand the result's `candidate` to `prune_tests` yourself — but most
    operators won't need to. The `signalforge prune-existing <model>
    --schema <path>` subcommand (shipped in
    [#105](https://github.com/wjduenow/SignalForge/issues/105)) wires
    ingest → prune → diff end to end with no LLM call. See the
    [CLI reference](cli-ops.md#signalforge-prune-existing-model-schema-path).

## Public API

Import from `signalforge.ingest`.

### Reader

- **`read_schema(schema, model, *, project_dir=None) -> IngestResult`** —
  the entry point. Parses an external `schema.yml` for one model and
  returns an `IngestResult`.
- **`read_test_files(tests_dir, model, manifest, *, project_dir=None, existing=None) -> IngestResult`** —
  reads the operator's **singular** dbt tests (`tests/*.sql`) for one
  model into `custom_sql` candidates (issue #116; see
  [Singular `tests/*.sql` tests](#singular-testssql-tests)).
- **`read_manifest_tests(manifest, model, *, project_dir=None) -> IngestResult`** —
  bridges the model's **dbt-compiled `manifest.json` test nodes**
  (`dbt-expectations`, `dbt-utils`, in-house generic tests) into
  `custom_sql` candidates by reading each node's already-Jinja-resolved
  `compiled_code` (issue #154; see
  [Recognition of dbt-compiled manifest tests](#recognition-of-dbt-compiled-manifest-tests)).

The `schema` argument is overloaded **by type** — this str-vs-`Path` split
is the contract:

- **`schema: pathlib.Path`** → a FILE path. It is canonicalised
  (symlink-/containment-hardened) against `project_dir` — defaulting to the
  file's parent directory when `project_dir` is `None` — then read. A
  missing file raises `IngestSchemaNotFoundError`; a symlink-loop / escape
  re-raises as `IngestSchemaParseError`.
- **`schema: str`** → RAW YAML CONTENT, not a path. No file read and no
  canonicalisation happen; the string is parsed directly. (Useful for
  testing or when the YAML is already in memory.)

`model` is the manifest `signalforge.manifest.Model` whose tests are being
ingested. `model.name` selects the matching `models:` entry in the
`schema.yml`; `model.columns` is the column set the
[anchor contract](#anchor-contract) validates against.

### Result shapes

- **`IngestResult`** — the reader's return value. Two fields:
    - `candidate: CandidateSchema` — the converted schema the prune stage
      consumes unchanged.
    - `skipped: tuple[SkippedTest, ...]` — one record per test the reader
      could not convert, in encounter order.
- **`SkippedTest`** — one structured skip record. Fields: `test_name: str`,
  `column: str | None` (`None` for a model-level test), `reason: SkipReason`,
  `detail: str` (a short free-text diagnostic).
- **`SkipReason`** — the closed reason literal (three values; see below).

These models are produced **in process** and handed straight to prune —
they are not serialised to a JSONL audit / sidecar.

### Errors

The `IngestError` hierarchy: `IngestError` (base), plus
`IngestSchemaNotFoundError`, `IngestSchemaParseError`,
`IngestSchemaTooLargeError`, `IngestModelNotFoundError`,
`IngestAnchorContractError`. See the [Error reference](#error-reference).

### Minimal usage

```python
from pathlib import Path

from signalforge.ingest import read_schema
from signalforge.manifest import load
from signalforge.prune import prune_tests
from signalforge.warehouse import WarehouseAdapter, load_profile

project_dir = Path("/path/to/dbt/project")
manifest = load(project_dir)
model = manifest.get_model("model.my_project.orders")

result = read_schema(project_dir / "models/marts/schema.yml", model)
print(f"{len(result.candidate.columns)} columns, "
      f"{len(result.skipped)} tests skipped")
for s in result.skipped:
    print(f"  skipped {s.test_name} on {s.column}: {s.reason} — {s.detail}")

# The candidate feeds prune unchanged. Pass an un-entered adapter —
# prune_tests owns the `with adapter:` lifecycle.
profile = load_profile(project_dir)
adapter = WarehouseAdapter.from_profile(profile)
prune_result = prune_tests(model, adapter, result.candidate, manifest)
```

## Supported vs skipped tests

The four supported types map to a typed `CandidateTest`; everything else is
recorded as a `SkippedTest`. The `SkipReason` literal is closed at three
values:

| `SkipReason` | Triggered by |
|---|---|
| `"unsupported-test-type"` | a bare-string test that isn't `not_null` / `unique` (e.g. `- positive`) |
| `"custom-or-generic-test"` | a namespaced or project-defined test (`dbt_utils.*`, `dbt_expectations.*` other than the recognised `expect_table_row_count_to_be_between` — see § ["Recognition of expect_table_row_count_to_be_between"](#recognition-of-expect_table_row_count_to_be_between), any custom generic), or a malformed test-entry shape |
| `"malformed-supported-test"` | a supported type whose required args are missing or empty (`accepted_values` with no `values`; `relationships` missing `to` or `field`) |

A skip is never a failure — the run continues. The skip records exist so an
operator can see *what was left out and why* — the `signalforge
prune-existing` CLI surfaces them as a "N tests skipped, here's why"
stderr report (one summary line, with per-test detail under `--verbose`).
Contrast this with the [anchor contract](#anchor-contract), which **does**
fail loud.

### Parser tolerances

The reader accepts the range of shapes dbt itself accepts:

- **`tests:` and `data_tests:` are both read and unioned.** dbt renamed
  `tests:` → `data_tests:` in 1.8; the reader concatenates both lists (in
  `tests:`-first encounter order) at column level and model level.
- **Identical tests are deduped.** A test appearing under both `tests:` and
  `data_tests:` (or twice in one list) collapses to one `CandidateTest`,
  keyed by `(type, column, args)`.
- **Inline args AND `arguments:`-nested args.** Both
  `accepted_values: {values: [...]}` and the dbt 1.8+
  `accepted_values: {arguments: {values: [...]}}` shapes are read.
- **Config keys are ignored, never mistaken for args.** Interleaved
  `config`, `severity`, `where`, `name`, `tags`, `error_if`, `warn_if`,
  `store_failures`, `limit` keys are stripped before the required-arg check.
- **`ref()` / `source()` are unwrapped on `relationships.to`.** `ref('m')`
  → `m`; `ref('pkg', 'm')` → `m` (last positional); `source('s', 't')` →
  `s.t`. This is a bounded regex unwrap — no Jinja engine, no new
  dependency. A `to` string matching neither pattern is carried verbatim.
- **A column with no supported tests is still a `CandidateColumn`** (it just
  carries an empty `tests` tuple), and a column / model with no
  `description` defaults to `""`.

## Anchor contract

A test that references a column **absent from the manifest `Model`** means
the ingested YAML is stale or wrong against the warehouse schema. That is a
correctness error the operator must fix, so the reader **fails loud** —
unlike unsupported types, which are skipped.

`read_schema` collects **every** anchor violation across the whole file
(it does not stop at the first) and raises one `IngestAnchorContractError`
whose `violations` tuple lists all of them — so you can fix the schema in a
single pass. Three checks run:

- each `CandidateColumn` name must exist in `model.columns`;
- each column-scoped test must reference its own column;
- each test's referenced column (column-level or model-level) must exist in
  `model.columns`.

A clean candidate raises nothing.

## Singular `tests/*.sql` tests

`read_schema` covers *generic* tests declared in YAML. dbt also supports
**singular** tests — a hand-authored `.sql` file under the project's
`tests/` directory that *is* a full failing-rows SELECT (a non-empty
result means the test failed). Those carry exactly the business rules
the four generic types cannot express ("a refund never exceeds its
order," "every shipped order has a ship date"). `read_test_files`
ingests them into the same `custom_sql` candidate variant the drafter
produces (issue #116), so they prune through the same engine.

```python
from signalforge.ingest import read_test_files

result = read_test_files(
    project_dir / "tests",          # directory of *.sql files
    model,                          # the manifest Model to associate to
    manifest,                       # used to resolve ref()/source()
    existing=schema_result.candidate,  # optional: dedupe against schema.yml custom_sql
)
```

It enumerates every `*.sql` file **directly under** `tests_dir` (sorted
by filename for byte-stable order) and classifies each:

- **ref/source/this resolution + association.** Each `.sql`'s
  dbt-Jinja refs are resolved with the **bounded** resolver
  (`{{ this }}`, `{{ ref('m') }}`, `{{ source('s','t') }}` — no Jinja
  *engine*). A file whose resolved refs reference **this** model
  becomes a model-level `custom_sql` candidate associated to it.
- **Unrelated files are ignored, not skip-recorded.** A `.sql` that
  references *some other* model is simply not included — it is not a
  defect of *this* model's ingest, so it produces no `SkippedTest`
  record. (A bare SELECT with no resolvable `ref()` is treated as
  unrelated, not associated.)
- **Unsupported Jinja is skip-recorded.** A `.sql` carrying Jinja the
  bounded resolver cannot evaluate (control-flow `{% if %}` / `{% for %}`,
  `var()` / `env_var()`, macros) lands in `IngestResult.skipped` with
  reason `malformed-supported-test` and a `singular .sql test contains
  Jinja the bounded resolver cannot evaluate` detail — never silently
  dropped. (The closed 3-value `SkipReason` literal has no singular-test
  specific value; `malformed-supported-test` is the closest fit since the
  `.sql` test cannot be compiled to runnable SQL.)
- **Dedupe across both sources.** Associated tests dedupe by
  `(model, "custom_sql", sql_hash)` where `sql_hash` is a 16-hex
  blake2b-8 of the SQL body. Two `.sql` files with byte-identical SQL
  collapse to one candidate; and when `existing` (a schema.yml-sourced
  candidate, e.g. from a prior `read_schema` call) is passed, any `.sql`
  whose SQL matches an `existing` `custom_sql` test is dropped so the
  same rule is not pruned twice.

No anchor-contract check runs here — singular tests are model-level, so
there is no per-column reference to validate against `model.columns`.

Each `.sql` is **size-capped** (from `stat().st_size`, before it is read
into memory) by the same 5 MB cap that guards `read_schema`; oversize
raises `IngestSchemaTooLargeError`. `tests_dir` is symlink-/containment-
hardened against `project_dir` (defaulting to `tests_dir` itself); a
missing or non-directory `tests_dir` raises `IngestSchemaNotFoundError`,
and an unreadable file or canonicalisation failure raises
`IngestSchemaParseError`.

!!! tip "There's a CLI for this too"
    The `signalforge prune-existing <model> --schema <path>` subcommand
    reads singular `tests/*.sql` (override the directory with
    `--tests-dir`) *alongside* the schema.yml in one run and prunes both
    — see the
    [CLI reference](cli-ops.md#singular-testssql-business-rule-tests-us-014).

## Recognition of `expect_table_row_count_to_be_between`

As of issue #169, the parser **promotes**
`dbt_expectations.expect_table_row_count_to_be_between` from a generic
`SkippedTest(reason="custom-or-generic-test")` to the typed
`CandidateTestRowCountBetween` variant — the same variant the drafter
produces (see
[`docs/draft-ops.md`](draft-ops.md#row-count-tests-row_count_between)).
This closes AC-5 of #169: `prune-existing` against a hand-authored
`schema.yml` that declares the dbt-expectations macro now prunes the
declaration through the warehouse alongside drafted candidates, instead
of skipping it as "we don't know how to evaluate this." Operators
running `prune-existing` against an existing dbt-expectations corpus
get the same kept / dropped / flagged signal SignalForge produces for
its own drafts.

**Inbound mapping** (DEC-008 of #169). The macro's body fields map to
the variant's Pydantic field names:

| `expect_table_row_count_to_be_between` field | `CandidateTestRowCountBetween` field |
|---|---|
| `min_value` | `minimum` |
| `max_value` | `maximum` |
| `where` | `where` |

The mapping is inverted by the diff emitter on the outbound path
(`minimum` → `min_value`, see
[`docs/diff-ops.md`](diff-ops.md#row-count-yaml-emission)), so an
ingested declaration round-trips to the same dbt-expectations YAML on
re-emit.

**Skip-recorded shapes.** The parser routes the macro to
`SkippedTest(reason="malformed-supported-test")` — the closest fit
under the closed 3-value `SkipReason` literal — when:

- both `min_value` AND `max_value` are missing (an unbounded row-count
  assertion would always pass — no signal);
- either bound is a non-integer (string, float, list, dict);
- either bound is negative;
- `min_value > max_value` (the constraint is unsatisfiable);
- `where` is set but isn't a string;
- the macro is declared **column-scoped** rather than model-level (a
  `COUNT(*)` is table-level by construction — a `column:` slot on the
  variant doesn't make sense).

The `SkipReason` literal stays closed at three values; recognising the
macro is a parser-arm change, not a literal change (DEC-013 of #116
generalised — `SkipReason` is the closed surface; adding a recognised
shape never grows it).

**Other `dbt_expectations.*` macros are unchanged.** The promotion is
narrow — exactly
`dbt_expectations.expect_table_row_count_to_be_between` matches. Every
other macro in the `dbt_expectations` namespace (e.g.
`expect_table_row_count_to_equal`, `expect_column_values_to_be_between`,
`expect_column_distinct_count_to_be_in_set`) continues to skip-record
as `SkipReason="custom-or-generic-test"`. Extending recognition to
other `dbt_expectations` shapes is a follow-up (tracked separately
under #154 for prune+grade-only adapters that consume the compiled
test SQL rather than rebuilding to a typed variant).

## Recognition of `dbt_utils.unique_combination_of_columns`

As of issue #170, the parser **promotes**
`dbt_utils.unique_combination_of_columns` from a generic
`SkippedTest(reason="custom-or-generic-test")` to the typed
`CandidateTestUniqueCombination` variant — the same variant the drafter
produces (see
[`docs/draft-ops.md`](draft-ops.md#composite-uniqueness-unique_combination)).
This closes the second leg of the structured-ingest story #169 started
with `expect_table_row_count_to_be_between`: `prune-existing` against
a hand-authored `schema.yml` that declares the dbt-utils macro now
prunes the declaration through the warehouse alongside drafted
candidates, instead of skipping it as "we don't know how to evaluate
this." Operators running `prune-existing` against an existing
`dbt_utils.unique_combination_of_columns` corpus get the same kept /
dropped / flagged signal SignalForge produces for its own drafts.

**Inbound mapping** (DEC-008 of #170). The macro's body fields map to
the variant's Pydantic field names:

| `unique_combination_of_columns` field | `CandidateTestUniqueCombination` field |
|---|---|
| `combination_of_columns` | `columns` |
| `where` | `where` |

The mapping is inverted by the diff emitter on the outbound path
(`columns` → `combination_of_columns`), so an ingested declaration
round-trips to the same dbt-utils YAML on re-emit. Match the
`expect_table_row_count_to_be_between` precedent above: prefix-free
Pydantic field names, naming-seam translation in two functions, one
each side of ingest+diff.

**Skip-recorded shapes.** The parser routes the macro to
`SkippedTest(reason="malformed-supported-test")` — the closest fit
under the closed 3-value `SkipReason` literal — when:

- `combination_of_columns` is missing or not a list;
- the list is empty or has fewer than 2 entries (single-column
  uniqueness is the existing `unique` test type, not this variant);
- any list item is not a string;
- the list contains duplicate column names;
- `where` is set but isn't a string;
- the macro is declared **column-scoped** rather than model-level
  (composite uniqueness is a table-level property; a `column:` slot on
  the variant doesn't make sense).

The `SkipReason` literal stays closed at three values — recognising the
macro is a parser-arm change, not a literal change (DEC-013 of #116
generalised).

**Other `dbt_utils.*` macros are unchanged.** The promotion is narrow
— exactly `dbt_utils.unique_combination_of_columns` matches. Every
other macro in the `dbt_utils` namespace (`expression_is_true`,
`relationships_where`, `recency`, etc.) continues to skip-record as
`SkipReason="custom-or-generic-test"`. Extending recognition to other
`dbt_utils` shapes is the same follow-up tracked for `dbt_expectations`
under #154.

## Recognition of dbt-compiled manifest tests

`read_manifest_tests(manifest, model, *, project_dir=None) -> IngestResult`
(issue #154) prunes the tests you already author with `dbt-expectations`,
`dbt-utils`, or your own in-house generic macros — **without** teaching
SignalForge each macro's semantics. It leans on work dbt already did: after
`dbt compile`, every `resource_type == "test"` node in `manifest.json`
carries `compiled_code`, the macro fully rendered to warehouse SQL. This
bridge reads that string and routes it through the **existing `custom_sql`
prune pipeline** ([`docs/prune-ops.md` § custom_sql](prune-ops.md#custom_sql-manifest-ingested-and-drafted)),
so a manifest test gets the same kept / kept-uncertain / dropped / flagged
treatment as everything else.

Where the other two readers take a `schema.yml` (string or file), this one
takes the loaded `Manifest` and reads `Manifest.tests` — the
`resource_type == "test"` nodes the loader now surfaces as typed
`GenericTest` read-back models (issue #154; the loader tolerates a null
`compiled_code` silently, exactly like a null `catalog.json` `data_type`).
Each test is associated to its model via `associate_test_model` (a
feature-detect ladder across dbt manifest schema versions v9–v12), and only
the ones belonging to `model.unique_id` are considered. `project_dir` is
accepted for signature parity with the adjacent stages but is unused (this
bridge does no path I/O — the raw `compiled_code` is carried verbatim; SQL
safety is re-validated in the prune compiler).

### Three gates decide prunable vs. skip-recorded

For each associated node, the bridge runs four checks in order —
**presence → row-returning → deterministic → comment-tolerant safety scan** —
and the first failure wins. Only a body that clears every gate becomes a
model-level `CandidateTestCustomSQL(column=None, sql=<compiled_code>)`.
A node that fails a gate is **skip-recorded, never silently dropped**, using
the same closed 3-value `SkipReason` (never grown):

| Disposition | `SkipReason` | Trigger |
|---|---|---|
| **Pruned** (`custom_sql` candidate) | — | `compiled_code` present, row-returning, deterministic, passes the safety scan |
| Absent / null `compiled_code` | `custom-or-generic-test` | `dbt parse` (not `dbt compile`) produced the manifest; the node has no body to evaluate. `detail` names the fix: run `dbt compile`. |
| Aggregate / scalar-shaped body | `malformed-supported-test` | the outer `SELECT` is a single collapsing aggregate with no `GROUP BY` — wrapping it in `SELECT COUNT(*) AS failures FROM (<sql>)` would report `failures=1` **always** (a silent wrong `kept`). Aggregate-macro support is a follow-up (#267). |
| Non-deterministic body | `malformed-supported-test` | `TABLESAMPLE` / `RAND` / `CURRENT_TIMESTAMP` / `NOW` / `GETDATE` / `UUID` / … — the prune verdict would not be reproducible (violates explainable-diffs). |
| `compiled_code` fails the safety scan | `malformed-supported-test` | a top-level `;` (multiple statements) or unbalanced parentheses survive the comment-tolerant scan (see below). |

The row-returning and determinism gates use **sqlglot AST inspection**, not
regex/substring: a column literally named `random_id` or a `CURRENT_TIMESTAMP`
token inside a string literal never false-positives, because membership is
checked against AST *function nodes* only. Both gates are conservative —
on a sqlglot parse failure they return the *permissive* verdict (treat as
row-returning / deterministic), leaving a genuinely broken body to be caught
by the safety scan or the warehouse adapter's `kept-without-evidence`
routing downstream.

### KEY FINDING — dbt-expectations bodies are almost all prunable

`dbt-expectations` compiles **every** macro — including
`expect_table_row_count_to_be_between` — into a **row-returning
`validation_errors` shell**:

```sql
with grouped_expression as (
    select ( 1=1 and count(*) >= 1 and count(*) <= 100 ) as expression
    from "db"."schema"."orders"
), validation_errors as (
    select * from grouped_expression where not(expression = true)
)
select * from validation_errors
```

The outer query is `select * from validation_errors` — a genuine
failing-rows `SELECT`. The `count(*)` lives *inside* the `grouped_expression`
CTE (a nested query scope the row-returning walk deliberately stops at), so
the aggregate gate does **not** fire and the test **is pruned**. Wrapping
this shell in `SELECT COUNT(*) AS failures FROM (…)` is semantically correct:
`failures=1` means "the row count is out of bounds", `failures=0` means "in
bounds".

The aggregate-SKIP disposition therefore fires **only on a BARE
`SELECT COUNT(*) …` body** — the shape dbt-utils / hand-written in-house
generic tests can emit — never on a `dbt-expectations` macro. In practice, of
a typical `dbt-expectations` test set, the only common skip cause is a
non-deterministic body (e.g. `expect_row_values_to_have_recent_data`, which
compiles a `now()` comparison).

### Macro identity travels into the diff `why`

Each pruned candidate's `rationale` is synthesized at construction from the
node's macro name + rendered args, e.g.
`dbt-expectations expect_column_values_to_be_between(column=amount, min_value=1000, max_value=2000)`.
That string flows through the diff `why` cascade unchanged
([`docs/diff-ops.md` § ingested manifest tests](diff-ops.md#ingested-manifest-tests-macro-identity-in-the-why)),
so a dropped or kept-uncertain manifest test names the source macro and the
operator can locate and remove the right test. No new field is added to
`CandidateTestCustomSQL` — the macro identity rides on `rationale`.

The synthesized rationale is scrubbed of the grade layer's `</ARTIFACT>`
envelope close tag (both the literal and the `</  ARTIFACT>` whitespace-split
variant) at construction time, so a hostile macro arg (a regex / value list)
cannot break the judge prompt fence and fail the whole `--grade` run closed.

### The "not a silent skip" surface

The manifest reader is stage-0 and tolerates a null `compiled_code`
silently. The operator-facing surface lives in this bridge: each
no-`compiled_code` node is skip-recorded with a `detail` naming `dbt compile`,
and when **every** associated node lacks `compiled_code` a single prominent
summary `SkippedTest` (`test_name="(manifest tests)"`) is prepended so the
operator gets one "run `dbt compile` and commit `target/manifest.json`"
pointer rather than only N per-node lines. This is soft — never a hard abort.
It mirrors the `catalog.json` / `data_type` guidance: the same
build-artifact gap surfaced at the layer that can act on it.

### CLI entry point

`read_manifest_tests` is wired into `signalforge prune-existing` behind the
opt-in `--from-manifest` flag (issue #154; see
[`docs/cli-ops.md` § dbt-compiled manifest tests](cli-ops.md#dbt-compiled-manifest-tests-from-manifest-issue-154)).
Off by default, `prune-existing` behaves byte-for-byte as before.

## Safety posture

Two attack surfaces, both mitigated before any parse:

- **Malicious YAML.** Only `yaml.safe_load` is used (never `yaml.load`), and
  the **raw byte length is size-capped before the parse runs** (5 MB) so a
  billion-laughs / deeply-nested-anchor payload never reaches the parser.
  Oversize raises `IngestSchemaTooLargeError`.
- **Path traversal / symlinks.** A `Path` argument is canonicalised through
  the project's symlink-/containment-hardened path safety helper before any
  read.

A `relationships.to` value carrying `ref('x')` or raw SQL is **not** an
injection vector at ingest — prune's identifier safety check gates every
identifier before it reaches compiled SQL.

The ingest layer is a stage-0 reader: it emits **no logging**.
Observability lives in the consuming prune / grade stages.

## Error reference

Every error subclasses `IngestError`, which renders its `message` plus a
`↳ Remediation:` line. The CLI exit-code tier each maps to (for the
fast-follow #105) is noted.

### `IngestSchemaNotFoundError` — tier 1 (load)

The supplied `schema.yml` `Path` does not exist.

> Verify the schema.yml path is correct and the file exists. Pass the path
> to the dbt YAML file that declares the model's tests (commonly
> `models/<dir>/schema.yml` or a per-model `_<name>.yml`).

### `IngestSchemaParseError` — tier 1 (load)

The `schema.yml` could not be parsed: malformed YAML, an unreadable file
(encoding / OS error), or a path-containment failure (symlink loop / escape
from the project directory). The triggering exception is chained via
`__cause__`.

> Verify the file is valid YAML (yaml.safe_load must parse it), is readable,
> and that no symlink in its path escapes the project directory. Run
> `dbt parse` to confirm dbt itself accepts the schema.

### `IngestSchemaTooLargeError` — tier 1 (load)

The raw `schema.yml` byte length exceeds the parse-safety cap (checked
before any `yaml.safe_load`).

> The schema.yml exceeded the configured byte safety cap applied before
> yaml.safe_load. Inspect the file for accidental bloat or an attempted
> billion-laughs / deeply-nested-anchor payload. Trim the schema or split
> it across multiple files.

### `IngestModelNotFoundError` — tier 2 (input)

No `models:` entry in the `schema.yml` matches `model.name`. A `schema.yml`
can declare several models; the operator named one the file does not
contain.

> Verify the model name matches a `name:` under `models:` in the
> schema.yml. Model names are matched exactly (case-sensitive).

### `IngestAnchorContractError` — tier 2 (input)

One or more tests reference a column absent from the `Model`. Carries the
full `violations` tuple (all violations across the file).

> Each listed test references a column that is absent from the model.
> Either correct the column name in the schema.yml to match the model, or
> regenerate the manifest (`dbt parse`) so the model's column set is
> current.

## Worked example

Given this `schema.yml` (a dbt-codegen-shaped file) and an `orders` model
whose columns are `order_id`, `status`, `customer_id`, `amount`,
`created_at`:

```yaml
version: 2

models:
  - name: customers
    description: "An unrelated model in the same file — not selected."
    columns:
      - name: id
        tests:
          - not_null

  - name: orders
    description: "Orders fact table."
    columns:
      - name: order_id
        description: "Surrogate key for the order."
        tests:
          - not_null
          - unique
        data_tests:
          - unique          # duplicate of the unique above — deduped

      - name: status
        tests:
          - accepted_values:           # inline args
              values: ["placed", "shipped", "cancelled"]
          - dbt_utils.unique_combination_of_columns:   # namespaced — skipped
              combination_of_columns: [order_id, status]

      - name: customer_id
        data_tests:
          - relationships:             # arguments:-nested + ref() unwrap
              arguments:
                to: "ref('customers')"
                field: id

      - name: amount
        tests:
          - positive                   # bare unsupported string — skipped

      - name: created_at               # no description, no tests

    tests:
      - dbt_utils.expression_is_true:  # model-level custom generic — skipped
          expression: "amount >= 0"
```

`read_schema(yaml_str, orders_model)` returns an `IngestResult` with:

**Kept candidate tests** (on `result.candidate`):

| column | test | notes |
|---|---|---|
| `order_id` | `not_null` | |
| `order_id` | `unique` | the `data_tests:` duplicate collapsed away |
| `status` | `accepted_values` values `("placed", "shipped", "cancelled")` | inline args |
| `customer_id` | `relationships` `to="customers"`, `field="id"` | `ref('customers')` unwrapped |

`created_at` is present as a `CandidateColumn` with an empty `tests` tuple
and `description=""`. The `customers` model is ignored — only the named
`orders` model is selected.

**Skipped tests** (`result.skipped`):

| `test_name` | `column` | `reason` |
|---|---|---|
| `dbt_utils.unique_combination_of_columns` | `status` | `custom-or-generic-test` |
| `positive` | `amount` | `unsupported-test-type` |
| `dbt_utils.expression_is_true` | `None` (model-level) | `custom-or-generic-test` |

The four kept tests flow into `prune_tests` exactly as if the drafter had
produced them; the three skipped tests are surfaced for the operator's
review and do not stop the run.
