# CLI — operations guide

Operational reference for the `signalforge` command. Companion to
[`docs/safety-ops.md`](safety-ops.md),
[`docs/draft-ops.md`](draft-ops.md),
[`docs/prune-ops.md`](prune-ops.md),
[`docs/grade-ops.md`](grade-ops.md),
[`docs/diff-ops.md`](diff-ops.md),
[`docs/manifest-loader-ops.md`](manifest-loader-ops.md), and
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md), and to the
design record in
[`plans/super/9-cli-entrypoint.md`](../plans/super/9-cli-entrypoint.md).

The CLI is the user-facing entry point for the v0.1 pipeline. It does
not implement new pipeline behaviour; it wires the existing stages —
`signalforge.manifest.load` → `signalforge.safety.load_safety_config` →
`signalforge.draft.draft_schema` → `signalforge.prune.prune_tests` →
`signalforge.grade.grade_artifacts` →
`signalforge.diff.render_diff` — into a single command, applies the
four-tier exit-code taxonomy on the way out, and renders the diff
to stdout (plus the JSON sidecar to disk by default).

This is the load-bearing operationalisation of Architectural Commitment
\#4 in [`CLAUDE.md`](../CLAUDE.md) — **OSS-first, Core-friendly**: the
runnable surface a user installs from PyPI and runs against any
dbt-core project, locally or in CI, with no dbt Cloud dependency.

## Installation

The published wheel:

```bash
pip install signalforge-dbt
signalforge --version
```

The PyPI distribution name is `signalforge-dbt`; the import name and
CLI command remain `signalforge`. The `-dbt` suffix exists because the
bare `signalforge` name on PyPI is held by an unrelated DSP package.

For development against a clone (`[dependency-groups].dev` pulls in
pytest, ruff, pyright, and `build`):

```bash
uv sync --dev   # canonical
# back-compat fallback for contributors without uv:
pip install -e ".[dev]"  # note: does not include the uv-only `build` extra
                         # used by the wheel_smoke marker (issue #47)
```

`uv.lock` is committed; `uv sync --dev` reproduces the exact resolved
versions CI uses across the 3.11 / 3.12 matrix.

After install, the `signalforge` console script is registered via
`pyproject.toml`'s `[project.scripts]` entry and resolves to
`signalforge.cli:main`.

## Subcommands

The CLI exposes five subcommands: `generate`, `init-demo`, `lint`,
`prune-existing`, `version`. `signalforge --help` prints the
top-level help; each subcommand has its own `--help` page (e.g.
`signalforge generate --help`).

### `signalforge generate <model>`

Run the full pipeline against `<model>`: load the manifest, build the
safety policy, draft candidate artifacts via the LLM, prune
always-pass / known-clean-fail tests against warehouse samples, grade
the survivors, and render a diff against any existing `schema.yml`.

> **Note:** the prune step runs warehouse SQL on every invocation
> regardless of `safety.mode`. To skip the prune layer entirely, set
> `prune.enabled: false` in `signalforge.yml` (see
> [`docs/prune-ops.md`](prune-ops.md#configuration-signalforgeyml-prune-block)).

Positional argument:

- `<model>` — Model under draft. Accepts a dbt `unique_id`
  (e.g. `model.proj.customers`) or a file path (e.g.
  `models/marts/customers.sql`). Routes to
  `Manifest.get_model(...)` which canonicalises the path and
  raises `ModelNotFoundError` / `ModelDisabledError` /
  `ModelPathOutsideProjectError` / `ModelMissingSqlError` on
  failure. Mutually exclusive with `--select`; exactly one of
  the two must be supplied.

Multi-model flag:

- `--select <expr>` — Run `generate` across multiple models in
  one process. `<expr>` is a comma-separated union of atoms (set
  OR); whitespace around commas is stripped. Three atom shapes:
    - `tag:<name>` — matches when `<name>` is in
      `Model.tags ∪ Model.config.tags`.
    - `path:<glob>` — shell-style `fnmatch` against
      `Model.original_file_path`. dbt's path-prefix convention
      diverges; v0.2 uses fnmatch and operators write
      `path:models/staging/*` (with the trailing wildcard),
      not `path:models/staging` (DEC-016 of
      [`plans/super/37-multi-model-select.md`](../plans/super/37-multi-model-select.md)).
    - bare `<value>` — `model.<...>` prefix routes as
      `unique_id`; otherwise routes as a file path (mirrors the
      v0.1 positional `<model>` semantics).

  Match results are deduplicated and ordered by `unique_id`
  (deterministic). Three concrete examples:

  ```bash
  signalforge generate --select tag:staging
  signalforge generate --select path:models/marts/*
  signalforge generate --select tag:staging,path:models/marts/*
  ```

  Mutually exclusive with the positional `<model>`. Empty atoms
  (`--select ,tag:foo`) exit 2 with `CliSelectorParseError`; a
  well-formed selector that matches zero models exits 2 with
  `CliSelectorNoMatchError`. See [Running across many
  models](#running-across-many-models) for semantics, the
  shell-loop alternative, and cumulative-cost guidance.

Path / project-discovery flags:

- `--project-dir PATH` — Absolute assertion: `<PATH>` must contain
  `dbt_project.yml`. The CLI does NOT walk up from the override
  (DEC-027). Default: walk up from the current working directory
  until `dbt_project.yml` is found (DEC-001).
- `--manifest PATH` — Override the default
  `<project_dir>/target/manifest.json`. Path is canonicalised
  against the resolved project_dir.
- `--profiles-dir PATH` — Override the default `profiles.yml`
  search location. Mirrors dbt-core's `--profiles-dir` flag.
  Sets `DBT_PROFILES_DIR` in the current process environment.

Runtime knob flags:

- `--mode {schema-only,aggregate-only,sample}` — Override the
  safety sampling mode. Precedence: CLI flag >
  `safety.mode` in `signalforge.yml` > library default. Applied
  via `SafetyPolicy.with_mode(...)` so the validators (notably
  the sample-mode warning from `safety-layer.md` DEC-021) re-run
  on the override. Argparse rejects unknown values → exit 2.
- `--min-score N` — Override `grade.min_mean_score` (closed
  interval `[0.0, 1.0]`). This is the **aggregate-verdict**
  threshold: it is consumed by `GradingReport.passed` (and, when
  `grade.fail_on_below_threshold=true`, by `GradeBelowThresholdError`).
  **Reporting-only by default** — does NOT affect the exit code
  by itself; the threshold-fail consequence lives in
  `signalforge.yml` (see [Threshold-fail
  behaviour](#threshold-fail-behaviour) below). The diff renderer's
  `flagged` tier is driven by per-criterion `GradingResult.passed`
  (set verbatim by the LLM judge), not by `min_mean_score`, so this
  flag does not change the kept/dropped/flagged counts in the diff
  table — it only changes the aggregate verdict and (opt-in) exit
  code. Out-of-range values exit 2.
- `--write` — Write the proposed `schema.yml` to disk under
  `<project_dir>/<model_dir>/schema.yml`. **Additionally** writes
  each proposed singular `.sql` business-rule test to its
  `tests/<model>__<descriptor>_<hash>.sql` path under the project
  (DEC-010/DEC-014 of issue #116); each file is prepended with a
  `-- signalforge:generated <hash>` header marker. The JSON sidecar
  is still written to `<project_dir>/.signalforge/diff.json`.
  Mutually exclusive with `--dry-run`. See `--force` for the
  `.sql` overwrite policy.
- `--force` — With `--write`, governs overwrite of an existing
  proposed `.sql` test file (DEC-010 of issue #116). New files
  always write. A file that already exists and carries
  SignalForge's `-- signalforge:generated` marker is overwritten
  **only** with `--force`; without `--force` it is skipped with a
  stderr WARNING naming the file. A file that exists **without**
  the marker (hand-authored) is **never** overwritten, even with
  `--force` — it is skipped with a clear stderr WARNING (we never
  clobber human-written tests). No-op without `--write`.
- `--dry-run` — Run the FULL pipeline (LLM + warehouse + grade)
  and print the diff to stdout, but write nothing — neither the
  `schema.yml`, the proposed `.sql` test files, nor the
  `.signalforge/diff.json` sidecar. Overrides the default-on
  sidecar (DEC-010). Mutually exclusive with `--write`.
- `--estimate` — Print a pre-flight cost preview and exit
  without making any billable Anthropic or warehouse call. The
  full pipeline prelude still runs (manifest, safety, draft,
  prune, grade, diff configs; warehouse profile; adapter
  construction) so typos in `--profiles-dir` /
  `signalforge.yml` surface BEFORE the estimate is computed
  (DEC-009 of `plans/super/36-estimate-cost-preview.md`).
  The estimate uses `client.messages.count_tokens(...)` for the
  drafter prompt and one representative artifact per grading
  criterion (`1 + len(rubric)` calls; `messages.create` is never
  invoked), plus one BigQuery `dryRun` to project the warehouse
  bytes for the prune step. Mutually exclusive with `--write`
  and `--dry-run`; argparse rejects two of the three with
  exit 2.

  Output goes to stdout as plain text with three sections —
  Draft / Grade / Warehouse — followed by totals and a footer
  listing the price-table version
  (`signalforge.llm.pricing.PRICE_TABLE_VERSION`) and the
  `3.5 tests/column` heuristic the prune-bytes projection uses
  (DEC-012). The exact byte-shape is pinned by
  `tests/cli/test_estimate_render.py` against
  `tests/fixtures/estimate/output_happy.txt`. The estimate
  reports a billing ceiling: actual scans usually come in
  lower because cache hits, sampled rows, and shorter LLM
  responses all trim the projected numbers.

  Exit codes: `0` on success AND on the partial-failure path
  where the warehouse `dryRun` raises any `WarehouseError`
  subclass (per DEC-005, mirrors `prune-engine.md` DEC-009's
  conservative-bias degrade — the operator still gets the LLM
  half of the estimate; the warehouse section renders
  `bytes-per-row: <unavailable: <ErrorClass>>` and the totals
  show `<unknown>` for the warehouse contribution; a single
  stderr WARNING carries the one-line reason). `3` (API tier)
  if the LLM `count_tokens` call fails on auth / connection /
  quota — `count_tokens` is a real round-trip against the
  Anthropic API, so `ANTHROPIC_API_KEY` is required (DEC-006).
  Tier-2 errors that fire before the short-circuit
  (`EstimateUnknownModelError` if `llm.model` in
  `signalforge.yml` isn't in `signalforge.llm.pricing.PRICES`,
  manifest / model-resolver failures) propagate via the usual
  `cmd_generate` catch surface.

  Example invocation:

  ```bash
  signalforge generate models/staging/stg_bikeshare_trips.sql --estimate
  ```
- `--format {ansi,markdown,json}` — Select the diff renderer.
  ANSI: coloured terminal output (default). Markdown:
  GitHub-friendly report. JSON: stdout receives the JSON
  sidecar's contents.
- `--scope {sample,full}` — Override `prune.scope`
  (default: from config). `sample`: tests run against a
  100k-row deterministic sample. `full`: tests run against
  the entire source table (no sampling). Per-run override;
  the config-file value is the durable default. (DEC-011 of
  issue #22.)
- `--sample-strategy {oneshot,materialised}` — Override
  `prune.sample_strategy` (default: from config). Only
  meaningful when `scope=sample`. `materialised` (default):
  materialise the sample once into a BigQuery temp table,
  then run all candidate tests against it. `oneshot`:
  re-sample per test (v0.1 behaviour, much costlier on wide
  tables). Per-run override; the config-file value is the
  durable default. (DEC-011 of issue #22; see
  `docs/prune-ops.md` cost model section.)

Observability flags:

- `--quiet` — Suppress per-stage stderr progress lines and raise
  the log level to `WARNING`. Mutually exclusive with `--verbose`.
- `--verbose` — Raise the log level to `DEBUG` and surface
  panic-path tracebacks for unexpected errors. Mutually exclusive
  with `--quiet`.
- `--no-color` — Strip ANSI colour codes from stdout. Sets
  `NO_COLOR=1` in the current process environment so the AnsiRenderer's
  existing precedence chain emits plain text.

The flag → config precedence chain is uniform across knobs: **CLI
flag > `signalforge.yml` block > library default**. Library defaults
are documented per-stage in each layer's ops doc.

### `signalforge init-demo [<dest>]`

Copy the bundled Austin bikeshare demo project (a minimal dbt
project pointing at the public
`bigquery-public-data.austin_bikeshare.bikeshare_trips` dataset)
out of the installed wheel into `<dest>` so a first-run PyPI user
can `cd` into a working project and exercise the full pipeline
without authoring their own dbt setup. The bundled `profiles.yml`
reads `GOOGLE_CLOUD_PROJECT` from the operator's environment, so
no profile editing is required.

Wraps the public library entry point
`signalforge.demo.copy_demo(dest, *, force=False) -> Path`; the
CLI re-raises the lower-level `DemoError` subclasses as
`CliInitDemo*Error` wrappers at the handler boundary so the
four-tier exit-code taxonomy stays homogeneous (DEC-012 of
[`plans/super/47-init-demo.md`](../plans/super/47-init-demo.md)).

Positional argument:

- `<dest>` — Destination directory. Optional; default
  `./signalforge-demo/`. Relative paths resolve against the
  current working directory; `~` expands;
  `Path(dest).expanduser().resolve(strict=False)` follows
  symlinks and raises `CliPathError` on a cycle. **No
  `--project-dir` containment gate applies** —
  `init-demo` is the one subcommand that *creates* a project
  rather than operating *inside* one, so the
  `canonicalise_user_path(...)` containment helper used by
  every other CLI flag is deliberately bypassed (DEC-004).

Flags:

- `--force` — Atomically replace `<dest>` if it exists and is
  non-empty (`shutil.rmtree` then `shutil.copytree`). Without
  `--force`, a non-empty `<dest>` raises
  `CliInitDemoDestExistsError` (tier 2); empty existing
  directories proceed without `--force`. As a blast-radius
  guard (DEC-001), `--force` refuses `/`, `$HOME`, and the
  current working directory and raises
  `CliInitDemoDestUnsafeError` (tier 2) on any of those.

Exit codes (four-tier taxonomy; see § Four-tier exit-code
taxonomy for the full table):

- `0` — copy succeeded; next-steps message printed to stdout.
- `1` — broken install (`CliInitDemoFixtureMissingError`:
  the wheel didn't ship `signalforge/_demo/`), symlink-cycle
  resolve failure (`CliPathError`), or generic filesystem
  failure such as `ENOSPC` / `EACCES` / `EROFS`
  (`CliInitDemoCopyError`).
- `2` — operator-side dest mistakes:
  `CliInitDemoDestExistsError` (non-empty dest without
  `--force`) or `CliInitDemoDestUnsafeError` (`--force`
  against `/`, `$HOME`, or cwd).

Output: on success, a plain-text "next steps" message lands on
stdout (DEC-014) — no ANSI colour codes, no Markdown, no env-var
*values*, just the env-var *names* (`GOOGLE_CLOUD_PROJECT` and
`ANTHROPIC_API_KEY`) and the three first-run commands an operator
needs to run (`cd <dest>`, `signalforge lint`, then
`signalforge generate models/staging/stg_bikeshare_trips.sql --dry-run`).
The message survives `--no-color` because it carries no colour
codes.

Example:

```bash
signalforge init-demo /tmp/sf-austin
cd /tmp/sf-austin
export GOOGLE_CLOUD_PROJECT=<your-billing-project>
export ANTHROPIC_API_KEY=sk-ant-...
signalforge lint
signalforge generate models/staging/stg_bikeshare_trips.sql --dry-run
```

### `signalforge lint`

Validate the five existing `signalforge.yml` config blocks (`safety:`,
`llm:`, `prune:`, `grade:`, `diff:`) against their per-stage loaders,
then load the dbt manifest. No warehouse, no LLM, no network —
sub-second target. The natural pre-flight check operators run on every
save.

Flags:

- `--config PATH` — Override the default
  `<project_dir>/signalforge.yml`. Path is canonicalised against
  the resolved project_dir.
- `--manifest PATH` — Override the default
  `<project_dir>/target/manifest.json`. Path is canonicalised against
  the resolved project_dir and must stay inside it (no symlink
  escapes).
- `--model NAME` — Optional. Also resolve a model in the loaded
  manifest and report whether it exists. Accepts three forms:
  - **unique_id** — `model.<pkg>.<name>`
  - **file path** — `models/path/to/<name>.sql`
  - **bare name** — `<name>` (matches against `Model.name` across
    enabled nodes via `Manifest.iter_models`; sidesteps the
    `Manifest.get_model` gotcha that routes bare names through the
    file-path branch). A bare name matching two or more enabled
    models fails loud with a disambiguation list.
- `--project-dir PATH` — Same semantics as `generate`'s flag
  (DEC-027 absolute assertion; walk-up applies only when the flag
  is omitted).

Multi-error reporting (DEC-008): when more than one block fails,
`lint` collects every failure and emits a header + bullet list rather
than short-circuiting on the first. The header generalises to
`ERROR: lint found N validation errors:` so manifest / model entries
sit alongside the five config blocks under the same shape. Stdout is
silent on success (git-style); stderr carries the failures.

The manifest load is appended **after** the config loaders so the
operator sees `signalforge.yml` typos AND a missing or
schema-mismatched `target/manifest.json` in one run rather than fixing
them one cascade at a time. Common manifest failures `lint` surfaces:

| Symptom | Typed error | Exit tier | Fix |
| --- | --- | --- | --- |
| `target/manifest.json` is missing | `ManifestNotFoundError` | 1 (load) | Run `dbt parse` (or `dbt compile`) inside the project. |
| Manifest schema is outside v9–v12 (e.g. dbt 1.13 → v13, Fusion → v20) | `UnsupportedManifestVersionError` | 1 (load) | Pin the project to a supported dbt version, or wait for the upstream tracking issue. |
| `--model <name>` does not resolve | `ModelNotFoundError` / `ModelDisabledError` | 2 (input) | Check the spelling, or pass the unique_id (`model.<pkg>.<name>`) / file-path form. |

`--model` resolution skips silently when the manifest load itself
fails — the resolver depends on a loaded manifest and emitting a
spurious second bullet would mislead the operator. The manifest entry
alone surfaces.

The Quick Start in [`README.md`](../README.md#5-pre-flight-check-signalforge-lint)
runs `signalforge lint` immediately after the fixture is prepared and
before the first `signalforge generate` call — sub-second, free, and
catches `extra="forbid"` typos (e.g. `safety: { mdoel: ... }`) plus
the manifest-version / model-name mistakes that would otherwise
surface only after a billable LLM round-trip.

### `signalforge prune-existing <model> --schema <path>`

Prune your existing dbt tests against real warehouse data. Runs
**ingest → prune → diff** — no draft, no grade, **no LLM call** — and
reports which of your existing tests add signal (kept), which could not
be evaluated (kept-uncertain), and which always pass or fail on
known-clean data (dropped). Both your externally-authored `schema.yml`
(`not_null` / `unique` / `accepted_values` / `relationships`) **and**
your project's singular `tests/*.sql` business-rule tests are ingested
and pruned in one run (US-014): each `.sql` referencing this model
becomes a model-level `custom_sql` test, deduped against the
schema.yml tests and pruned alongside them. The product story: *point
SignalForge at your existing dbt tests and let the warehouse tell you
which ones add no signal* — extending Architectural Commitment #1
("signal over volume") to any generator's tests (hand-written,
dbt-codegen, dbt Copilot, DinoAI, datapilot). The library seams this
wraps are `signalforge.ingest.read_schema(...)` (issue #104) and
`signalforge.ingest.read_test_files(...)` (US-014); the subcommand is
issue #105 (DEC-002 … DEC-010 of
[`plans/super/105-prune-existing-cli.md`](../plans/super/105-prune-existing-cli.md)).
For which dbt test shapes are supported vs. skipped (and the `tests:` /
`data_tests:` and `ref()` / `source()` tolerances), see the
[ingest layer guide](ingest-ops.md#supported-vs-skipped-tests).

Because there is no LLM call, this is strictly cheaper than
`generate`: the only warehouse cost is exactly what the prune step
already incurs.

Positional argument:

- `<model>` — Model whose existing tests to prune. Accepts a
  **bare model name** (e.g. `customers`), a dbt **unique_id**
  (e.g. `model.proj.customers`), or a **file path** (e.g.
  `models/marts/customers.sql`). Resolved via the shared
  `_resolve_model_by_key` helper (DEC-008): bare names match against
  `Model.name` across enabled nodes; a bare name matching two or more
  enabled models fails loud with a disambiguation list.

Flag reference:

| Flag | Required | Choices / default | Purpose |
| --- | --- | --- | --- |
| `--schema PATH` | **yes** | — | The externally-authored dbt `schema.yml` whose tests to prune. Canonicalised via `canonicalise_user_path` (symlink/containment → `CliPathError`); the `Path` is passed to `read_schema` so the full ingest typed-error surface fires (DEC-005), and the same file's UTF-8 text is fed to the diff renderer as `existing_schema` (DEC-004). |
| `--project-dir PATH` | no | walk-up default | Absolute assertion: `<PATH>` must contain `dbt_project.yml`; the CLI does NOT walk up from the override (DEC-027). Default: walk up from the current working directory. |
| `--manifest PATH` | no | `<project_dir>/target/manifest.json` | Override the manifest location. Canonicalised against the resolved project_dir. |
| `--profiles-dir PATH` | no | dbt default search | Override the `profiles.yml` search location (mirrors dbt-core's flag). Sets `DBT_PROFILES_DIR` in the current process environment. |
| `--tests-dir PATH` | no | `<project_dir>/tests` | Override the singular-test directory enumerated for model-level `tests/*.sql` files (US-014). Each `.sql` referencing this model is pruned alongside the schema.yml tests; unrelated files are ignored. The **default** directory is optional — when absent only the schema.yml tests are pruned; an **explicit** `--tests-dir` pointing at a missing directory fails loud (`IngestSchemaNotFoundError`). |
| `--scope {sample,full}` | no | from config | Override `prune.scope`. Applied via `PruneConfig.model_validate` so validators re-run (DEC-002). |
| `--sample-strategy {oneshot,materialised}` | no | from config | Override `prune.sample_strategy`. Applied via `PruneConfig.model_validate` (DEC-002). |
| `--format {ansi,markdown,json}` | no | `ansi` | Select the diff renderer. ANSI: coloured terminal output. Markdown: GitHub-friendly report. JSON: stdout receives the JSON sidecar's contents. |
| `--dry-run` | no | off | Run ingest → prune → diff and print the diff to stdout, suppressing the default-on `.signalforge/diff.json` sidecar. The fail-closed `.signalforge/prune.jsonl` audit is **still written** (every prune run leaves a durable receipt — the cross-stage fail-closed invariant; mirrors `generate`). There is **no `--write`** (read-only w.r.t. your `schema.yml`). |
| `--quiet` | no | off | Suppress per-stage stderr progress lines and the skipped-test report, and raise the log level to `WARNING`. Mutually exclusive with `--verbose`. |
| `--verbose` | no | off | Raise the log level to `DEBUG`, list each skipped test in detail, and surface panic-path tracebacks. Mutually exclusive with `--quiet`. |
| `--no-color` | no | off | Strip ANSI colour codes from stdout. Sets `NO_COLOR=1` in the current process environment. |

**Dropped vs. `generate`** (DEC-002 / DEC-003): `--mode`,
`--min-score`, `--estimate`, `--select`, `--write`. `--mode` shapes
the LLM payload, which doesn't exist on this path, so it would be a
dead flag; `--scope` / `--sample-strategy` are the genuinely-relevant
warehouse knobs that take its place.

#### Read-only by design (DEC-003)

`prune-existing` deliberately ships **no `--write` flag**. The
`--schema` file is hand-authored, so silently overwriting it would be
surprising and destructive. The command prints the rendered diff to
stdout and writes the `.signalforge/diff.json` sidecar by default;
`--dry-run` suppresses that sidecar for a pure-stdout diff. (The
fail-closed `.signalforge/prune.jsonl` audit is still written even under
`--dry-run` — every prune run leaves a durable receipt by design, the
same as `generate`; `--dry-run` governs only the end-of-run diff
sidecar.) Re-pruning into the file is a possible v0.3 follow-up with a
confirmation/backup story.

#### Unified diff against your file (DEC-004)

The deliverable is a unified diff against your *actual* `schema.yml`:
the `--schema` content is fed to `render_diff` as `existing_schema`,
so the diff shows exactly what to remove from that file. There is no
grading report (`grading_report=None`), so the diff renders **kept /
kept-uncertain / dropped only — never `flagged`** (the `flagged` tier
requires a grading report, locked by #104 DEC-011).

> **Cosmetic-reformatting caveat.** The proposed YAML is re-emitted
> from the kept `CandidateSchema` via the diff emitter, so it may
> reorder keys or normalise whitespace relative to your hand-authored
> file, adding cosmetic diff lines. This is accepted and documented;
> the **kept / kept-uncertain / dropped table is the load-bearing
> signal**, not the byte-exact diff body.

#### Singular `tests/*.sql` business-rule tests (US-014)

In addition to the `--schema` file, `prune-existing` ingests your
project's **singular tests** — the hand-authored `.sql` files under
`<project_dir>/tests` (override with `--tests-dir`). Each `.sql` whose
resolved `ref()` / `source()` / `this` references the model under
prune becomes a model-level `custom_sql` candidate test and is pruned
**alongside** the schema.yml tests in the same warehouse run, so the
warehouse tells you which of *all* your existing tests — schema.yml
AND singular — add no signal. Specifics:

- **Dedupe across both sources.** A singular `.sql` whose body matches
  a `custom_sql` test already present in the schema.yml collapses to
  one (deduped by SQL hash via `read_test_files(..., existing=...)`).
- **Unrelated `.sql` files are ignored**, not recorded — a singular
  test referencing a *different* model is simply not included.
- **Unsupported Jinja folds into the skipped-test report.** A `.sql`
  carrying control-flow Jinja, `var()` / `env_var()`, or macro calls
  the bounded resolver cannot evaluate is recorded as
  `malformed-supported-test` and appears in the same grouped stderr
  summary as the schema.yml skips.
- **Multi-table singular tests run full-scan** (within the bytes cap)
  rather than against a sampled CTE — a business rule spanning a JOIN
  must see the whole join.
- **Read-only still holds:** kept `custom_sql` tests surface as
  standalone `.sql` proposals in the diff; nothing is written back to
  your test files.

Exit codes (four-tier taxonomy; see § Four-tier exit-code taxonomy):

- `0` — pipeline completed; diff printed to stdout.
- `1` — load/parse failure: ingest schema errors
  (`IngestSchemaNotFoundError` / `IngestSchemaParseError` /
  `IngestSchemaTooLargeError`), `CliPathError`, `ManifestNotFoundError`,
  `ProfileNotFoundError`, the panic-path catch.
- `2` — input-validation failure: `IngestModelNotFoundError` /
  `IngestAnchorContractError` (a test references a column absent from
  the model), `ModelNotFoundError`.
- `3` — external-dependency / fail-closed audit-write failure:
  `WarehouseAuthError`, `BytesBilledExceededError`, the prune/diff
  audit-write durability errors.

No bespoke `CliPruneExisting*` wrapper classes exist (DEC-006): the
five `IngestError` concretes are already first-class in
`_EXCEPTION_TO_EXIT_CODE` (#104 DEC-004 landed them precisely so #105
needs no rework), so they route to the correct tier via the
`map_exception_to_exit_code` MRO walk.

Stderr shapes:

- **Skipped-test summary** (DEC-007) — after `read_schema`, when
  `IngestResult.skipped` is non-empty and not `--quiet`, one line
  grouped by `SkipReason`:

  ```text
  Skipped 3 unsupported tests: custom-or-generic-test×2, unsupported-test-type×1
  ```

  Under `--verbose`, one indented line per `SkippedTest` follows
  (`test_name`, `column`, `reason`, `detail`). Emitted via
  `print_stderr` (the ANSI-safe sink), not `_LOGGER` — it is
  operator-facing info, not a log event. `--quiet` suppresses both.

- **Errors** — the standard `ERROR: <message>` + optional
  `↳ Remediation: <text>` shape (see § Stderr message shape per
  tier).

- **Progress** — three stages (DEC-010): `[1/3] ingest`,
  `[2/3] prune`, `[3/3] diff`, each with a paired `done in <X>` line.
  TTY-gated via `should_emit_progress`; `--quiet` suppresses,
  `--verbose` forces on.

Worked example:

```text
$ cd /repo/dbt/analytics
$ signalforge prune-existing customers --schema models/marts/schema.yml
# stderr (TTY): three entry lines + three done lines
[1/3] ingest: parsing schema.yml...
[1/3] ingest: done in 0.0s
Skipped 1 unsupported test: dbt-expectations×1
[2/3] prune: running 7 existing tests against warehouse...
[2/3] prune: done in 18.4s
[3/3] diff: rendering...
[3/3] diff: done in 0.1s
# stdout: rendered ANSI diff (kept / kept-uncertain / dropped; never flagged)
Kept (4):
  column.customer_id.test.not_null     — caught a real failing row
  ...
Dropped (3):
  column.region.test.not_null          — always-passes
  ...
$ echo $?
0
```

`--dry-run` would skip the `.signalforge/diff.json` sidecar while
still printing the diff to stdout. `--format json` would emit the
JSON sidecar's contents to stdout.

### `signalforge version`

Print the same string as `signalforge --version` and exit 0. Both
surfaces share one source of truth (`signalforge.__version__` in
`src/signalforge/__init__.py`); the flag uses argparse's
`action="version"`, the subcommand prints directly.

## Project-root discovery

The CLI resolves the dbt project root before any pipeline work
(DEC-001 + DEC-027). The convention mirrors how `git` discovers
`.git`:

- **No flag → walk-up.** Ascend from `Path.cwd()` until a directory
  containing `dbt_project.yml` is found. If we reach the filesystem
  root without finding one, the CLI exits 1 with remediation.
- **`--project-dir <PATH>` → absolute assertion.** The override is
  NOT a walk-up starting point. The supplied path must directly
  contain `dbt_project.yml`; if not, the CLI exits 1.

Worked example. Suppose your dbt project lives at
`/repo/dbt/my_project/` and your shell is in
`/repo/dbt/my_project/models/marts/`:

```
$ pwd
/repo/dbt/my_project/models/marts

$ signalforge generate customers.sql
# walk-up resolves to /repo/dbt/my_project (the nearest dir
# containing dbt_project.yml). Equivalent to:
$ signalforge generate customers.sql --project-dir /repo/dbt/my_project
```

When the override points at a directory that does NOT contain
`dbt_project.yml`:

```
$ signalforge generate models/marts/customers.sql --project-dir /tmp
ERROR: --project-dir '/tmp' does not contain dbt_project.yml
  ↳ Remediation: Pass a path that points directly at a dbt project
    root (the directory containing dbt_project.yml). The flag is an
    absolute assertion; the CLI does not walk up from it.
$ echo $?
1
```

The discovered (or asserted) directory is logged at DEBUG level —
run with `--verbose` to surface it.

## Four-tier exit-code taxonomy

Every `cmd_<name>` handler in the CLI returns an integer drawn from
exactly four values. Ported from clauditor's
`llm-cli-exit-code-taxonomy.md` rule and pinned by the AST scan in
`tests/test_audit_completeness.py` (DEC-019 / DEC-024) and the
parametrized tests in `tests/cli/test_exit_codes.py`. See
[`.claude/rules/cli-layer.md`](../.claude/rules/cli-layer.md) for the
canonical statement of the rule.

| Exit | Tier | Meaning | Example sources |
| --- | --- | --- | --- |
| `0` | success | Artifact written / printed; pipeline completed cleanly. | Happy path. |
| `1` | load | Configuration / path / manifest / system not in a coherent state to start work. | `ManifestNotFoundError`, `ProfileNotFoundError`, `ConfigNotFoundError`, `DraftConfigInvalidError`, `DiffError`, `CliPathError`, the panic-path catch for unexpected exceptions. |
| `2` | input | Caller-supplied data is wrong, OR a post-call invariant failed. | `ModelNotFoundError`, `LLMOutputAnchorContractError`, `TableNotFoundError` (DEC-012 — the model's table reference is wrong), `GradeBelowThresholdError` (DEC-011), `DiffCandidateModelMismatchError`, `CliInputError`. |
| `3` | API | External dependency unavailable. | `LLMRateLimitError`, `LLMAuthError`, `LLMServerError`, `WarehouseAuthError`, `BytesBilledExceededError`, `GradeLLMError`, `GradeAuditWriteError`, every fail-closed audit-write durability error. |

Do NOT invent a fifth category. Do NOT collapse categories 2 and 3
into one "bad exit"; pipelines need the split to decide retry vs
don't-retry. The mapping table lives at
`signalforge.cli._helpers._EXCEPTION_TO_EXIT_CODE` and the AST scan
asserts every typed exception under `src/signalforge/*/errors.py`
appears there.

### Stderr message shape per tier

Per DEC-008 — CI parsers key on the shape:

- **Tier 1 and 3** — single line: `ERROR: <message>`, optionally
  followed by `  ↳ Remediation: <text>` when the typed error
  carries one. The remediation footer is rendered by the typed
  error's own `__str__` (the layer-base pattern from
  `signalforge.safety.errors` and its siblings); the CLI passes
  it through unchanged.

- **Tier 2** — header + bullets for multi-violation errors;
  single line for everything else. The drafter's
  `LLMOutputAnchorContractError` and the `lint` subcommand's
  multi-block failures both use this shape:

  ```text
  ERROR: candidate response failed anchor contract: 3 violations
    - column 'phantom' not in model
    - test 'not_null' on column 'foo' has duplicate parameter-less tests
    - test 'relationships' references missing model 'orders'
    ↳ Remediation: Inspect the LLM response in the response audit JSONL ...
  ```

Multi-violation formatting lives in
`signalforge.cli._helpers.format_error_to_stderr` — the CLI is the
sink; the layer error classes do NOT override `__str__` for the
header+bullet shape (DEC-017, "escape at the sink").

## Stderr shapes (WARNING)

Three operator-facing WARNINGs surface to stderr through the CLI's
log handler (`setup_logging`, default level `INFO`). They are
distinct from the four-tier exit-code message shapes above —
WARNINGs are signal that the run continued (or that a cleanup
side-effect failed), not signal that the run failed. CI parsers
that key on `ERROR:` lines should ignore these.

### Cleanup-failure WARNING (issue #22 DEC-014)

Source: `BigQueryAdapter.__exit__` after a failed
`CALL BQ.ABORT_SESSION();` cleanup attempt at the end of a
materialised-strategy prune run. The materialisation work succeeded;
only the explicit teardown failed (network blip, session already
revoked, quota issue). BigQuery's server-side session timeout will
reap the orphan (~24h max) but the operator can clean up
immediately by running the printed manual command.

Multi-line shape (verbatim from issue #22 DEC-014 — the manual
command's flag form is load-bearing; do not paraphrase):

```text
BigQuery session cleanup failed; session will auto-expire in <N>s (BigQuery TTL).
  Session ID: <raw session_id>
  Reason: <exception class name>
  To clean up immediately:
    bq query --connection_property=session_id=<raw> --use_legacy_sql=false "CALL BQ.ABORT_SESSION();"
```

`<N>` is `max(1, int(ttl_seconds - elapsed_in_session))` — floor
at 1 avoids "auto-expire in 0s" confusion. The raw `session_id`
appears only in this WARNING (deliberate exception to the
otherwise-strict redaction rule per issue #22 DEC-003): it's the
only piece of info the operator needs to construct the manual
command, the audience is the same principal who owns the session
(BigQuery rejects `BQ.ABORT_SESSION()` from any other identity),
and the surface is bounded (cleanup-failure path only, never on the
happy path, never in audit JSONL, never in `__repr__`).

**`--quiet` does NOT suppress this WARNING.** `--quiet` raises the
log floor to `WARNING`, which keeps WARNINGs flowing. The
cleanup-failure WARNING is operator-actionable (manual command +
identifier inside) — silently dropping it would defeat the point.

See [`docs/warehouse-adapter-ops.md` § Session cleanup & manual
recovery](warehouse-adapter-ops.md#session-cleanup--manual-recovery)
for the three-layer cleanup model and the
`INFORMATION_SCHEMA.JOBS_BY_PROJECT` query template that surfaces
orphan sessions for periodic audits.

### Materialisation-failure / degraded-run WARNING (issue #22 DEC-009)

Source: `prune_tests` orchestrator at the head of the
conservative-bias routing path, BEFORE the per-decision JSONL
audit writes. Fires when `adapter.materialise_sample(...)` raises
any `WarehouseError` subclass (`MaterialisationFailedError`,
`MaterialisationNotSupportedError`, `UnknownTableSizeError`,
`SamplingRequiresPartitionFilterError`, etc.) at orchestrator entry.
The orchestrator catches the exception, routes every candidate
test to `kept-without-evidence`, and emits one stderr WARNING so
the operator gets a one-line out-of-band signal that the run was
degraded (the only in-band signal is N identical
`why="sample materialisation failed: ..."` fields buried in the
`prune.jsonl` audit).

Single-line lazy-format JSON (passes the grep gate at
`tests/llm/test_logger_grep_gate.py`):

```text
materialisation failed; routing all tests to kept-without-evidence: {"model_unique_id": "model.proj.customers", "candidate_count": 30, "error_class": "MaterialisationFailedError", "error_message": "BigQuery API: 503 Service Unavailable"}
```

Distinct from the per-decision `why` field (the in-band signal in
the prune JSONL audit) and from the cleanup-failure WARNING above
(different boundary — that's `__exit__` cleanup; this is
orchestrator entry). The exit code is still `0` if everything else
succeeds — the run produces a valid `PruneResult` with every
candidate marked `kept-without-evidence`. Operators that want a
hard-fail on degraded runs use `prune.sample_strategy: oneshot`
to bypass materialisation entirely.

Mirrors the existing budget-exceeded WARNING pattern (next
section).

### Budget-exceeded WARNING (v0.1, prune-engine.md DEC-011)

Source: `prune_tests` orchestrator after `total_budget_seconds`
has elapsed. Existing v0.1 surface; documented here for parity
with the two new WARNINGs above so all three sit in one place for
operator reference.

Single-line lazy-format JSON:

```text
budget exceeded: {"unstarted_count": 14, "model": "model.proj.customers"}
```

The orchestrator marks every remaining un-started test
`kept-without-evidence` with
`why="total prune budget exceeded before evaluation"` and emits one
final WARNING with the count of un-started tests. No partial
evaluation results — a test that was running when the budget
tripped is `kept-without-evidence`, not `kept` (failing-rows count
is unknown). See
[`docs/prune-ops.md` § Drop-reason taxonomy](prune-ops.md#drop-reason-taxonomy).

### Proposed `.sql` overwrite-skip WARNING (issue #116 DEC-010/DEC-014)

Source: `generate --write` when materialising a proposed singular
`.sql` business-rule test whose target file already exists and the
overwrite policy declines to clobber it. Two distinct shapes:

```text
WARNING: <tests/...sql> already exists; pass --force to overwrite SignalForge-generated tests. Skipping.
WARNING: refusing to overwrite hand-authored <tests/...sql> (no '-- signalforge:generated' marker); skipping.
```

The first fires when the existing file carries SignalForge's
`-- signalforge:generated` marker but `--force` was not passed; the
second fires when the existing file does NOT carry the marker
(hand-authored) — that file is **never** overwritten, even with
`--force`. The run continues and exits `0` for the skip; only the
named file is left untouched. New files (no existing target) write
silently.

## Threshold-fail behaviour

By default, a below-threshold rubric is reported (the diff renderer
shows the artifacts as `flagged`) but does NOT fail the run — the
operator's diff surfaces the verdict and the operator decides. This
matches the grade layer's "report-only by default" posture; see
[`docs/grade-ops.md` § Threshold-fail
behaviour](grade-ops.md#threshold-fail-behaviour) for the full
ordering invariant on the grade-layer side.

Operators that want hard-fail-on-threshold behaviour opt in by
setting the grade-layer config knob:

```yaml
# signalforge.yml
grade:
  fail_on_below_threshold: true
```

When the knob is true and `GradingReport.passed` is `False`,
`grade_artifacts(...)` raises `GradeBelowThresholdError` AFTER the
sidecar JSON is durably persisted. The CLI catches the typed error
and exits 2 (input/invariant tier). The complete `grade.json` and
per-pair `grade.jsonl` audit are on disk so the operator can
diagnose *why* the run fell below threshold; raising before the
sidecar write would defeat that durable hand-off (graduated in #9
US-002 / DEC-021 from the grade layer's v0.2 reservation).

`--min-score N` is reporting-only and never affects the exit code by
itself. The two surfaces are deliberately layered: the flag overrides
the aggregate-verdict threshold (`grade.min_mean_score`) consumed by
`GradingReport.passed`; the config knob (`grade.fail_on_below_threshold`)
turns that aggregate signal into an exit-code consequence. The diff
renderer's `flagged` tier is driven by per-criterion
`GradingResult.passed` (set by the LLM judge) — separate from the
aggregate threshold. Operators can have any combination of the three
signals (per-criterion pass/fail in the diff, aggregate pass/fail in
the report, exit-code consequence) without the others.

## Environment variables

The CLI honours three environment variables:

- **`NO_COLOR`** — when set to a non-empty string, the AnsiRenderer's
  precedence chain (DEC-021 of `diff-renderer.md`) emits plain text.
  `--no-color` is the CLI-side ergonomic equivalent: it sets
  `NO_COLOR=1` for the duration of the call (DEC-023). The
  unconditional ANSI strip on user-content fields (DEC-007 of
  `diff-renderer.md`) runs regardless — that's the security boundary,
  not a UX knob.
- **`FORCE_COLOR`** — overrides `NO_COLOR` per the diff layer's
  precedence chain. `--no-color` clears `FORCE_COLOR`
  belt-and-braces so an environmental override doesn't defeat the
  operator's explicit opt-out.
- **`DBT_PROFILES_DIR`** — read by the warehouse profile loader. The
  `--profiles-dir <PATH>` flag sets this in the current process
  environment (DEC-007); a pre-existing value is honoured when the
  flag is omitted.

There is no env-var override for `--project-dir`, `--mode`, or
`--min-score`. Project-discovery is the walk-up convention; the
behaviour-knob flags map to `signalforge.yml` blocks under the
documented precedence chain.

## Logging

The CLI configures the root logger once via `setup_logging(...)` in
`signalforge.cli._helpers`. Logger name: `signalforge.cli`. Default
level is `INFO`; `--quiet` raises to `WARNING`; `--verbose` lowers
to `DEBUG`. Output goes to stderr in
`%(asctime)s [%(levelname)s] %(name)s: %(message)s` format.

Every logger call in `src/signalforge/cli/` uses the lazy-format
JSON convention — `_LOGGER.debug("...: %s", json.dumps({...}))` —
enforced by the grep gate at
`tests/llm/test_logger_grep_gate.py`, which now covers six
directories (`{llm, draft, prune, grade, diff, cli}`).

`--verbose` additionally suppresses the `_safe_excepthook`
installation, so any panic-path traceback (an exception that
escapes the `cmd_<name>` boundary) reaches stderr. Default
behaviour strips the traceback (DEC-016): no `Traceback (most
recent call last):` ever leaks to a non-verbose run.

## Progress lines

When the CLI is attached to an interactive terminal (both stderr AND
stdout return `True` from `isatty()`), `cmd_generate` emits one
stderr progress line per stage entry plus a paired `done in <X>`
line at stage exit (DEC-014, DEC-026). Live values; no hardcoded
duration hints. Example shape:

```text
[1/5] safety: building LLM request...
[1/5] safety: done in 0.0s
[2/5] draft: calling LLM (model claude-sonnet-4-6)...
[2/5] draft: done in 41.7s
[3/5] prune: running 12 candidate tests against warehouse...
[3/5] prune: done in 18.4s
[4/5] grade: scoring 8 artifacts × 4 criteria (32 calls)...
[4/5] grade: done in 1m 12s
[5/5] diff: rendering...
[5/5] diff: done in 0.1s
```

Non-TTY runs (piped, redirected, CI logs) emit no progress lines by
default. `--quiet` suppresses regardless of TTY; `--verbose` forces
progress on regardless of TTY (the operator explicitly opted in).

The `<fact>` field on each entry line is computed from objects
already in scope (model id, candidate test count,
`kept_count × criteria_count`) so the operator sees the size of the
work that's about to happen rather than a stale estimate.

## Worked example

A complete `signalforge generate` session against a sample dbt
project. Shell prompt is `$`; commentary is in `#` comments;
stderr / stdout are annotated.

```text
$ cd /repo/dbt/analytics
$ signalforge generate models/marts/customers.sql --mode schema-only
# stderr (TTY): five entry lines + five done lines
[1/5] safety: building LLM request...
[1/5] safety: done in 0.0s
[2/5] draft: calling LLM (model claude-sonnet-4-6)...
[2/5] draft: done in 38.2s
[3/5] prune: running 14 candidate tests against warehouse...
[3/5] prune: done in 22.6s
[4/5] grade: scoring 9 artifacts × 4 criteria (36 calls)...
[4/5] grade: done in 1m 24s
[5/5] diff: rendering...
[5/5] diff: done in 0.1s
# stdout: rendered ANSI diff (truncated for brevity)
Kept (8):
  column.customer_id.description       — Stable surrogate key from upstream...
  column.customer_id.test.not_null     — Always-pass=false on warehouse sample
  ...
Dropped (6):
  column.email.test.not_null           — always-passes (kept_without_evidence=0)
  column.created_at.test.relationships — requires-future-data
  ...
$ echo $?
0
$ ls -la .signalforge/
diff.json   # the JSON sidecar (DEC-002 default)
grade.json  # the grading report sidecar
grade.jsonl # the per-(artifact, criterion) audit
prune.jsonl # the per-decision prune audit
```

`--write` would additionally produce
`models/marts/schema.yml` (or merge into the existing one); `--dry-run`
would skip both `schema.yml` and `.signalforge/diff.json` while
still printing the diff to stdout.

For a guided end-to-end run against a public BigQuery dataset
(requires `gcloud auth application-default login`, the
`GOOGLE_CLOUD_PROJECT` env var for BigQuery billing, and
`ANTHROPIC_API_KEY`), see the README's
[Quick start](../README.md#quick-start) section for the
walkthrough or [docs/e2e-smoke-test.md](e2e-smoke-test.md) for the
deeper operator guide (prerequisites, cost ceiling, troubleshooting
matrix). The fixture lives at `tests/fixtures/dbt_project_austin/`;
the gated maintainer-only test that exercises the same flow lives
at `tests/cli/test_e2e_bigquery_smoke.py` (run via
`pytest -m e2e --no-cov`).

## Running across many models

A typical dbt project has dozens or hundreds of models. SignalForge
v0.2 supports two patterns for running `generate` across many of them:
an in-process selector (`--select`) and a process-per-model shell
loop. Pick based on whether you want shared LLM prompt-cache state
across models (in-process wins) or process-level isolation and
per-model sidecars (shell loop wins). See
[`plans/super/37-multi-model-select.md`](../plans/super/37-multi-model-select.md)
for the design record (DEC-001 … DEC-017).

### In-process: `--select <expr>`

`--select` matches models from the manifest and iterates `generate`
over each in invocation order. Grammar (also pinned in the argparse
`--help` string and in DEC-001 / DEC-016 of the design plan):

- `tag:<name>` — matches when `<name>` is in
  `Model.tags ∪ Model.config.tags` (the union of model-level and
  config-block tags, case-sensitive).
- `path:<glob>` — shell-style `fnmatch` against
  `Model.original_file_path`. dbt's own selector grammar uses a
  path-prefix-with-implicit-wildcard convention; v0.2 deliberately
  uses fnmatch instead, which means operators write
  `path:models/staging/*` (with the trailing wildcard) rather than
  `path:models/staging` (dbt-compat semantics are a v0.3 ask;
  DEC-016).
- bare `<value>` — `model.<...>` prefix routes as `unique_id`;
  otherwise routes as a file path. Mirrors the v0.1 positional
  `<model>` resolver (`Manifest.get_model`).

Atoms combine as a comma-separated union (set OR); whitespace around
commas is stripped. Match results are deduplicated by `unique_id` and
ordered lexicographically.

Three concrete examples:

```bash
signalforge generate --select tag:staging
signalforge generate --select path:models/marts/*
signalforge generate --select tag:staging,path:models/marts/*
```

Semantics:

- **Sequential.** Models are processed one at a time in
  lexicographic `unique_id` order. Parallelism inside one process is
  a v0.3 ask; use the shell-loop pattern below if you need it now.
- **Fresh `BigQueryAdapter` per model** (DEC-010). The batch driver
  constructs a new adapter inside the per-model loop, not once at
  batch start, so `_active_session_id` and the rest of the BigQuery
  session state cannot bleed between iterations. Adds ~100-500ms BQ
  client init per model — acceptable vs. state-corruption risk.
- **Anthropic prompt cache behavior** (DEC-015). The drafter's
  explicitly cache-marked block is the manifest summary (model
  under draft + its neighbours), which **changes per model** — so
  the marked cache does NOT amortise across siblings in a batch.
  Cost savings within one process come from Anthropic's automatic
  caching of the static system prompt, which IS byte-stable across
  iterations once it crosses the auto-cache size threshold. Net:
  expect partial cache savings on system-prompt tokens; do not
  expect the marked manifest-summary block to hit on subsequent
  models.
- **Per-model progress prefix.** When a TTY is attached and the
  batch driver runs, each iteration emits one stderr line
  `[i/N] <model_unique_id>` before that model's existing stage
  progress lines fire. `--quiet` suppresses; `--verbose` forces on.
  The single-model (positional) path does not emit the prefix.
- **Continue on per-model failure.** A model that raises an
  exception logs the failure, continues to the next model, and
  contributes to the run-level exit code via
  `max(per_model_exit_codes)` across the four-tier taxonomy.

At end of run, an aggregated summary lands on stderr (DEC-005). The
summary always emits when ≥2 models matched OR ≥1 model failed:

```text
Generated 142 kept / 31 dropped / 8 flagged across 12 models in 4m 18s
2 models failed:
  - model.proj.broken_a        exit 3  (LLMRateLimitError)
  - model.proj.broken_b        exit 2  (LLMOutputAnchorContractError)
```

Failed-model list is capped at 50 entries; overflow renders
`  ... and <K> more`.

**Sidecar caveat (DEC-003 — last-writer-wins).**
`.signalforge/grade.json` and `.signalforge/diff.json` are
single-document overwrite (`O_TRUNC`) per-call; across N in-process
iterations, only the FINAL model's sidecars persist. The four
append-only JSONLs (`audit.jsonl`, `llm_responses.jsonl`,
`prune.jsonl`, `grade.jsonl`) accumulate across iterations and
survive — that's where the per-model auditable record lives. If you
need per-model JSON sidecars, use the shell-loop pattern below.

### Process-level: shell-loop pattern

When you need process-level isolation (per-model sidecars, parallel
execution, hard memory boundaries between models), run one
`signalforge` process per model from a shell loop:

```bash
find models -name '*.sql' | \
  xargs -n1 -P4 -I{} signalforge generate {} --project-dir "$PWD"
```

Parallelism caveats:

- **BigQuery session isolation:** safe. Each process has its own
  `BigQueryAdapter` instance with its own `_active_session_id`;
  sessions cannot cross process boundaries. The cleanup-failure
  WARNING (above) still surfaces per-process when teardown fails.
- **Anthropic prompt cache:** each process pays its own
  cache-creation tokens on its first call. With `-P4`, four
  processes redundantly pay the cache-creation cost on the cached
  block (the system prompt + neighbours portion). Choose `-P1`
  (cheaper, slower) vs. `-P4` (faster, more LLM cost) based on
  budget. In-process `--select` amortises the cache cost across
  iterations within one process.
- **Sidecar overwrite:** each process writes
  `.signalforge/grade.json` and `.signalforge/diff.json` to the
  same project dir; whichever process finishes last wins. If you
  need per-model sidecars, either invoke each process with a
  per-model `--project-dir` overlay (e.g. `cp -r` the project into
  a tmp dir per model) or accept last-writer-wins semantics.
- **Anti-pattern: do NOT share `.signalforge/` across concurrent
  processes** if which model "owns" the surviving sidecars matters.
  Configure per-process project directories, or run sequentially
  (`-P1`), or accept the overwrite.

### Cumulative cost

Per-call cost caps already exist — `maximum_bytes_billed` in the
BigQuery profile (see
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md)) bounds
each prune query, and the LLM draft + grade calls have per-call
retry / token budgets in their respective config blocks. Multi-model
runs are N independent calls; cumulative cost is the operator's
responsibility (DEC-008).

Recommendations:

- Preview the match-count before committing to a large batch. The
  library-level helper `signalforge.manifest.select_models(manifest, expr)`
  returns matches without invoking the pipeline; a quick Python
  shell call (`from signalforge.manifest import load, select_models;
  m = load("."); print(len(select_models(m, "tag:staging")))`)
  tells you how many models a given selector would touch.
- Start with a single tag (e.g. `--select tag:staging`) before
  running a wider union, to confirm cost and runtime on a smaller
  slice.
- A `cli.max_models_per_run` config knob is reserved as a v0.3
  forward-compat surface (not shipped in v0.2). For now, the
  operator's selector is the only governor on batch size.

## Subprocess smoke test

The CLI ships one belt-and-braces test gated behind the
`cli_subprocess` pytest marker (DEC-018):

```bash
pytest -m cli_subprocess --no-cov
```

The marker is registered in `pyproject.toml` and
`addopts` excludes it by default (`-m 'not bigquery and not anthropic
and not cli_subprocess'`). Mirrors the `bigquery` and `anthropic`
integration-test gates from
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md). Maintainers
should run `pytest -m cli_subprocess --no-cov` once before declaring a CLI PR
ready — the in-process `main(argv)` tests that run on every default
suite cannot catch a typo in `pyproject.toml`'s `[project.scripts]`
entry or a console-script wrapper regression after a wheel rebuild.

## Cross-references

- [`docs/safety-ops.md`](safety-ops.md) — `--mode` flag wiring,
  PII redaction, audit JSONL schema.
- [`docs/draft-ops.md`](draft-ops.md) — LLM retry taxonomy
  (mapped to exit 3), prompt cache, response audit.
- [`docs/prune-ops.md`](prune-ops.md) — drop-reason taxonomy,
  `prune.jsonl` audit, total-budget semantics.
- [`docs/grade-ops.md`](grade-ops.md) — threshold-fail
  behaviour, sidecar shape, rubric reproducibility.
- [`docs/diff-ops.md`](diff-ops.md) — `--format` flag wiring
  (`render_kind`), `render_to_text` helper, ANSI / Markdown
  safety, sidecar JSON shape.
- [`docs/manifest-loader-ops.md`](manifest-loader-ops.md) —
  `<model>` resolver, manifest schema-version tolerance.
- [`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md) —
  profile loader, BigQuery adapter, sampling cost defaults.
- [`.claude/rules/cli-layer.md`](../.claude/rules/cli-layer.md) —
  CLI rules distilled from this ticket (load-bearing for
  contributors).
- [`plans/super/9-cli-entrypoint.md`](../plans/super/9-cli-entrypoint.md)
  — design record (DEC-001 … DEC-027).

Cross-reference DECs (from `plans/super/9-cli-entrypoint.md`):
DEC-001 (project-root walk-up), DEC-002 (default print-diff =
stdout + JSON sidecar), DEC-006 (lint = config-only, sub-second),
DEC-007 (path canonicalisation at the orchestrator), DEC-008
(stderr message shape per tier), DEC-010 (`--dry-run` writes
nothing), DEC-011 (grade-layer `fail_on_below_threshold` graduation
→ exit 2), DEC-012 (`TableNotFoundError` → tier 2), DEC-014
(stderr progress lines), DEC-015 (`render_to_text` helper),
DEC-016 (no traceback ever leaks), DEC-017 (`format_error_to_stderr`
single source of truth), DEC-018 (subprocess-gated smoke test),
DEC-019 (7th AST scan: every typed exception → exit-code mapping),
DEC-020 (`--format` wires `render_kind`), DEC-023 (`--no-color`
sets `NO_COLOR`), DEC-026 (progress with live values), DEC-027
(`--project-dir` is an absolute assertion).
