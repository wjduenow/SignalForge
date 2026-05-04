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
pip install signalforge
signalforge --version
```

For development against a clone (the `[dev]` extra pulls in pytest,
ruff, and pyright):

```bash
pip install -e ".[dev]"
```

Quote the `".[dev]"` — bare `.[dev]` is a glob in zsh and fails with
`no matches found`.

After install, the `signalforge` console script is registered via
`pyproject.toml`'s `[project.scripts]` entry and resolves to
`signalforge.cli:main`.

## Subcommands

The CLI exposes three subcommands. `signalforge --help` prints the
top-level help; each subcommand has its own `--help` page (e.g.
`signalforge generate --help`).

### `signalforge generate <model>`

Run the full pipeline against `<model>`: load the manifest, build the
safety policy, draft candidate artifacts via the LLM, prune
always-pass / known-clean-fail tests against warehouse samples, grade
the survivors, and render a diff against any existing `schema.yml`.

Positional argument:

- `<model>` — Model under draft. Accepts a dbt `unique_id`
  (e.g. `model.proj.customers`) or a file path (e.g.
  `models/marts/customers.sql`). Routes to
  `Manifest.get_model(...)` which canonicalises the path and
  raises `ModelNotFoundError` / `ModelDisabledError` /
  `ModelPathOutsideProjectError` / `ModelMissingSqlError` on
  failure.

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
  `<project_dir>/<model_dir>/schema.yml`. The JSON sidecar is
  still written to `<project_dir>/.signalforge/diff.json`.
  Mutually exclusive with `--dry-run`.
- `--dry-run` — Run the FULL pipeline (LLM + warehouse + grade)
  and print the diff to stdout, but write nothing — neither the
  `schema.yml` nor the `.signalforge/diff.json` sidecar.
  Overrides the default-on sidecar (DEC-010). Mutually exclusive
  with `--write`.
- `--format {ansi,markdown,json}` — Select the diff renderer.
  ANSI: coloured terminal output (default). Markdown:
  GitHub-friendly report. JSON: stdout receives the JSON
  sidecar's contents.

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

### `signalforge lint`

Validate the five existing `signalforge.yml` config blocks (`safety:`,
`llm:`, `prune:`, `grade:`, `diff:`) against their per-stage loaders.
No warehouse, no LLM, no network — sub-second target. The natural
pre-flight check operators run on every save.

Flags:

- `--config PATH` — Override the default
  `<project_dir>/signalforge.yml`. Path is canonicalised against
  the resolved project_dir.
- `--project-dir PATH` — Same semantics as `generate`'s flag
  (DEC-027 absolute assertion; walk-up applies only when the flag
  is omitted).

Multi-error reporting (DEC-008): when more than one block fails,
`lint` collects every failure and emits a header + bullet list rather
than short-circuiting on the first. Stdout is silent on success
(git-style); stderr carries the failures.

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

## Subprocess smoke test

The CLI ships one belt-and-braces test gated behind the
`cli_subprocess` pytest marker (DEC-018):

```bash
pytest -m cli_subprocess
```

The marker is registered in `pyproject.toml` and
`addopts` excludes it by default (`-m 'not bigquery and not anthropic
and not cli_subprocess'`). Mirrors the `bigquery` and `anthropic`
integration-test gates from
[`docs/warehouse-adapter-ops.md`](warehouse-adapter-ops.md). Maintainers
should run `pytest -m cli_subprocess` once before declaring a CLI PR
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
