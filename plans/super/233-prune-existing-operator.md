# Super Plan (lightweight) — #233: Airflow `SignalForgePruneExistingOperator`

> **Lightweight plan.** #233 is an explicit sibling of #232, which established the operator
> pattern wholesale. The architecture is inherited verbatim from `airflow-integration.md` +
> the `signalforge-airflow-operator-pattern` memory; this file records only the few DECs that
> are genuinely *new* for the no-LLM `prune-existing` variant. Full `/super-plan` ceremony was
> deliberately skipped — the design space is closed (see #232's `plans/super/232-generate-operator.md`).

## Meta
- **Ticket:** #233 — Airflow: SignalForgePruneExistingOperator (no-LLM) (part of epic #228, depends on #231, sibling of #232)
- **Phase:** Planned
- **Beads epic:** TBD (`bd create --parent=` chain US-001 → US-002 → US-003 → US-004 (Quality Gate) → US-005 (Patterns))
- **Branch:** `feature/233-prune-existing-operator` (base `dev`)

---

## Discovery

### What
Add `SignalForgePruneExistingOperator` to `src/signalforge/airflow/operators.py`, wrapping
`signalforge prune-existing` (ingest → prune → diff, **no LLM call**) as an Airflow operator.
It is the cheapest, fastest, zero-LLM entry point — a "signal-rot monitor" that grades the dbt
tests a team *already has* against live warehouse data on a schedule.

### Why
Epic #228 (v0.7 Airflow integration). #231 landed the airflow-free invocation seam +
result→task-state + XCom contract; #232 consumed it for `generate`. #233 reuses the identical
machinery for the no-LLM path, so a team can run SignalForge from a DAG with **no Anthropic
credential**.

### Key codebase findings (from research)
- **#232 operator pattern is complete & stable** — `operators.py` already ships
  `_build_generate_argv` / `_validate_operator_config` / `_resolve_select_models` /
  `_aggregate_batch_result` (pure, ungated) + `_make_generate_operator_class()` (deferred via
  module `__getattr__` + `find_spec("airflow")` branch, `functools.cache`) + the airflow-free
  `_GenerateOperatorAirflowMissing` placeholder. #233 adds a *parallel* set of helpers + a
  second deferred class; it does **not** refactor the generate operator.
- **#231 seam** — `run_signalforge(argv, *, project_dir, invocation="in_process", timeout_seconds=None) -> SignalForgeRunResult`;
  `decide_task_outcome(result, *, on_flagged="fail") -> TaskOutcome`; shim
  `raise_for_outcome(outcome, *, message)` (the ONLY `from airflow ...` site). `to_xcom()` = counts + sidecar paths only.
- **CLI `prune-existing` surface** (`src/signalforge/cli/prune_existing.py`) — positional `<model>`
  (bare-name / unique_id / file-path via `_resolve_model_by_key`); flags: `--schema` (**required**,
  the hand-authored schema.yml to prune), `--project-dir`, `--manifest`, `--profiles-dir`,
  `--tests-dir`, `--scope`, `--sample-strategy`, `--as-of`, `--format {ansi,markdown,json}`,
  `--dry-run`. **No `--write`** (read-only by design, #105 DEC-003/004), **no `--mode`** (inert on
  the no-LLM path, #105 DEC-002), **no `--select`** (single-model positional only).
- **No-LLM ⇒ no grading ⇒ no `flagged` tier.** `prune-existing` calls `render_diff(... grading_report=None)`,
  so entries are only kept / kept-uncertain / dropped — `result.flagged` is always 0,
  `result.below_threshold` always False. `on_flagged` is therefore inert today.
- **Errors already mapped** — the five `IngestError` concretes + `ModelNotFoundError` are
  registered in `_EXCEPTION_TO_EXIT_CODE` (#104/#105); `AirflowConfigError` (tier 2) covers
  operator misconfiguration. No new error class needed; the existing import-confinement +
  no-eager-import gates already scan `operators.py`.

### Rules constraints in force
Same set as #232 (`airflow-integration.md`, `cli-layer.md`, `python-build.md`, `testing-signal.md`,
`diff-renderer.md`, `ingest-layer.md`). The load-bearing ones for #233:
- **airflow-integration.md** — pure core decides / shim raises; carry typed `TaskOutcome` (never
  string-match); never grow a 5th exit tier; XCom = paths + counts, no secrets; airflow tests
  gated + certified against `.venv-airflow`.
- **ingest-layer.md / cli-layer.md** — `prune-existing` is read-only, no-LLM; `--mode` is inert;
  the relevant warehouse knobs are `--scope` / `--sample-strategy`; library `IngestError`s are
  already first-class in the exit-code table, so **no bespoke `Cli*`/`Airflow*` wrappers** (#105 DEC-006).
- **python-build.md** — `[airflow]` extra stays OUT of the dev group; `import signalforge.airflow`
  stays airflow-free; default `pyright`/`pytest` green with no airflow installed.

---

## Decisions (new for #233; everything else inherited from #232)

- **DEC-001 — No-LLM / read-only posture is the operator contract.** The operator exposes **no
  `write` param** (mirrors `prune-existing` having no `--write`) and **no `mode` param** (`--mode`
  is inert on the no-LLM path). It requires no Anthropic connection and never sets / reads
  `ANTHROPIC_API_KEY`. There is **no `signalforge_conn_id` / LLM-connection param** — the operator
  family (generate + prune-existing) takes no LLM connection param at all, so this no-LLM path
  simply never references one. (The issue's example sketch showed a `signalforge_conn_id` kwarg;
  it was dropped as unused — the operators read credentials from the ambient env per the CLI's
  own resolution, not an Airflow connection.) Rationale: faithful to #105's read-only design;
  surfacing `--write`/`--mode` or a phantom connection param would invite misconfiguration.
- **DEC-002 — `schema` is a required operator param.** `prune-existing` cannot run without
  `--schema <path>` (the hand-authored schema.yml to prune). `_validate_operator_config` rejects
  empty/None `schema` with `AirflowConfigError` (tier 2) BEFORE any `run_signalforge` call —
  alongside the existing empty-`project_dir` / leading-dash / bad-`on_flagged` guards.
- **DEC-003 — Single-model only; NO batch, NO `--select`, NO cache-scope, NO aggregation.**
  `prune-existing` takes a single positional `<model>`. The operator therefore drops #232's entire
  batch apparatus (`_resolve_select_models` / `_aggregate_batch_result` / loop-per-model /
  `--cache-scope project` / aggregate-max-exit). One `run_signalforge` call, one
  `SignalForgeRunResult`, one `to_xcom()`. This is the major simplification vs #232.
- **DEC-004 — `on_flagged` kept for symmetry but documented inert.** With no grading there is no
  `flagged` tier, so `decide_task_outcome` on a clean (exit 0) run always yields `SUCCESS`
  regardless of `on_flagged`. The param is retained (matches the issue shape + the sibling
  operators) and validated against `{fail,skip,succeed}`, but `docs/airflow-ops.md` states plainly
  that it has no effect until a future grade pass is layered on. Rationale: API symmetry across the
  operator family without pretending a code path exists.
- **DEC-005 — Param → argv parity with the prune-only flag surface.** `_build_prune_existing_argv`
  emits `["prune-existing", <resolved-model>, "--schema", <schema>, "--project-dir", <dir>,
  "--format", "json", "--dry-run", …]` with optional passthrough of `--profiles-dir`, `--manifest`,
  `--scope`, `--sample-strategy`, `--as-of`, and `--tests-dir`. **`--dry-run` is always passed**
  (the operator is a read-only monitor; the JSON transport is stdout per #231, not the suppressed
  sidecar) — there is no `write=True` branch (DEC-001). `--format json` is always present (XCom
  transport).
- **DEC-006 — `--tests-dir` exposed as the `tests_dir` param (custom_sql ingestion).** The issue
  calls for a `--tests-dir` analogue so singular-test (`tests/*.sql`) ingestion works
  (`business-rule-tests.md`). Exposed as an optional `tests_dir` operator param, passed through to
  `--tests-dir` when set, omitted otherwise.
- **DEC-007 — `template_fields = ("project_dir", "model", "schema", "profiles_dir", "as_of", "tests_dir")`.**
  All plain-string kwargs safe for Jinja templating; `schema` and `tests_dir` are the #233-specific
  additions over #232's set, `model` stays (no `select`).
- **DEC-008 — Reuse the deferred-class + pure/gated split verbatim.** A second module-level
  deferred class `SignalForgePruneExistingOperator` resolved via the existing `__getattr__`
  (branch on `find_spec("airflow")`; airflow present → cached factory builds the real
  `BaseOperator` subclass via `make_base_operator()`; absent → an airflow-free placeholder whose
  `__init__` raises `ModuleNotFoundError`). All decision logic in pure ungated helpers
  (`_build_prune_existing_argv`, `_validate_prune_existing_config`); `execute()` is a thin
  `# pragma: no cover` wire-up tested gated. No new `from airflow` line in `operators.py`; no new
  AST scan (the existing confinement scan already covers the module).

---

## Architecture Review

Deferred to the build's Quality Gate (US-004) — the pattern is inherited from #232's reviewed
design and the surface area is strictly smaller (single-model, no batch, no LLM). Re-confirm at QG:
argv injection (leading-dash guard on `model`/`schema`), Jinja path safety (`project_dir`
containment, `schema`/`profiles_dir` DAG-author-owned), XCom carries no secrets, `IngestError` /
`ModelNotFoundError` typed surfaces map cleanly, no-eager-import + confinement gates green,
pure-helper coverage 100% ungated for the codecov patch gate.

---

## Detailed Breakdown

Ordering: pure helpers → operator wiring → example DAG + docs → quality gate → patterns. Each story
ends with the canonical validate command
`uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`;
gated airflow tests run separately via `uv run pytest -m airflow --no-cov` and are certified against
`.venv-airflow` (#229) before close.

### US-001 — Pure argv + config-validation helpers (ungated)
**Description:** Add `_build_prune_existing_argv(...)` (params → CLI argv list, DEC-005) and
`_validate_prune_existing_config(...)` (DEC-001/002/004 guards) to
`src/signalforge/airflow/operators.py`. No airflow import; no I/O.
**Traces to:** DEC-001, DEC-002, DEC-004, DEC-005, DEC-006, DEC-008.
**Acceptance:** `_build_prune_existing_argv` emits the prune-only argv with `--dry-run` +
`--format json` always present, and optional `--profiles-dir`/`--manifest`/`--scope`/
`--sample-strategy`/`--as-of`/`--tests-dir` only when set. `_validate_prune_existing_config` raises
`AirflowConfigError` for: empty `project_dir`; empty `schema`; `model`/`schema` beginning with `-`;
`on_flagged ∉ {fail,skip,succeed}`. No `write`/`mode` params exist. Validate command green.
**Done when:** Both helpers pure (no `from airflow`); ungated tests cover every branch (100% on new lines).
**Files:** `src/signalforge/airflow/operators.py`; `tests/airflow/test_operators_helpers.py` (extend, ungated).
**Depends on:** none.
**TDD:** single-model argv with `--schema`/`--project-dir`/`--format json`/`--dry-run`;
`--scope`/`--sample-strategy`/`--as-of`/`--tests-dir`/`--profiles-dir`/`--manifest` injection when set
and omitted when None; empty `project_dir` raises; empty `schema` raises; leading-dash `model` raises;
leading-dash `schema` raises; bad `on_flagged` raises.

### US-002 — Real `SignalForgePruneExistingOperator.execute()` (gated)
**Description:** Add the deferred class (DEC-008): airflow-free placeholder + cached
`_make_prune_existing_operator_class()` factory wired through the existing module `__getattr__`.
`__init__` stores params + `template_fields` (DEC-007); `execute(context)` wires helpers →
`run_signalforge` (single call, DEC-003) → `decide_task_outcome` → `raise_for_outcome`, pushing XCom
(`result.to_xcom()`, single dict). `execute` body `# pragma: no cover`.
**Traces to:** DEC-003, DEC-004, DEC-007, DEC-008.
**Acceptance:** No `from airflow` import in `operators.py` (confinement scan green);
`import signalforge.airflow` pulls no airflow (no-eager-import gate green). Gated tests monkeypatch
`run_signalforge` → canned `SignalForgeRunResult` and assert: argv built via
`_build_prune_existing_argv`; exit0/flagged0 → SUCCESS, XCom pushed, no raise; exit1/2 →
`AirflowFailException`; exit3 → `AirflowException`; `on_flagged` documented-inert (clean run is
SUCCESS for all three values). Validate command green; `uv run pytest -m airflow --no-cov` green in
`.venv-airflow`.
**Done when:** Operator runs `execute(context)` end-to-end against a faked `run_signalforge`;
deferred class resolves airflow-free on access, raises on construction without airflow.
**Files:** `src/signalforge/airflow/operators.py`; `src/signalforge/airflow/__init__.py` (lazy
`__getattr__` re-export of the new name); `tests/airflow/test_operators.py` (extend, gated).
**Depends on:** US-001.

### US-003 — Example DAG + DagBag parse + docs (gated + docs)
**Description:** Add `examples/airflow/signalforge_prune_existing_operator_dag.py` (single-task
no-LLM drift-monitor, templated `model`/`schema`). Extend `tests/airflow/test_dag_parse.py` to parse
it via `DagBag(...).dags` (no metadata DB). Document the operator in `docs/airflow-ops.md`: param
table + param→flag mapping, the no-LLM/no-credential story (DEC-001), `schema` requirement (DEC-002),
`on_flagged` inertness (DEC-004), `tests_dir`/custom_sql note (DEC-006), single-model-only (DEC-003).
Mark the child landed in `airflow-integration.md`; add `mkdocs.yml` nav entry if needed.
**Traces to:** DEC-001, DEC-002, DEC-003, DEC-004, DEC-006, DEC-007.
**Acceptance:** New DAG parses (`import_errors == {}`), exposes the expected `task_id`, uses templated
`model`/`schema`; a gated `render_template_fields` test asserts `{{ ds }}` renders. `uv run --only-group docs mkdocs build` succeeds; param table matches the operator's kwargs. Validate command green;
`uv run pytest -m airflow --no-cov` green in `.venv-airflow`.
**Done when:** Example DAG + parse test + ops doc + rule-file update all land.
**Files:** `examples/airflow/signalforge_prune_existing_operator_dag.py` (new);
`tests/airflow/test_dag_parse.py` (extend); `docs/airflow-ops.md`;
`.claude/rules/airflow-integration.md`; `mkdocs.yml` (nav if needed).
**Depends on:** US-002.

### US-004 — Quality Gate (code review ×4 + CodeRabbit)
**Description:** Run the code reviewer 4× across the full changeset (distinct angles:
correctness / conventions / tests / docs+UX), fixing every real bug each pass; run CodeRabbit if
available. Confirm: pure helpers 100% ungated (codecov patch gate); no-eager-import + confinement
gates green; exit-code lockstep (no new error class — `AirflowConfigError` + the existing
`IngestError`/`ModelNotFoundError` registrations cover it); `[airflow]` extra still out of dev group;
`uv.lock` still additive.
**Traces to:** all DECs.
**Acceptance:** 4 passes complete, all real findings fixed; canonical validate green;
`uv run pytest -m airflow --no-cov` green in `.venv-airflow`.
**Done when:** Validation green after fixes; reviewers find no remaining real bugs.
**Depends on:** US-001 … US-003.

### US-005 — Patterns & Memory (priority 99)
**Description:** Capture the durable delta: the no-LLM operator is a second instance of the
pure-helpers-ungated + gated-`execute` + deferred-class pattern, with the simplification that a
single-positional-model CLI command needs NO batch apparatus (no `_resolve_select` / aggregate /
cache-scope). Note the `on_flagged`-inert-without-grading convention. Update
`.claude/rules/airflow-integration.md`; refresh the `signalforge-airflow-operator-pattern` memory
pointer if a reusable lesson emerged (the operator pattern is now a 2-instance precedent).
**Traces to:** DEC-003, DEC-004, DEC-008.
**Acceptance:** Rule file + memory reflect the shipped operator; no contradiction with #231/#232 entries.
**Done when:** Conventions documented; validate command green.
**Depends on:** US-004.

### Rules-compliance gate (validated against Discovery constraints)
- Two-layer split: pure helpers decide / `execute` wires / shim raises ✔ (DEC-008).
- No 5th exit tier; `AirflowConfigError` already tier-2; `IngestError`/`ModelNotFoundError`
  already registered; excluded-base unchanged ✔.
- No-LLM / read-only: no `write`/`mode` param; no Anthropic credential required ✔ (DEC-001).
- `[airflow]` extra stays out of dev group; no-eager-import + confinement gates green ✔ (US-002).
- Gated tests: marker + in-test `importorskip`; pure helpers ungated for patch coverage ✔ (US-001).
- XCom paths+counts only; fail-soft reads inherited from `run_signalforge` ✔.
- Certify against `.venv-airflow` before close ✔ (US-002/003/004).
