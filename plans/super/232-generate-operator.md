# Super Plan — #232: Airflow `SignalForgeGenerateOperator`

## Meta
- **Ticket:** #232 — Airflow: SignalForgeGenerateOperator (part of epic #228, depends on #231)
- **Phase:** Complete
- **PR:** #241 — merged to `dev` 2026-06-16 (squash `bc41661`). Certified against Airflow 2.10.4 (22 gated tests passed).
- **Beads epic:** `bd_1-scaffolding-q5c` — children `.1`…`.7` (US-001…US-007), all closed; chain US-001 → US-002 → US-003 → {US-004 → US-005} → US-006 (Quality Gate) → US-007 (Patterns).
- **Branch:** `feature/232-generate-operator` (deleted post-merge), base `dev`
- **Sessions:** 1 (2026-06-15 plan/build, 2026-06-16 review+merge)

---

## Discovery

### What
Replace the skeleton `SignalForgeGenerateOperator` stub (`src/signalforge/airflow/operators.py`, currently raises `NotImplementedError`) with a real Airflow operator that wraps `signalforge generate` (single model **and** `--select` batch). It's the primary scheduled task: draft → prune → grade → diff against the warehouse, on a cron or downstream of dbt `run`/`build`.

### Why
Epic #228 (v0.7 Airflow integration). #231 landed the airflow-free invocation seam + result→task-state + XCom contract; #232 is the operator that consumes that seam. `write=False` (dry-run) makes a scheduled DAG a drift monitor; `write=True` is opt-in for the commit-the-result workflow.

### Key codebase findings (from research)
- **#231 seam is complete & stable** — `run_signalforge(argv, *, project_dir, invocation="in_process", timeout_seconds=None) -> SignalForgeRunResult`; `decide_task_outcome(result, *, on_flagged="fail") -> TaskOutcome`; shim `raise_for_outcome(outcome, *, message)` (the ONLY `from airflow ...` site). `SignalForgeRunResult.to_xcom()` = counts + sidecar paths only. `TaskOutcome` is a 4-value StrEnum, a SEPARATE axis from the 4-tier exit code.
- **`operators.py` stub** — plain class (does NOT subclass `BaseOperator` at module scope per #230 DEC-004), `__init__` raises `NotImplementedError`. The real operator subclasses `make_base_operator()` (lazy factory in `_airflow_compat.py`), implements `execute(context)`, declares `template_fields`.
- **`AirflowConfigError` (tier 2)** exists for misconfiguration; `AirflowIntegrationError` is excluded-only in the exit-code table (no dual-registration). Error/exit-code lockstep done by #230.
- **CLI `generate` surface** — `model` positional vs `--select` argparse mutex (`required=True`); flags incl. `--project-dir`, `--profiles-dir`, `--write`, `--dry-run`, `--no-grade`, `--cache-scope {per-model,project}`, `--format {ansi,markdown,json}`, `--as-of`, `--mode`, `--scope`, `--sample-strategy`, `--no-cache`. Batch path (`_run_batch`) raises `CliSelectorParseError` / `CliSelectorNoMatchError` (both tier 2) and auto-promotes `cache_scope=project` on ≥2 models.
- **Selector resolution** — `signalforge.manifest.select.select_models(manifest, expr)` + `SelectorParseError`; `signalforge.manifest.load(project_dir)`.
- **Example DAG** — `examples/airflow/signalforge_generate_dag.py` uses two `PythonOperator`s (generate → gate) demonstrating the result→task-state contract.
- **Tests** — `tests/airflow/`: airflow-free core tested ungated; airflow-touching tests carry `@pytest.mark.airflow` + in-test `pytest.importorskip("airflow")`; `test_dag_parse.py` parses the example DAG via `DagBag(...).dags` (no metadata DB). Certified against `.venv-airflow` (#229).

### Rules constraints in force
- **airflow-integration.md** — two-layer split (pure core decides, shim raises); never re-derive outcome by string-match (carry typed `TaskOutcome`); never grow a 5th exit tier; in-process isolation must restore `sys.excepthook` + env keys `NO_COLOR`/`FORCE_COLOR`/`DBT_PROFILES_DIR` + root logger (already done inside `run_signalforge`); fail-soft reads; XCom = paths + counts, no secrets; airflow tests gated, certified against `.venv-airflow`.
- **cli-layer.md** — four-tier exit codes; `AirflowIntegrationError` excluded-only; every concrete `Airflow*Error` registered in `_EXCEPTION_TO_EXIT_CODE`; 7th AST scan enforces it.
- **python-build.md** — `[airflow]` extra stays OUT of the dev group; `import signalforge.airflow` stays airflow-free; default `pyright`/`pytest` green with no airflow; `tests/airflow` excluded from pyright.
- **testing-signal.md** — belt-and-suspenders gating (marker + runtime skip); planted-violation self-checks for any new AST scan; `--no-cov` on gated runs.
- **diff-renderer.md** — sidecars are `O_TRUNC` last-writer-wins at `<project_dir>/.signalforge/diff.json`; `--dry-run` suppresses them.
- **grade-layer.md** — cost/time ceilings (`max_grade_cost_usd`/`max_grade_calls`/`max_grade_tokens`/`total_budget_seconds`) are `signalforge.yml grade:` knobs, not CLI flags.

---

## Decisions

- **DEC-001 — `--select` batch: operator resolves selector + loops per model.** The operator loads the manifest and calls `select_models(manifest, expr)` itself, then calls `run_signalforge(["generate", <unique_id>, …])` once per matched model, aggregating per-model XCom (list of `to_xcom()` dicts + a rollup). Rationale: airflow-integration.md makes accurate per-model XCom "the GenerateOperator child's job"; a single batch call reflects only the last model's sidecar. Chosen over single-batch-call (insufficient XCom) and defer-batch (contradicts issue scope).
- **DEC-002 — Drop `config_overrides`; cost ceilings via committed `signalforge.yml`.** The grade cost/time ceilings are `signalforge.yml` fields with no CLI-flag landing strip and no `--config` override; `config_overrides` would require materialising a yml on disk. For v0.7 the operator does NOT take `config_overrides`; cost/time guardrails come from the project's committed `signalforge.yml grade:` block (documented). Rationale: keeps #232 a clean CLI wrapper, matches the repo's ship-the-seam/defer-scope-creep ethos. Drops a param the issue listed (a `--config` flag or per-run config overlay is a tracked follow-up).
- **DEC-003 — `write=False` → `--dry-run`.** The safe scheduled default writes nothing (no `.signalforge/diff.json`), builds XCom from stdout JSON, and leaves `diff_sidecar_path`/`grade_sidecar_path` = `None`. `write=True` passes `--write` (schema.yml + proposed `.sql`) and sidecar paths populate. Rationale: a concurrent scheduled monitor must not collide on sidecars; the JSON transport is stdout, not the file.
- **DEC-004 — `invocation` param defaults to `"in_process"`.** Matches the seam default and the issue's stated in-process invocation. The concurrency caveat (in-process `redirect_stdout` clobbers under parallel in-process tasks; `subprocess` is the clean-isolation choice) is documented on the param and in `docs/airflow-ops.md`.
- **DEC-005 — `template_fields` = `("project_dir", "select", "model", "profiles_dir", "as_of")`.** Drops `config_overrides` (DEC-002); adds `model` and `as_of` (natural `{{ ds }}` / `{{ params.* }}` targets). All are plain-string kwargs safe for Jinja templating.
- **DEC-006 — Selector/no-match errors surface as `AirflowConfigError`.** When the operator resolves `--select`, `SelectorParseError` and zero-match both re-raise as `AirflowConfigError` (tier 2) with remediation — not a stack trace, not a raw CLI error. (Open: confirm whether to reuse `CliSelectorParseError`/`CliSelectorNoMatchError` directly or wrap; see refinement.)
- **DEC-007 — Force `cache_scope="project"` on ≥2-model loops.** Looping single-model `run_signalforge` calls loses `_run_batch`'s project-cache auto-promotion. The operator passes `--cache-scope project` on each looped call when ≥2 models match, so Anthropic's server-side prompt cache still amortises the byte-identical project prefix across the loop within TTL. Single-model runs use the default. (To verify in architecture review.)

---

## Architecture Review

Three focused review agents (Security/robustness, Performance, Testing). **No blockers.**

| Area | Rating | Finding / resolution |
|---|---|---|
| argv injection | pass | subprocess list-form (never `shell=True`); in-process via argparse. Edge: `model`/`select` starting with `-` → DEC-009 guard. |
| Jinja path safety | concern → accept | `project_dir` implicit containment (`dbt_project.yml` check); `profiles_dir` uncontained by dbt convention. DAG author owns the template — not an escalation seam. Documented in `docs/airflow-ops.md`. |
| XCom secrets | pass | `to_xcom()` omits stdout/stderr; audits are blake2b-only. |
| Selector errors | pass | `ManifestNotFoundError`/`UnsupportedManifestVersionError`/`SelectorParseError` typed → `AirflowConfigError` (DEC-006). |
| In-process isolation | pass | Handled inside `run_signalforge` per call; no per-iteration leak. |
| Cache amortisation | pass | `--cache-scope project` on the loop preserves server-side cache across separate in-process calls (DEC-007). |
| Manifest reload cost | concern → accept | Looped design reloads manifest N× (each `run_signalforge` → own `cmd_generate`); sub-second vs 10s–100s+ per-model pipeline cost → <1% overhead. |
| Fresh adapter | pass | Free in looped design. |
| Testing | pass | Pure-helper factoring mandatory for codecov patch gate (DEC-011); existing import-confinement scan already covers `operators.py` — no new AST scan. |

### Additional decisions from review

- **DEC-008 — Batch task-state via aggregate max-exit-code.** For `--select`, the operator builds ONE aggregate `SignalForgeRunResult` (summed `kept`/`kept_uncertain`/`dropped`/`flagged`; `exit_code = max(per-model exit codes)` over the 4-tier severity; `model_unique_ids` = all matched; `mean_grade` = mean of non-None per-model means or `None`; sidecar paths `None` for batch) and calls `decide_task_outcome(aggregate, on_flagged=...)` once to drive the single Airflow task state. Mirrors the CLI's `_run_batch.total_exit_code`. Rejected: outcome-precedence aggregation (diverges from CLI batch semantics). Consequence (documented): a batch mixing tier-2 + tier-3 maps to FAIL_RETRYABLE; the tier-2 model re-fails until retry budget exhausts — acceptable at single-task granularity.
- **DEC-009 — Param guard.** `_validate_operator_config` rejects empty/None `project_dir`; rejects a `model`/`select` value beginning with `-` (argv-injection belt-and-braces); rejects `on_flagged` outside `{fail,skip,succeed}`; enforces the `model` XOR `select` mutex (exactly one). All raise `AirflowConfigError` (tier 2) with remediation, BEFORE any `run_signalforge` call.
- **DEC-010 — XCom shape.** Single model → `result.to_xcom()` (one dict). Batch → `{"models": [per-model to_xcom() dicts…], "aggregate": aggregate.to_xcom()}`. Counts + paths only; never stdout/stderr/secrets. (Full-sidecar push stays a future opt-in.)
- **DEC-011 — Pure/gated structural split.** Operator module = pure helpers tested UNGATED (`_build_generate_argv`, `_validate_operator_config`, `_resolve_select_models`, `_aggregate_batch_result`) + a thin airflow-touching `execute()` marked `# pragma: no cover`, tested GATED (`@pytest.mark.airflow` + in-test `importorskip`). Satisfies the airflow-free-core 100%-ungated codecov patch gate.

---

## Detailed Breakdown

Architecture ordering: pure core (helpers) → operator wiring (`execute`) → example DAG → docs/parity → quality gate → patterns. Each story ends with the canonical validate command `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest`; gated airflow tests run separately via `uv run pytest -m airflow --no-cov` (and certified against `.venv-airflow`).

### US-001 — Pure argv + config-validation helpers (ungated)
**Description:** Add the airflow-free pure helpers to `src/signalforge/airflow/operators.py`: `_build_generate_argv(...)` (params → CLI argv list) and `_validate_operator_config(...)` (DEC-009 guards). No airflow import; no I/O.
**Traces to:** DEC-002, DEC-003, DEC-004, DEC-005, DEC-009, DEC-011.
**Acceptance:** `_build_generate_argv` emits `["generate", <model>|"--select" <expr>, "--project-dir", <dir>, "--format", "json", …]` with `write=False→--dry-run`, `write=True→--write`, `no_grade→--no-grade`, `as_of→--as-of <date>`, `cache_scope` passthrough, `profiles_dir→--profiles-dir`. `_validate_operator_config` raises `AirflowConfigError` for: empty `project_dir`; `model` AND `select` both set or both unset; `model`/`select` beginning with `-`; `on_flagged ∉ {fail,skip,succeed}`. Validate command green.
**Done when:** Both helpers exist, are pure (no `from airflow`), and ungated unit tests cover every branch (100% on the new lines).
**Files:** `src/signalforge/airflow/operators.py` (helpers replace nothing yet — stub `__init__` stays until US-003); `tests/airflow/test_operators_helpers.py` (new, ungated).
**Depends on:** none.
**TDD:** argv for single-model dry-run; argv for `--select` + `--write`; `--no-grade`/`--as-of`/`--cache-scope`/`--profiles-dir` injection; `--format json` always present; mutex-both-set raises; mutex-neither raises; leading-dash `model` raises; bad `on_flagged` raises.

### US-002 — Selector resolution + batch aggregation helpers (ungated)
**Description:** Add `_resolve_select_models(project_dir, select) -> tuple[str, ...]` (loads manifest, calls `select_models`, maps `ManifestError`/`SelectorParseError`/empty-match → `AirflowConfigError` with remediation) and `_aggregate_batch_result(results) -> SignalForgeRunResult` (DEC-008 aggregate).
**Traces to:** DEC-001, DEC-006, DEC-008, DEC-010, DEC-011.
**Acceptance:** `_resolve_select_models` returns matched unique_ids sorted; raises `AirflowConfigError` (carrying the source `.remediation`) on manifest-not-found / unsupported-version / parse-error / zero-match. `_aggregate_batch_result` sums counts, takes `max` exit code, unions model ids, means the non-None grades (→`None` if all None), sets sidecar paths `None`. Validate command green.
**Done when:** Both helpers exist, pure, ungated tests cover the error mappings (each exception type) + the aggregation math (incl. all-None grade, mixed exit codes).
**Files:** `src/signalforge/airflow/operators.py`; `tests/airflow/test_operators_helpers.py`.
**Depends on:** US-001.
**TDD:** parse-error→AirflowConfigError; zero-match→AirflowConfigError; manifest-missing→AirflowConfigError; aggregate max-exit `[0,3,2]→3`; aggregate flagged sum; aggregate mean grade with one None; aggregate all-None→None.

### US-003 — Real `SignalForgeGenerateOperator.execute()` (gated)
**Description:** Replace the `NotImplementedError` stub with the real operator: subclass `make_base_operator()`, `__init__` storing params + `template_fields = ("project_dir","select","model","profiles_dir","as_of")` (DEC-005), and `execute(context)` wiring helpers → `run_signalforge` (single: one call; batch DEC-001: loop per resolved model, force `--cache-scope project` when ≥2 per DEC-007) → `decide_task_outcome` → `raise_for_outcome`, pushing XCom (DEC-010). `execute` body `# pragma: no cover`.
**Traces to:** DEC-001, DEC-003, DEC-004, DEC-007, DEC-008, DEC-010, DEC-011.
**Acceptance:** No `from airflow` import in `operators.py` (routes through `make_base_operator()`/shim — existing confinement scan stays green). Gated tests monkeypatch `run_signalforge` → canned results and assert: argv built via `_build_generate_argv`; SUCCESS (exit0/flagged0) returns XCom no-raise; flagged + `on_flagged` fail/skip/succeed branches; exit1/2→`AirflowFailException`; exit3→`AirflowException`; batch loops per model + aggregates. `import signalforge.airflow` still pulls no airflow (no-eager-import gate green). Validate command green; `uv run pytest -m airflow --no-cov` green in `.venv-airflow`.
**Done when:** Operator runs `execute(context)` end-to-end against a faked `run_signalforge`; `tests/airflow/test_skeleton.py` stub-raises test updated/removed; confinement + no-eager-import gates green.
**Files:** `src/signalforge/airflow/operators.py`; `src/signalforge/airflow/__init__.py` (eager re-export already wired via `__getattr__` — verify); `tests/airflow/test_operators.py` (new, gated); `tests/airflow/test_skeleton.py` (update the stub-NotImplementedError test).
**Depends on:** US-001, US-002.

### US-004 — Operator-based example DAG + DagBag parse (gated)
**Description:** Add `examples/airflow/signalforge_generate_operator_dag.py` using `SignalForgeGenerateOperator` (single-task drift-monitor shape, `write=False`, `on_flagged` from Variable/env). Extend `tests/airflow/test_dag_parse.py` to parse it via `DagBag(...).dags` with no import errors.
**Traces to:** DEC-003, DEC-004, DEC-005.
**Acceptance:** New DAG parses (`import_errors == {}`), exposes the expected `task_id`, and uses templated `select`/`model`. Existing PythonOperator DAG test unchanged. Gated.
**Done when:** `uv run pytest -m airflow --no-cov tests/airflow/test_dag_parse.py` green in `.venv-airflow`; the new DAG demonstrates the operator (not PythonOperator).
**Files:** `examples/airflow/signalforge_generate_operator_dag.py` (new); `tests/airflow/test_dag_parse.py` (extend); add a gated `render_template_fields` test asserting `{{ ds }}` renders in `select`.
**Depends on:** US-003.

### US-005 — Docs + multi-surface parity (`docs/airflow-ops.md`)
**Description:** Document the operator in `docs/airflow-ops.md`: param table (with the param→flag mapping), `write=False`=dry-run semantics, `on_flagged` branches, `invocation` concurrency caveat (DEC-004), the dropped-`config_overrides`/cost-ceiling-via-`signalforge.yml` story (DEC-002), batch per-model XCom + aggregate task-state (DEC-001/008), and the concurrent-`project_dir` sidecar caveat. Update `airflow-integration.md` (mark the operator child landed; cross-ref DECs). Add `mkdocs.yml` nav entry if not present.
**Traces to:** DEC-001, DEC-002, DEC-003, DEC-004, DEC-008, DEC-010.
**Acceptance:** `uv run --only-group docs mkdocs build` succeeds; param table matches the operator's actual kwargs; cost-ceiling guidance points at `signalforge.yml grade:` knobs. Validate command green.
**Done when:** Ops doc covers every operator param + the four behavioural DECs; rule file updated.
**Files:** `docs/airflow-ops.md`; `.claude/rules/airflow-integration.md`; `mkdocs.yml` (nav if needed).
**Depends on:** US-003, US-004.

### US-006 — Quality Gate (code review ×4 + CodeRabbit)
**Description:** Run the code reviewer 4× across the full changeset, fixing every real bug each pass; run CodeRabbit if available. Confirm the airflow-free-core coverage contract (pure helpers 100% ungated), the no-eager-import + import-confinement gates, and exit-code lockstep (no new error class needed — `AirflowConfigError` already registered).
**Traces to:** all DECs.
**Acceptance:** 4 review passes complete, all real findings fixed; `uv sync --dev && uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest` green; `uv run pytest -m airflow --no-cov` green in `.venv-airflow`; `uv.lock` still additive (no default-env downgrade).
**Done when:** Validation green after all fixes; reviewers find no remaining real bugs.
**Depends on:** US-001 … US-005.

### US-007 — Patterns & Memory (priority 99)
**Description:** Capture durable conventions: the operator wiring pattern (pure helpers ungated + gated `execute`), batch loop-per-model + `--cache-scope project` amortisation, batch aggregate-max-exit task-state, `config_overrides` deferral rationale. Update `.claude/rules/airflow-integration.md` and add a memory pointer if a new reusable lesson emerged.
**Traces to:** DEC-001, DEC-002, DEC-007, DEC-008, DEC-011.
**Acceptance:** Rule file + memory reflect the shipped operator; no contradiction with #231 entries.
**Done when:** Conventions documented; validate command green.
**Depends on:** US-006.

### Rules-compliance gate (validated against Discovery constraints)
- Two-layer split: pure helpers decide / `execute` wires / shim raises ✔ (DEC-011).
- No 5th exit tier; `AirflowConfigError` already tier-2 registered, excluded-base unchanged ✔.
- `[airflow]` extra stays out of dev group; no-eager-import + confinement gates green ✔ (US-003).
- Gated tests: marker + in-test `importorskip`; pure helpers ungated for patch coverage ✔ (US-001/002).
- XCom paths+counts only ✔ (DEC-010). Fail-soft reads inherited from `run_signalforge` ✔.
- Certify against `.venv-airflow` before close ✔ (US-003/004/006).
